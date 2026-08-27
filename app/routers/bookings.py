from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from postgrest.exceptions import APIError
from supabase import AsyncClient

from app.dependencies import AuthUser, get_current_user, get_db
from app.pagination import MAX_LIMIT, limit_param, offset_param
from app.schemas.booking import Booking, BookingCreate, BookingStatusUpdate
from app.utils import get_logger

logger = get_logger("routepool.bookings")

router = APIRouter(prefix="/bookings", tags=["bookings"])

# set_booking_status() raises with these SQLSTATEs; map them onto HTTP so the
# frontend's `detail` unwrapping shows the user something meaningful.
_PG_ERROR_STATUS = {
    "P0002": status.HTTP_404_NOT_FOUND,       # no such booking
    "42501": status.HTTP_403_FORBIDDEN,       # caller is not the ride's driver
    "23514": status.HTTP_409_CONFLICT,        # not enough seats left
    "22023": status.HTTP_422_UNPROCESSABLE_ENTITY,  # bad status value
}


# The token is verified before the handler runs, rather than being handed
# straight to Postgres and left to fail there. Without this, an expired token
# reached PostgREST, PostgREST rejected it, and the caller got an opaque 500
# instead of the 401 that tells the frontend to sign them out. Declared as a
# route dependency because the handler has no use for the user object itself --
# `get_db` scopes every query to them through RLS.
#
# It is free to add now: verification is a local signature check, not the
# network round trip it used to be. See app/dependencies.py.
@router.get(
    "",
    response_model=List[Booking],
    dependencies=[Depends(get_current_user)],
)
async def list_bookings(
    db: AsyncClient = Depends(get_db),
    limit: int = limit_param("bookings"),
    offset: int = offset_param("bookings"),
) -> List[Booking]:
    """Everything this user may see: their own requests, plus every request on
    a ride they drive.

    No filtering here on purpose — the RLS policy on `bookings` already returns
    exactly that set, so a plain select is both correct and unforgeable.

    Paginated on the same opt-in terms as `GET /rides`: `limit` defaults to the
    maximum so `MyTrips` and `DriverDashboard`, which both read this list once
    and treat it as complete, keep seeing everything rather than quietly losing
    the oldest bookings. See `app/pagination.py`.
    """
    response = await (
        db.table("bookings")
        .select("*")
        .order("created_at", desc=True)
        # Newest first means the *oldest* bookings fall off the end, which is
        # the right way round -- but `created_at` alone is not unique, so `id`
        # settles ties and keeps page boundaries from shifting between calls.
        .order("id", desc=True)
        .limit(limit)
        .offset(offset)
        .execute()
    )
    logger.debug(
        "listed %d booking(s) visible to the caller (limit=%d offset=%d)",
        len(response.data),
        limit,
        offset,
    )
    if len(response.data) == MAX_LIMIT:
        logger.warning(
            "GET /bookings filled a full page of %d -- a caller that does not "
            "paginate is now missing its oldest bookings",
            MAX_LIMIT,
        )
    return [Booking.from_row(row) for row in response.data]


@router.post("", response_model=Booking, status_code=status.HTTP_201_CREATED)
async def create_booking(
    payload: BookingCreate,
    user: AuthUser = Depends(get_current_user),
    db: AsyncClient = Depends(get_db),
) -> Booking:
    if payload.passenger_id is not None and str(payload.passenger_id) != str(user.id):
        # Someone hand-crafting a request rather than a slip by the frontend,
        # which always sends its own id -- so this one is worth noticing.
        logger.warning(
            "user %s tried to book ride %s on behalf of %s",
            user.id,
            payload.ride_id,
            payload.passenger_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only book seats for yourself.",
        )

    ride = await db.table("rides").select("driver_id, available_seats").eq("id", payload.ride_id).limit(1).execute()
    if not ride.data:
        logger.warning("user %s tried to book missing ride %s", user.id, payload.ride_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ride not found")
    ride_row = ride.data[0]

    if str(ride_row["driver_id"]) == str(user.id):
        logger.warning("driver %s tried to book their own ride %s", user.id, payload.ride_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot book a seat on your own ride.",
        )

    # An early courtesy check only. Seats are not actually held until the driver
    # confirms, so the authoritative capacity check lives in set_booking_status.
    if payload.seats > ride_row["available_seats"]:
        logger.warning(
            "booking refused: user %s asked for %d seat(s) on ride %s, %d left",
            user.id,
            payload.seats,
            payload.ride_id,
            ride_row["available_seats"],
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only {ride_row['available_seats']} seat(s) left on this ride.",
        )

    try:
        response = await (
            db.table("bookings")
            .upsert(
                {
                    "ride_id": payload.ride_id,
                    "passenger_id": str(user.id),
                    "seats": payload.seats,
                    "status": "pending",
                },
                on_conflict="ride_id,passenger_id",
            )
            .execute()
        )
    except APIError as exc:
        logger.error(
            "booking upsert failed for user %s on ride %s [%s]: %s",
            user.id,
            payload.ride_id,
            exc.code,
            exc.message,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.message or "Could not create the booking.",
        )
    if not response.data:
        # An empty result from an upsert that did not raise usually means RLS
        # silently filtered the returned row.
        logger.error(
            "booking upsert returned no row for user %s on ride %s",
            user.id,
            payload.ride_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Booking was not created."
        )
    booking = Booking.from_row(response.data[0])
    logger.info(
        "booking %s pending: user %s requested %d seat(s) on ride %s",
        response.data[0].get("id"),
        user.id,
        payload.seats,
        payload.ride_id,
    )
    return booking


@router.patch(
    "/{booking_id}/status",
    response_model=Booking,
    dependencies=[Depends(get_current_user)],
)
async def update_booking_status(
    booking_id: str,
    payload: BookingStatusUpdate,
    db: AsyncClient = Depends(get_db),
) -> Booking:
    """Driver accepts or declines.

    Delegated to a Postgres function so the status change and the ride's seat
    count move in one locked transaction — two drivers confirming at once
    cannot oversell the car.
    """
    logger.info("booking %s -> %s requested", booking_id, payload.status)
    try:
        response = await db.rpc(
            "set_booking_status",
            {"p_booking_id": booking_id, "p_status": payload.status},
        ).execute()
    except APIError as exc:
        # A SQLSTATE we mapped is the function rejecting the caller on purpose
        # (wrong driver, no seats left); anything else is unexpected and gets
        # the louder level.
        expected = (exc.code or "") in _PG_ERROR_STATUS
        log = logger.warning if expected else logger.error
        log(
            "set_booking_status(%s, %s) failed [%s]: %s",
            booking_id,
            payload.status,
            exc.code,
            exc.message,
        )
        raise HTTPException(
            status_code=_PG_ERROR_STATUS.get(exc.code or "", status.HTTP_400_BAD_REQUEST),
            detail=exc.message or "Could not update the booking.",
        )
    if not response.data:
        logger.warning("booking %s not found when setting status", booking_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    row = response.data[0] if isinstance(response.data, list) else response.data
    logger.info("booking %s is now %s", booking_id, row.get("status", payload.status))
    return Booking.from_row(row)
