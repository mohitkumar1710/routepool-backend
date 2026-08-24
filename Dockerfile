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

EXPOSE 8000

# Shell form so $PORT expands — Render assigns its own port at runtime and
# the service must bind to it, not the hardcoded 8000.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
