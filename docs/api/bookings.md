# `/api/bookings`

`app/routers/bookings.py`. Every response is a `Booking` (or an array) — see
[types.md](types.md). Flat objects: no embedded ride, no embedded passenger.

Every endpoint here requires a bearer token.

---

## `GET /api/bookings` → `200`

**Response — `Booking[]`**, newest first (`created_at desc`):

```json
[
  { "id": "4d7e...", "ride_id": "c1a0...", "passenger_id": "b93f...",
    "seats": 2, "status": "pending", "created_at": "2026-08-19T09:14:02.117382+00:00" }
]
```

The set is **both** sides at once: the caller's own requests *and* every request
on a ride they drive. The query is a bare `select *` — no `where` clause in
Python. The RLS policy `riders and the ride's driver can read a booking` returns
exactly that set, which is why filtering here would be redundant and
unforgeable.

Consequence for consumers: you cannot tell from the response alone whether a row
is "mine as a passenger" or "someone else's request on my ride". Compare
`passenger_id` against the signed-in user to split them.

`[]` for a user with no bookings and no rides.

| Status | `detail` |
| --- | --- |
| 401 | No / bad token |

---

## `POST /api/bookings` → `201`

**Request**

```json
{ "ride_id": "c1a0...", "seats": 2 }
```

| Field | Rule |
| --- | --- |
| `ride_id` | uuid string, required |
| `seats` | integer 1–8 |
| `passenger_id` | optional, accepted for frontend compatibility but **never trusted** — if present and it is not the caller, the request is refused |

**Response** — the created `Booking`, always `"status": "pending"`.

Upserted on `(ride_id, passenger_id)`: re-requesting the same ride **updates the
existing row and returns the same `id`**, it does not create a second booking.

Seats are *not* held at this point. The `available_seats` check here is an early
courtesy only; the authoritative one runs when the driver confirms.

| Status | `detail` |
| --- | --- |
| 400 | `"You cannot book a seat on your own ride."` |
| 401 | No / bad token |
| 403 | `"You can only book seats for yourself."` — `passenger_id` was someone else |
| 404 | `"Ride not found"` |
| 409 | `"Only N seat(s) left on this ride."` |
| 422 | Validation |
| 502 | `"Booking was not created."` |

---

## `PATCH /api/bookings/{booking_id}/status` → `200`

The driver accepting or declining a request.

**Request**

```json
{ "status": "confirmed" }
```

Only `"confirmed"` and `"cancelled"` are accepted — `"pending"` is not a value
you can set.

**Response** — the updated `Booking`.

Delegated to the Postgres function `set_booking_status`, so the status change
and the ride's seat count move inside one locked transaction. Two confirmations
racing for the last seats cannot oversell the car; the loser gets a 409.

Confirming decrements `rides.available_seats` — **the ride the caller is holding
is now stale** and should be re-fetched.

| Status | `detail` | Postgres |
| --- | --- | --- |
| 401 | No / bad token | |
| 403 | Caller is not this ride's driver | `42501` |
| 404 | `"Booking not found"` / no such booking | `P0002` |
| 409 | Not enough seats left to confirm | `23514` |
| 422 | Bad status value | `22023` |
| 400 | Anything else from Postgres | |

There is **no** `GET /bookings/{id}`, no `DELETE`, and no per-ride listing. A
rider cancelling their own booking has an RLS policy in `schema.sql` but no
endpoint.
