"""Starts and stops the service stack: analysis -> gateway (+ MediaMTX)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from . import paths, services
from .services import Child, StartFailure
from .winjob import Job

log = logging.getLogger("launcher")


class Stack:
    def __init__(
        self,
        job: Job | None,
        web: Path,
        gateway_port: int,
        analysis_port: int,
        root: Path = paths.APP_ROOT,
        logs: Path | None = None,
        health_timeout: float = services.HEALTH_TIMEOUT_S,
    ) -> None:
        self.job = job
        self.web = web
        self.gateway_port = gateway_port
        self.analysis_port = analysis_port
        self.root = root
        self.logs = logs or paths.log_dir()
        self.health_timeout = health_timeout
        self.analysis: Child | None = None
        self.gateway: Child | None = None
        self.mediamtx: Child | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.gateway_port}/"

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # nothing may write under the app folder (it may be Program Files); the package ships .pyc
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def start(self) -> None:
        py = services.child_python()
        log.info("ports: gateway %d, analysis %d", self.gateway_port, self.analysis_port)
        log.info("web dir: %s", self.web)
        self.logs.mkdir(parents=True, exist_ok=True)

        self.analysis = services.spawn(
            "analysis",
            [py, "-m", "analysis", "--port", str(self.analysis_port)],
            self.root,
            self._env(),
            self.logs / "analysis.out.log",
            self.job,
        )
        if not services.wait_healthy(
            f"http://127.0.0.1:{self.analysis_port}/api/health", self.analysis, self.health_timeout
        ):
            raise StartFailure("Analysis service did not start", self.analysis.log_path)

        genv = self._env()
        genv["PW_WEB_DIR"] = str(self.web)
        genv["PW_ANALYSIS_URL"] = f"http://127.0.0.1:{self.analysis_port}"
        self.gateway = services.spawn(
            "gateway",
            [py, "-m", "gateway", "--port", str(self.gateway_port)],
            self.root,
            genv,
            self.logs / "gateway.out.log",
            self.job,
        )
        if not services.wait_healthy(
            f"http://127.0.0.1:{self.gateway_port}/api/mount/health",
            self.gateway,
            self.health_timeout,
        ):
            raise StartFailure("Gateway did not start", self.gateway.log_path)

        self._maybe_mediamtx()

    def _maybe_mediamtx(self) -> None:
        folder = paths.mediamtx_folder(self.root)
        ok, reason = services.mediamtx_decision(paths.settings_path(), folder)
        if not ok or folder is None:
            log.info("MediaMTX not started: %s", reason)
            return
        try:
            self.mediamtx = services.spawn(
                "mediamtx",
                [str(folder / "mediamtx.exe"), str(folder / "mediamtx.yml")],
                folder,
                self._env(),
                self.logs / "mediamtx.out.log",
                self.job,
            )
        except OSError as exc:
            log.warning("MediaMTX did not start: %s", exc)

    def stop(self) -> None:
        """Gateway first (CTRL_BREAK -> lifespan shutdown -> G4 mount stop), then the rest."""
        for child, timeout, graceful in (
            (self.gateway, services.GATEWAY_STOP_S, True),
            (self.analysis, services.ANALYSIS_STOP_S, True),
            (self.mediamtx, services.MEDIAMTX_STOP_S, False),
        ):
            if child is not None:
                try:
                    services.stop_child(child, timeout, graceful)
                except Exception as exc:  # keep stopping the others
                    log.warning("%s: stop failed: %s", child.name, exc)
        self.gateway = self.analysis = self.mediamtx = None
