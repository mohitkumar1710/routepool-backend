# `/api/users`

`app/routers/users.py`. One endpoint.

---

## `GET /api/users/{user_id}` → `200`

**Public — no token.** Served by the anon-key client; profiles are
world-readable by RLS.

Exists so the frontend can fill in a passenger or driver it has not already
cached — most notably the passengers on a driver's dashboard, who appear in
bookings only as a `passenger_id`.

**Response — `User` with `email: null`**

```json
{ "id": "b93f...", "name": "Priya Nair", "email": null, "role": "rider",
  "avatar_url": null, "rating": null, "review_count": 0, "is_verified": false }
```

The email is stripped for everyone. Use `GET /api/auth/me` for the signed-in
user's own address.

| Status | `detail` |
| --- | --- |
| 404 | `"User not found"` — no profile with that id |
| 500 | An id that is not a valid uuid. PostgREST rejects the comparison, the `APIError` is uncaught, and FastAPI turns it into a 500. |

---

## Not implemented

`GET /api/users/me` and `PATCH /api/users/me` **do not exist**. `GET /users/me`
matches the route above with `user_id = "me"`, which is not a uuid, so it 500s
rather than 404s. `PATCH /users/me` matches the path but not the method, so
FastAPI returns 405. The frontend's `becomeDriver()` calls
`PATCH /users/me` — see the gaps section of `../../../FLOW.md`.
