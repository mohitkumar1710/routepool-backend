from typing import Any, Literal, Optional

from pydantic import BaseModel, EmailStr, Field

UserRole = Literal["driver", "rider", "both"]


class User(BaseModel):
    """A profile as the frontend sees it.

    `email` is deliberately optional: it is only filled in for the signed-in
    user (login / signup / `/auth/me`). Drivers embedded in a public ride
    listing come back without one, so a scrape of `GET /rides` cannot harvest
    addresses.
    """

    id: str
    name: str
    email: Optional[EmailStr] = None
    role: UserRole
    avatar_url: Optional[str] = None
    rating: Optional[float] = Field(default=None, ge=0, le=5)
    review_count: int = 0
    is_verified: bool = False

    @classmethod
    def from_row(cls, row: dict[str, Any], *, include_email: bool = False) -> "User":
        return cls(
            id=str(row["id"]),
            name=row["name"],
            email=row.get("email") if include_email else None,
            role=row.get("role") or "rider",
            avatar_url=row.get("avatar_url"),
            rating=float(row["rating"]) if row.get("rating") is not None else None,
            review_count=row.get("review_count") or 0,
            is_verified=bool(row.get("is_verified")),
        )


class SignupRequest(BaseModel):
    name: str = Field(min_length=1)
    email: EmailStr
    password: str = Field(min_length=8)
    role: UserRole = "rider"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class AuthResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    user: User


class RoleUpdate(BaseModel):
    """Body of `PATCH /users/me`.

    Only `role` is accepted. Name, avatar and the verification/rating fields are
    either the user's to change through a route that does not exist yet, or the
    server's alone -- listing them here would imply an endpoint that honours
    them.
    """

    role: UserRole
