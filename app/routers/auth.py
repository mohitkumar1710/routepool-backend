"""Signup, login, session restore and logout, all backed by Supabase Auth.

Supabase issues the JWT; this router just relays it. The token the frontend
stores in localStorage is a real Supabase access token, which is what makes the
RLS policies in `supabase/schema.sql` apply to every subsequent request.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from supabase import Client
from supabase_auth.errors import AuthApiError
from supabase_auth.types import User as AuthUser

from app.config import get_admin_supabase, get_supabase, get_user_supabase
from app.dependencies import get_access_token, get_current_user, get_db
from app.repository import get_profile
from app.schemas.user import AuthResponse, LoginRequest, SignupRequest, User

router = APIRouter(prefix="/auth", tags=["auth"])


def _sign_in(email: str, password: str) -> Any:
    """Exchange credentials for a Supabase session, or raise a 400.

    Wrong password and unknown address deliberately produce the same message —
    telling them apart would let anyone probe which emails have accounts.
    """
    try:
        session = get_supabase().auth.sign_in_with_password(
            {"email": email, "password": password}
        )
    except AuthApiError:
        session = None
    if session is None or session.session is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No account found with that email, or the password is incorrect.",
        )
    return session


def _profile_or_502(db: Client, user_id: str) -> dict[str, Any]:
    profile = get_profile(db, user_id)
    if profile is None:
        # The on_auth_user_created trigger should have made this row. If it is
        # missing, schema.sql was never applied to this project.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Account exists but has no profile row. Has supabase/schema.sql been run?",
        )
    return profile


@router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest) -> AuthResponse:
    admin = get_admin_supabase()
    try:
        admin.auth.admin.create_user(
            {
                "email": payload.email,
                "password": payload.password,
                # Skip the confirmation email: the frontend expects to be signed
                # in immediately after signing up.
                "email_confirm": True,
                # `name` and `role` here feed the on_auth_user_created trigger,
                # which copies them into public.profiles.
                "user_metadata": {"name": payload.name, "role": payload.role},
                # app_metadata is service-role-only, so require_role() can trust
                # it in a way it could never trust user_metadata.
                "app_metadata": {"role": payload.role},
            }
        )
    except AuthApiError as exc:
        already_registered = "already" in str(exc).lower()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT if already_registered else status.HTTP_400_BAD_REQUEST,
            detail="An account with that email already exists."
            if already_registered
            else f"Could not create the account: {exc}",
        )

    session = _sign_in(payload.email, payload.password)
    profile = _profile_or_502(get_supabase(), session.user.id)
    return AuthResponse(
        access_token=session.session.access_token,
        user=User.from_row(profile, include_email=True),
    )


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest) -> AuthResponse:
    session = _sign_in(payload.email, payload.password)
    profile = _profile_or_502(get_supabase(), session.user.id)
    return AuthResponse(
        access_token=session.session.access_token,
        user=User.from_row(profile, include_email=True),
    )


@router.get("/me", response_model=User)
def me(
    user: AuthUser = Depends(get_current_user),
    db: Client = Depends(get_db),
) -> User:
    """Restore the session on reload. `get_current_user` 401s on a dead token."""
    return User.from_row(_profile_or_502(db, user.id), include_email=True)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(token: str = Depends(get_access_token)) -> Response:
    try:
        get_user_supabase(token).auth.sign_out()
    except AuthApiError:
        # Already expired or revoked — the caller is signed out either way, and
        # the frontend signs out locally regardless of what this returns.
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)
