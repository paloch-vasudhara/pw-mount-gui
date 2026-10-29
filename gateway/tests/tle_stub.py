"""TLE stub (T4.2c): a tiny stand-in for the analysis service's `GET /api/sats/{norad}`.

A stdlib `http.server` in a daemon thread, a context manager, and a dict that can be changed at
run time (`set` / `remove` / `rows`), so a test (or a throwaway gateway started with
`PW_ANALYSIS_URL=<stub.url>`) can serve synthetic TLEs (`gateway/tests/tlegen.py`) and change them
mid-run. Binds 127.0.0.1 on a free port (or `port=`); never a fixed service port.

    with TleStub() as stub:
        stub.set(25544, "SYN-A", line1, line2, epoch="2026-10-28T12:00:00", age_days=0.5)
        ... PW_ANALYSIS_URL=stub.url ...
        stub.remove(25544)   # -> 404 from now on
        stub.calls           # the paths requested, in order

Answers `{"norad", "name", "tle_line1", "tle_line2", "tle_epoch", "tle_age_days"}` for a known
NORAD id, 404 `{"detail": ...}` for an unknown one and for any other path.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_PATH = re.compile(r"^/api/sats/(\d+)$")


class TleStub:
    def __init__(self, port: int = 0, host: str = "127.0.0.1") -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.calls: list[str] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                path = self.path.split("?", 1)[0]
                stub.calls.append(path)
                m = _PATH.match(path)
                row = stub.rows.get(int(m.group(1))) if m else None
                if row is None:
                    self._send(404, {"detail": "not found or decayed"})
                else:
                    self._send(200, dict(row))

            def _send(self, code: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass  # quiet

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # -- the served data

    def set(
        self,
        norad: int,
        name: str,
        line1: str,
        line2: str,
        epoch: str | None = None,
        age_days: float | None = 0.5,
    ) -> None:
        self.rows[int(norad)] = {
            "norad": int(norad),
            "name": name,
            "tle_line1": line1,
            "tle_line2": line2,
            "tle_epoch": epoch,
            "tle_age_days": age_days,
        }

    def remove(self, norad: int) -> None:
        self.rows.pop(int(norad), None)

    # -- lifecycle

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> TleStub:
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)

    def __enter__(self) -> TleStub:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()
