from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from supabase import AsyncClient
from supabase_auth.errors import AuthApiError

from app.config import get_admin_supabase, get_supabase
from app.dependencies import AuthUser, get_current_user, get_db
from app.repository import get_profile, get_profiles
from app.schemas.user import RoleUpdate, User
from app.utils import get_logger

logger = get_logger("routepool.users")

router = APIRouter(prefix="/users", tags=["users"])

# Roles that let an account post rides. `rider` is deliberately absent: the role
# is additive and one-way (see `UserRole` in the frontend's types/ride.ts), so
# adding driving is a promotion and there is no supported way back.
_DRIVING_ROLES = ("driver", "both")

# Cap on `GET /users?ids=`. The driver dashboard asks for one id per distinct
# passenger with a request outstanding, so this only binds on a very busy
# driver -- and past it the frontend sends a second request rather than being
# refused. Mirrors `MAX_ROUTE_IDS` in routes.py.
MAX_USER_IDS = 100


# Declared before `/{user_id}` so the literal segment is matched first. Only the
# method differs today, but the ordering is what keeps that true if a `PATCH
# /users/{user_id}` is ever added.
@router.patch("/me", response_model=User)
async def update_me(
    payload: RoleUpdate,
    user: AuthUser = Depends(get_current_user),
    db: AsyncClient = Depends(get_db),
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
        logger.warning(
            "user %s tried to set role=%s; only %s are grantable",
            user.id,
            payload.role,
            _DRIVING_ROLES,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Driving can be added to an account but not removed.",
        )

    # Worth an INFO line either way: this is a privilege change, and the two
    # writes below can leave the account half-promoted if the second one fails.
    logger.info("promoting user %s to role=%s", user.id, payload.role)

    try:
        admin = await get_admin_supabase()
        await admin.auth.admin.update_user_by_id(
            str(user.id), {"app_metadata": {"role": payload.role}}
        )
    except AuthApiError as exc:
        logger.error("app_metadata role write failed for user %s: %s", user.id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not update the account's role: {exc}",
        )

    # The user's own client, so the "users update their own profile" RLS policy
    # is what authorises this -- not the service-role key we just used above.
    response = await (
        db.table("profiles")
        .update({"role": payload.role})
        .eq("id", str(user.id))
        .execute()
    )
    if not response.data:
        # app_metadata moved but profiles.role did not: the server trusts the
        # promotion, the UI does not. Retrying the same call repairs it.
        logger.error(
            "user %s is half-promoted -- app_metadata says %s but the profiles "
            "update returned no row (RLS policy missing?)",
            user.id,
            payload.role,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Role was updated but the profile did not come back.",
        )

    logger.info("user %s is now role=%s", user.id, payload.role)

    # Their own profile, so the email is theirs to see -- and returning the full
    # user lets `becomeDriver()` skip its `/auth/me` fallback.
    return User.from_row(response.data[0], include_email=True)


# Declared before `/{user_id}` for the same reason `/me` is: the literal path
# has to be matched before the parameterised one can swallow it.
@router.get("", response_model=List[User])
async def list_users(
    ids: str = Query(
        ...,
        description="Comma-separated user ids.",
        examples=["b1e0…,c2f1…"],
    ),
) -> List[User]:
    """Several profiles at once, by id.

    Exists for the driver dashboard, which renders a row per pending booking
    and needs the passenger behind each one. Asking for them one at a time is a
    textbook N+1: a driver with twelve outstanding requests fired twelve
    requests, and the browser only runs six per host at once, so the last of
    them waited on the first six to finish before it even started.

    Public and email-free, exactly like `GET /users/{user_id}` — this is the
    same read, batched, not a wider one. Ids that name no profile are simply
    absent from the response rather than 404ing the batch: one deleted account
    among a driver's passengers must not cost the other eleven their names.
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
            logger.warning("user id %r is not a uuid", candidate)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{candidate}' is not a valid user id.",
            )

    if not wanted:
        return []

    if len(wanted) > MAX_USER_IDS:
        logger.warning(
            "user batch of %d exceeds the cap of %d", len(wanted), MAX_USER_IDS
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ask for at most {MAX_USER_IDS} users at a time.",
        )

    profiles = await get_profiles(await get_supabase(), wanted)
    if len(profiles) != len(wanted):
        # Absent ids are dropped by design, so this line is the only trace that
        # something is referencing a profile that no longer exists.
        logger.warning(
            "asked for %d profile(s), found %d -- some user ids are stale",
            len(wanted),
            len(profiles),
        )
    # Returned in the order asked for, so the caller can zip the response
    # against its own list without re-sorting.
    return [User.from_row(profiles[user_id]) for user_id in wanted if user_id in profiles]


@router.get("/{user_id}", response_model=User)
async def get_user(user_id: str) -> User:
    """Fill in a passenger or driver the frontend has not cached.

    Public, and returns no email — use `/auth/me` for the signed-in user's own
    address.
    """
    profile = await get_profile(await get_supabase(), user_id)
    if profile is None:
        logger.warning("profile lookup missed for user id %s", user_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return User.from_row(profile)
