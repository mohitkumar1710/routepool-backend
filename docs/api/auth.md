# `/api/auth`

`app/routers/auth.py`. Supabase Auth issues the JWT; this router relays it. The
token the frontend stores is a real Supabase access token, which is what makes
the RLS policies apply to every later request.

All four responses use `User` with `email` **filled in** — this is the only
place it is.

---

## `POST /api/auth/signup` → `201`

Public.

**Request**

```json
{ "name": "Aarav Sharma", "email": "aarav@example.com", "password": "atleast8chars", "role": "rider" }
```

`role` defaults to `"rider"`. `password` must be ≥ 8 chars, `name` ≥ 1.

**Response — `AuthResponse`**

```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "user": { "id": "8f2b...", "name": "Aarav Sharma", "email": "aarav@example.com",
            "role": "rider", "avatar_url": null, "rating": null,
            "review_count": 0, "is_verified": false }
}
```

The account is created with the service-role client so `app_metadata.role` can
be written (only that key is trusted by `require_role`), `email_confirm` is
forced true, then the user is immediately signed in to produce the token.

| Status | `detail` |
| --- | --- |
| 409 | `"An account with that email already exists."` |
| 400 | `"Could not create the account: <supabase message>"`, or the ambiguous login message if the follow-up sign-in fails |
| 422 | Validation — short password, malformed email, bad `role` |
| 502 | `"Account exists but has no profile row. Has supabase/schema.sql been run?"` — the `on_auth_user_created` trigger is missing |

---

## `POST /api/auth/login` → `200`

Public.

**Request**

```json
{ "email": "aarav@example.com", "password": "atleast8chars" }
```

**Response** — identical `AuthResponse` shape to signup.

| Status | `detail` |
| --- | --- |
| 400 | `"No account found with that email, or the password is incorrect."` — wrong password and unknown address return the *same* string on purpose, so the endpoint cannot be used to probe which emails have accounts |
| 502 | Missing profile row, as above |

---

## `GET /api/auth/me` → `200`

Requires a bearer token. Used to restore the session on reload.

**Response — `User`, with email**

```json
{ "id": "8f2b...", "name": "Aarav Sharma", "email": "aarav@example.com", "role": "both",
  "avatar_url": null, "rating": null, "review_count": 0, "is_verified": false }
```

Read through the caller's own RLS-scoped client.

| Status | `detail` |
| --- | --- |
| 401 | `"Not authenticated"` (no header) or `"Invalid or expired token"` |
| 502 | Missing profile row |

---

## `POST /api/auth/logout` → `204`

Requires a bearer token. **No response body at all.**

Revokes the session at Supabase. Always 204, even when the token was already
dead — the caller ends up signed out either way, and the frontend clears local
state regardless of what this returns.

| Status | `detail` |
| --- | --- |
| 401 | Header missing entirely |
