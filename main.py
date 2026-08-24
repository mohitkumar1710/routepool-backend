import os
from typing import Dict

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import auth, bookings, rides, routes, users

app = FastAPI(title="routepool-api", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "https://route-pool.netlify.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(rides.router, prefix="/api")
app.include_router(routes.router, prefix="/api")
app.include_router(bookings.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(auth.router, prefix="/api")


@app.get("/health", tags=["health"])
def health() -> Dict[str, str]:
    return {"status": "ok"}


def main():
    # Defaults suit local dev; the Dockerfile sets HOST=0.0.0.0 and RELOAD=0,
    # since 127.0.0.1 inside a container is unreachable from the host.
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("RELOAD", "1") == "1",
    )


if __name__ == "__main__":
    main()
