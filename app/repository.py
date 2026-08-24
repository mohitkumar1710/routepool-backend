"""Thin query helpers shared by more than one router.

Everything here takes an explicit client so the caller decides the trust level:
pass `Depends(get_db)` for an RLS-enforced client acting as the caller.
"""

from typing import Any, Iterable

from supabase import Client

PROFILE_COLUMNS = "id, name, email, role, avatar_url, rating, review_count, is_verified"


def get_profile(db: Client, user_id: str) -> dict[str, Any] | None:
    response = (
        db.table("profiles")
        .select(PROFILE_COLUMNS)
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def get_profiles(db: Client, user_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Fetch several profiles at once, keyed by id — one round trip, not N."""
    ids = list({str(uid) for uid in user_ids})
    if not ids:
        return {}
    response = db.table("profiles").select(PROFILE_COLUMNS).in_("id", ids).execute()
    return {str(row["id"]): row for row in response.data}
