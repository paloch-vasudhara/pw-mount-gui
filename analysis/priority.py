"""Lower the CPU priority of analysis processes so the gateway always wins (I5)."""

from __future__ import annotations

import sys

BELOW_NORMAL_PRIORITY_CLASS = 0x4000


def set_below_normal() -> bool:
    """Windows: BELOW_NORMAL for the current process. No-op elsewhere. Returns True if applied."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        return bool(
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)
        )
    except Exception:
        return False
