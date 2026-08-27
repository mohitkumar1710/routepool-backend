from datetime import datetime, timedelta, timezone
from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from supabase import AsyncClient

from app.config import get_supabase
from app.dependencies import AuthUser, get_current_user, get_db
from app.pagination import MAX_LIMIT, limit_param, offset_param
from app.pricing import calculate_price_per_seat
from app.schemas.ride import Ride, RideCreate
from app.utils import get_logger

logger = get_logger("routepool.rides")

router = APIRouter(prefix="/rides", tags=["rides"])

# `departure_date` / `departure_time` are a bare date and a bare time, with no
# zone attached, because a ride leaving at 07:30 leaves at 07:30 Indian
# wall-clock time wherever the driver happened to be sitting when they posted
# it. So "has this departed?" has to be asked against India's clock, never the
# server's -- the frontend's utils/datetime.ts makes the same choice for the
# post-ride form, and the two must agree or a ride can be un-postable while
# still listed, or vice versa.
#
# A fixed offset rather than ZoneInfo("Asia/Kolkata"): India has observed no
# daylight saving since 1945, so UTC+05:30 is exact rather than an
# approximation -- and it needs no system tz database, which Windows does not
# ship and which would otherwise make this module fail at import.
IST = timezone(timedelta(hours=5, minutes=30), "IST")

# PostgREST embeds the driver's profile (and, if the ride has one, its route's
# distance/duration/waypoints) in the same round trip, so a page of rides is one
# request rather than one-plus-N. The route embed omits `geometry` on purpose,
# and includes `waypoints` on purpose — see `Ride.distance_meters` and
# `Ride.waypoints` in app/schemas/ride.py for both halves of that trade.
RIDE_SELECT = (
    "id, driver_id, from_location, to_location, departure_date, departure_time, "
    "available_seats, price_per_seat, price_per_km, vehicle, notes, "
    "origin_lat, origin_lng, destination_lat, destination_lng, route_id, created_at, "
    "driver:profiles(id, name, email, role, avatar_url, rating, review_count, is_verified), "
    "route:routes(distance_meters, duration_seconds, waypoints)"
)


def _not_departed_filter(now: datetime) -> str:
    """A PostgREST `or=` clause matching only rides that have not yet left.

    Two columns means two cases rather than one comparison: any later date is
    in the future outright, and today's rides survive only while the clock has
    not passed their departure time.
    """
    return (
        f"departure_date.gt.{now.date().isoformat()},"
        f"and(departure_date.eq.{now.date().isoformat()},"
        f"departure_time.gte.{now.strftime('%H:%M:%S')})"
    )


def _to_ride(row: dict[str, Any]) -> Ride:
    driver = row.get("driver")
    if not driver:
        # The embed is an inner join in practice, so an absent driver means the
        # profiles row went missing under a ride that still references it.
        logger.error("ride %s has no driver profile embedded", row.get("id"))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ride is missing its driver profile.",
        )
    return Ride.from_row(row, driver)


@router.get("", response_model=List[Ride])
async def list_rides(
    include_past: bool = Query(
        default=False,
        description=(
            "Include rides whose departure has already passed. Off by default: "
            "a departed ride is not bookable, so search must never show one. "
            "The app turns it on for a signed-in user, whose trip history and "
            "driver dashboard are read from the same cached list."
        ),
    ),
    limit: int = limit_param("rides"),
    offset: int = offset_param("rides"),
) -> List[Ride]:
    """Every open ride. Public — the search page calls this before sign-in.

    "Open" is two conditions, and both are enforced here rather than in the
    browser: seats still available, and a departure still ahead of us. Filtering
    by route, price and vehicle stays client-side, so this returns the full set
    for those. Embedded drivers come back without an email (see `User`).

    Paginated, but opt-in: `limit` defaults to the maximum, so a caller that
    sends neither parameter — which is every caller today — gets what it always
    got. See `app/pagination.py` for why the default is not smaller. The
    response stays a bare JSON array rather than growing a
    `{items, total, next}` envelope, because `normalizeList` in the frontend
    parses it as an array and an envelope would break every existing caller at
    once.

    The ordering was already stable (departure date, then time) and now carries
    weight: it is what makes a page boundary mean the same thing across two
    requests.
    """
    client = await get_supabase()
    query = client.table("rides").select(RIDE_SELECT).gt("available_seats", 0)

    if not include_past:
        query = query.or_(_not_departed_filter(datetime.now(IST)))

    response = await (
        query.order("departure_date")
        .order("departure_time")
        # `id` breaks ties. Two rides leaving at the same minute would otherwise
        # be in whatever order Postgres felt like, and a row can then appear on
        # both page 1 and page 2, or on neither.
        .order("id")
        .limit(limit)
        .offset(offset)
        .execute()
    )
    logger.info(
        "listed %d ride(s) (include_past=%s limit=%d offset=%d)",
        len(response.data),
        include_past,
        limit,
        offset,
    )
    if len(response.data) == MAX_LIMIT:
        # The board has outgrown one response. Nothing is broken yet -- the
        # frontend just cannot see past here, and this is the line that says so
        # before a user reports rides going missing.
        logger.warning(
            "GET /rides filled a full page of %d -- the board no longer fits in "
            "one response, and callers that do not paginate are seeing a "
            "truncated set",
            MAX_LIMIT,
        )
    return [_to_ride(row) for row in response.data]


@router.get("/{ride_id}", response_model=Ride)
async def get_ride(ride_id: str) -> Ride:
    client = await get_supabase()
    response = await (
        client.table("rides").select(RIDE_SELECT).eq("id", ride_id).limit(1).execute()
    )
    if not response.data:
        logger.warning("ride %s not found", ride_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ride not found")
    return _to_ride(response.data[0])


@router.post("", response_model=Ride, status_code=status.HTTP_201_CREATED)
async def create_ride(
    payload: RideCreate,
    user: AuthUser = Depends(get_current_user),
    db: AsyncClient = Depends(get_db),
) -> Ride:
    """Publish a ride. The driver is the bearer token's owner, never the body.

    The seat price is computed here rather than accepted: the body carries a
    rate band, and the distance it is multiplied by is read back out of the
    cached route the driver selected. Taking the distance from the request
    instead would let anyone post a 900 km ride priced as a 12 km one.
    """
    route = await (
        db.table("routes")
        .select("distance_meters")
        .eq("id", payload.route_id)
        .limit(1)
        .execute()
    )
    if not route.data:
        logger.warning(
            "driver %s posted a ride against unknown route %s",
            user.id,
            payload.route_id,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="That route no longer exists. Pick a route again.",
        )

    distance_meters = int(route.data[0]["distance_meters"])
    price_per_seat = calculate_price_per_seat(
        distance_meters=distance_meters,
        rate_per_km=payload.price_per_km,
        available_seats=payload.available_seats,
    )
    # The inputs as well as the answer: a driver disputing a seat price can only
    # be answered if the distance and rate that produced it were recorded.
    logger.info(
        "priced ride for driver %s: %dm at Rs%d/km over %d seat(s) + driver = Rs%s/seat",
        user.id,
        distance_meters,
        payload.price_per_km,
        payload.available_seats,
        price_per_seat,
    )

    response = await (
        db.table("rides")
        .insert(
            {
                "driver_id": user.id,
                "from_location": payload.from_,
                "to_location": payload.to,
                "departure_date": payload.departure_date.isoformat(),
                "departure_time": payload.departure_time.isoformat(),
                "available_seats": payload.available_seats,
                "price_per_seat": price_per_seat,
                "price_per_km": payload.price_per_km,
                "vehicle": payload.vehicle,
                "notes": payload.notes,
                "origin_lat": payload.origin_lat,
                "origin_lng": payload.origin_lng,
                "destination_lat": payload.destination_lat,
                "destination_lng": payload.destination_lng,
                "route_id": payload.route_id,
            }
        )
        .execute()
    )
    if not response.data:
        logger.error("ride insert returned no row for driver %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Ride was not created."
        )
    ride_id = str(response.data[0]["id"])
    logger.info(
        "ride %s created by driver %s: %s -> %s on %s at %s",
        ride_id,
        user.id,
        payload.from_,
        payload.to,
        payload.departure_date,
        payload.departure_time,
    )
    return await get_ride(ride_id)
