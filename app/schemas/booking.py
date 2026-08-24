import datetime as dt
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

BookingStatus = Literal["pending", "confirmed", "cancelled"]


class BookingCreate(BaseModel):
    ride_id: str
    # Accepted for compatibility with the frontend payload but never trusted:
    # the passenger is taken from the bearer token, and a mismatch is a 403.
    passenger_id: Optional[str] = None
    seats: int = Field(ge=1, le=8)


class BookingStatusUpdate(BaseModel):
    status: Literal["confirmed", "cancelled"]


class Booking(BaseModel):
    id: str
    ride_id: str
    passenger_id: str
    seats: int
    status: BookingStatus
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Booking":
        return cls(
            id=str(row["id"]),
            ride_id=str(row["ride_id"]),
            passenger_id=str(row["passenger_id"]),
            seats=row["seats"],
            status=row["status"],
            created_at=row["created_at"],
        )
