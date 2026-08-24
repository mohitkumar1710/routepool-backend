# `/api/rides`

`app/routers/rides.py`. Every response here is a `Ride` (or an array of them) —
see [types.md](types.md). The driver profile is always embedded, and its `email`
is always `null`.

---

## `GET /api/rides` → `200`

**Public — no token.** The search page calls it before anyone signs in, through
the anon-key client.

**Response — `Ride[]`**, a bare array:

```json
[
  {
    "id": "c1a0...",
    "driver": { "id": "8f2b...", "name": "Aarav Sharma", "email": null, "role": "both",
                "avatar_url": null, "rating": null, "review_count": 0, "is_verified": false },
    "from": "Delhi", "to": "Jaipur",
    "departure_date": "2026-08-25", "departure_time": "07:30",
    "available_seats": 3, "price_per_seat": 650.0,
    "vehicle": "Hyundai Creta", "notes": null
  }
]
```

Only rides with `available_seats > 0` are returned, ordered by
`departure_date`, then `departure_time`. **No pagination, no query parameters** —
the whole open set comes back and the frontend filters it in memory. `[]` when
nothing is open.

| Status | `detail` |
| --- | --- |
| 502 | `"Ride is missing its driver profile."` — an orphaned row |

---

## `GET /api/rides/{ride_id}` → `200`

**Public — no token.**

**Response** — one `Ride` object, same shape as an element above. Ignores the
`available_seats > 0` filter, so a fully booked ride is still readable by link.

| Status | `detail` |
| --- | --- |
| 404 | `"Ride not found"` — no row with that id |
| 502 | `"Ride is missing its driver profile."` |
| 500 | An id that is not a valid uuid. PostgREST rejects it, the `APIError` is uncaught, and FastAPI turns it into a 500 — there is no exception handler in `main.py`. |

---

## `POST /api/rides` → `201`

Requires a bearer token. Runs through the caller's RLS client, so the
`drivers publish their own rides` policy is the real gate.

**Request**

```json
{
  "from": "Delhi",
  "to": "Jaipur",
  "departure_date": "2026-08-25",
  "departure_time": "07:30",
  "available_seats": 3,
  "price_per_seat": 650,
  "vehicle": "Hyundai Creta",
  "notes": "Leaving from Dhaula Kuan."
}
```

| Field | Rule |
| --- | --- |
| `from` | non-empty string (`from_` also accepted) |
| `to` | non-empty string |
| `departure_date` | `YYYY-MM-DD` |
| `departure_time` | `HH:MM` or `HH:MM:SS` |
| `available_seats` | integer 1–8 |
| `price_per_seat` | number ≥ 0 |
| `vehicle` | non-empty string |
| `notes` | string or omitted |

**There is no `driver_id` field, and sending one does nothing.** The driver is
the bearer token's owner. Any field not listed above — `vehicle_type`, for
instance — is silently dropped by Pydantic.

**Response** — the created `Ride`, re-read through `get_ride()` so the embedded
driver is present. Note this makes the endpoint two DB round trips.

| Status | `detail` |
| --- | --- |
| 401 | No / bad token |
| 422 | Validation — seats out of 1–8, empty `from`, bad date |
| 502 | `"Ride was not created."` — insert returned no row (usually an RLS refusal) |

There is **no** `PATCH`, `DELETE`, or driver-scoped listing on this router. The
RLS policies for updating and deleting a ride exist in `schema.sql`, but no
endpoint uses them yet.
