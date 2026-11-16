"""python -m launcher / pythonw -m launcher: one click starts everything, closing stops it."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from . import TITLE, paths, services, ui, winjob
from .runner import Stack
from .services import StartFailure

MUTEX_NAME = "Local\\PWMountControl"
log = logging.getLogger("launcher")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="launcher")
    p.add_argument("--browser", action="store_true", help="open the default browser, no window")
    p.add_argument("--console-log", action="store_true", help="mirror the launcher log to stdout")
    p.add_argument("--gateway-port", type=int, default=None)
    p.add_argument("--analysis-port", type=int, default=None)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for name in ("stdout", "stderr"):  # pythonw has none; libraries may write to them
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))  # noqa: SIM115
    logs = paths.log_dir()
    try:
        services.setup_logging(logs, console=args.console_log)
    except OSError:
        pass  # no log folder: carry on, the services still start
    log.info("launcher starting (root %s)", paths.APP_ROOT)

    mutex = winjob.Mutex(MUTEX_NAME)
    if not mutex.acquired:
        log.info("already running: focusing the existing window")
        winjob.focus_window(TITLE)
        mutex.close()
        return 0

    job = None
    stack = None
    try:
        web = paths.web_dir()
        if web is None:
            ui.error_window(
                "The web app is not built",
                "Build it once with:  cd web && pnpm install && pnpm generate\n"
                "or set PW_WEB_DIR to a folder with the built app.",
            )
            return 1
        if args.gateway_port is not None:
            gport = args.gateway_port
        else:
            gport = services.pick_port(*services.GATEWAY_PORTS)
        if args.analysis_port is not None:
            aport = args.analysis_port
        else:
            aport = services.pick_port(*services.ANALYSIS_PORTS, taken=(gport or 0,))
        if gport is None or aport is None:
            ui.error_window("No free port for the services (8400-8429 are all busy)")
            return 1

        job = winjob.Job()
        stack = Stack(job, web, gport, aport, logs=logs)
        try:
            stack.start()
        except StartFailure as exc:
            log.error("%s", exc.part)
            stack.stop()
            ui.error_window(exc.part, services.tail_lines(exc.log_path, 30), logs)
            return 1

        _show(args, stack)
        return 0
    except Exception:
        log.exception("launcher failed")
        if stack is not None:
            stack.stop()
        ui.error_window("PW Mount Control failed to start", "See launcher.log", logs)
        return 1
    finally:
        if stack is not None:
            stack.stop()
        if job is not None:
            job.close()
        mutex.close()
        log.info("launcher exit")


def _show(args: argparse.Namespace, stack: Stack) -> None:
    if args.browser:
        ui.run_browser(stack.url)
        return
    try:
        ui.run_window(stack.url, paths.icon_path(), paths.data_dir() / "window.json")
    except ui.WebViewUnavailable as exc:
        log.warning("window unavailable (%s): falling back to the browser", exc)
        ui.run_browser(
            stack.url,
            "The app window needs the Microsoft Edge WebView2 runtime, which was not found, "
            "so the app opened in your default browser.",
        )


if __name__ == "__main__":
    sys.exit(main())
