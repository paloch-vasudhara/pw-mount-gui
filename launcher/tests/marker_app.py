"""Tiny FastAPI app for the graceful-stop test: its lifespan shutdown writes a marker file.

Run as a script: marker_app.py <port> <marker-file>  (throwaway ports 18761-18769 only).
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

MARKER = Path(sys.argv[2]) if len(sys.argv) > 2 else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        if MARKER is not None:
            MARKER.write_text("shutdown ran", encoding="utf-8")


app = FastAPI(lifespan=lifespan)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
