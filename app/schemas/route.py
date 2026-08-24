from typing import Any, List

from pydantic import BaseModel, Field


class LatLng(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class RoutePreviewRequest(BaseModel):
    """Payload for POST /routes/preview."""

    origin: LatLng
    destination: LatLng


class RouteAlternative(BaseModel):
    """One cached OSRM route, whether served from the cache or just inserted."""

    id: str
    geometry: str
    distance_meters: int
    duration_seconds: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "RouteAlternative":
        return cls(
            id=str(row["id"]),
            geometry=row["geometry"],
            distance_meters=int(row["distance_meters"]),
            duration_seconds=int(row["duration_seconds"]),
        )


class RoutePreviewResponse(BaseModel):
    routes: List[RouteAlternative]
