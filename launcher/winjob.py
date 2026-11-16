"""Windows plumbing through ctypes (no pywin32): job object, named mutex, window focus."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9

if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CreateMutexW.restype = wintypes.HANDLE
    _k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _u32.FindWindowW.restype = wintypes.HWND
    _u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    _u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _u32.IsIconic.argtypes = [wintypes.HWND]


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("a", "b", "c", "d", "e", "f")]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class Job:
    """A job object whose processes die when its last handle closes (launcher exit / crash)."""

    def __init__(self) -> None:
        self.handle = _k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(
            self.handle, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, pid: int) -> None:
        h = _k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not h:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not _k32.AssignProcessToJobObject(self.handle, h):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _k32.CloseHandle(h)

    def close(self) -> None:
        h, self.handle = self.handle, None
        if h:
            _k32.CloseHandle(h)


class Mutex:
    """A named mutex; `acquired` is False when another process already holds the name."""

    def __init__(self, name: str) -> None:
        self.handle = _k32.CreateMutexW(None, False, name)
        self.acquired = bool(self.handle) and ctypes.get_last_error() != ERROR_ALREADY_EXISTS

    def close(self) -> None:
        h, self.handle = self.handle, None
        if h:
            _k32.CloseHandle(h)


def focus_window(title: str) -> bool:
    """Restore + raise the top-level window with this exact title (the running instance)."""
    hwnd = _u32.FindWindowW(None, title)
    if not hwnd:
        return False
    if _u32.IsIconic(hwnd):
        _u32.ShowWindow(hwnd, SW_RESTORE)
    _u32.SetForegroundWindow(hwnd)
    return True


# ---------------------------------------------------------------- window helpers

WM_SETICON = 0x0080
ICON_SMALL, ICON_BIG = 0, 1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
LR_DEFAULTSIZE = 0x40
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
SM_CXICON, SM_CYICON, SM_CXSMICON, SM_CYSMICON = 11, 12, 49, 50


def find_window(title: str) -> int:
    return int(_u32.FindWindowW(None, title) or 0)


def is_zoomed(hwnd: int) -> bool:
    return bool(_u32.IsZoomed(hwnd))


def is_iconic(hwnd: int) -> bool:
    return bool(_u32.IsIconic(hwnd))


def virtual_screen() -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the whole desktop across monitors."""
    x, y = _u32.GetSystemMetrics(SM_XVIRTUALSCREEN), _u32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    return (
        x,
        y,
        x + _u32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        y + _u32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def set_window_icon(title: str, ico: str) -> bool:
    """Set the native window's title-bar / taskbar icon from an .ico (WM_SETICON big + small)."""
    hwnd = find_window(title)
    if not hwnd:
        return False
    ok = False
    for kind, cx, cy in (
        (ICON_BIG, SM_CXICON, SM_CYICON),
        (ICON_SMALL, SM_CXSMICON, SM_CYSMICON),
    ):
        h = _u32.LoadImageW(
            None,
            ico,
            IMAGE_ICON,
            _u32.GetSystemMetrics(cx),
            _u32.GetSystemMetrics(cy),
            LR_LOADFROMFILE,
        )
        if h:
            _u32.SendMessageW(hwnd, WM_SETICON, kind, h)
            ok = True
    return ok


if sys.platform == "win32":
    _u32.IsZoomed.argtypes = [wintypes.HWND]
    _u32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _u32.LoadImageW.restype = wintypes.HANDLE
    _u32.LoadImageW.argtypes = [
        wintypes.HINSTANCE,
        wintypes.LPCWSTR,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    _u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _u32.SendMessageW.restype = ctypes.c_ssize_t
