"""Which surface currently owns the keyboard: the browser page, or something else.

The routing rule is deliberately simple and auditable: the foreground window is
a browser *page* only if it belongs to a known browser process AND uses the
browser's main-window class. Anything else (a native dialog owned by the
browser process, a Windows Hello prompt, Excel, Explorer, ...) is "desktop".
"""

from __future__ import annotations

import ctypes
import sys
import time
from dataclasses import dataclass

import psutil

BROWSER_PROCESSES = {"chrome.exe", "msedge.exe", "chromium.exe", "firefox.exe", "brave.exe"}
BROWSER_WINDOW_CLASSES = {"Chrome_WidgetWin_1", "MozillaWindowClass"}
CHROMIUM_WINDOW_CLASS = "Chrome_WidgetWin_1"

_u32 = None


def _user32():
    """user32 with explicit signatures so HWNDs survive the 64-bit round trip."""
    global _u32
    if _u32 is None:
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        u.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        u.IsWindowVisible.argtypes = [ctypes.c_void_p]
        u.IsIconic.argtypes = [ctypes.c_void_p]
        u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.SwitchToThisWindow.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        _u32 = u
    return _u32


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32().GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_title(hwnd: int) -> str:
    u = _user32()
    length = u.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    u.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def top_windows_for_pid(pid: int, window_class: str | None = None) -> list[int]:
    """Visible top-level windows owned by a process, optionally filtered by class."""
    u = _user32()
    found: list[int] = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _lparam):
        if not u.IsWindowVisible(hwnd):
            return True
        owner = ctypes.c_ulong()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        if window_class and _window_class(hwnd) != window_class:
            return True
        found.append(int(hwnd))
        return True

    u.EnumWindows(proto(cb), 0)
    return found


def all_top_windows() -> set[int]:
    """Handles of every visible top-level window."""
    u = _user32()
    found: set[int] = set()
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _lparam):
        if u.IsWindowVisible(hwnd):
            found.add(int(hwnd))
        return True

    u.EnumWindows(proto(cb), 0)
    return found


def activate_new_window(before: set[int], timeout: float = 6.0) -> int | None:
    """Wait for a top-level window that did not exist in `before`, bring it to the front.

    A process started from a background process does not get foreground
    rights on Windows, so a freshly launched app can open behind the browser.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        new = [h for h in all_top_windows() - before if _window_title(h)]
        if new:
            hwnd = new[-1]
            bring_to_front(hwnd)
            return hwnd
        time.sleep(0.25)
    return None


def activate_window_by_title(title: str) -> bool:
    """Bring the visible top-level window with this title (exact, else prefix) to the front."""
    if not title:
        return False
    exact, prefix = [], []
    for h in all_top_windows():
        t = _window_title(h)
        if t == title:
            exact.append(h)
        elif t and (t.startswith(title) or title.startswith(t)):
            prefix.append(h)
    for h in exact + prefix:
        if bring_to_front(h):
            return True
    return False


def bring_to_front(hwnd: int) -> bool:
    """Force a window to the OS foreground. Playwright's page.bring_to_front only switches tabs.

    Windows refuses SetForegroundWindow from a background process (the
    "foreground lock"). Three escalating attempts: plain, then a synthetic Alt
    key press that lifts the lock, then attaching to the foreground thread's input.
    """
    u = _user32()
    k32 = ctypes.windll.kernel32
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)  # SW_RESTORE

    def is_front() -> bool:
        time.sleep(0.25)
        return u.GetForegroundWindow() == hwnd

    u.SwitchToThisWindow(hwnd, True)
    u.SetForegroundWindow(hwnd)
    if is_front():
        return True

    VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x0002
    u.keybd_event(VK_MENU, 0, 0, 0)
    u.SetForegroundWindow(hwnd)
    u.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    if is_front():
        return True

    fg = u.GetForegroundWindow()
    fg_tid = u.GetWindowThreadProcessId(fg, None) if fg else 0
    my_tid = k32.GetCurrentThreadId()
    if fg_tid and fg_tid != my_tid:
        u.AttachThreadInput(fg_tid, my_tid, True)
        try:
            u.SetForegroundWindow(hwnd)
            u.BringWindowToTop(hwnd)
        finally:
            u.AttachThreadInput(fg_tid, my_tid, False)
    return is_front()


@dataclass(frozen=True)
class Focus:
    kind: str  # "browser" | "desktop"
    hwnd: int
    pid: int
    process: str
    window_class: str
    title: str

    def describe(self) -> str:
        return f"{self.kind}: {self.process} [{self.window_class}] {self.title!r}"


def get_focus() -> Focus:
    if sys.platform != "win32":
        return Focus("desktop", 0, 0, "unsupported-platform", "", "")
    u = _user32()
    hwnd = u.GetForegroundWindow() or 0
    pid = ctypes.c_ulong()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        process = psutil.Process(pid.value).name().lower()
    except Exception:
        process = "?"
    cls = _window_class(hwnd)
    kind = "browser" if (process in BROWSER_PROCESSES and cls in BROWSER_WINDOW_CLASSES) else "desktop"
    return Focus(kind, int(hwnd), int(pid.value), process, cls, _window_title(hwnd))
