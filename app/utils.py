"""Shared helpers. Right now that means the application logger.

Render has no log file to collect -- it captures whatever the container writes
to stdout/stderr and shows it under the service's Logs tab. So everything here
goes to stdout, and `PYTHONUNBUFFERED=1` in the Dockerfile keeps lines from
sitting in a buffer while Python waits for more output (without it, logs show
up in the dashboard minutes late, or not at all if the process is killed).

Usage:

    from app.utils import logger

    logger.info("booking %s confirmed", booking_id)

Pass values as arguments, not f-strings -- the formatting is then skipped
entirely when the level is filtered out.
"""

import logging
import os
import sys

# Set LOG_LEVEL=DEBUG in the Render dashboard to get more without a redeploy.
_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# No colour codes: Render's log viewer renders them as literal escape junk.
_FORMAT = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str = "routepool") -> logging.Logger:
    """Return a stdout logger, configuring it the first time it is asked for."""
    log = logging.getLogger(name)

    # Import-time configuration runs once per process, but a reload or a second
    # caller would otherwise stack a second handler and double every line.
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
        log.addHandler(handler)
        log.setLevel(_LEVEL)

        # uvicorn installs its own handler on the root logger. Propagating
        # would hand every record to that one as well -- same message, twice,
        # in two different formats.
        log.propagate = False

    return log


# The shared instance. Import this unless you specifically want a named
# sub-logger (`get_logger("routepool.bookings")`) to filter on later.
logger = get_logger()
