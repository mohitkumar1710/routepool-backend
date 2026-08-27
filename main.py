import os
import time
from contextlib import asynccontextmanager
from typing import Dict

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.config import close_supabase_clients, get_supabase, settings
from app.dependencies import describe_verification_mode
from app.routers import auth, bookings, rides, routes, users
from app.utils import get_logger

logger = get_logger("routepool.api")

# Render polls this every few seconds. Logging each one at INFO would bury every
# real line in the dashboard, so these are demoted to DEBUG in the middleware.
_QUIET_PATHS = {"/health"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Which keys are present, never what they are. A missing service-role key
    # only fails much later, at the first signup, with a RuntimeError from
    # _require() -- saying so at boot turns that into an obvious cause.
    logger.info(
        "starting routepool-api | supabase_url=%s anon_key=%s service_role_key=%s",
        "set" if settings.supabase_url else "MISSING",
        "set" if settings.supabase_anon_key else "MISSING",
        "set" if settings.supabase_service_role_key else "MISSING",
    )
    if not settings.supabase_service_role_key:
        logger.warning(
            "SUPABASE_SERVICE_ROLE_KEY is not set -- signup and role changes will fail"
        )

    # Stated outright rather than left to be inferred from whether requests feel
    # slow. This also warms the JWKS cache, so the first authenticated request
    # does not pay for the key fetch. It never raises -- a project whose keys
    # cannot be read still starts, just on the remote path.
    mode = await describe_verification_mode()
    logger.info("token verification: %s", mode)
    if mode.startswith("REMOTE"):
        logger.warning(
            "no local verification is possible -- every authenticated request "
            "will cost a round trip to Supabase Auth"
        )

    yield

    # Shutdown. Every connection pool the process opened gets closed here, so a
    # redeploy or a SIGTERM hands its sockets back rather than leaving Supabase
    # and OSRM holding half-open connections until they time out on their side.
    #
    # Each close is guarded separately and on purpose: one of them raising must
    # not skip the others, and a failure to tidy up on the way out is never
    # worth turning into a crash in the shutdown path.
    logger.info("shutting down routepool-api")
    for what, close in (
        # Both cached Supabase clients (anon and service-role) share one httpx
        # pool by construction, so this closes them together -- see
        # `close_supabase_clients`.
        ("supabase clients", close_supabase_clients),
        ("osrm client", routes.close_osrm_client),
    ):
        try:
            await close()
            logger.debug("closed %s", what)
        except Exception:
            logger.exception("failed to close %s cleanly", what)
    logger.info("routepool-api shutdown complete")


app = FastAPI(title="routepool-api", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "https://route-pool.netlify.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """One line per request: what was asked, what came back, how long it took.

    The level follows who is at fault -- 5xx is ours (error), 4xx is the
    caller's (warning), anything else is routine (info). That way a Render log
    filtered to WARNING and above shows only requests that actually went wrong.
    """
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # An unhandled exception never reaches the lines below, so it gets
        # logged here with its traceback before Starlette turns it into a 500.
        logger.exception(
            "%s %s raised after %.0fms",
            request.method,
            request.url.path,
            (time.perf_counter() - started) * 1000,
        )
        raise

    elapsed_ms = (time.perf_counter() - started) * 1000
    if response.status_code >= 500:
        log = logger.error
    elif response.status_code >= 400:
        log = logger.warning
    elif request.url.path in _QUIET_PATHS:
        log = logger.debug
    else:
        log = logger.info
    log(
        "%s %s -> %s in %.0fms",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


app.include_router(rides.router, prefix="/api")
app.include_router(routes.router, prefix="/api")
app.include_router(bookings.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(auth.router, prefix="/api")


@app.get("/health", tags=["health"])
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/supabase", tags=["health"])
async def supabase_ping() -> Dict[str, str]:
    """Proves the database is actually reachable, not just that the process is."""
    try:
        client = await get_supabase()
        await client.table("profiles").select("id").limit(1).execute()
    except Exception:
        logger.exception("supabase ping failed")
        raise
    logger.info("supabase ping ok")
    return {"status": "ok"}


def main():
    # Defaults suit local dev; the Dockerfile sets HOST=0.0.0.0 and RELOAD=0,
    # since 127.0.0.1 inside a container is unreachable from the host.
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    reload = os.getenv("RELOAD", "1") == "1"
    logger.info("uvicorn listening on %s:%s (reload=%s)", host, port, reload)
    uvicorn.run("main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()
