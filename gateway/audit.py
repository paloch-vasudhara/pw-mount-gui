"""Append-only JSONL audit log: one file per UTC day, 90-day retention.

Entries carry only whitelisted, non-secret fields (ts, event, client, cmd, args, result,
reason, ...). Writing never raises: an audit failure must not break the control path.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

RETENTION_DAYS = 90
_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
# Keys that must never be written, whatever a caller passes.
_FORBIDDEN = ("token", "key", "secret", "password", "auth")


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): _clean(v)
            for k, v in value.items()
            if not any(f in str(k).lower() for f in _FORBIDDEN)
        }
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


class AuditLog:
    def __init__(self, directory: Path, retention_days: int = RETENTION_DAYS) -> None:
        self.dir = Path(directory)
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self.errors = 0

    def cleanup(self, today: date | None = None) -> list[str]:
        """Delete day files older than the retention window. Returns the removed names."""
        today = today or datetime.now(UTC).date()
        cutoff = today - timedelta(days=self.retention_days)
        removed: list[str] = []
        try:
            files = list(self.dir.iterdir()) if self.dir.is_dir() else []
        except OSError:
            return removed
        for f in files:
            m = _FILE_RE.match(f.name)
            if not m:
                continue
            try:
                if date.fromisoformat(m.group(1)) < cutoff:
                    f.unlink()
                    removed.append(f.name)
            except (ValueError, OSError):
                continue
        return removed

    def write(self, event: str, **fields: Any) -> dict[str, Any]:
        now = datetime.now(UTC)
        entry: dict[str, Any] = {"ts": now.isoformat(timespec="milliseconds"), "event": event}
        entry.update(_clean(fields))
        line = json.dumps(entry, separators=(",", ":"), ensure_ascii=True)
        try:
            with self._lock:
                self.dir.mkdir(parents=True, exist_ok=True)
                with open(self.dir / f"{now:%Y-%m-%d}.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError:
            self.errors += 1
        return entry
