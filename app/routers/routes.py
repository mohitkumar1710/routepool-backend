"""Route previews: OSRM driving directions, cached in `public.routes`.

Autocomplete (place search) is browser-side and never touches this file — see
`PlaceAutocomplete.tsx`. This router only turns a start, an
end and any stops in between into a driving route, and only calls out to OSRM
on a cache miss.
"""

from typing import Any, List
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from postgrest.exceptions import APIError
from supabase import AsyncClient

from app.config import get_supabase
from app.dependencies import AuthUser, get_current_user, get_db
from app.schemas.route import (
    LatLng,
    RouteAlternative,
    RoutePreviewRequest,
    RoutePreviewResponse,
    Waypoint,
)
from app.utils import get_logger

logger = get_logger("routepool.routes")

router = APIRouter(prefix="/routes", tags=["routes"])

# `waypoints` is included but `geometry` stays out of the ride embed in
# rides.py -- a stop list is a handful of short strings, a polyline is not.
ROUTE_SELECT = "id, geometry, distance_meters, duration_seconds, waypoints"

# The columns of `routes_cache_key_unique`, in its own order. `waypoints_key`
# is a stored generated column over `waypoints`, so it is never written here --
# but ON CONFLICT infers the index by its full column list, and naming only the
# other five matches no constraint at all (a plain 400 from PostgREST on every
# cache miss).
CACHE_CONFLICT_TARGET = (
    "origin_lat,origin_lng,destination_lat,destination_lng,waypoints_key,route_rank"
)

# Cap on `GET /routes?ids=`. Search asks for one id per distinct route in the
# ride list, so this only binds on a very large board -- and past it the
# frontend just sends a second request rather than being refused.
MAX_ROUTE_IDS = 60

OSRM_BASE_URL = "https://router.project-osrm.org/route/v1/driving"

# How long to wait on the public OSRM demo server before giving up.
#
# Down from a flat 20s, which was never actually reachable: the frontend's own
# `API_TIMEOUT_MS` is 15s (config/constants.ts), so the browser had already
# abandoned the request by the time this fired. All the extra 5s bought was a
# handler still holding a connection open for a response nobody was left to
# receive.
#
# Split rather than flat, because the two halves fail for different reasons and
# deserve different patience:
#
# - `connect` 3s. Opening a TCP+TLS connection to a healthy host takes well
#   under a second. Three seconds of it means the host is unreachable, not busy,
#   and no amount of further waiting will change that.
# - `read` 8s. This is the half that has to be generous. router.project-osrm.org
#   is a free, unmetered demo box shared by everyone on the internet, and it is
#   genuinely, legitimately slow under load -- a multi-waypoint request there
#   can take several seconds and still return a perfectly good route. Cutting
#   this to the 2-3s a paid routing service would justify would turn ordinary
#   slowness into a failed ride post. Eight seconds sits above the slow-but-fine
#   band and below the frontend's 15s ceiling, so a timeout here still leaves
#   room to answer the browser with a real error message instead of having the
#   fetch aborted underneath it.
#
# Worst case end to end is ~11s (connect + read), which stays inside that 15s.
OSRM_TIMEOUT = httpx.Timeout(connect=3.0, read=8.0, write=3.0, pool=3.0)

# Module-level and reused: one connection pool, one TLS handshake, kept warm
# across requests. Async now, so a slow OSRM no longer occupies a worker thread
# while it waits -- it parks a coroutine and the loop serves everyone else.
# Closed on shutdown in main.py's lifespan.
_osrm_client = httpx.AsyncClient(timeout=OSRM_TIMEOUT)

# OSRM reports a refused request in the `code` field of a JSON body it serves
# with HTTP 400 -- not with a 5xx -- so the body has to be read on a non-200
# too, or every one of these collapses into an indistinguishable 502.
#
# Each entry pairs the status this maps to with wording a rider can act on;
# OSRM's own `message` is written for whoever is calling the API ("URL string
# malformed close to position 12") and is not worth showing to a driver.
_OSRM_ERROR_RESPONSE = {
    "NoRoute": (
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "No driving route connects these two points. Try a different start or destination.",
    ),
    "NoSegment": (
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "One of these points is too far from a road we can route along. "
        "Try moving it closer to a town or highway.",
    ),
    "NoTrips": (
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "No driving route connects these two points. Try a different start or destination.",
    ),
}

# Anything OSRM refuses for a reason not listed above is still the request's
# fault, not the routing service's, so it maps to a 4xx rather than a 502.
_OSRM_FALLBACK_ERROR = (
    status.HTTP_400_BAD_REQUEST,
    "We could not work out a route between these two points.",
)


def _coord_key(value: float) -> str:
    """Rounds to 4 decimal places (~11m) -- the cache key precision -- and
    formats as a fixed-point string so PostgREST compares an exact decimal
    against the `numeric(7, 4)` column, not a float with binary rounding noise.
    """
    return f"{round(value, 4):.4f}"


def _waypoints_key(waypoints: list[Waypoint]) -> str:
    """Mirrors `public.route_waypoints_key` exactly: "lat,lng;lat,lng" in array
    order, each rounded to 4 decimals.

    Postgres computes that column on write, so this only has to agree with it
    to *query* by it -- and it has to agree exactly, because the two are
    compared as strings. Names are deliberately not part of the key: two
    drivers who stop at the same place and call it different things should
    share one cached route, not fork it.
    """
    return ";".join(
        f"{round(stop.lat, 4):.4f},{round(stop.lng, 4):.4f}" for stop in waypoints
    )


async def _query_cache(
    db: AsyncClient, origin: LatLng, destination: LatLng, waypoints: list[Waypoint]
) -> list[dict[str, Any]]:
    response = await (
        db.table("routes")
        .select(ROUTE_SELECT)
        .eq("origin_lat", _coord_key(origin.lat))
        .eq("origin_lng", _coord_key(origin.lng))
        .eq("destination_lat", _coord_key(destination.lat))
        .eq("destination_lng", _coord_key(destination.lng))
        # Same endpoints but a different stop list is a different trip, and so
        # a different cached row -- not a hit that quietly drops the stops.
        .eq("waypoints_key", _waypoints_key(waypoints))
        .order("route_rank")
        .execute()
    )
    return response.data


async def _call_osrm(
    origin: LatLng, destination: LatLng, waypoints: list[Waypoint]
) -> list[dict[str, Any]]:
    """Ask OSRM for driving directions. Awaited straight from the handler.

    Previously a blocking `httpx.Client` call handed to `run_in_threadpool`.
    Now that it awaits, a slow OSRM costs one parked coroutine instead of one
    of the threadpool's finite workers, so a spell of slowness on the demo
    server can no longer starve unrelated requests of somewhere to run.
    """
    # OSRM takes lng,lat (not lat,lng) pairs joined by semicolons, and treats
    # everything between the first and last as an ordered via-point.
    points = [origin, *waypoints, destination]
    path = ";".join(f"{point.lng},{point.lat}" for point in points)
    url = f"{OSRM_BASE_URL}/{path}"

    logger.info("calling OSRM for %d point(s): %s", len(points), path)
    try:
        response = await _osrm_client.get(
            url,
            params={"alternatives": "true", "overview": "full", "geometries": "polyline"},
        )
    except httpx.TimeoutException as exc:
        # Split out from the branch below because it is a different event with a
        # different answer. Unreachable means the box is down and a retry will
        # fail the same way; timed out means it answered slowly or not at all,
        # which on a free shared demo server is usually transient and worth
        # retrying. 504 rather than 502 says exactly that, and the handler
        # returns it promptly instead of leaving the browser to hit its own 15s
        # abort with nothing to show the user.
        logger.warning(
            "OSRM timed out after %ss (%s) for %s",
            OSRM_TIMEOUT.read,
            type(exc).__name__,
            path,
        )
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="The routing service is taking too long to respond. Please try again.",
        )
    except httpx.HTTPError as exc:
        # DNS failure, refused connection, TLS problem. The exception type is
        # what makes these tellable apart when reading the logs afterwards.
        logger.error("OSRM unreachable (%s): %s", type(exc).__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach the routing service. Please try again.",
        )

    # A 5xx is the one case where OSRM itself is the thing that is broken, so
    # it is the one case that stays a 502. A 4xx carries a `code` explaining
    # what was wrong with the request, and is handled with the body below.
    if response.status_code >= 500:
        logger.error("OSRM returned HTTP %s for %s", response.status_code, path)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The routing service is unavailable right now. Please try again.",
        )

    try:
        payload = response.json()
    except ValueError:
        logger.error(
            "OSRM returned unparseable body (HTTP %s): %.200s",
            response.status_code,
            response.text,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The routing service returned a response we could not read. Please try again.",
        )

    if payload.get("code") != "Ok" or not payload.get("routes"):
        code = payload.get("code")
        error_status, detail = _OSRM_ERROR_RESPONSE.get(code, _OSRM_FALLBACK_ERROR)
        # A code we know about is a routable-world problem (points in the sea,
        # no road between them); an unknown one may mean OSRM changed on us.
        log = logger.warning if code in _OSRM_ERROR_RESPONSE else logger.error
        log("OSRM refused %s: code=%s message=%s", path, code, payload.get("message"))
        raise HTTPException(status_code=error_status, detail=detail)

    logger.info("OSRM returned %d alternative(s) for %s", len(payload["routes"]), path)
    return payload["routes"]


async def close_osrm_client() -> None:
    """Close the shared OSRM connection pool. Called from the lifespan.

    Exists so `main.py` does not have to reach into this module's private
    `_osrm_client`, and so the reason it needs closing lives next to the reason
    it is shared in the first place.
    """
    await _osrm_client.aclose()


@router.post("/preview", response_model=RoutePreviewResponse)
async def preview_route(
    payload: RoutePreviewRequest,
    user: AuthUser = Depends(get_current_user),
    db: AsyncClient = Depends(get_db),
) -> RoutePreviewResponse:
    """Look up a cached route for this origin/destination/waypoints, or fetch
    and cache one from OSRM.

    All of OSRM's alternatives are cached (`route_rank` 0, 1, 2, ...), not just
    the fastest one, so a repeat request returns every option next time too.

    That said, expect exactly one route back whenever `waypoints` is non-empty:
    OSRM only computes alternatives for a plain two-coordinate request, and
    silently returns a single route once any via-point is present (verified
    against the public server -- Delhi->Dehradun yields 2 routes direct and 1
    with a single stop, even at `alternatives=3`). Nothing here special-cases
    that; the loop below just caches the one route it is given. The frontend
    reads the returned length rather than assuming a picker is possible.
    """
    cached = await _query_cache(db, payload.origin, payload.destination, payload.waypoints)
    if cached:
        logger.info(
            "route cache hit for user %s: %d alternative(s), %d waypoint(s)",
            user.id,
            len(cached),
            len(payload.waypoints),
        )
        return RoutePreviewResponse(routes=[RouteAlternative.from_row(row) for row in cached])

    logger.info(
        "route cache miss for user %s: %d waypoint(s), asking OSRM",
        user.id,
        len(payload.waypoints),
    )
    osrm_routes = await _call_osrm(
        payload.origin, payload.destination, payload.waypoints
    )

    # Stored on every alternative of this trip, so a cache hit can hand back
    # the driver's own labels -- OSRM never sees them and never returns them.
    waypoints_json = [stop.model_dump() for stop in payload.waypoints]

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
            "waypoints": waypoints_json,
        }
        for rank, route in enumerate(osrm_routes)
    ]

    try:
        # ignore_duplicates so a second request racing on the same uncached
        # pair doesn't 409 on the unique constraint -- it just skips the rows
        # the first request already won, and both re-read the cache below.
        await db.table("routes").upsert(
            rows_to_insert,
            on_conflict=CACHE_CONFLICT_TARGET,
            ignore_duplicates=True,
        ).execute()
    except APIError as exc:
        logger.error(
            "caching %d route row(s) failed [%s]: %s",
            len(rows_to_insert),
            exc.code,
            exc.message,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.message or "Could not cache the route.",
        )

    cached = await _query_cache(db, payload.origin, payload.destination, payload.waypoints)
    if not cached:
        # Written, then not found: the usual cause is `_waypoints_key` here
        # drifting from `public.route_waypoints_key` in the database.
        logger.error(
            "route rows were upserted but the cache re-read came back empty "
            "(waypoints_key=%r) -- does it still match the generated column?",
            _waypoints_key(payload.waypoints),
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Route was not cached.")
    logger.info("cached %d route alternative(s) for user %s", len(cached), user.id)
    return RoutePreviewResponse(routes=[RouteAlternative.from_row(row) for row in cached])


@router.get("", response_model=List[RouteAlternative])
async def list_routes(
    ids: str = Query(
        ...,
        description="Comma-separated route ids.",
        examples=["b1e0…,c2f1…"],
    ),
) -> List[RouteAlternative]:
    """Several cached routes at once, geometry included.

    Exists for search: `SearchResults` matches a rider's location against the
    actual road each ride drives, which means holding the polyline for every
    ride on the board. One request for all of them beats N requests for one
    each -- the browser caps concurrency per host at six, so a board of thirty
    rides would otherwise queue five deep before matching could even start.

    Geometry stays the encoded polyline the cache already holds; see
    `ROUTE_SELECT`. Ids that name no row are simply absent from the response
    rather than 404ing the batch -- one stale `route_id` on one ride must not
    cost every other ride its geometry.
    """
    wanted: list[str] = []
    for raw_id in ids.split(","):
        candidate = raw_id.strip()
        if not candidate or candidate in wanted:
            continue
        try:
            # Parsed rather than passed through: `in_` interpolates these into
            # a PostgREST filter, and a non-uuid would fail the whole query.
            wanted.append(str(UUID(candidate)))
        except ValueError:
            logger.warning("route id %r is not a uuid", candidate)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{candidate}' is not a valid route id.",
            )

    if not wanted:
        return []

    if len(wanted) > MAX_ROUTE_IDS:
        logger.warning(
            "route batch of %d exceeds the cap of %d", len(wanted), MAX_ROUTE_IDS
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ask for at most {MAX_ROUTE_IDS} routes at a time.",
        )

    client = await get_supabase()
    response = await (
        client.table("routes").select(ROUTE_SELECT).in_("id", wanted).execute()
    )
    # Ids that name no row are dropped silently by design, so a shortfall here
    # is the only trace that some ride is carrying a stale route_id.
    if len(response.data) != len(wanted):
        logger.warning(
            "asked for %d route(s), found %d -- some route ids are stale",
            len(wanted),
            len(response.data),
        )
    return [RouteAlternative.from_row(row) for row in response.data]


@router.get("/{route_id}", response_model=RouteAlternative)
async def get_route(route_id: UUID) -> RouteAlternative:
    """One cached route, geometry and waypoints included. Public — matches the
    `routes are readable by everyone` RLS policy, and lets RideDetail fetch a
    ride's polyline and its stop labels by `route_id` without needing the rider
    to be signed in.
    """
    client = await get_supabase()
    response = await (
        client.table("routes")
        .select(ROUTE_SELECT)
        .eq("id", str(route_id))
        .limit(1)
        .execute()
    )
    if not response.data:
        logger.warning("route %s not found", route_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Route not found")
    return RouteAlternative.from_row(response.data[0])
