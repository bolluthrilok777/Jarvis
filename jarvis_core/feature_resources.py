"""Feature-specific runtime resource registry (V1).

Tracks applications / browser tabs / processes that JARVIS opened for feature
work so the user can see them and close unnecessary ones from Telegram.

Hard safety rules:
- Only resources Jarvis explicitly registered (opened_by_jarvis=True) are
  listed and closable. User-owned tabs/apps are never touched.
- Closing always targets an EXACT tracked identity (pid or stored closer).
  Process-name-wide kills are never performed.
- Risky surfaces (Cline, VS Code, editors, terminals) require explicit
  confirmation before close.
- Chrome/browser tabs are only closed through a closer callable captured at
  registration time (browser-automation page handle). Title matching is never
  used to find or kill arbitrary tabs.
"""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

RISKY_TYPES = {"application", "terminal_process", "editor", "cline", "vscode"}
_PROTECTED_TITLES = ("jarvis server", "ollama", "gmail monitor", "telegram poller")


@dataclass
class FeatureResource:
    resource_id: str
    feature_id: str
    type: str
    title: str
    safe_display_target: str = ""
    pid: Optional[int] = None
    opened_by_jarvis: bool = True
    status: str = "open"
    close_allowed: bool = True
    close_requires_confirmation: bool = False
    disposable: bool = False
    _closer: Optional[Callable[[], bool]] = field(default=None, repr=False)

    def public(self) -> dict:
        return {
            "resource_id": self.resource_id,
            "feature_id": self.feature_id,
            "type": self.type,
            "title": self.title,
            "safe_display_target": self.safe_display_target,
            "pid": self.pid,
            "opened_by_jarvis": self.opened_by_jarvis,
            "status": self.status,
            "close_allowed": self.close_allowed,
            "close_requires_confirmation": self.close_requires_confirmation,
            "disposable": self.disposable,
        }


class FeatureResourceRegistry:
    """Thread-safe registry of Jarvis-owned feature resources."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._resources: dict[str, FeatureResource] = {}
        self._seq = 0


    def register(self, feature_id, type_, title, *, safe_display_target="",
                 pid=None, closer=None, disposable=False, opened_by_jarvis=True):
        with self._lock:
            self._seq += 1
            r = FeatureResource(
                resource_id=f"{feature_id[:8]}-r{self._seq}",
                feature_id=feature_id, type=type_, title=str(title)[:120],
                safe_display_target=str(safe_display_target)[:120],
                pid=pid, closer=closer, disposable=disposable,
                opened_by_jarvis=opened_by_jarvis,
            )
            lowered = r.title.lower()
            if any(pt in lowered for pt in _PROTECTED_TITLES):
                r.close_allowed = False
            if r.type in RISKY_TYPES:
                r.close_requires_confirmation = True
            self._resources[r.resource_id] = r
            return r.public()

    def list_for_feature(self, feature_id):
        with self._lock:
            items = [r for r in self._resources.values()
                     if r.feature_id == feature_id and r.opened_by_jarvis]
        return [r.public() for r in items]

    def get(self, resource_id):
        with self._lock:
            r = self._resources.get(resource_id)
        return r.public() if r else None

    def close_resource(self, resource_id, *, confirmed=False):
        """Close one exact tracked resource. Never name-wide, never unowned."""
        with self._lock:
            r = self._resources.get(resource_id)
        if r is None:
            return {"ok": False, "message": "Resource not found."}
        if not r.opened_by_jarvis:
            return {"ok": False, "message": "Refusing: resource was not opened by Jarvis."}
        if r.status != "open":
            return {"ok": True, "message": f"'{r.title}' is already closed."}
        if not r.close_allowed:
            return {"ok": False, "message": f"'{r.title}' is protected and stays open."}
        if r.close_requires_confirmation and not confirmed:
            return {"ok": False, "needs_confirmation": True,
                    "message": f"Close {r.title}? Unsaved work may exist."}
        ok = self._terminate(r)
        with self._lock:
            r.status = "closed" if ok else r.status
        return ({"ok": True, "message": f"Closed: {r.title}"} if ok
                else {"ok": False, "message": f"Could not close '{r.title}'."})

    def close_idle(self, feature_id):
        """Close Idle Resources - only safe, Jarvis-owned, disposable ones."""
        closed = []
        with self._lock:
            targets = [r for r in self._resources.values()
                       if r.feature_id == feature_id and r.status == "open"
                       and r.disposable and r.close_allowed
                       and not r.close_requires_confirmation and r.opened_by_jarvis]
        for r in targets:
            if self._terminate(r):
                with self._lock:
                    r.status = "closed"
                closed.append(r.title)
        return closed

    def _terminate(self, r):
        if r.type in ("browser_tab", "browser_window"):
            if r._closer is None:
                return False
            try:
                return bool(r._closer())
            except Exception:
                return False
        if r.pid:
            try:
                result = subprocess.run(["taskkill", "/PID", str(r.pid), "/F"],
                                        capture_output=True, timeout=10)
                return result.returncode == 0
            except Exception:
                return False
        if r._closer is not None:
            try:
                return bool(r._closer())
            except Exception:
                return False
        return False


feature_resources = FeatureResourceRegistry()


def format_resources_message(feature_title, items):
    """Build ACTIVE FEATURE RESOURCES text + inline keyboard rows."""
    lines = ["\U0001f9e9 ACTIVE FEATURE RESOURCES\n\nFeature: %s\n" % feature_title]
    rows = []
    if not items:
        lines.append("No Jarvis-owned resources are currently tracked.")
    for i, item in enumerate(items, 1):
        marker = "[Keep]" if not item["close_allowed"] else "[Close]"
        lines.append(f"{i}. {item['title']}\n   {marker}")
        if item["close_allowed"] and item["status"] == "open":
            cb = (f"feature:res_ask:{item['resource_id']}"
                  if item["close_requires_confirmation"]
                  else f"feature:res_close:{item['resource_id']}")
            label = ("\u26a0\ufe0f Close Anyway" if item["close_requires_confirmation"]
                     else "\U0001f9f9 Close")
            rows.append([{"text": f"{label} {i}", "callback_data": cb}])
    rows.append([{"text": "\U0001f9f9 Close Idle Resources",
                  "callback_data": "feature:res_idle"}])
    return "\n".join(lines), rows
