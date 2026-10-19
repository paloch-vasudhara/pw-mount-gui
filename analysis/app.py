from fastapi import FastAPI

VERSION = "0.1.0"

app = FastAPI(title="pw-mount-control analysis", version=VERSION)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "analysis", "version": VERSION}
