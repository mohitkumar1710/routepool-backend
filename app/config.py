"""Settings and the shared Supabase clients.

Supabase backs both halves of the app: auth (signup/login, JWT issuance) and
data (rides, bookings, profiles). Three accessors live here, each for a
different trust level:

- `get_supabase()`      anon key. Public, RLS-enforced, no user attached.
                        Use for auth calls (sign_up / sign_in) and public reads.
- `get_user_supabase()` anon key + the caller's access token. Every query runs
                        as that user, so RLS policies decide what they can see.
                        This is the one routers should reach for.
- `get_admin_supabase()` service-role key. Bypasses RLS completely. Server-side
                        trusted paths only — never build it from a client-
                        supplied value, and never leak the key to the frontend.

All three are async: they return `AsyncClient`, and every call made through
them (`.table(...).execute()`, `.auth.*`, `.rpc(...)`) must be awaited. The
accessors themselves are coroutines too, so reach for them as
`await get_supabase()`.
"""

import asyncio
import os
from typing import Optional

import httpx
from dotenv import load_dotenv
from supabase import AsyncClient, AsyncClientOptions, create_async_client

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill in the "
            f"values from your Supabase project (Settings -> API)."
        )
    return value


class Settings:
    """Environment-backed config, read once at import time."""

    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_anon_key: str = os.getenv("SUPABASE_ANON_KEY", "")
    supabase_service_role_key: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    # ONLY for projects still signing access tokens with the legacy shared
    # HS256 secret (Settings -> API -> JWT Settings -> "JWT Secret").
    #
    # Most projects no longer are. Supabase has moved to asymmetric signing
    # keys, where tokens are ES256/RS256 and carry a `kid` naming the public
    # key that verifies them; those are fetched from the project's JWKS
    # endpoint and need no secret here at all. `app/dependencies.py` reads the
    # algorithm off each token and picks the right one, so leaving this empty
    # is correct and expected on a modern project.
    #
    # Beware: the *anon key* is an HS256 JWT signed with this secret, so seeing
    # HS256 there says nothing about what user access tokens use. Check a real
    # access token, not an API key.
    supabase_jwt_secret: str = os.getenv("SUPABASE_JWT_SECRET", "")

    @property
    def jwks_url(self) -> str:
        """Where this project publishes the public keys for its access tokens.

        Public and unauthenticated by design -- these are the keys anyone needs
        in order to verify a token this project issued.
        """
        return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json" if self.supabase_url else ""

    @property
    def jwt_issuer(self) -> str:
        """The `iss` claim Supabase stamps on this project's access tokens.

        Checked so a token minted by a *different* Supabase project cannot be
        replayed here, in the event two projects were ever handed the same
        secret. Empty when SUPABASE_URL is unset, in which case the issuer
        check is skipped rather than failing everything closed.
        """
        return f"{settings.supabase_url.rstrip('/')}/auth/v1" if self.supabase_url else ""


settings = Settings()

# One HTTP connection pool for every Supabase client in the process.
#
# This is what makes the per-request client below cheap. `AsyncClientOptions`
# threads `httpx_client` into both the PostgREST client and the GoTrue auth
# client, and both of them send the Authorization header *per request* rather
# than storing it on the httpx client:
#
#   postgrest/base_request_builder.py  RequestConfig.send() ->
#       additional_headers.update(self.headers); session.request(..., headers=...)
#   supabase_auth/_async/gotrue_base_api.py  _request() ->
#       headers = {**self._headers, **(headers or {})}; _http_client.request(..., headers=...)
#
# Both also build an absolute URL, so the shared client needs no `base_url`.
# That is the property that makes sharing safe: this object contributes TCP
# connections and nothing else. No token is ever written to it, so no token can
# leak from one caller to the next through it.
#
# `timeout=120` is not a new policy — it is what PostgREST was already using
# (postgrest.constants.DEFAULT_POSTGREST_CLIENT_TIMEOUT). Injecting a client
# without saying so would silently drop the effective timeout to httpx's 5s
# default, which would be a behaviour change dressed up as a refactor. Revisit
# it deliberately when timeouts are on the table, not here.
#
# `http2=True` and `follow_redirects=True` match what the library builds for
# itself when nothing is injected.
_http_client = httpx.AsyncClient(
    http2=True,
    follow_redirects=True,
    timeout=120,
)


def get_http_client() -> httpx.AsyncClient:
    """The process-wide connection pool, for callers outside this module.

    `app/dependencies.py` fetches the project's JWKS through it rather than
    opening a second pool for one small, rarely-repeated GET. Pass an explicit
    `timeout=` on such a call: the pool's default is PostgREST's 120s, which is
    far too patient for a key fetch sitting in front of a request.
    """
    return _http_client


def _options(headers: Optional[dict[str, str]] = None) -> AsyncClientOptions:
    """Client options sharing the pool above.

    A fresh object per call rather than one shared module-level instance:
    `AsyncClientOptions` carries a mutable `storage`, and handing the same
    instance to several clients would have them share one session store.
    The server is stateless — it never persists or refreshes a session, it just
    forwards whatever token the request carried — so there is nothing to share
    and no reason to risk it.
    """
    return AsyncClientOptions(
        persist_session=False,
        auto_refresh_token=False,
        httpx_client=_http_client,
        **({"headers": headers} if headers else {}),
    )


# The two process-wide clients, built once on first use.
#
# Deliberately NOT `@lru_cache`: `create_async_client` is a coroutine function,
# and `lru_cache` would cache the *coroutine object* it returns, not the client.
# The first request would await it and get a client; the second would await the
# same, already-consumed coroutine and raise "cannot reuse already awaited
# coroutine". The lock below is the async equivalent of what `lru_cache` was
# doing here: build exactly once, hand the same instance to everyone after.
_anon_client: Optional[AsyncClient] = None
_admin_client: Optional[AsyncClient] = None
_client_lock = asyncio.Lock()


async def get_supabase() -> AsyncClient:
    """Anon-key client. Safe to share across requests — carries no user."""
    global _anon_client
    if _anon_client is None:
        async with _client_lock:
            # Re-checked under the lock: several requests can get past the
            # check above concurrently, and only the first should build.
            if _anon_client is None:
                _anon_client = await create_async_client(
                    _require("SUPABASE_URL"),
                    _require("SUPABASE_ANON_KEY"),
                    options=_options(),
                )
    return _anon_client


async def get_admin_supabase() -> AsyncClient:
    """Service-role client. Bypasses RLS — use deliberately."""
    global _admin_client
    if _admin_client is None:
        async with _client_lock:
            if _admin_client is None:
                _admin_client = await create_async_client(
                    _require("SUPABASE_URL"),
                    _require("SUPABASE_SERVICE_ROLE_KEY"),
                    options=_options(),
                )
    return _admin_client


async def close_supabase_clients() -> None:
    """Release everything the Supabase clients hold. Called from the lifespan.

    There is exactly one thing to close here, and that is by design. Both
    cached clients — and every short-lived per-request client `get_user_supabase`
    hands out — were built with `_options()`, which injects the single
    `_http_client` above. The sub-clients underneath them (auth, postgrest,
    storage, functions) all borrow that same pool rather than opening their own,
    so `_http_client.aclose()` drains the connections for all of them at once.
    Calling `client.auth.close()` on each cached client in turn would be the
    same call to the same object, twice, dressed up as thoroughness.

    Realtime is deliberately left alone: `AsyncClient.__init__` constructs one,
    but nothing in this app ever calls `.connect()`, so there is no socket open
    to close.

    The cached clients are dropped as well as closed. Without that, a caller
    arriving after shutdown (a stray task, or a test that restarts the app in
    the same process) would be handed a live-looking client over a dead pool;
    setting them back to None makes the next `get_supabase()` rebuild instead.
    """
    global _anon_client, _admin_client
    _anon_client = None
    _admin_client = None
    await _http_client.aclose()


async def get_user_supabase(access_token: str) -> AsyncClient:
    """Anon-key client acting as the user who owns `access_token`.

    Not cached: each request gets a fresh client bound to its own token, so
    tokens can never bleed between callers.

    The *client object* is per-request; the TCP connections underneath it are
    not. `_options()` hands it the shared `_http_client`, so this no longer
    opens a fresh pool and re-does a TLS handshake on every call — it borrows a
    warm connection and puts it back. What stays per-request is the thing that
    has to: the Authorization header, which travels with each individual
    request rather than living on the shared client.
    """
    return await create_async_client(
        _require("SUPABASE_URL"),
        _require("SUPABASE_ANON_KEY"),
        options=_options(headers={"Authorization": f"Bearer {access_token}"}),
    )
