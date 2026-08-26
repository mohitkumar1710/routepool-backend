from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client
from supabase_auth.errors import AuthApiError
from supabase_auth.types import User as AuthUser

from app.config import get_admin_supabase, get_supabase
from app.dependencies import get_current_user, get_db
from app.repository import get_profile
from app.schemas.user import RoleUpdate, User

router = APIRouter(prefix="/users", tags=["users"])

# Roles that let an account post rides. `rider` is deliberately absent: the role
# is additive and one-way (see `UserRole` in the frontend's types/ride.ts), so
# adding driving is a promotion and there is no supported way back.
_DRIVING_ROLES = ("driver", "both")


# Declared before `/{user_id}` so the literal segment is matched first. Only the
# method differs today, but the ordering is what keeps that true if a `PATCH
# /users/{user_id}` is ever added.
@router.patch("/me", response_model=User)
def update_me(
    payload: RoleUpdate,
    user: AuthUser = Depends(get_current_user),
    db: Client = Depends(get_db),
) -> User:
    """Promote the signed-in account to a driver.

    The role is stored twice, and both copies have to move or the account ends
    up half-promoted:

    - `app_metadata.role` is what `require_role` gates on. It is writable only
      by the service-role key, which is exactly why it can be trusted -- a user
      cannot forge it against their own account.
    - `profiles.role` is what every read path returns, so it is what the UI
      believes. `canDrive` on the frontend comes from here.

    `app_metadata` goes first. If the second write fails the account can still
    be trusted by the server but the UI has not caught up, so the user is shown
    the old state and retries into an identical, idempotent call. The reverse
    order would leave a UI offering a driver dashboard the server would refuse.
    """
    if payload.role not in _DRIVING_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Driving can be added to an account but not removed.",
        )

    try:
        get_admin_supabase().auth.admin.update_user_by_id(
            str(user.id), {"app_metadata": {"role": payload.role}}
        )
    except AuthApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not update the account's role: {exc}",
        )

    # The user's own client, so the "users update their own profile" RLS policy
    # is what authorises this -- not the service-role key we just used above.
    response = (
        db.table("profiles")
        .update({"role": payload.role})
        .eq("id", str(user.id))
        .execute()
    )
    if not response.data:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Role was updated but the profile did not come back.",
        )

    # Their own profile, so the email is theirs to see -- and returning the full
    # user lets `becomeDriver()` skip its `/auth/me` fallback.
    return User.from_row(response.data[0], include_email=True)


@router.get("/{user_id}", response_model=User)
def get_user(user_id: str) -> User:
    """Fill in a passenger or driver the frontend has not cached.

    Public, and returns no email — use `/auth/me` for the signed-in user's own
    address.
    """
    profile = get_profile(get_supabase(), user_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return User.from_row(profile)
