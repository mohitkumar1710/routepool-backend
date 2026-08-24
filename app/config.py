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
"""

import os
from functools import lru_cache

from dotenv import load_dotenv
from supabase import Client, ClientOptions, create_client

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

    cors_origins: list[str] = [
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
        if origin.strip()
    ]


settings = Settings()

# The server is stateless: it never persists or refreshes a session on disk, it
# just forwards whatever token the request carried.
_SERVER_OPTIONS = ClientOptions(persist_session=False, auto_refresh_token=False)


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    """Anon-key client. Safe to share across requests — carries no user."""
    return create_client(
        _require("SUPABASE_URL"),
        _require("SUPABASE_ANON_KEY"),
        options=_SERVER_OPTIONS,
    )


@lru_cache(maxsize=1)
def get_admin_supabase() -> Client:
    """Service-role client. Bypasses RLS — use deliberately."""
    return create_client(
        _require("SUPABASE_URL"),
        _require("SUPABASE_SERVICE_ROLE_KEY"),
        options=_SERVER_OPTIONS,
    )


def get_user_supabase(access_token: str) -> Client:
    """Anon-key client acting as the user who owns `access_token`.

    Not cached: each request gets a fresh client bound to its own token, so
    tokens can never bleed between callers.
    """
    return create_client(
        _require("SUPABASE_URL"),
        _require("SUPABASE_ANON_KEY"),
        options=ClientOptions(
            persist_session=False,
            auto_refresh_token=False,
            headers={"Authorization": f"Bearer {access_token}"},
        ),
    )
