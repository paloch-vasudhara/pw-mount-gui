"""Station settings store (T1.2): JSON file on the gateway + API key in the OS keyring.

The API key is a secret: it lives only in the SecretStore (Windows Credential Manager),
never in settings.json, never in a response, never logged. Everything else is plain JSON,
written atomically (temp file + os.replace).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("gateway.settings")

SCHEMA_VERSION = 1
KEYRING_SERVICE = "pw-mount-control"
KEYRING_USER = "astrometry.net"

DEFAULT_LAT = 13.7862
DEFAULT_LON = 101.4639
DEFAULT_ALT_M = 40.8
DEFAULT_LIM_EL = 15.0
DEFAULT_HOME_ALT = 85.0  # T3.2: home pose used for the Sun path check (hand-editable)
DEFAULT_HOME_AZ = 180.0
SUN_KEEPOUT_DEG = 15.0
TOL_LAT_LON_DEG = 0.01
TOL_ALT_M = 100.0


def default_settings_path() -> Path:
    env = os.environ.get("PW_SETTINGS_PATH")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "pw-mount-control" / "settings.json"


# ---------------------------------------------------------------- secrets


class SecretStore(Protocol):
    def get(self) -> str | None: ...
    def set(self, value: str) -> None: ...
    def clear(self) -> None: ...


class MemorySecretStore:
    def __init__(self, value: str | None = None) -> None:
        self._v = value

    def get(self) -> str | None:
        return self._v

    def set(self, value: str) -> None:
        self._v = value

    def clear(self) -> None:
        self._v = None


class KeyringSecretStore:
    """Windows Credential Manager via `keyring` (service pw-mount-control)."""

    def get(self) -> str | None:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER)

    def set(self, value: str) -> None:
        import keyring

        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, value)

    def clear(self) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        with contextlib.suppress(PasswordDeleteError):
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)


_MEMORY = MemorySecretStore()


def default_secret_store() -> SecretStore:
    """Credential Manager, unless PW_SECRET_STORE=memory (throwaway dev / browser-gate runs)."""
    if os.environ.get("PW_SECRET_STORE") == "memory":
        return _MEMORY
    return KeyringSecretStore()


# ---------------------------------------------------------------- schema


class Location(BaseModel):
    lat_deg: float = Field(DEFAULT_LAT, ge=-90, le=90)
    lon_deg: float = Field(DEFAULT_LON, ge=-180, le=180)
    alt_m: float = Field(DEFAULT_ALT_M, ge=-500, le=9000)
    # None = not chosen yet (first-run seed); the gateway treats it as UTC for its own needs.
    timezone: str | None = None

    @property
    def effective_timezone(self) -> str:
        return self.timezone or "UTC"

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except Exception as exc:
            raise ValueError(f"unknown IANA timezone: {v!r}") from exc
        return v


class Hardware(BaseModel):
    mount: str = "PW L-350"
    ota: str = "Celestron RASA11"
    lim_el_deg: float = Field(DEFAULT_LIM_EL, ge=0, le=90)
    # Home pose (T3.2): only used to predict the HOME path for the Sun check. Not on the Settings
    # screen; edit settings.json by hand. Absent in older files -> defaults.
    home_alt_deg: float = Field(DEFAULT_HOME_ALT, ge=0, le=90)
    home_az_deg: float = Field(DEFAULT_HOME_AZ, ge=0, lt=360)


class Providers(BaseModel):
    astrometry: str = "astrometry.net"
    satellite_db: str = "DELV Aerospace"


class Display(BaseModel):
    lat_format: Literal["dd", "dms"] = "dd"
    lon_format: Literal["dd", "dms"] = "dd"
    alt_unit: Literal["ft", "m", "km"] = "m"


class StationSettings(BaseModel):
    version: int = SCHEMA_VERSION
    location: Location = Field(default_factory=Location)
    hardware: Hardware = Field(default_factory=Hardware)
    providers: Providers = Field(default_factory=Providers)
    display: Display = Field(default_factory=Display)


class SettingsUpdate(StationSettings):
    """PUT body: settings plus an optional write-only key.

    api_key: a string sets it, an empty string clears it, absent/null keeps it.
    """

    api_key: str | None = None

    @field_validator("location")
    @classmethod
    def _tz_required(cls, v: Location) -> Location:
        if v.timezone is None:  # null is only ever the stored first-run value
            raise ValueError("timezone is required")
        return v


# ---------------------------------------------------------------- site check


def wrap180(x: float) -> float:
    if -180.0 <= x <= 180.0:
        return x
    return (x + 180.0) % 360.0 - 180.0


def compute_site_check(loc: Location, pwi4_site: tuple[float, float, float] | None) -> dict:
    """Compare the Settings location with PWI4 site (lat, lon, height_m) or None if unreachable."""
    tol = {"lat_lon_deg": TOL_LAT_LON_DEG, "alt_m": TOL_ALT_M}
    if pwi4_site is None:
        return {
            "pwi4_reachable": False,
            "ok": None,
            "lat_diff_deg": None,
            "lon_diff_deg": None,
            "alt_diff_m": None,
            "tolerance": tol,
        }
    dlat = loc.lat_deg - pwi4_site[0]
    dlon = wrap180(loc.lon_deg - wrap180(pwi4_site[1]))
    dalt = loc.alt_m - pwi4_site[2]
    ok = (
        round(abs(dlat), 9) <= TOL_LAT_LON_DEG
        and round(abs(dlon), 9) <= TOL_LAT_LON_DEG
        and round(abs(dalt), 6) <= TOL_ALT_M
    )
    return {
        "pwi4_reachable": True,
        "ok": ok,
        "lat_diff_deg": dlat,
        "lon_diff_deg": dlon,
        "alt_diff_m": dalt,
        "tolerance": tol,
    }


# ---------------------------------------------------------------- store


class SettingsStore:
    def __init__(self, path: Path | None = None, secrets: SecretStore | None = None) -> None:
        self.path = Path(path) if path else default_settings_path()
        self.secrets: SecretStore = secrets if secrets is not None else default_secret_store()
        self._lock = threading.Lock()
        self.load_error: str | None = None
        self.current = StationSettings()
        self.first_run = not self.path.exists()
        if not self.first_run:
            self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            raw.pop("api_key", None)  # a key must never be read back from the file
            self.current = StationSettings.model_validate(raw)
        except Exception as exc:
            self.load_error = type(exc).__name__
            log.warning("settings file unreadable (%s); using defaults", self.load_error)
            with contextlib.suppress(OSError):
                self.path.replace(self.path.with_suffix(".bad"))
            self.current = StationSettings()
            self.first_run = True

    def _write(self, s: StationSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(s.model_dump(), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def seed_from_pwi4(self, site: tuple[float, float, float] | None) -> bool:
        """First run only: location from the PWI4 site if given (else defaults); persist."""
        with self._lock:
            if not self.first_run:
                return False
            s = self.current.model_copy(deep=True)
            if site is not None:
                lat, lon, h = site
                with contextlib.suppress(ValueError):  # implausible PWI4 site -> keep defaults
                    s.location = Location(
                        lat_deg=lat,
                        lon_deg=wrap180(lon),
                        alt_m=h,
                        timezone=s.location.timezone,
                    )
            self._write(s)
            self.current = s
            self.first_run = False
            return True

    def save(self, new: StationSettings, api_key: str | None = None) -> StationSettings:
        """Persist settings; api_key: str = set, empty = clear, None = keep."""
        with self._lock:
            clean = StationSettings.model_validate(new.model_dump())
            # The Settings screen does not carry the home pose: a PUT that omits it must keep the
            # (possibly hand-edited) stored values instead of resetting them to the defaults.
            fs = new.hardware.model_fields_set
            for name in ("home_alt_deg", "home_az_deg"):
                if name not in fs:
                    setattr(clean.hardware, name, getattr(self.current.hardware, name))
            if api_key is not None:
                if api_key == "":
                    self.secrets.clear()
                else:
                    self.secrets.set(api_key)
            self._write(clean)
            self.current = clean
            self.first_run = False
            return clean

    def api_key_set(self) -> bool:
        try:
            return bool(self.secrets.get())
        except Exception:
            return False

    def public(self) -> dict:
        d = self.current.model_dump()
        d["api_key_set"] = self.api_key_set()
        return d
