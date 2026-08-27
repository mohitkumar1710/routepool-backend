# routepool-backend

FastAPI backend for RoutePool, a ride-pooling app. Exposes rides, bookings, users
and auth under `/api`, with Supabase wired up for auth and data access.

> **Status:** every route is backed by Supabase — no mock data anywhere. Auth is
> real Supabase Auth (email + password), and the JWT it issues is what enforces
> the RLS policies on every subsequent query.
>
> **Before the API will work, run [`supabase/schema.sql`](supabase/schema.sql)
> once** in the Supabase SQL editor. Without it every call fails with
> "Could not find the table".

## Requirements

- Python 3.13 (the Docker image pins `python:3.13-slim`)
- Docker + Docker Compose, if you prefer running it in a container

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows (PowerShell / cmd)
# source .venv/bin/activate     # macOS / Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create your env file (see below)
cp .env.example .env            # Windows: copy .env.example .env
```

## Environment variables

All three `SUPABASE_*` values are required — the app boots without them but
every `/api` route fails.

`SUPABASE_URL` is the **project URL only** (`https://<ref>.supabase.co`). The
client appends `/rest/v1` and `/auth/v1` itself; leaving a path on the end
produces `PGRST125 Invalid path specified in request URL` on every call.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SUPABASE_URL` | – | Supabase project URL (Settings → API) |
| `SUPABASE_ANON_KEY` | – | Public anon key; RLS-enforced client |
| `SUPABASE_SERVICE_ROLE_KEY` | – | Service-role key; **bypasses RLS**, server-side only |
| `SUPABASE_JWT_SECRET` | – | *Legacy projects only.* HS256 secret; leave empty if your project issues ES256 tokens |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated list of allowed frontend origins |
| `HOST` | `127.0.0.1` | Bind address (`0.0.0.0` in Docker) |
| `PORT` | `8000` | Port to listen on |
| `RELOAD` | `1` | `1` enables auto-reload; the Docker image sets `0` |
| `WEB_CONCURRENCY` | `1` | uvicorn worker processes (Docker only); see the Dockerfile before raising it |

### How tokens are verified

The API checks each bearer token itself instead of asking Supabase Auth over
the network, which takes a full round trip off every authenticated request.
There are two signing schemes, and the project decides which applies:

- **Asymmetric (current, and almost certainly yours).** Tokens are ES256 with a
  `kid`, verified against the public keys published at
  `<SUPABASE_URL>/auth/v1/.well-known/jwks.json`. Nothing to configure — the
  URL comes from `SUPABASE_URL` and the keys are cached at startup.
- **Symmetric (legacy).** Tokens are HS256 signed with the shared JWT secret.
  Only then does `SUPABASE_JWT_SECRET` need setting, from **Settings → API →
  JWT Settings → JWT Secret**.

The startup log states the mode outright:
`token verification: local (JWKS, asymmetric keys)`.

**Do not infer the scheme from the anon key.** The anon key is an HS256 JWT
signed with the legacy secret even on projects whose *access tokens* are ES256,
so checking it tells you nothing, confidently. Decode a real access token.

If the keys cannot be read at all, the API falls back to a remote
`auth.get_user()` per request and logs a warning — slower, but working.

## Running locally

```bash
python main.py
```

That reads `HOST` / `PORT` / `RELOAD` and starts uvicorn with reload on by
default. To drive uvicorn yourself instead:

```bash
uvicorn main:app --reload --port 8000
```

Then:

- API root: http://127.0.0.1:8000
- Swagger UI: http://127.0.0.1:8000/docs
- ReDoc: http://127.0.0.1:8000/redoc
- Health check: http://127.0.0.1:8000/health

## Running with Docker

```bash
docker compose up --build
```

Compose mounts `./app` and `./main.py` into the container and runs uvicorn with
`--reload`, so edits are picked up without a rebuild. The API is served on
http://localhost:8000.

To build and run the image on its own (no reload, production-ish):

```bash
docker build -t routepool-api .
docker run -p 8000:8000 --env-file .env routepool-api
```

## API endpoints

All routes are prefixed with `/api`.

🔒 = requires `Authorization: Bearer <access_token>`.

### Auth
| Method | Path | Body | Description |
| --- | --- | --- | --- |
| `POST` | `/api/auth/signup` | `{name, email, password, role}` | Create an account and sign in → `{access_token, user}` |
| `POST` | `/api/auth/login` | `{email, password}` | Sign in → `{access_token, user}`; `400` = bad email *or* password |
| `GET` | `/api/auth/me` 🔒 | — | Restore the session on reload → `User`; `401` if the token is dead |
| `POST` | `/api/auth/logout` 🔒 | — | Revoke the token server-side → `204` |

### Rides
| Method | Path | Body | Description |
| --- | --- | --- | --- |
| `GET` | `/api/rides` | — | Every ride with a free seat; the frontend filters client-side. Optional `?limit=&offset=` — omitting them returns the full set as before |
| `GET` | `/api/rides/{ride_id}` | — | One ride, with fresh seat count |
| `POST` | `/api/rides` 🔒 | `{from, to, departure_date, departure_time, available_seats, price_per_seat, vehicle, notes}` | Publish a ride; driver comes from the token |

### Bookings
| Method | Path | Body | Description |
| --- | --- | --- | --- |
| `GET` | `/api/bookings` 🔒 | — | The caller's own requests **plus** every request on rides they drive. Optional `?limit=&offset=` |
| `POST` | `/api/bookings` 🔒 | `{ride_id, seats}` | Request seats, created `pending` |
| `PATCH` | `/api/bookings/{booking_id}/status` 🔒 | `{status: "confirmed"｜"cancelled"}` | Driver accepts/declines; adjusts the ride's seat count atomically |

### Users
| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/users?ids=a,b,c` | Several profiles at once, by id (max 100). Unknown ids are absent rather than an error |
| `GET` | `/api/users/{user_id}` | A profile, without its email address |

Ride and profile reads are public; everything else needs a token. The `email`
field only comes back for the signed-in user (`/auth/me`, login, signup) — a
driver embedded in a ride listing has `email: null`, so the public ride feed
cannot be scraped for addresses.

### Health
| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Liveness probe → `{"status": "ok"}` |

## Quick smoke test

```bash
curl http://127.0.0.1:8000/health

# Create an account — copy the access_token out of the response
curl -X POST http://127.0.0.1:8000/api/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"name":"Aarav","email":"aarav@example.com","password":"hunter2hunter2","role":"driver"}'

# Publish a ride as that user
curl -X POST http://127.0.0.1:8000/api/rides \
  -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"from":"Delhi","to":"Chandigarh","departure_date":"2026-08-20",
       "departure_time":"07:30","available_seats":3,"price_per_seat":650,
       "vehicle":"Hyundai Creta","notes":null}'

curl http://127.0.0.1:8000/api/rides
```

## Project structure

```
.
├── main.py               # App factory, CORS, router wiring, /health, uvicorn entrypoint
├── app/
│   ├── config.py         # Settings + the three Supabase client accessors
│   ├── dependencies.py   # Bearer -> verified user; get_db(); require_role()
│   ├── repository.py     # Shared profile queries
│   ├── routers/          # auth.py, rides.py, bookings.py, users.py
│   └── schemas/          # Pydantic models: user.py, ride.py, booking.py
├── supabase/
│   └── schema.sql        # Tables, RLS policies, triggers, set_booking_status()
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

### The three Supabase clients

[`app/config.py`](app/config.py) exposes one accessor per trust level — pick
deliberately:

- `get_supabase()` — anon key, no user attached. Auth calls and public reads.
- `get_user_supabase(access_token)` — anon key acting as the caller, so RLS
  decides what they can see. **This is the one routers should normally use.**
- `get_admin_supabase()` — service-role key, bypasses RLS entirely. Trusted
  server-side paths only; never build it from client-supplied input.

## Database

[`supabase/schema.sql`](supabase/schema.sql) is the whole thing: three tables,
their RLS policies, a trigger that mirrors each new `auth.users` row into
`public.profiles`, and `set_booking_status()`.

Two pieces are worth knowing about:

- **RLS does the authorization, not Python.** Routers query through
  `Depends(get_db)`, a client acting as the caller, so the policies decide what
  comes back. `GET /api/bookings` is a bare `select *` precisely because the
  policy already scopes it to the caller's own requests plus the ones on rides
  they drive.
- **`set_booking_status()` is a Postgres function, not Python.** Confirming a
  booking has to flip its status *and* decrement the ride's seats together. Done
  in the handler, two drivers confirming at once would both read
  `available_seats = 3` and both succeed. In the function, the ride row is
  locked, so the second call sees the first one's decrement. Cancelling a
  confirmed booking hands the seats back the same way.

## Next steps

- Reviews table, so `rating` / `review_count` stop being zero for everyone
- Something behind `is_verified` — nothing sets it today
- Tests
