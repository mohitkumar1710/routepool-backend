FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

# Copy requirements first so the pip layer is cached across code edits.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY main.py .

# 0.0.0.0 so the port is reachable from outside the container.
ENV HOST=0.0.0.0 \
    PORT=8000 \
    RELOAD=0

# How many uvicorn worker processes to run.
#
# One, by default, and that is a deliberate answer rather than a placeholder.
# The app is now async end to end: no handler blocks, so a single process
# already serves many requests at once by parking coroutines on the event loop
# while they wait on Supabase or OSRM. Concurrency comes from the loop, not
# from processes.
#
# That is a real change from the sync + threadpool version. Back then every
# handler occupied one of AnyIO's ~40 worker threads for the whole life of its
# database call, so ~40 in-flight requests was a hard ceiling and extra workers
# were the only way past it. There is no such ceiling now: the limit is CPU,
# and this app spends almost none -- it awaits the network and serialises small
# JSON payloads.
#
# What more workers still buy:
#   - Parallel CPU. Only worth it on a plan with more than one core; two workers
#     on a single-core instance take turns and add memory for nothing.
#   - Isolation. A worker that dies takes only its own connections with it.
#
# What they cost, specifically here:
#   - Each worker builds its own Supabase clients and its own OSRM pool, so
#     pools and warm connections multiply by the worker count.
#   - Memory multiplies too. Render's free tier is 512 MB.
#
# So: raise WEB_CONCURRENCY when the instance has cores to use, not by reflex.
# Render sets this variable itself on some plans, which is exactly why the name
# was chosen -- the platform's value wins without touching this file.
ENV WEB_CONCURRENCY=1

EXPOSE 8000

# Shell form so the variables expand -- Render assigns its own port at runtime
# and the service must bind to it, not the hardcoded 8000.
#
# `exec` is what makes the shell form safe, and it is not decoration. Without
# it PID 1 is /bin/sh, which does not forward signals to its child, so uvicorn
# never sees the SIGTERM that a `docker stop` or a Render redeploy sends -- it
# is simply SIGKILLed once the grace period expires. Measured on this image
# before the fix: `docker stop -t 20` took the full 20s and the lifespan's
# shutdown block never ran, so the Supabase and OSRM pools were never closed
# and --timeout-graceful-shutdown had nothing to act on. `exec` replaces the
# shell with uvicorn, so uvicorn is PID 1 and gets the signal itself.
#
# --loop uvloop and --http httptools replace asyncio's own event loop and h11
# with their C equivalents. Both are Linux-only and both are installed in this
# image (see requirements.txt); naming them explicitly means a missing wheel
# fails loudly at startup instead of silently falling back to the slower pure
# Python path, which is the failure mode worth avoiding in a deploy.
#
# --timeout-graceful-shutdown gives in-flight requests a moment to finish and,
# just as importantly, lets main.py's lifespan run its shutdown block, which is
# what actually closes the Supabase and OSRM connection pools.
CMD exec uvicorn main:app \
    --host 0.0.0.0 \
    --port ${PORT:-8000} \
    --workers ${WEB_CONCURRENCY:-1} \
    --loop uvloop \
    --http httptools \
    --timeout-graceful-shutdown 20
