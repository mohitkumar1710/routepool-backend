# Shared response objects

The three objects every endpoint is built from. Defined in `app/schemas/`.

## `User`

`app/schemas/user.py`, built by `User.from_row()`.

```json
{
  "id": "8f2b1c4e-...",
  "name": "Aarav Sharma",
  "email": "aarav@example.com",
  "role": "both",
  "avatar_url": null,
  "rating": 4.8,
  "review_count": 12,
  "is_verified": false
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string (uuid) | Same id as `auth.users` / `public.profiles`. |
| `name` | string | Never null — `profiles.name` is `not null`. |
| `email` | string \| null | **Only filled in for the signed-in user** (signup, login, `/auth/me`). `null` everywhere else, including the driver embedded in a ride. This is deliberate: it stops `GET /rides` from being an email harvester. |
| `role` | `"driver"` \| `"rider"` \| `"both"` | Falls back to `"rider"` if the row has none. |
| `avatar_url` | string \| null | No uploads exist yet, so in practice always `null`. |
| `rating` | number \| null | 0–5. No reviews table exists, so always `null` today. |
| `review_count` | integer | Always `0` today. |
| `is_verified` | boolean | Always `false` today. |

## `Ride`

`app/schemas/ride.py`, built by `Ride.from_row()`. The driver is embedded — one
request gets you the listing and its driver profile, because `RIDE_SELECT` in
`rides.py` asks PostgREST for `driver:profiles(...)` in the same round trip.

```json
{
  "id": "c1a0...",
  "driver": { "id": "8f2b...", "name": "Aarav Sharma", "email": null, "role": "both",
              "avatar_url": null, "rating": null, "review_count": 0, "is_verified": false },
  "from": "Delhi",
  "to": "Jaipur",
  "departure_date": "2026-08-25",
  "departure_time": "07:30",
  "available_seats": 3,
  "price_per_seat": 650.0,
  "vehicle": "Hyundai Creta",
  "notes": "Leaving from Dhaula Kuan, one stop for chai."
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string (uuid) | |
| `driver` | `User` | Always present; a ride whose driver row is missing raises 502 rather than returning a half object. `email` is always `null` here. |
| `from` / `to` | string | Aliased from the `from_location` / `to_location` columns. |
| `departure_date` | string `YYYY-MM-DD` | |
| `departure_time` | string `HH:MM` | A **string**, not a time. Postgres renders `07:30` as `07:30:00`; the model truncates to 5 chars so the frontend can print it as-is. |
| `available_seats` | integer 0–8 | Decremented by the DB when a driver confirms a booking. |
| `price_per_seat` | number | Rupees. `numeric(10,2)` in Postgres, a float on the wire. |
| `vehicle` | string | Free text model name, e.g. `"Royal Enfield Classic 350"`. Required. |
| `notes` | string \| null | |

**There is no `vehicle_type` field.** Neither the table nor the schema has one —
see the gaps section of `../../../FLOW.md`.

## `Booking`

`app/schemas/booking.py`, built by `Booking.from_row()`. Flat — no embedded ride
and no embedded passenger. The frontend joins these client-side against the
rides it already holds.

```json
{
  "id": "4d7e...",
  "ride_id": "c1a0...",
  "passenger_id": "b93f...",
  "seats": 2,
  "status": "pending",
  "created_at": "2026-08-19T09:14:02.117382+00:00"
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string (uuid) | |
| `ride_id` | string (uuid) | |
| `passenger_id` | string (uuid) | Taken from the bearer token on create, never from the body. |
| `seats` | integer ≥ 1 | |
| `status` | `"pending"` \| `"confirmed"` \| `"cancelled"` | New bookings start `pending`. Only the ride's driver can move it. |
| `created_at` | string (ISO 8601, tz-aware) | |

## Error bodies

| Shape | When |
| --- | --- |
| `{"detail": "<sentence>"}` | Every `HTTPException` the routers raise. The text is written to be shown to a user as-is. |
| `{"detail": [{"loc": [...], "msg": "...", ...}]}` | 422 from Pydantic request validation. |
