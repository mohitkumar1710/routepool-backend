from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from postgrest.exceptions import APIError
from supabase import Client
from supabase_auth.types import User as AuthUser

from app.dependencies import get_current_user, get_db
from app.schemas.booking import Booking, BookingCreate, BookingStatusUpdate

router = APIRouter(prefix="/bookings", tags=["bookings"])

# set_booking_status() raises with these SQLSTATEs; map them onto HTTP so the
# frontend's `detail` unwrapping shows the user something meaningful.
_PG_ERROR_STATUS = {
    "P0002": status.HTTP_404_NOT_FOUND,       # no such booking
    "42501": status.HTTP_403_FORBIDDEN,       # caller is not the ride's driver
    "23514": status.HTTP_409_CONFLICT,        # not enough seats left
    "22023": status.HTTP_422_UNPROCESSABLE_ENTITY,  # bad status value
}


@router.get("", response_model=List[Booking])
def list_bookings(db: Client = Depends(get_db)) -> List[Booking]:
    """Everything this user may see: their own requests, plus every request on
    a ride they drive.

    No filtering here on purpose — the RLS policy on `bookings` already returns
    exactly that set, so a plain select is both correct and unforgeable.
    """
    response = db.table("bookings").select("*").order("created_at", desc=True).execute()
    return [Booking.from_row(row) for row in response.data]


@router.post("", response_model=Booking, status_code=status.HTTP_201_CREATED)
def create_booking(
    payload: BookingCreate,
    user: AuthUser = Depends(get_current_user),
    db: Client = Depends(get_db),
) -> Booking:
    if payload.passenger_id is not None and str(payload.passenger_id) != str(user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only book seats for yourself.",
        )

    ride = db.table("rides").select("driver_id, available_seats").eq("id", payload.ride_id).limit(1).execute()
    if not ride.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ride not found")
    ride_row = ride.data[0]

    if str(ride_row["driver_id"]) == str(user.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot book a seat on your own ride.",
        )

    # An early courtesy check only. Seats are not actually held until the driver
    # confirms, so the authoritative capacity check lives in set_booking_status.
    if payload.seats > ride_row["available_seats"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only {ride_row['available_seats']} seat(s) left on this ride.",
        )

    try:
        response = (
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.message or "Could not create the booking.",
        )
    if not response.data:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Booking was not created."
        )
    return Booking.from_row(response.data[0])


@router.patch("/{booking_id}/status", response_model=Booking)
def update_booking_status(
    booking_id: str,
    payload: BookingStatusUpdate,
    db: Client = Depends(get_db),
) -> Booking:
    """Driver accepts or declines.

    Delegated to a Postgres function so the status change and the ride's seat
    count move in one locked transaction — two drivers confirming at once
    cannot oversell the car.
    """
    try:
        response = db.rpc(
            "set_booking_status",
            {"p_booking_id": booking_id, "p_status": payload.status},
        ).execute()
    except APIError as exc:
        raise HTTPException(
            status_code=_PG_ERROR_STATUS.get(exc.code or "", status.HTTP_400_BAD_REQUEST),
            detail=exc.message or "Could not update the booking.",
        )
    if not response.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    row = response.data[0] if isinstance(response.data, list) else response.data
    return Booking.from_row(row)
