"""Route previews: OSRM driving directions, cached in `public.routes`.

Autocomplete (place search) is browser-side and never touches this file — see
`PlaceAutocomplete.tsx`. This router only turns two points into a driving
route, and only calls out to OSRM on a cache miss.
"""

from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from postgrest.exceptions import APIError
from supabase import Client
from supabase_auth.types import User as AuthUser

from app.config import get_supabase
from app.dependencies import get_current_user, get_db
from app.schemas.route import LatLng, RouteAlternative, RoutePreviewRequest, RoutePreviewResponse

router = APIRouter(prefix="/routes", tags=["routes"])

ROUTE_SELECT = "id, geometry, distance_meters, duration_seconds"

OSRM_BASE_URL = "https://router.project-osrm.org/route/v1/driving"
_osrm_client = httpx.Client(timeout=20.0)

# OSRM's own `code` field for a request that reached the server but could not
# be resolved to a route. Anything not listed here (a malformed request, an
# out-of-bounds coordinate) falls back to 400.
_OSRM_ERROR_STATUS = {
    "NoRoute": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "NoSegment": status.HTTP_422_UNPROCESSABLE_ENTITY,
}


def _coord_key(value: float) -> str:
    """Rounds to 4 decimal places (~11m) -- the cache key precision -- and
    formats as a fixed-point string so PostgREST compares an exact decimal
    against the `numeric(7, 4)` column, not a float with binary rounding noise.
    """
    return f"{round(value, 4):.4f}"


def _query_cache(db: Client, origin: LatLng, destination: LatLng) -> list[dict[str, Any]]:
    response = (
        db.table("routes")
        .select(ROUTE_SELECT)
        .eq("origin_lat", _coord_key(origin.lat))
        .eq("origin_lng", _coord_key(origin.lng))
        .eq("destination_lat", _coord_key(destination.lat))
        .eq("destination_lng", _coord_key(destination.lng))
        .order("route_rank")
        .execute()
    )
    return response.data


def _call_osrm(origin: LatLng, destination: LatLng) -> list[dict[str, Any]]:
    url = f"{OSRM_BASE_URL}/{origin.lng},{origin.lat};{destination.lng},{destination.lat}"

    try:
        response = _osrm_client.get(
            url,
            params={"alternatives": "true", "overview": "full", "geometries": "polyline"},
        )
    except httpx.HTTPError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach the routing service. Please try again.",
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The routing service returned an unexpected error. Please try again.",
        )

    payload = response.json()
    code = payload.get("code")
    if code != "Ok" or not payload.get("routes"):
        raise HTTPException(
            status_code=_OSRM_ERROR_STATUS.get(code, status.HTTP_400_BAD_REQUEST),
            detail=payload.get("message") or "No driving route was found between these two points.",
        )

    return payload["routes"]


@router.post("/preview", response_model=RoutePreviewResponse)
def preview_route(
    payload: RoutePreviewRequest,
    user: AuthUser = Depends(get_current_user),
    db: Client = Depends(get_db),
) -> RoutePreviewResponse:
    """Look up a cached route for this origin/destination, or fetch and cache
    one from OSRM.

    All of OSRM's alternatives are cached (`route_rank` 0, 1, 2, ...), not just
    the fastest one, so a repeat request returns every option next time too.
    """
    cached = _query_cache(db, payload.origin, payload.destination)
    if cached:
        return RoutePreviewResponse(routes=[RouteAlternative.from_row(row) for row in cached])

    osrm_routes = _call_osrm(payload.origin, payload.destination)

    rows_to_insert = [
        {
            "origin_lat": _coord_key(payload.origin.lat),
            "origin_lng": _coord_key(payload.origin.lng),
            "destination_lat": _coord_key(payload.destination.lat),
            "destination_lng": _coord_key(payload.destination.lng),
            "route_rank": rank,
            "geometry": route["geometry"],
            "distance_meters": round(route["distance"]),
            "duration_seconds": round(route["duration"]),
        }
        for rank, route in enumerate(osrm_routes)
    ]

    try:
        # ignore_duplicates so a second request racing on the same uncached
        # pair doesn't 409 on the unique constraint -- it just skips the rows
        # the first request already won, and both re-read the cache below.
        db.table("routes").upsert(
            rows_to_insert,
            on_conflict="origin_lat,origin_lng,destination_lat,destination_lng,route_rank",
            ignore_duplicates=True,
        ).execute()
    except APIError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.message or "Could not cache the route.",
        )

    cached = _query_cache(db, payload.origin, payload.destination)
    if not cached:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Route was not cached.")
    return RoutePreviewResponse(routes=[RouteAlternative.from_row(row) for row in cached])


@router.get("/{route_id}", response_model=RouteAlternative)
def get_route(route_id: UUID) -> RouteAlternative:
    """One cached route, geometry included. Public — matches the `routes are
    readable by everyone` RLS policy, and lets RideDetail fetch a ride's
    polyline by `route_id` without needing the rider to be signed in.
    """
    response = (
        get_supabase()
        .table("routes")
        .select("id, geometry, distance_meters, duration_seconds")
        .eq("id", str(route_id))
        .limit(1)
        .execute()
    )
    if not response.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Route not found")
    return RouteAlternative.from_row(response.data[0])
