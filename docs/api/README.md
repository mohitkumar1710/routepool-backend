# API response contracts

What every endpoint actually returns, as of the code in `app/`. One file per
router. These are descriptions of the current behaviour, not a wish list — if a
file here disagrees with the code, the code is right and the file is a bug.

| File | Router | Endpoints |
| --- | --- | --- |
| [auth.md](auth.md) | `app/routers/auth.py` | signup, login, me, logout |
| [rides.md](rides.md) | `app/routers/rides.py` | list, detail, create |
| [bookings.md](bookings.md) | `app/routers/bookings.py` | list, create, status |
| [users.md](users.md) | `app/routers/users.py` | public profile |
| [types.md](types.md) | `app/schemas/` | `User`, `Ride`, `Booking`, error bodies |

## Conventions that hold everywhere

**Base path.** Every router is mounted under `/api` in `main.py`. The one
exception is `GET /health`, which sits at the root and returns
`{"status": "ok"}`.

**Casing.** Request and response bodies are `snake_case`, straight from the
Pydantic models. The single alias is `from` on a ride (the Python field is
`from_`, because `from` is a keyword); FastAPI serialises responses with
`by_alias=True`, so the wire always says `from`.

**Auth.** Protected endpoints read `Authorization: Bearer <supabase-jwt>`. The
token is the raw Supabase access token handed out by login/signup. Missing
header and bad token both produce 401 with a `WWW-Authenticate: Bearer` header.

**Nothing is enveloped.** A list endpoint returns a bare JSON array, not
`{ items: [...] }`. A single object returns that object at the top level.

**Errors** are always FastAPI's shape:

```json
{ "detail": "Ride not found" }
```

…except 422 validation failures, which return a list:

```json
{ "detail": [{ "type": "missing", "loc": ["body", "seats"], "msg": "Field required", "input": {} }] }
```
