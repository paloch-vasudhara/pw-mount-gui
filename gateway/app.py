from fastapi import FastAPI

VERSION = "0.1.0"

app = FastAPI(title="pw-mount-control gateway", version=VERSION)


@app.get("/api/mount/health")
def health() -> dict:
    return {"ok": True, "service": "gateway", "version": VERSION}
