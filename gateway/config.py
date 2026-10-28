"""Env-driven gateway settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .settings import default_settings_path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 8400
# Link-loss detection (G4/I3): server-driven WebSocket protocol ping/pong (answered by the
# browser's network stack, not JS timers, so background tabs still reply). A silent peer is
# dropped at most interval + timeout after its last pong.
WS_PING_INTERVAL_S = 0.2
WS_PING_TIMEOUT_S = 0.4
WS_MAX_MESSAGE_BYTES = 16 * 1024  # inbound frame cap (auth + commands are tiny)
DEFAULT_WEB_ORIGINS = ("http://127.0.0.1:3200", "http://localhost:3200")


def default_audit_dir() -> Path:
    env = os.environ.get("PW_AUDIT_DIR")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "pw-mount-control" / "audit"


def _csv(value: str) -> list[str]:
    return [v.strip().rstrip("/") for v in value.split(",") if v.strip()]


@dataclass
class Settings:
    port: int = DEFAULT_PORT
    pwi4_url: str = "http://127.0.0.1:8220"
    poll_hz: float = 10.0
    web_origins: list[str] = field(default_factory=lambda: list(DEFAULT_WEB_ORIGINS))
    ephemeris: Path = REPO_ROOT / "data" / "ephemeris" / "de421.bsp"
    settings_path: Path = field(default_factory=default_settings_path)
    audit_dir: Path = field(default_factory=lambda: default_audit_dir())
    poll_enabled: bool = True
    token: str | None = None  # None -> generated per process start
    # T3.2 CONNECT: PWI4 is started (never stopped) when /status is unreachable.
    pwi4_exe: str | None = None  # None -> auto-detect (Program Files (x86), then Program Files)
    pwi4_start_timeout_s: float = 60.0
    # T4.1: the analysis service the TLE of a tracked object is fetched from (by NORAD id).
    analysis_url: str = "http://127.0.0.1:8410"

    @classmethod
    def from_env(cls, port: int | None = None) -> Settings:
        env = os.environ
        eph = Path(env.get("PW_EPHEMERIS", "data/ephemeris/de421.bsp"))
        if not eph.is_absolute():
            eph = REPO_ROOT / eph
        origins = (
            _csv(env["PW_ALLOWED_ORIGINS"])
            if "PW_ALLOWED_ORIGINS" in env
            else list(DEFAULT_WEB_ORIGINS)
        )
        return cls(
            port=port if port is not None else int(env.get("PW_GATEWAY_PORT", DEFAULT_PORT)),
            pwi4_url=env.get("PW_PWI4_URL", "http://127.0.0.1:8220").rstrip("/"),
            poll_hz=float(env.get("PW_POLL_HZ", "10")),
            web_origins=origins,
            ephemeris=eph,
            settings_path=default_settings_path(),
            audit_dir=default_audit_dir(),
            pwi4_exe=env.get("PW_PWI4_EXE") or None,
            pwi4_start_timeout_s=float(env.get("PW_PWI4_START_TIMEOUT_S", "60")),
            analysis_url=env.get("PW_ANALYSIS_URL", "http://127.0.0.1:8410").rstrip("/"),
        )

    @property
    def allowed_origins(self) -> set[str]:
        own = {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}
        return set(self.web_origins) | own

    @property
    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
