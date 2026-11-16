"""The windows: the pywebview app window, the --browser keep-alive window, error window."""

from __future__ import annotations

import logging
import time
import webbrowser
from pathlib import Path

from . import TITLE, paths, windowstate, winjob

log = logging.getLogger("launcher")
_BLANK = chr(10) * 2  # paragraph break in a message box


class WebViewUnavailable(Exception):
    """pywebview / the WebView2 runtime cannot show the window."""


def have_tk() -> bool:
    """tkinter is missing from the embeddable Python of the package: the windows then use a
    Win32 message box."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    return True


def message_box(text: str, title: str = TITLE, icon: int = 0x40) -> None:
    """A modal Win32 MessageBoxW (0x40 = information, 0x10 = error). Blocks until OK."""
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, text, title, icon | 0x10000)  # | MB_SETFOREGROUND


def error_window(heading: str, detail: str = "", log_folder: Path | None = None) -> None:
    """A small tkinter error dialog (the launcher has no console under pythonw)."""
    log.error("%s", heading)
    if not have_tk():
        try:
            parts = [heading]
            if detail:
                parts.append(detail.strip()[-1500:])
            if log_folder is not None:
                parts.append(f"Logs: {log_folder}")
            message_box(_BLANK.join(parts), icon=0x10)
        except Exception:
            log.exception("could not show the error window")
        return
    try:
        import tkinter as tk
        from tkinter import scrolledtext

        root = tk.Tk()
        root.title(TITLE)
        tk.Label(root, text=heading, font=("Segoe UI", 12, "bold"), anchor="w").pack(
            fill="x", padx=12, pady=(12, 4)
        )
        if detail:
            box = scrolledtext.ScrolledText(root, width=100, height=16, font=("Consolas", 9))
            box.insert("1.0", detail)
            box.configure(state="disabled")
            box.pack(fill="both", expand=True, padx=12, pady=4)
        if log_folder is not None:
            tk.Label(root, text=f"Logs: {log_folder}", anchor="w").pack(fill="x", padx=12)
        tk.Button(root, text="Close", width=10, command=root.destroy).pack(pady=12)
        root.mainloop()
    except Exception:
        log.exception("could not show the error window")


def run_browser(url: str, note: str = "") -> None:
    """--browser mode: default browser + a tiny window; closing it ends the launcher."""
    webbrowser.open(url)
    if not have_tk():
        lines = ["PW Mount Control is running. Press OK to stop it."]
        lines += [note] if note else []
        lines.append(url)
        message_box(_BLANK.join(lines))
        return
    try:
        import tkinter as tk

        root = tk.Tk()
        root.title(TITLE)
        root.resizable(False, False)
        text = "PW Mount Control is running - close this window to stop"
        tk.Label(root, text=text, padx=24, pady=16).pack()
        if note:
            tk.Label(root, text=note, padx=24, fg="#555555", wraplength=420).pack()
        tk.Label(root, text=url, padx=24, pady=8, fg="#555555").pack()
        root.mainloop()
    except Exception:
        log.exception("the keep-alive window failed")
        raise


def run_window(url: str, icon: Path | None, state_path: Path) -> None:
    """The app window (pywebview, Edge WebView2). Returns when the window is closed."""
    try:
        import webview
    except Exception as exc:
        raise WebViewUnavailable(str(exc)) from exc

    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.settings["ALLOW_DOWNLOADS"] = False
    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False

    state = windowstate.load(state_path, winjob.virtual_screen())
    normal = {k: state[k] for k in ("x", "y", "width", "height") if k in state}
    kwargs = {k: state[k] for k in ("x", "y") if k in state}
    # js_api stays unset: nothing of Python is exposed to the page
    window = webview.create_window(
        TITLE,
        url,
        width=state["width"],
        height=state["height"],
        min_size=windowstate.MIN_SIZE,
        js_api=None,
        text_select=True,
        **kwargs,
    )

    def track(**kv) -> None:
        hwnd = winjob.find_window(TITLE)
        if hwnd and not winjob.is_zoomed(hwnd) and not winjob.is_iconic(hwnd):
            normal.update(kv)

    def on_shown() -> None:
        if icon is not None:
            for _ in range(20):  # the native window may need a moment
                if winjob.set_window_icon(TITLE, str(icon)):
                    break
                time.sleep(0.25)
        if state["maximized"]:
            window.maximize()

    def on_closing() -> None:
        hwnd = winjob.find_window(TITLE)
        maximized = bool(hwnd) and winjob.is_zoomed(hwnd)
        if hwnd and not maximized and not winjob.is_iconic(hwnd):
            normal.update(x=window.x, y=window.y, width=window.width, height=window.height)
        out = dict(normal)
        out["maximized"] = maximized
        windowstate.save(state_path, out)

    window.events.shown += on_shown
    window.events.moved += lambda x, y: track(x=x, y=y)
    window.events.resized += lambda w, h: track(width=w, height=h)
    window.events.closing += on_closing

    storage = paths.data_dir() / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    try:
        webview.start(
            gui="edgechromium",
            debug=False,
            private_mode=False,  # the app's own localStorage survives restarts
            storage_path=str(storage),
        )
    except Exception as exc:
        raise WebViewUnavailable(str(exc)) from exc
