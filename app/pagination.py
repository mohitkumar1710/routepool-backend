"""Shared `limit` / `offset` query parameters for the list endpoints.

Why the default sits at the cap rather than below it
----------------------------------------------------
`GET /rides` and `GET /bookings` both used to return every matching row, with
no ceiling at all. That is the actual defect: one unlucky query and the API
serialises an unbounded result set into one response.

The obvious fix -- a small default page, say 50 -- would swap that defect for a
worse one. `useRides.ts` fetches each list exactly once and treats what comes
back as the complete set: `SearchResults` filters that array, `MyTrips` reads
bookings out of it, `DriverDashboard` counts earnings from it. Cut the default
to 50 and none of them error. They just quietly show less: a rider stops seeing
ride 51, a driver's earnings total silently shrinks. Wrong answers presented
confidently are far worse than a slow response.

So the default is the cap. Today's callers send no parameters, get up to
`MAX_LIMIT` rows, and behave exactly as they did -- while the unbounded
response is gone, because a ceiling now exists either way. `limit` and `offset`
are there for a caller that wants to page, and the first one that will is
`SearchResults`, once it learns to. That is a real piece of frontend work --
incremental fetching, merged state, a loading affordance, and search filters
that currently assume they can see everything -- and it is deliberately not
part of this change. This change only makes the backend capable of it.

`MAX_LIMIT` is the value to lower once the frontend can page. Lowering it
before then is the breaking change this module exists to avoid.
"""

from fastapi import Query

# The most rows any single response will carry. Comfortably above the size of
# the board today, so nothing currently truncates; low enough that a single
# response stays a sane size.
MAX_LIMIT = 200

# Deliberately equal to MAX_LIMIT. See the module docstring: a smaller default
# would silently truncate every caller that exists right now.
DEFAULT_LIMIT = MAX_LIMIT


def limit_param(resource: str) -> Query:
    return Query(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description=(
            f"How many {resource} to return, newest page first. Defaults to "
            f"{DEFAULT_LIMIT}, which is also the maximum -- omit it to get the "
            f"same full-set behaviour this endpoint has always had."
        ),
    )


def offset_param(resource: str) -> Query:
    return Query(
        default=0,
        ge=0,
        description=(
            f"How many {resource} to skip before the page starts. Pair it with "
            f"`limit` to walk the list. The ordering is stable, so a page "
            f"boundary is meaningful."
        ),
    )
