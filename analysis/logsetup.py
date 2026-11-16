r"""Logging for ``python -m analysis`` (T5.2g): console + a rotating file, INFO for the
``analysis`` loggers, WARNING for everything else (third-party).

File: ``%LOCALAPPDATA%/pw-mount-control/logs/analysis.log`` (``PW_LOG_DIR`` overrides the
folder), 2 MB x 5 files. The astrometry lines carry ids, statuses and sizes only - never the
API key, a session token, a URL or a body.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_NAME = "analysis.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 4                      # analysis.log + 4 rotated = 5 files
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_TAG = "_pw_analysis"            # marks our handlers so a second call replaces, not stacks


def log_dir() -> Path:
    env = os.environ.get("PW_LOG_DIR")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "pw-mount-control" / "logs"


def configure_logging(directory: str | os.PathLike | None = None) -> Path | None:
    """Install the handlers; returns the log file path (None when only the console works)."""
    fmt = logging.Formatter(FORMAT)
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, _TAG, False)]:
        root.removeHandler(h)
        h.close()
    root.setLevel(logging.WARNING)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    setattr(console, _TAG, True)
    root.addHandler(console)
    path: Path | None = None
    try:
        d = Path(directory) if directory else log_dir()
        d.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(d / LOG_NAME, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                 encoding="utf-8")
        fh.setFormatter(fmt)
        setattr(fh, _TAG, True)
        root.addHandler(fh)
        path = d / LOG_NAME
    except OSError:
        logging.getLogger("analysis").warning("could not open the analysis log file")
    logging.getLogger("analysis").setLevel(logging.INFO)
    return path
