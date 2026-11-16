"""Where things are: the repo layout today, the T6.2c package layout later (same code)."""

from __future__ import annotations

import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
DATA_NAME = "pw-mount-control"


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / DATA_NAME


def log_dir() -> Path:
    env = os.environ.get("PW_LOG_DIR")
    return Path(env) if env else data_dir() / "logs"


def web_dir(root: Path = APP_ROOT) -> Path | None:
    """PW_WEB_DIR, else <root>/web/.output/public (repo), else <root>/web (package)."""
    env = os.environ.get("PW_WEB_DIR")
    if env:
        p = Path(env)
        p = p if p.is_absolute() else root / p
        return p if p.is_dir() else None
    for p in (root / "web" / ".output" / "public", root / "web"):
        if p.is_dir() and (p / "index.html").is_file():
            return p
    return None


def icon_path(root: Path = APP_ROOT) -> Path | None:
    for p in (root / "tools" / "package" / "icon" / "app.ico", root / "app.ico"):
        if p.is_file():
            return p
    return None


def settings_path() -> Path:
    env = os.environ.get("PW_SETTINGS_PATH")
    return Path(env) if env else data_dir() / "settings.json"


def mediamtx_folder(root: Path = APP_ROOT) -> Path | None:
    """A folder holding mediamtx.exe + mediamtx.yml: APP_ROOT/relay, else launcher.json's."""
    import json

    cands = [root / "relay"]
    try:
        cfg = json.loads((data_dir() / "launcher.json").read_text(encoding="utf-8"))
        if isinstance(cfg, dict) and isinstance(cfg.get("mediamtx"), str) and cfg["mediamtx"]:
            cands.append(Path(cfg["mediamtx"]))
    except Exception:
        pass
    for c in cands:
        if (c / "mediamtx.exe").is_file() and (c / "mediamtx.yml").is_file():
            return c
    return None
