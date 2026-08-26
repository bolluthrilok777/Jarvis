"""Desktop capability detection for cross-platform Jarvis.

Jarvis runs on two kinds of nodes:

* Desktop nodes (Windows laptop, or Linux with a display) — full GUI
  automation (pyautogui, pygetwindow, Notepad/Calculator/Explorer control,
  screenshots) keeps working exactly as before.
* Headless nodes (Linux cloud VM without X11/Wayland) — GUI libraries are
  unavailable. Jarvis must still start and serve every non-GUI capability;
  desktop-only commands return a structured ``unsupported_on_this_node``
  response instead of crashing.

Nothing here removes desktop functionality — it only gates it safely.
"""
from __future__ import annotations

import sys

IS_WINDOWS = sys.platform.startswith("win")


def _detect_display() -> bool:
    """Return True when an interactive display server is reachable."""
    if IS_WINDOWS:
        # A Windows session always has a desktop available to this process.
        return True
    # Linux/BSD: X11 or Wayland must be present for GUI automation.
    import os
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


DESKTOP_AVAILABLE = _detect_display()


def require_desktop(feature: str, available: bool | None = None) -> dict | None:
    """Gate a desktop-only operation.

    ``available`` lets the caller pass its own module-level capability flag
    (so tests can patch it per-module); it defaults to this module's global
    detection result.

    Returns a structured refusal dict when the current node has no desktop,
    or None when the caller may proceed exactly as before.
    """
    has_desktop = DESKTOP_AVAILABLE if available is None else available
    if has_desktop:
        return None
    return {
        "status": "unsupported_on_this_node",
        "feature": feature,
        "message": (
            f"{feature} requires a desktop session, which is not available "
            "on this node. Jarvis is running headless; all other features "
            "remain operational."
        ),
        "desktop_available": False,
    }
