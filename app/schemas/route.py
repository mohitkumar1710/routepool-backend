from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator


class LatLng(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class Waypoint(LatLng):
    """One intermediate stop on a route, in the order the driver added it.

    `name` is why this is not just a `LatLng`: OSRM is given coordinates and
    hands coordinates back, so the label a rider actually recognises ("Solan")
    exists only because the driver's autocomplete pick carried it here. It is
    also not optional at the database level -- `routes_waypoints_shape` rejects
    a stop whose name is missing or empty -- so a blank one is backfilled with
    its own coordinates rather than being allowed to fail the insert.
    """

    name: str = Field(default="", max_length=200)

    @field_validator("name")
    @classmethod
    def _fallback_to_coordinates(cls, value: str) -> str:
        return value.strip()

    def model_post_init(self, _context: Any) -> None:
        if not self.name:
            # `object.__setattr__` is not needed (the model is mutable), but
            # assigning through `self.name` re-runs no validator, so this is
            # the last word on the value.
            self.name = f"{self.lat:.5f}, {self.lng:.5f}"


class RoutePreviewRequest(BaseModel):
    """Payload for POST /routes/preview.

    `waypoints` is ordered and optional; omitting it (or sending an empty
    list) is the plain origin-to-destination request. The cap is not
    arbitrary: every stop becomes another coordinate pair in the OSRM request
    path, and the public OSRM server rejects an over-long URL outright.
    """

    origin: LatLng
    destination: LatLng
    waypoints: List[Waypoint] = Field(default_factory=list, max_length=8)


class RouteAlternative(BaseModel):
    """One cached OSRM route, whether served from the cache or just inserted."""

    id: str
    geometry: str
    distance_meters: int
    duration_seconds: int
    waypoints: List[Waypoint] = Field(default_factory=list)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "RouteAlternative":
        return cls(
            id=str(row["id"]),
            geometry=row["geometry"],
            distance_meters=int(row["distance_meters"]),
            duration_seconds=int(row["duration_seconds"]),
            waypoints=parse_waypoints(row.get("waypoints")),
        )


class RoutePreviewResponse(BaseModel):
    routes: List[RouteAlternative]


def parse_waypoints(value: Optional[Any]) -> List[Waypoint]:
    """Reads the `waypoints` jsonb column back into models.

    Tolerant on purpose: this column is readable by anything holding the anon
    key, and a row written by some future path with a malformed stop should
    cost that one stop its label, not 500 the whole ride list.
    """
    if not isinstance(value, list):
        return []

    stops: List[Waypoint] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            stops.append(Waypoint.model_validate(item))
        except ValueError:
            continue
    return stops
