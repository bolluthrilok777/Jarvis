"""Real screenshot capture for tracked application windows / Chrome tabs.

Application windows are captured with GDI PrintWindow (PW_RENDERFULLCONTENT) so
even partially occluded windows render correctly, falling back to a screen
region grab. Chrome tabs use the CDP page screenshot endpoint / Playwright CDP
attachment (handled in browser_resource_manager).

Sensitive surfaces are refused BEFORE any capture happens:
``looks_sensitive`` guards .env files, password managers, credential dialogs,
Telegram/Gmail OAuth tokens, browser cookies and API keys.
"""
from __future__ import annotations

import ctypes
import logging
import os
import time
from ctypes import wintypes
from typing import Optional

import PIL.Image
import PIL.ImageGrab

logger = logging.getLogger("jarvis.resources.screenshot")

_PW_RENDERFULLCONTENT = 0x00000002
_DIB_RGB_COLORS = 0

# Substrings that mark a window/tab as likely-sensitive. Conservative: better
# to refuse a screenshot than leak a credential.
SENSITIVE_MARKERS = (
    ".env", "password", "passwords", "credential", "credentials",
    "secret", "keeper", "lastpass", "1password", "bitwarden",
    "token", "oauth", "api key", "apikey", "cookies", "telegram bot",
)

_SENSITIVE_HOSTS = (
    "accounts.google.com", "login.microsoftonline.com",
    "myaccount.google.com", "signin.aws.amazon.com",
)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long), ("top", ctypes.c_long),
        ("right", ctypes.c_long), ("bottom", ctypes.c_long),
    ]


def _gdi():
    return ctypes.windll.gdi32


def _user():
    return ctypes.windll.user32


def looks_sensitive(title: str, display_target: str = "", raw_url: str = "") -> bool:
    """True when a resource should never be screenshotted."""
    haystack = " ".join([title or "", display_target or ""]).lower()
    if any(marker in haystack for marker in SENSITIVE_MARKERS):
        return True
    if raw_url:
        lowered = raw_url.lower()
        if any(host in lowered for host in _SENSITIVE_HOSTS):
            return True
        # Credential-shaped values inside the URL (bot tokens, OAuth tokens,
        # API keys) are detected with the same regexes used everywhere else.
        try:
            from jarvis_core.feature_utils import contains_sensitive

            if contains_sensitive(raw_url):
                return True
        except Exception:
            pass
    return False


def validate_png_file(path: str) -> dict:
    """Verify a captured file is a real, non-empty, decodable image.

    Returns ``{"ok", "bytes", "width", "height"}``; never raises.
    """
    info = {"ok": False, "bytes": 0, "width": 0, "height": 0}
    try:
        size = os.path.getsize(path)
    except OSError:
        return info
    info["bytes"] = int(size)
    if size <= 0:
        return info
    try:
        with PIL.Image.open(path) as image:
            width, height = image.size
            image.verify()
        info.update(ok=width > 0 and height > 0,
                    width=int(width), height=int(height))
    except Exception:
        logger.warning("Captured file failed image validation: %s", path)
    return info


def capture_screen(out_path: str) -> dict:
    """Capture the REAL current monitor(s) and save a validated PNG."""
    started = time.monotonic()
    img = PIL.ImageGrab.grab(all_screens=True)
    if img.size[0] <= 0 or img.size[1] <= 0:
        raise ValueError("Screen grab returned an empty image.")
    img.save(out_path, "PNG")
    check = validate_png_file(out_path)
    if not check["ok"]:
        raise ValueError("Screen grab produced an invalid image.")
    logger.info(
        "Current-screen capture completed: %d bytes, %dx%d in %.2fs",
        check["bytes"], check["width"], check["height"],
        time.monotonic() - started,
    )
    return {"file": str(out_path), **{k: check[k] for k in ("bytes", "width", "height")}}


def capture_region(x: int, y: int, width: int, height: int, out_path: str) -> dict:
    """Capture a real screen region and save a PNG. Returns file info."""
    if width <= 0 or height <= 0:
        raise ValueError("Capture region has zero or negative size.")
    img = PIL.ImageGrab.grab(bbox=(int(x), int(y), int(x + width), int(y + height)))
    img.save(out_path, "PNG")
    return {"file": str(out_path), "width": int(width), "height": int(height)}


def capture_window(hwnd: int, out_path: str) -> dict:
    """Capture exactly one window via PrintWindow; fall back to a screen grab.

    Raises ValueError when the window is gone or has no drawable area.
    """
    from jarvis_core.windows_resource_manager import is_window_alive, window_rect

    logger.info("Capture started for window %s", hwnd)
    started = time.monotonic()
    if not is_window_alive(hwnd):
        raise ValueError("Window no longer exists.")
    rect = window_rect(hwnd)
    if rect is None:
        raise ValueError("Window has no drawable area.")
    width, height = rect["width"], rect["height"]
    if width <= 0 or height <= 0:
        raise ValueError("Window has no drawable area.")

    user = _user()
    gdi = _gdi()
    hwnd_dc = user.GetWindowDC(hwnd)
    if not hwnd_dc:
        return capture_region(rect["x"], rect["y"], width, height, out_path)
    mem_dc = gdi.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi.CreateCompatibleBitmap(hwnd_dc, width, height)
    old_bitmap = gdi.SelectObject(mem_dc, bitmap)
    try:
        ok = user.PrintWindow(hwnd, mem_dc, _PW_RENDERFULLCONTENT)
        if not ok:
            logger.info("PrintWindow returned 0 for window %s; using screen grab", hwnd)
            return capture_region(rect["x"], rect["y"], width, height, out_path)
        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # top-down rows
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0
        buffer = ctypes.create_string_buffer(width * height * 4)
        got = gdi.GetDIBits(
            mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), _DIB_RGB_COLORS
        )
        if got == 0:
            return capture_region(rect["x"], rect["y"], width, height, out_path)
        image = PIL.Image.frombuffer(
            "RGB", (width, height), buffer, "raw", "BGRX", 0, 1
        )
        image.save(out_path, "PNG")
        check = validate_png_file(out_path)
        if not check["ok"]:
            return capture_region(rect["x"], rect["y"], width, height, out_path)
        logger.info(
            "Capture completed for window %s: %d bytes in %.2fs",
            hwnd, check["bytes"], time.monotonic() - started,
        )
        return {"file": str(out_path), **{k: check[k] for k in ("bytes", "width", "height")}}
    finally:
        gdi.SelectObject(mem_dc, old_bitmap)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(mem_dc)
        user.ReleaseDC(hwnd, hwnd_dc)


def capture_foreground(out_path: str) -> Optional[dict]:
    """Capture the currently focused window (used as a fallback)."""
    from jarvis_core.windows_resource_manager import foreground_window

    hwnd = foreground_window()
    if not hwnd:
        return None
    try:
        return capture_window(hwnd, out_path)
    except ValueError:
        return None