"""Real Windows application discovery and exact window-level actions.

Uses the Win32 API (via ctypes, no extra dependency) together with psutil for
process metadata to enumerate REAL top-level application windows. Invisible
background windows, shell internals and Windows services are excluded from the
user-facing list.

All actions target an EXACT tracked window handle (HWND):

* ``focus_window`` activates exactly that window.
* ``close_window`` posts WM_CLOSE only to that window (graceful, allows the
  application to prompt about unsaved work).
* Never a process-name-wide kill.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import time
from ctypes import wintypes
from typing import Optional

import psutil

logger = logging.getLogger("jarvis.resources.windows")

_IS_WINDOWS = sys.platform.startswith("win")

# Only initialise the Win32 API on Windows; every public function degrades
# gracefully (returns empty / False) elsewhere so tests never crash.
if _IS_WINDOWS:
    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi
else:  # pragma: no cover - non-Windows guard
    user32 = None
    dwmapi = None

# Window messages / show commands
_WM_CLOSE = 0x0010
_SW_RESTORE = 9
_DWMWA_CLOAKED = 14

# Window classes that are never user applications.
_EXCLUDED_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "Button", "ToolbarWindow32", "DV2ControlHost", "MultitaskingViewFrame",
    "XamlExplorerHostIslandWindow", "OsDnDNavHost", "SHELLDLL_DefView",
    "MSTaskSwWClass", "Windows.UI.Composition.DesktopWindowContentBridge",
    "MSCTFIME UI", "IME", "tooltips_class32",
}

# Jarvis infrastructure that must never be closed from Telegram.
_PROTECTED_TITLE_MARKERS = (
    "jarvis server", "jarvis", "ollama", "gmail monitor", "telegram poller",
)

# Runtime/helper host processes that are NOT user-facing applications in their
# own right. WebView2 (msedgewebview2.exe) hosts other apps' UI (e.g. the
# WhatsApp desktop UWP renders through it), so it must never be listed as a
# separate "application" alongside the real host. These are matched on the
# process/executable basename (lowercased).
_HELPER_PROCESS_BASENAMES = {
    "msedgewebview2.exe",   # Microsoft Edge WebView2 runtime host
}

# Real web-browser hosts. Their top-level windows are already represented by
# the dedicated Chrome/browser-tab subsystem (grouped by account under the
# "Chrome" section), so they must NEVER appear under "Applications". They are
# still ENUMERATED (kept in enumerate_windows) because they are the source of
# truth for Chrome tab discovery; the Applications list filters them out via
# ``is_browser_window`` in ResourceRegistry.scan. NOTE: Electron apps such as
# Kiro also use the ``Chrome_WidgetWin_1`` window class but run under their OWN
# executable (Kiro.exe), so exe-basename matching never hides them.
_BROWSER_PROCESS_BASENAMES = {
    "chrome.exe",       # Google Chrome
    "msedge.exe",       # Microsoft Edge (browser)
}


def is_browser_window(process_name: str = "", executable: str = "") -> bool:
    """True when a window belongs to a real web browser (Chrome/Edge).

    Used to keep browser windows OUT of the Applications list while still
    enumerating them for the Chrome tab-discovery subsystem.
    """
    proc = (process_name or "").lower()
    exe = (executable or "").rsplit("\\", 1)[-1].lower()
    return proc in _BROWSER_PROCESS_BASENAMES or exe in _BROWSER_PROCESS_BASENAMES


def _load_signatures() -> None:
    """Declare Win32 signatures once, so ctypes does not auto-convert ints."""
    if user32 is None:
        return
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL


if _IS_WINDOWS:
    _load_signatures()


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long), ("top", ctypes.c_long),
        ("right", ctypes.c_long), ("bottom", ctypes.c_long),
    ]


def _window_text(hwnd) -> str:
    if user32 is None:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _window_class(hwnd) -> str:
    if user32 is None:
        return ""
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _is_cloaked(hwnd) -> bool:
    """True when the window is not actually rendered (DWMWA_CLOAKED)."""
    if dwmapi is None:
        return False
    cloaked = ctypes.c_int(0)
    try:
        dwmapi.DwmGetWindowAttribute(
            hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
    except Exception:
        return False
    return bool(cloaked.value)


def _process_info(pid: int) -> dict:
    """psutil process metadata; never raises for dead/denied processes."""
    try:
        proc = psutil.Process(pid)
        name = proc.name()
        try:
            exe = proc.exe()
        except Exception:
            exe = ""
        return {"process_name": name or "", "executable": exe or ""}
    except Exception:
        return {"process_name": "", "executable": ""}


def _friendly_name(process_name: str, window_title: str, window_class: str) -> str:
    """Map an executable to a stable user-facing display name."""
    base = (process_name or "").lower()
    mapping = {
        "code.exe": "VS Code",
        "chrome.exe": "Chrome",
        "msedge.exe": "Edge",
        "notepad.exe": "Notepad",
        "calc.exe": "Calculator",
        "terminal.exe": "Windows Terminal",
        "microsoft.terminal.exe": "Windows Terminal",
        "windowsterminal.exe": "Windows Terminal",
        "cmd.exe": "Command Prompt",
        "powershell.exe": "PowerShell",
        "pwsh.exe": "PowerShell",
        "explorer.exe": "File Explorer",
        "taskmgr.exe": "Task Manager",
        "outlook.exe": "Outlook",
        "winword.exe": "Word",
        "excel.exe": "Excel",
        "powerpnt.exe": "PowerPoint",
        "pycharm64.exe": "PyCharm",
        "idea64.exe": "IntelliJ IDEA",
        "sublime_text.exe": "Sublime Text",
        "spotify.exe": "Spotify",
        "slack.exe": "Slack",
        "discord.exe": "Discord",
        "telegram.exe": "Telegram",
        "paint.exe": "Paint",
        "mspaint.exe": "Paint",
        "mintty.exe": "Mintty",
        "alacritty.exe": "Alacritty",
        "whatsapp.exe": "WhatsApp",
        "whatsapp.root.exe": "WhatsApp",
        "kiro.exe": "Kiro",
    }
    friendly = mapping.get(base)
    if friendly:
        return friendly
    # Modern UWP apps run under ApplicationFrameHost; the window title is the
    # real app name (e.g. "Calculator", "Settings").
    if base == "applicationframehost.exe":
        return (window_title or "Windows App").strip()[:60] or "Windows App"
    if base:
        return base.replace(".exe", "").replace("_", " ").title()
    return (window_title or window_class or "Unknown Window")[:60]


def _is_protected(title: str) -> bool:
    lowered = (title or "").lower()
    return any(marker in lowered for marker in _PROTECTED_TITLE_MARKERS)


def enumerate_windows() -> list[dict]:
    """Enumerate REAL visible top-level application windows.

    Returns one dict per window with a stable ``hwnd`` identity and process
    metadata. Invisible/background/system windows and Windows services are
    excluded.
    """
    if user32 is None:  # pragma: no cover - non-Windows guard
        return []
    found: list[dict] = []

    def _callback(hwnd, _lparam) -> bool:
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            title = _window_text(hwnd).strip()
            if not title:
                return True
            cls = _window_class(hwnd)
            if cls in _EXCLUDED_CLASSES:
                return True
            # Owned windows are dialogs/tool-windows, not top-level apps.
            if user32.GetWindow(hwnd, 4):  # GW_OWNER
                return True
            if _is_cloaked(hwnd):
                return True
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pid = int(pid.value)
            info = _process_info(pid)
            # Skip runtime/helper hosts (e.g. WebView2). They are not
            # user-facing applications; the real host app (WhatsApp, etc.) is
            # enumerated separately. Never expose them as their own app row.
            _proc_base = (info["process_name"] or "").lower()
            _exe_base = (info["executable"] or "").rsplit("\\", 1)[-1].lower()
            if (_proc_base in _HELPER_PROCESS_BASENAMES
                    or _exe_base in _HELPER_PROCESS_BASENAMES):
                return True
            # NOTE: real browser windows (chrome.exe/msedge.exe) are INTENTIONALLY
            # kept in this enumeration. They are the source of truth for the
            # Chrome tab-discovery subsystem (chrome_host_windows()); excluding
            # them here would starve UIA tab discovery. Browsers are filtered
            # OUT of the *Applications* list separately, in
            # ResourceRegistry.scan (see is_browser_window), so they appear only
            # under the Chrome section, never as an Application row.
            focused = user32.GetForegroundWindow() == hwnd
            found.append({
                "hwnd": int(hwnd),
                "title": title[:160],
                "class_name": cls,
                "pid": pid,
                "process_name": info["process_name"],
                "executable": info["executable"],
                "display_name": _friendly_name(
                    info["process_name"], title, cls,
                ),
                "focused": bool(focused),
                "visible": True,
                "protected": _is_protected(title),
            })
        except Exception:
            logger.debug("Skipping window %s during enumeration", hwnd, exc_info=True)
        return True

    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_callback)
    try:
        user32.EnumWindows(proc, 0)
    except Exception:
        logger.exception("EnumWindows failed")
    return found


def window_title(hwnd) -> str:
    """Current title of a window (empty string when it no longer exists)."""
    if user32 is None or not is_window_alive(hwnd):
        return ""
    return _window_text(hwnd)


def is_window_alive(hwnd) -> bool:
    if user32 is None:
        return False
    try:
        return bool(user32.IsWindow(hwnd))
    except Exception:
        return False


def foreground_window():
    if user32 is None:
        return None
    try:
        hwnd = user32.GetForegroundWindow()
        return int(hwnd) if hwnd else None
    except Exception:
        return None


def window_rect(hwnd) -> Optional[dict]:
    """Physical pixel rect of a window: ``{x, y, width, height}`` or None.

    Used by the screenshot service for screen-region fallback captures. Uses
    ``wintypes.RECT`` because GetWindowRect's argtypes declare exactly that
    pointer type (a structurally identical custom _Rect is rejected by
    ctypes). Returns None when the window no longer exists or has a zero-area
    rect.
    """
    if user32 is None or not is_window_alive(hwnd):
        return None
    rect = wintypes.RECT()
    try:
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            logger.debug("GetWindowRect failed for %s", hwnd)
            return None
    except Exception:
        logger.debug("GetWindowRect failed for %s", hwnd, exc_info=True)
        return None
    x, y = int(rect.left), int(rect.top)
    width = int(rect.right) - x
    height = int(rect.bottom) - y
    if width <= 0 or height <= 0:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


def focus_window(hwnd) -> bool:
    """Bring the EXACT tracked window to the foreground.

    Restores minimized windows and works around the Windows foreground lock by
    issuing a harmless Alt key event, then verifies the result. Returns True
    only when the foreground window really is *hwnd*.
    """
    if user32 is None or not is_window_alive(hwnd):
        return False
    try:
        user32.ShowWindow(hwnd, _SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        # Release the foreground lock if a console/other app owns it.
        time.sleep(0.05)
        if user32.GetForegroundWindow() != hwnd:
            user32.keybd_event(0x12, 0, 0, 0)   # VK_MENU down
            user32.keybd_event(0x12, 0, 2, 0)   # VK_MENU up
            time.sleep(0.05)
            user32.SetForegroundWindow(hwnd)
        time.sleep(0.05)
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        logger.exception("Could not focus window %s", hwnd)
        return False


def close_window(hwnd) -> bool:
    """Close exactly one window by posting WM_CLOSE to its HWND.

    WM_CLOSE is graceful: applications may prompt about unsaved work. A
    process-name-wide kill is NEVER performed.
    """
    if user32 is None or not is_window_alive(hwnd):
        return False
    try:
        result = bool(user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0))
        if result:
            logger.info("Posted WM_CLOSE to window %s", hwnd)
        return result
    except Exception:
        logger.exception("Could not close window %s", hwnd)
        return False
