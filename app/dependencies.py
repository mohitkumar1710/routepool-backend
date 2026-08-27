"""Request-scoped auth dependencies.

`get_current_user` turns the `Authorization: Bearer <jwt>` header into a
verified caller; `require_role` builds a gate on top of it. Attach either to a
route (or a whole router) with `Depends`.

Tokens are verified *locally*, with PyJWT. Supabase issues them itself, so
checking the signature here is the same check Supabase Auth would do -- it just
does not cost a round trip to do it. That matters more than it sounds:
`auth.get_user(token)` was an HTTPS call to Supabase on *every authenticated
request*, so a page firing four API calls paid for four of them, each one
blocking its handler before that handler's own query had even started.

Two signing schemes are supported, because which one a project uses is not
something this code gets to choose -- see the notes above the constants below.
A project on modern asymmetric keys needs no secret configured at all.

Nothing about database authorisation moves here. The raw token is still handed
to PostgREST by `get_db`, and Postgres verifies it again, independently, before
applying RLS. Local verification decides who the caller is; the database still
decides what they may read and write.

Every dependency here is `async def`. FastAPI would happily run a sync one in
a threadpool, but these sit in front of handlers that touch the async Supabase
client, and keeping the whole chain async means a request never bounces between
the loop and a worker thread on its way in.
"""

import asyncio
import time
from typing import Any, Callable, Optional

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from supabase import AsyncClient

from app.config import get_http_client, get_supabase, get_user_supabase, settings
from app.utils import get_logger

logger = get_logger("routepool.auth")

# auto_error=False so a missing header lands in our own 401 below, with a
# message that matches the rest of the API.
_bearer = HTTPBearer(auto_error=False)

# Supabase signs access tokens one of two ways, and which one is a property of
# the project rather than a choice made here:
#
# - Asymmetric (current). ES256 or RS256, with a `kid` in the header naming the
#   key. The public keys are published, unauthenticated, at the project's JWKS
#   endpoint, so nothing secret is needed to verify one.
# - Symmetric (legacy). HS256, signed with the shared JWT secret.
#
# Both are handled; the algorithm is read off the token and dispatched below.
#
# Do not infer the scheme from the anon key. The anon key is itself an HS256
# JWT signed with the legacy secret, and it stays that way on projects whose
# *access tokens* are ES256 -- so checking it tells you nothing, confidently.
#
# A WORD ON ALGORITHM CONFUSION, because reading `alg` from an untrusted header
# is where that attack begins. The classic version takes the public key (which
# is public) and signs a token with HS256 using it as the HMAC secret; a server
# that reads `alg` and then reaches for "the key" verifies the forgery. That
# cannot happen here, because the branches never share key material: the HS256
# branch uses `SUPABASE_JWT_SECRET` and nothing else, the asymmetric branch uses
# a JWKS key and nothing else, and a JWKS public key is never handed to HMAC.
# `algorithms=` is then pinned to the single algorithm that branch chose its key
# for, so `"alg": "none"` matches neither.
_JWT_ALGORITHMS_ASYMMETRIC = ("ES256", "RS256")
_JWT_ALGORITHM_SYMMETRIC = "HS256"

# Every Supabase *user* token carries aud="authenticated". The anon and
# service-role keys carry "anon"/"service_role" instead, so checking this is
# what stops someone authenticating as the API key pasted out of the frontend.
_JWT_AUDIENCE = "authenticated"

# Tolerance on `exp`/`iat` for clock skew between this host and Supabase.
# Small: these tokens live an hour, so it only has to cover drift.
_JWT_LEEWAY_SECONDS = 10

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or expired token",
    headers={"WWW-Authenticate": "Bearer"},
)


class AuthUser(BaseModel):
    """The caller, as the access token describes them.

    Deliberately not `supabase_auth.types.User`. That model is the shape of the
    admin API's answer and requires fields a token does not carry (notably
    `created_at`); filling those in with plausible-looking stand-ins would put
    wrong data behind right-looking attribute names. This carries exactly what
    a Supabase access token actually asserts, and nothing else.

    `id` is the `sub` claim -- the same uuid `auth.get_user()` used to return,
    and the same one `auth.uid()` resolves to inside an RLS policy, so every
    `user.id` in the routers still means what it always meant.
    """

    id: str
    email: Optional[str] = None
    # Service-role-writable only, which is why `require_role` can trust it.
    # See `user_role` below for the one caveat that comes with reading it here.
    app_metadata: dict[str, Any] = Field(default_factory=dict)
    # User-writable. Never authorise anything on this.
    user_metadata: dict[str, Any] = Field(default_factory=dict)
    # Postgres' own role ("authenticated"), not the app's driver/rider role.
    postgres_role: Optional[str] = None

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> "AuthUser":
        return cls(
            id=str(claims["sub"]),
            email=claims.get("email") or None,
            app_metadata=claims.get("app_metadata") or {},
            user_metadata=claims.get("user_metadata") or {},
            postgres_role=claims.get("role"),
        )


async def get_access_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    if credentials is None:
        logger.warning("request arrived with no Authorization header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials


class CannotVerifyLocally(Exception):
    """No key material was available to check a token against.

    Deliberately distinct from "the token is invalid", because the two deserve
    different answers: an invalid token is a 401, while being unable to verify
    at all means falling back to Supabase Auth rather than signing a user out
    over what is really our own configuration or network problem.
    """


# The project's published verification keys, by `kid`.
#
# Cached for the life of the process. Supabase rotates signing keys rarely and
# announces the new one in each token's `kid`, so an unknown kid -- and nothing
# else -- is what prompts a refetch.
_jwks_keys: dict[str, Any] = {}
_jwks_lock = asyncio.Lock()
_jwks_fetched_at: float = 0.0

# Floor on how often an unknown `kid` may trigger a refetch. Without it, anyone
# could drive requests to Supabase's JWKS endpoint as fast as they can send
# tokens carrying random kids -- cheap for them, not for us.
_JWKS_MIN_REFRESH_SECONDS = 60.0

# Short on purpose: this fetch sits in front of a user's request, and the keys
# are almost always cached already, so waiting long on a slow one helps nobody.
_JWKS_TIMEOUT_SECONDS = 5.0


async def _refresh_jwks(force: bool = False) -> None:
    """Pull the project's JWKS into `_jwks_keys`.

    Raises `CannotVerifyLocally` if the keys could not be fetched at all.
    """
    global _jwks_fetched_at

    if not settings.jwks_url:
        raise CannotVerifyLocally("SUPABASE_URL is not set, so there is no JWKS to fetch")

    async with _jwks_lock:
        # Re-checked under the lock: a burst of requests all missing the same
        # new kid should produce one fetch between them, not one each.
        age = time.monotonic() - _jwks_fetched_at
        if not force and _jwks_keys and age < _JWKS_MIN_REFRESH_SECONDS:
            return

        try:
            response = await get_http_client().get(
                settings.jwks_url, timeout=_JWKS_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CannotVerifyLocally(f"could not fetch JWKS: {exc}") from exc

        keys: dict[str, Any] = {}
        for entry in payload.get("keys", []):
            kid = entry.get("kid")
            if not kid:
                continue
            try:
                keys[kid] = jwt.PyJWK(entry).key
            except Exception as exc:
                # One unusable entry must not cost us the others.
                logger.warning("skipping JWKS key %s: %s", kid, exc)

        _jwks_fetched_at = time.monotonic()
        if keys:
            # Replaced wholesale, but only when the fetch actually yielded
            # something -- an empty answer should leave the working keys alone.
            _jwks_keys.clear()
            _jwks_keys.update(keys)
        logger.info("loaded %d signing key(s) from the project JWKS", len(keys))


async def _key_for_kid(kid: str) -> Any:
    """The public key named by `kid`, refetching once if it is not yet known."""
    key = _jwks_keys.get(kid)
    if key is not None:
        return key

    # Unknown kid: either a key rotation we have not seen yet, or a forged
    # header. One refetch tells us which, and the rate limit bounds the cost.
    await _refresh_jwks()
    return _jwks_keys.get(kid)


async def decode_token(token: str) -> dict[str, Any] | None:
    """Verify `token` locally and return its claims; None if it does not hold.

    Raises `CannotVerifyLocally` when there is no key material to check against
    at all -- an unconfigured legacy secret, or a JWKS endpoint we could not
    reach. That is the caller's cue to fall back, not to reject.

    Otherwise returns claims rather than raising, so the caller decides the
    response. A failure here is ordinary -- an hour-old tab, a token from
    another project -- so it logs at WARNING, never ERROR, and never logs the
    token itself.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        # Not even shaped like a JWT: nothing to look up and nothing to fall
        # back to, so this one is simply invalid.
        logger.warning("token rejected: unreadable header (%s)", type(exc).__name__)
        return None

    algorithm = header.get("alg")

    if algorithm == _JWT_ALGORITHM_SYMMETRIC:
        if not settings.supabase_jwt_secret:
            raise CannotVerifyLocally("token is HS256 but SUPABASE_JWT_SECRET is not set")
        key: Any = settings.supabase_jwt_secret
    elif algorithm in _JWT_ALGORITHMS_ASYMMETRIC:
        kid = header.get("kid")
        if not kid:
            logger.warning("token rejected: %s header carries no kid", algorithm)
            return None
        key = await _key_for_kid(kid)
        if key is None:
            # The JWKS was fetched and does not contain this kid, so the token
            # was not signed by this project.
            logger.warning("token rejected: no published key matches kid %s", kid)
            return None
    else:
        # Covers "none", and anything Supabase might adopt that has not been
        # deliberately allowed here yet.
        logger.warning("token rejected: unsupported algorithm %r", algorithm)
        return None

    try:
        return jwt.decode(
            token,
            key,
            # Pinned to the one algorithm this branch selected its key for --
            # never the full list. See the note above the constants.
            algorithms=[algorithm],
            audience=_JWT_AUDIENCE,
            # Skipped rather than failed closed when SUPABASE_URL is unset:
            # signature, expiry and audience are all checked regardless, and an
            # unset URL means the process could not reach Supabase anyway.
            **({"issuer": settings.jwt_issuer} if settings.jwt_issuer else {}),
            leeway=_JWT_LEEWAY_SECONDS,
            # `sub` is optional in the JWT spec but required by us: it is the
            # user id every router reads, so a token without one is unusable.
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        logger.warning("token rejected: expired")
    except jwt.InvalidAudienceError:
        # Nearly always someone sending the anon key as a bearer token.
        logger.warning("token rejected: wrong audience (expected %s)", _JWT_AUDIENCE)
    except jwt.InvalidIssuerError:
        logger.warning("token rejected: issued by a different Supabase project")
    except jwt.InvalidTokenError as exc:
        # Bad signature, refused algorithm, malformed. The exception type is
        # worth having; the token itself is never worth writing down.
        logger.warning("token rejected (%s): %s", type(exc).__name__, exc)
    return None


async def describe_verification_mode() -> str:
    """Work out how tokens will be verified, warming the key cache on the way.

    Called once from the lifespan so the boot log states the mode outright
    instead of leaving it to be inferred from whether requests feel slow. Never
    raises: failing to work this out must not stop the process starting.
    """
    if settings.jwks_url:
        try:
            await _refresh_jwks(force=True)
        except CannotVerifyLocally as exc:
            logger.warning("could not reach the project JWKS at boot: %s", exc)
        else:
            if _jwks_keys:
                return "local (JWKS, asymmetric keys)"
    if settings.supabase_jwt_secret:
        return "local (HS256 shared secret)"
    return "REMOTE -- a network round trip to Supabase Auth on every request"


async def _verify_remotely(token: str) -> AuthUser | None:
    """The pre-PyJWT path: ask Supabase Auth who this token belongs to.

    Kept for two narrow jobs, and used for nothing else:

    - Local verification is impossible right now -- no legacy secret for an
      HS256 token, or a JWKS endpoint we could not reach. Rather than 401 every
      request over that, the API keeps working at the old speed and says so.
    - `require_role` is about to refuse someone. See its note on staleness.
    """
    try:
        client = await get_supabase()
        response = await client.auth.get_user(token)
    except Exception as exc:
        logger.warning("remote token verification failed: %s", exc)
        return None
    if response is None or response.user is None:
        return None
    user = response.user
    return AuthUser(
        id=str(user.id),
        email=user.email,
        app_metadata=user.app_metadata or {},
        user_metadata=user.user_metadata or {},
        postgres_role=user.role,
    )


# Said once per process, not once per request: being unable to verify locally is
# a fact about the deployment, and repeating it per call would bury the log.
_warned_about_local_verification = False


async def get_current_user(token: str = Depends(get_access_token)) -> AuthUser:
    """The caller, from a locally verified JWT. 401s on anything else."""
    global _warned_about_local_verification

    try:
        claims = await decode_token(token)
    except CannotVerifyLocally as exc:
        if not _warned_about_local_verification:
            _warned_about_local_verification = True
            logger.warning(
                "cannot verify tokens locally (%s) -- falling back to a remote "
                "auth.get_user() call on every authenticated request",
                exc,
            )
        user = await _verify_remotely(token)
        if user is None:
            raise _UNAUTHORIZED
        return user

    if claims is None:
        raise _UNAUTHORIZED
    return AuthUser.from_claims(claims)


async def get_db(token: str = Depends(get_access_token)) -> AsyncClient:
    """A Supabase client acting as the caller, so RLS applies to its queries."""
    return await get_user_supabase(token)


def user_role(user: AuthUser) -> str | None:
    """The app role (driver / rider / both).

    Read from `app_metadata`, which only the service-role key can write.
    `user_metadata` is user-writable — a role stored there could be edited by
    the account holder — and `postgres_role` is Postgres' own role
    ("authenticated"), not ours.

    One caveat comes with reading this from the token rather than from a live
    lookup: it is a *snapshot*, taken when the token was issued. `PATCH
    /users/me` promotes an account to driver, but the token the caller is
    holding still says whatever it said before, and will keep saying it until
    the token expires (an hour) or they sign in again. `require_role` handles
    that; nothing else should read this to make an allow/deny decision without
    handling it too.

    Stays a plain function: it reads a dict already in hand and does no I/O, so
    there is nothing here to await.
    """
    return (user.app_metadata or {}).get("role")


def require_role(*allowed: str) -> Callable[..., AuthUser]:
    """Gate a route to the given app roles.

        @router.post("", dependencies=[Depends(require_role("driver", "both"))])

    Fast path, then a second opinion. The role in the token is correct for every
    caller whose role has not changed since they signed in — nearly all of
    them — and costs nothing to read. It is stale for exactly one person: the
    driver who tapped "become a driver" a minute ago and is still holding the
    rider token they had before (see `user_role`). So a token that *grants*
    access is believed at once, and only a token that would be *refused* is
    re-checked against Supabase Auth before the 403 goes out.

    That keeps the round trip off the success path entirely while letting a
    promotion take effect immediately, exactly as it did when every request was
    checked remotely. The cost is bounded by construction: it can only fire on
    requests that were already about to fail.

    The factory itself is sync — it runs at import time to build the dependency.
    The dependency it returns is async, because FastAPI awaits that one per
    request and it sits downstream of `get_current_user`.
    """

    async def dependency(
        user: AuthUser = Depends(get_current_user),
        token: str = Depends(get_access_token),
    ) -> AuthUser:
        if user_role(user) in allowed:
            return user

        # The token says no. Before believing it, check whether the account was
        # promoted after this token was minted.
        fresh = await _verify_remotely(token)
        if fresh is not None and user_role(fresh) in allowed:
            logger.info(
                "user %s carries a stale role claim (%s); Supabase says %s -- allowing",
                fresh.id,
                user_role(user),
                user_role(fresh),
            )
            return fresh

        logger.warning(
            "user %s (role=%s) blocked from a route requiring %s",
            user.id,
            user_role(user),
            allowed,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires one of these roles: {', '.join(allowed)}",
        )

    return dependency
