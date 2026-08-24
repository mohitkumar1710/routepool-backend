from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client
from supabase_auth.types import User as AuthUser

from app.config import get_supabase
from app.dependencies import get_current_user, get_db
from app.schemas.ride import Ride, RideCreate

router = APIRouter(prefix="/rides", tags=["rides"])

# PostgREST embeds the driver's profile (and, if the ride has one, its route's
# distance/duration) in the same round trip, so a page of rides is one request
# rather than one-plus-N. The route embed omits `geometry` on purpose — see
# `Ride.distance_meters` in app/schemas/ride.py for why.
RIDE_SELECT = (
    "id, driver_id, from_location, to_location, departure_date, departure_time, "
    "available_seats, price_per_seat, vehicle, notes, "
    "origin_lat, origin_lng, destination_lat, destination_lng, route_id, created_at, "
    "driver:profiles(id, name, email, role, avatar_url, rating, review_count, is_verified), "
    "route:routes(distance_meters, duration_seconds)"
)


def _to_ride(row: dict[str, Any]) -> Ride:
    driver = row.get("driver")
    if not driver:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ride is missing its driver profile.",
        )
    return Ride.from_row(row, driver)


@router.get("", response_model=List[Ride])
def list_rides() -> List[Ride]:
    """Every open ride. Public — the search page calls this before sign-in.

    Filtering by route, date and price happens client-side, so this returns the
    full set. Embedded drivers come back without an email (see `User`).
    """
    response = (
        get_supabase()
        .table("rides")
        .select(RIDE_SELECT)
        .gt("available_seats", 0)
        .order("departure_date")
        .order("departure_time")
        .execute()
    )
    return [_to_ride(row) for row in response.data]


@router.get("/{ride_id}", response_model=Ride)
def get_ride(ride_id: str) -> Ride:
    response = (
        get_supabase().table("rides").select(RIDE_SELECT).eq("id", ride_id).limit(1).execute()
    )
    if not response.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ride not found")
    return _to_ride(response.data[0])


@router.post("", response_model=Ride, status_code=status.HTTP_201_CREATED)
def create_ride(
    payload: RideCreate,
    user: AuthUser = Depends(get_current_user),
    db: Client = Depends(get_db),
) -> Ride:
    """Publish a ride. The driver is the bearer token's owner, never the body."""
    response = (
        db.table("rides")
        .insert(
            {
                "driver_id": user.id,
                "from_location": payload.from_,
                "to_location": payload.to,
                "departure_date": payload.departure_date.isoformat(),
                "departure_time": payload.departure_time.isoformat(),
                "available_seats": payload.available_seats,
                "price_per_seat": payload.price_per_seat,
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
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Ride was not created."
        )
    return get_ride(str(response.data[0]["id"]))
