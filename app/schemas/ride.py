import datetime as dt
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.user import User


class RideCreate(BaseModel):
    """Payload for POST /rides.

    No driver field — the driver is taken from the bearer token, so you cannot
    publish a ride in someone else's name.
    """

    # `from` is a Python keyword, so the field is `from_` with an alias. The
    # alias is what appears on the wire in both directions.
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)
    departure_date: dt.date
    departure_time: dt.time
    available_seats: int = Field(ge=1, le=8)
    price_per_seat: float = Field(ge=0)
    vehicle: str = Field(min_length=1)
    notes: Optional[str] = None
    # A ride can't be posted without a resolved route: the frontend runs
    # RouteSelector against POST /routes/preview first, and sends the chosen
    # alternative's id (plus the coordinates that produced it) here. This
    # router never calls OSRM itself.
    origin_lat: float = Field(ge=-90, le=90)
    origin_lng: float = Field(ge=-180, le=180)
    destination_lat: float = Field(ge=-90, le=90)
    destination_lng: float = Field(ge=-180, le=180)
    route_id: str = Field(min_length=1)


class Ride(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    driver: User
    from_: str = Field(alias="from")
    to: str
    departure_date: dt.date
    # A plain string, not dt.time: Postgres renders "07:30" as "07:30:00" and
    # the frontend displays this value as-is.
    departure_time: str
    available_seats: int
    price_per_seat: float
    vehicle: str
    notes: Optional[str] = None
    # Optional: nullable in the DB so rides posted before this column existed
    # still read back cleanly.
    origin_lat: Optional[float] = None
    origin_lng: Optional[float] = None
    destination_lat: Optional[float] = None
    destination_lng: Optional[float] = None
    route_id: Optional[str] = None
    # Embedded from `routes` so a ride list doesn't need a second request to
    # show trip length. The full polyline geometry is deliberately NOT
    # embedded here — SearchResults.tsx loads every open ride at once, and a
    # polyline string is large; RideDetail.tsx fetches it separately, by
    # `route_id`, only when someone actually opens a ride.
    distance_meters: Optional[int] = None
    duration_seconds: Optional[int] = None

    @classmethod
    def from_row(cls, row: dict[str, Any], driver: dict[str, Any]) -> "Ride":
        route = row.get("route") or {}
        return cls(
            id=str(row["id"]),
            driver=User.from_row(driver),
            **{"from": row["from_location"]},
            to=row["to_location"],
            departure_date=row["departure_date"],
            departure_time=str(row["departure_time"])[:5],
            available_seats=row["available_seats"],
            price_per_seat=float(row["price_per_seat"]),
            vehicle=row["vehicle"],
            notes=row.get("notes"),
            origin_lat=float(row["origin_lat"]) if row.get("origin_lat") is not None else None,
            origin_lng=float(row["origin_lng"]) if row.get("origin_lng") is not None else None,
            destination_lat=float(row["destination_lat"])
            if row.get("destination_lat") is not None
            else None,
            destination_lng=float(row["destination_lng"])
            if row.get("destination_lng") is not None
            else None,
            route_id=str(row["route_id"]) if row.get("route_id") else None,
            distance_meters=int(route["distance_meters"])
            if route.get("distance_meters") is not None
            else None,
            duration_seconds=int(route["duration_seconds"])
            if route.get("duration_seconds") is not None
            else None,
        )
