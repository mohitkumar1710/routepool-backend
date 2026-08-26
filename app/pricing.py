"""What a seat costs, and the one place that decides it.

RoutePool is cost-sharing, not a taxi fare: a trip has a running cost, and the
people in the car split it. So the price a rider pays is not a number the driver
invents, it is a division:

    trip cost    = distance travelled  x  the driver's chosen rate per km
    price a seat = trip cost  /  everyone in the vehicle, driver included

Counting the driver is the part worth being explicit about, because it is what
makes this a *share* rather than a charge. A driver taking three passengers on a
100 km trip at Rs 10/km is splitting Rs 1,000 four ways, not three -- they pay
Rs 250 of their own journey like everyone else, and each passenger pays Rs 250
rather than Rs 333.

The rate is a fixed menu rather than free text. A driver picking a number out of
the air is how a cost-sharing app quietly turns into an unlicensed taxi service;
picking from three published bands keeps every listing comparable and keeps the
pricing defensible.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Tuple

# Rupees per kilometre. The driver picks one of these and nothing else -- see
# `RideCreate.price_per_km`, which rejects any other value outright.
#
# The three bands are meant to cover the real spread of running costs: a small
# hatchback and a diesel SUV do not cost the same per kilometre, and a driver
# who has to answer "why is this ride dearer?" can point at the band.
ALLOWED_RATES_PER_KM: Tuple[int, ...] = (8, 10, 12)

METERS_PER_KM = Decimal(1000)


def calculate_price_per_seat(
    distance_meters: int,
    rate_per_km: int,
    available_seats: int,
) -> float:
    """The price of one seat, in whole rupees.

    `available_seats` is what the driver is *offering*, so the number of people
    sharing the cost is that plus one for the driver. A ride with 0 seats left
    still has a well-defined seat price -- the division never sees a zero.

    Returns whole rupees. Splitting a trip four ways rarely lands on an exact
    amount, and a listing reading "Rs 249.67 per seat" is worse than one reading
    "Rs 250": nobody settles a carpool in paise, and the rounding error is a
    fraction of a rupee against a driver's own share.
    """
    if distance_meters < 0:
        raise ValueError("distance_meters cannot be negative")
    if available_seats < 0:
        raise ValueError("available_seats cannot be negative")
    if rate_per_km not in ALLOWED_RATES_PER_KM:
        raise ValueError(
            f"rate_per_km must be one of {ALLOWED_RATES_PER_KM}, got {rate_per_km}"
        )

    distance_km = Decimal(distance_meters) / METERS_PER_KM
    trip_cost = distance_km * Decimal(rate_per_km)
    # +1 for the driver. This is the whole point of the function.
    sharers = Decimal(available_seats + 1)

    # ROUND_HALF_UP rather than Python's default banker's rounding: money is
    # expected to round the way people were taught in school, and a price that
    # rounds .5 down half the time is the kind of thing that generates support
    # tickets rather than savings.
    return float((trip_cost / sharers).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
