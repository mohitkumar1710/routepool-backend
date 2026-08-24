"""Request-scoped auth dependencies.

`get_current_user` turns the `Authorization: Bearer <jwt>` header into a
verified Supabase user; `require_role` builds a gate on top of it. Attach
either to a route (or a whole router) with `Depends`.
"""

from typing import Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from supabase import Client
from supabase_auth.types import User

from app.config import get_supabase, get_user_supabase

# auto_error=False so a missing header lands in our own 401 below, with a
# message that matches the rest of the API.
_bearer = HTTPBearer(auto_error=False)


def get_access_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials


def get_current_user(token: str = Depends(get_access_token)) -> User:
    """Verify the JWT with Supabase and return the user it belongs to."""
    try:
        response = get_supabase().auth.get_user(token)
    except Exception:
        response = None
    if response is None or response.user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return response.user


def get_db(token: str = Depends(get_access_token)) -> Client:
    """A Supabase client acting as the caller, so RLS applies to its queries."""
    return get_user_supabase(token)


def user_role(user: User) -> str | None:
    """The app role (driver / rider / both).

    Read from `app_metadata`, which only the service-role key can write.
    `user_metadata` is user-writable — a role stored there could be edited by
    the account holder — and `user.role` is Postgres' own role ("authenticated"),
    not ours.
    """
    return (user.app_metadata or {}).get("role")


def require_role(*allowed: str) -> Callable[..., User]:
    """Gate a route to the given app roles.

        @router.post("", dependencies=[Depends(require_role("driver", "both"))])
    """

    def dependency(user: User = Depends(get_current_user)) -> User:
        if user_role(user) not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of these roles: {', '.join(allowed)}",
            )
        return user

    return dependency
