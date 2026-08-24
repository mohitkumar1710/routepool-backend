from fastapi import APIRouter, HTTPException, status

from app.config import get_supabase
from app.repository import get_profile
from app.schemas.user import User

router = APIRouter(prefix="/users", tags=["users"])


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
