"""Central registry for REAL laptop resources surfaced during a feature review.

A single short-lived, in-memory scan session holds application windows and
Chrome tabs discovered on this laptop. Each refresh creates a NEW generation;
resource ids are generation-stamped so a stale Telegram button (from an older
scan) is rejected and can never act on a newly created window/tab.

Actions always target the EXACT tracked identity:

* application -> exact HWND (focus / WM_CLOSE)
* browser tab  -> exact CDP targetId (activate / close / screenshot)

Never a process-name-wide kill and never a "close all" of any kind.

Nothing here is persisted - no browser sessions, cookies or credentials.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from jarvis_core.config import SCREENSHOT_DIR

logger = logging.getLogger("jarvis.resources")

#: Resource references auto-expire - a stale scan cannot be acted upon.
RESOURCE_TTL_SECONDS = 600

# Executables that are considered inherently safe to close without confirmation.
# ``calculatorapp.exe`` is the modern Windows Calculator (Microsoft Store app);
# matching also uses the executable BASENAME because store installs live under
# deep WindowsApps package paths.
_SAFE_APP_EXECUTABLES = (
    "calc", "calc.exe", "calculator", "calculator.exe",
    "calculatorapp", "calculatorapp.exe",
)


def _safe_app_identity(process_name: str, executable: str,
                       display_name: str) -> bool:
    """True when a window belongs to a disposable, stateless application.

    Matched on process name / executable basename plus an EXACT display-name
    match - never a window-title substring, which could misclassify e.g. an
    editor holding "calculator.txt" as disposable.
    """
    candidates: set[str] = set()
    for raw in (process_name, executable):
        if not raw:
            continue
        lowered = str(raw).lower().strip().replace("/", "\\")
        candidates.add(lowered)
        candidates.add(lowered.rsplit("\\", 1)[-1].strip())
    display = (display_name or "").lower().strip()
    if display in ("calc", "calculator"):
        candidates.add(display)
    return bool(candidates & set(_SAFE_APP_EXECUTABLES))

# Surfaces that may hold unsaved work or many tabs -> require confirmation.
_RISKY_APP_MARKERS = (
    "code", "cline", "notepad", "terminal", "cmd", "powershell", "pwsh",
    "pycharm", "idea", "sublime", "vim", "word", "excel", "powerpoint",
    "outlook", "slack", "obsidian", "typora", "studio", "xcode",
    "chrome", "edge", "browser", "explorer",
)

_RISKY_TAB_MARKERS = (
    "/edit", "docs.google.com/document", "docs.google.com/spreadsheets",
    "docs.google.com/presentation", "docs.google.com/forms", "mail.google.com",
    "outlook", "office.com", "drive.google.com", "keep.google.com",
    "notion.so", "github.com", "gitlab.com", "stackblitz", "codesandbox",
    "colab.research.google.com", "chatgpt.com/c/", "claude.ai", "gemini.google.com",
)

_TYPE_LABELS = {"application": "Application", "browser_tab": "Browser Tab"}


@dataclass
class Resource:
    resource_id: str
    feature_id: Optional[str]
    resource_type: str
    title: str
    display_name: str
    safe_display_target: str = ""
    pid: Optional[int] = None
    window_handle: Optional[int] = None
    page_id: Optional[str] = None
    executable: str = ""
    opened_by_jarvis: bool = False
    visible: bool = True
    focused: bool = False
    active: bool = False
    status: str = "open"
    close_allowed: bool = True
    close_requires_confirmation: bool = False
    screenshot_allowed: bool = True
    # Tab-control specifics:
    control_mode: str = ""                # "cdp" | "uia" | "" (apps)
    host_hwnd: Optional[int] = None       # Chrome window owning a UIA tab
    runtime_id: str = ""                  # UIA runtime-id (comma-joined ints)
    tab_index: int = -1                   # position in the discovered strip
    # Safe Chrome account/profile display identity of the owning window
    # (email preferred). Proven identity only - NEVER fabricated, never
    # includes tokens/cookies/Gaia IDs; display-only, short-lived memory.
    account_email: str = ""
    profile_label: str = ""
    identity_source: str = ""
    identity_label: str = ""
    discovered_at: float = field(default_factory=time.time)
    _raw_url: str = field(default="", repr=False)

    def public(self, include_identity: bool = False) -> dict:
        """Safe view - the raw URL (and any credentials it may carry) is never
        included.

        ``include_identity`` opts the CALLER into account/profile display
        labels (the allowlisted Telegram render path only); generic callers -
        e.g. other API surfaces and logs - never receive them.
        """
        data = {
            "resource_id": self.resource_id,
            "feature_id": self.feature_id,
            "type": self.resource_type,
            "title": self.title,
            "display_name": self.display_name,
            "safe_display_target": self.safe_display_target,
            "pid": self.pid,
            "window_handle": self.window_handle,
            "page_id": self.page_id,
            "executable": self.executable,
            "control_mode": self.control_mode,
            "host_hwnd": self.host_hwnd,
            "runtime_id": self.runtime_id,
            "tab_index": self.tab_index,
            "opened_by_jarvis": self.opened_by_jarvis,
            "visible": self.visible,
            "focused": self.focused,
            "active": self.active,
            "status": self.status,
            "close_allowed": self.close_allowed,
            "close_requires_confirmation": self.close_requires_confirmation,
            "screenshot_allowed": self.screenshot_allowed,
            "discovered_at": datetime.fromtimestamp(
                self.discovered_at
            ).isoformat() if self.discovered_at else "",
        }
        if include_identity:
            # Minimal display-grade identity only - no tokens/cookies/Gaia IDs.
            data["account_email"] = self.account_email
            data["profile_label"] = self.profile_label
            data["identity_source"] = self.identity_source
            data["identity_label"] = self.identity_label
        return data


_MEMORY_SUFFIX_RE = re.compile(
    r"\s*[\u2014-]\s*Memory usage\s*[\u2014-]\s*[\d.,]+\s*[KMG]B\s*$",
    re.IGNORECASE,
)


def _clean_tab_title(title: str) -> str:
    """Strip ONLY Chrome's accessibility ``- Memory usage - NNN MB`` suffix
    from a tab's DISPLAY title. Never alters the underlying tab identity
    (runtime_id / page_id / host_hwnd are unaffected).
    """
    text = str(title or "").strip()
    cleaned = _MEMORY_SUFFIX_RE.sub("", text).strip()
    return cleaned or text


def _generation_of(resource_id: str) -> Optional[int]:
    """Extract the generation number stamped into a resource id."""
    try:
        return int(str(resource_id).split("-", 1)[0][1:])
    except (IndexError, ValueError):
        return None


def resource_display_name(r: Resource) -> str:
    """User-facing name, e.g. 'Chrome — ChatGPT' or 'VS Code — app.py'."""
    name = r.display_name or r.title
    if r.resource_type == "browser_tab":
        return f"{r.display_name or 'Chrome'} \u2014 {r.title}"
    if r.title and r.title.lower() != name.lower():
        return f"{name} \u2014 {r.title}"
    return name


def resource_type_label(r: Resource) -> str:
    return _TYPE_LABELS.get(r.resource_type, r.resource_type.title())


def _classify_application(display_name: str, process_name: str, title: str,
                          protected: bool, executable: str) -> tuple[bool, bool]:
    """(close_allowed, close_requires_confirmation) for an application window."""
    if protected:
        return False, False
    haystack = " ".join([display_name, process_name, executable, title]).lower()
    if _safe_app_identity(process_name, executable, display_name):
        return True, False
    # Browsers/editors/terminals may hold unsaved work or many tabs.
    if any(marker in haystack for marker in _RISKY_APP_MARKERS):
        return True, True
    return True, True


def _classify_tab(title: str, display_target: str) -> tuple[bool, bool]:
    """(close_allowed, close_requires_confirmation) for a browser tab.

    Editors/forms/chat-composers signal visible columns for /edit etc.: ask
    before closing those. Reading tabs are closed exactly, never wide.
    """
    haystack = " ".join([title, display_target]).lower()
    return True, any(marker in haystack for marker in _RISKY_TAB_MARKERS)


#: Grace period (seconds) after posting WM_CLOSE before verifying whether the
#: exact HWND really closed. A still-alive window means the application is
#: showing its own confirmation dialog (e.g. unsaved work) - Jarvis never
#: touches that dialog and reports honestly instead.
CLOSE_SETTLE_SECONDS = 1.2


class ResourceRegistry:
    """Thread-safe, short-lived registry with generation-stamped resource ids."""
class ResourceRegistry:
    """Thread-safe, short-lived registry with generation-stamped resource ids."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._resources: dict[str, Resource] = {}
        self._feature_id: Optional[str] = None
        self._cdp_url: Optional[str] = None
        self._chrome_detected = False
        self._chrome_controllable = False
        self._chrome_reason = ""
        self._chrome_control_mode = ""
        self._last_scan_at = 0.0
        self._last_scan_read_only = True
        # Last-known-good Chrome tab set (raw discovery dicts) preserved across
        # a transient technical discovery failure while Chrome stays open.
        self._last_good_tabs: list[dict] = []
        self._last_good_mode: str = ""

    def scan(self, feature_id: Optional[str] = None, cdp_url=None,
             include_identity: bool = False, read_only: bool = True) -> dict:
        """Fresh REAL scan. Bumps the generation so earlier ids go stale.

        READ-ONLY discovery by contract: this enumerates visible windows and
        applications and reads safe Chrome tab titles. It NEVER focuses,
        closes, clicks, types, hotkeys or switches tabs. ``read_only`` is the
        default and the only mode the Feature workflow's Resource Review uses.
        """
        from jarvis_core.browser_resource_manager import discover_tabs
        from jarvis_core import windows_resource_manager
        from jarvis_core.windows_resource_manager import enumerate_windows

        with self._lock:
            self._generation += 1
            generation = self._generation
            resources: dict[str, Resource] = {}

            for window in enumerate_windows() or []:
                # Real browser windows (Chrome/Edge) are represented under the
                # Chrome tab section, never as standalone Applications. They are
                # still enumerated above so the tab-discovery subsystem can find
                # their host windows.
                if windows_resource_manager.is_browser_window(
                    window.get("process_name", ""),
                    window.get("executable", ""),
                ):
                    continue
                profile = _classify_application(
                    window.get("display_name", ""),
                    window.get("process_name", ""),
                    window.get("title", ""),
                    bool(window.get("protected")),
                    window.get("executable", ""),
                )
                rid = f"r{generation}-a{window.get('hwnd', '')}"
                resources[rid] = Resource(
                    resource_id=rid,
                    feature_id=feature_id,
                    resource_type="application",
                    title=(window.get("title") or "").strip(),
                    display_name=(window.get("display_name") or "Application").strip(),
                    pid=window.get("pid"),
                    window_handle=window.get("hwnd"),
                    executable=window.get("executable") or "",
                    visible=bool(window.get("visible", True)),
                    focused=bool(window.get("focused")),
                    close_allowed=profile[0],
                    close_requires_confirmation=profile[1],
                    screenshot_allowed=True,
                )

            chrome = discover_tabs(cdp_url)
            # ── Last-known-good safety ────────────────────────────────────
            # A TECHNICAL discovery failure (Chrome windows are clearly open
            # but this scan enumerated zero tabs) must NOT wipe the previously
            # known tabs. Only a genuine "Chrome closed / no Chrome windows"
            # yields zero. We detect real Chrome windows independently of the
            # tab enumeration result.
            _chrome_hosts_now = 0
            try:
                from jarvis_core import uia_browser_manager as _uia
                _chrome_hosts_now = len(_uia.chrome_host_windows())
            except Exception:
                _chrome_hosts_now = 0
            _fresh_tabs = list(chrome.get("tabs") or [])
            if (not _fresh_tabs and _chrome_hosts_now > 0
                    and self._last_good_tabs):
                # Preserve the last-known-good tab set (marked stale) instead of
                # dropping to an empty list on a transient failure.
                logger.warning(
                    "Chrome tab discovery returned 0 while %d Chrome window(s) "
                    "are open; preserving %d last-known-good tab(s) (stale).",
                    _chrome_hosts_now, len(self._last_good_tabs),
                )
                _fresh_tabs = [dict(t, _stale=True) for t in self._last_good_tabs]
                chrome = dict(chrome, chrome_detected=True, controllable=True,
                              control_mode=(self._last_good_mode or "uia"))

            for index, tab in enumerate(_fresh_tabs, 1):
                profile = _classify_tab(
                    tab.get("title", ""), tab.get("safe_display_target", "")
                )
                rid = f"r{generation}-t{index}"
                runtime_ids = tab.get("runtime_id") or []
                resources[rid] = Resource(
                    resource_id=rid,
                    feature_id=feature_id,
                    resource_type="browser_tab",
                    title=_clean_tab_title(tab.get("title") or "Chrome tab"),
                    display_name="Chrome",
                    safe_display_target=tab.get("safe_display_target", ""),
                    page_id=tab.get("target_id"),
                    executable="chrome.exe",
                    active=bool(tab.get("active")) or bool(tab.get("selected")),
                    close_allowed=profile[0],
                    close_requires_confirmation=profile[1],
                    screenshot_allowed=True,
                    control_mode=str(tab.get("control_mode") or ""),
                    host_hwnd=tab.get("host_hwnd"),
                    runtime_id=",".join(str(x) for x in runtime_ids),
                    tab_index=int(tab.get("tab_index", index - 1)),
                    _raw_url=tab.get("raw_url", ""),
                )

            # ── Layered account/profile identity per REAL Chrome window ───
            # Preferred display identity is the signed-in Google email.
            # Evidence ladder (never guesses, never fabricates): UIA chip
            # string matched against safe profile metadata → metadata-only
            # single-profile attribution → empty (honest fallback labels
            # rendered later). Nothing persisted; never logged.
            chrome_host_ids: set = set()
            for res in resources.values():
                if res.resource_type == "browser_tab" and res.host_hwnd:
                    chrome_host_ids.add(int(res.host_hwnd))
                elif (res.resource_type == "application"
                      and ((res.executable or "").rsplit("\\", 1)[-1].lower()
                           in ("chrome.exe", "chrome")
                           or "chrome" in (res.display_name or "").lower())
                      and res.window_handle):
                    chrome_host_ids.add(int(res.window_handle))
            ordered_windows = sorted(chrome_host_ids)
            _cpi = None
            identities: dict = {}
            if ordered_windows:
                try:
                    from jarvis_core import (
                        chrome_profile_identity as _cpi_mod,
                    )

                    identities = _cpi_mod.identify_hosts(ordered_windows) or {}
                    _cpi = _cpi_mod
                except Exception:
                    logger.warning(
                        "Chrome identity discovery unavailable", exc_info=True
                    )
                    identities = {}
            email_counts: dict = {}
            for hwnd in ordered_windows:
                email = (identities.get(hwnd) or {}).get("email") or ""
                email_counts[email] = email_counts.get(email, 0) + 1
            for window_number, hwnd in enumerate(ordered_windows, 1):
                ident = identities.get(hwnd) or {}
                label = (
                    _cpi.display_label_for(
                        ident, window_number,
                        duplicate_of_same_account=email_counts.get(
                            ident.get("email") or "", 0) > 1,
                    )
                    if _cpi is not None else ""
                )
                for res in resources.values():
                    owns = (
                        (res.resource_type == "browser_tab"
                         and res.host_hwnd == hwnd)
                        or (res.resource_type == "application"
                            and res.window_handle == hwnd
                            and ("chrome" in (res.display_name or "").lower()
                                 or (res.executable or "").rsplit("\\", 1)[-1]
                                 .lower() in ("chrome.exe", "chrome")))
                    )
                    if owns:
                        res.account_email = ident.get("email") or ""
                        res.profile_label = ident.get("profile_label") or ""
                        res.identity_source = ident.get("identity_source") or ""
                        res.identity_label = label

            self._resources = resources
            self._feature_id = feature_id
            self._cdp_url = chrome.get("cdp_url")
            self._chrome_detected = bool(chrome.get("chrome_detected"))
            self._chrome_controllable = bool(chrome.get("controllable"))
            self._chrome_reason = str(chrome.get("reason") or "")
            self._chrome_control_mode = str(chrome.get("control_mode") or "")
            # Maintain the last-known-good tab set:
            #  * genuinely-fresh, non-empty, non-preserved tabs -> remember them
            #  * Chrome truly closed (no host windows) -> clear (0 is real)
            #  * preserved/stale set -> leave the remembered set intact
            _preserved = any(t.get("_stale") for t in _fresh_tabs)
            if _fresh_tabs and not _preserved:
                self._last_good_tabs = [dict(t) for t in _fresh_tabs]
                self._last_good_mode = str(chrome.get("control_mode") or "")
            elif _chrome_hosts_now == 0:
                self._last_good_tabs = []
                self._last_good_mode = ""
            self._last_scan_at = time.time()
            self._last_scan_read_only = bool(read_only)
            logger.info(
                "Resource scan #%d: %d applications, %d tabs, chrome_controllable=%s",
                generation, sum(1 for r in resources.values()
                                if r.resource_type == "application"),
                sum(1 for r in resources.values()
                    if r.resource_type == "browser_tab"),
                self._chrome_controllable,
            )
        return self.scan_result(
            feature_id=feature_id, include_identity=include_identity
        )

    def scan_result(self, feature_id: Optional[str] = None,
                    include_identity: bool = False) -> dict:
        """Structured snapshot of the CURRENT scan generation.

        Identities ride along ONLY when ``include_identity`` is set (the
        allowlisted Telegram review path); the orchestrator and any other
        consumers receive the identity-free view.
        """
        with self._lock:
            applications = [
                r.public(include_identity) for r in self._resources.values()
                if r.resource_type == "application"
            ]
            tabs = [
                r.public(include_identity) for r in self._resources.values()
                if r.resource_type == "browser_tab"
            ]
            return {
                "generation": self._generation,
                "feature_id": self._feature_id or feature_id,
                "applications": applications,
                "tabs": tabs,
                "chrome_detected": self._chrome_detected,
                "chrome_controllable": self._chrome_controllable,
                "chrome_reason": self._chrome_reason,
                "chrome_control_mode": self._chrome_control_mode,
                "scanned_at": datetime.fromtimestamp(
                    self._last_scan_at
                ).isoformat() if self._last_scan_at else "",
            }

    def get(self, resource_id: str) -> Optional[Resource]:
        """Return the resource only if it belongs to the CURRENT scan
        generation and has not expired. Stale (or unknown) ids yield None so old
        Telegram buttons can never act on different, newly created resources."""
        with self._lock:
            resource = self._resources.get(resource_id)
            if resource is None:
                return None
            if _generation_of(resource_id) != self._generation:
                return None
            if time.time() - resource.discovered_at > RESOURCE_TTL_SECONDS:
                return None
        return resource

    def find(self, query: str) -> list[Resource]:
        """Current-generation resources whose name/title matches *query*."""
        wanted = str(query or "").lower().strip()
        if not wanted:
            return []
        with self._lock:
            items = list(self._resources.values())
        matched = []
        for r in items:
            if r.status != "open":
                continue
            haystack = " ".join([
                r.title, r.display_name, r.safe_display_target, r.executable,
            ]).lower()
            if wanted in haystack:
                matched.append(r)
        return matched

    # ── Actions (exact identity only) ───────────────────────────────────

    def focus(self, resource_id: str) -> dict:
        from jarvis_core import (
            browser_resource_manager,
            uia_browser_manager,
            windows_resource_manager,
        )

        r = self.get(resource_id)
        if r is None:
            return self._stale_result(resource_id)
        if r.resource_type == "browser_tab":
            if r.control_mode == "uia":
                try:
                    ok = bool(uia_browser_manager.focus_tab(
                        int(r.host_hwnd or 0),
                        [int(x) for x in (r.runtime_id or "").split(",") if x],
                        r.title, int(r.tab_index),
                    ))
                except uia_browser_manager.UIATabError as exc:
                    return {"status": "error", "message": str(exc)}
                except Exception as exc:
                    logger.warning("UIA focus failed: %s", exc)
                    ok = False
            else:
                ok = bool(browser_resource_manager.focus_tab(
                    self._cdp_url or "", r.page_id,
                ))
        else:
            ok = bool(windows_resource_manager.focus_window(r.window_handle))
        if ok:
            with self._lock:
                if r.resource_type == "application":
                    r.focused = True
                else:
                    r.active = True
            return {
                "status": "ok", "action": "focused",
                "resource_id": r.resource_id,
                "resource": r.public(),
                "message": f"\u2705 Focused: {resource_display_name(r)}",
            }
        return {
            "status": "error",
            "message": (
                f"Windows prevented foreground activation for "
                f"{resource_display_name(r)}."
            ),
        }

    #: Hard ceiling for one capture attempt so a Telegram user can never be
    #: left in an unbounded "Working…" state.
    CAPTURE_TIMEOUT_SECONDS = 12.0

    @staticmethod
    def _run_with_timeout(fn, timeout_seconds: float):
        """Run a capture callable with a hard deadline.

        Production: a daemon worker thread is joined with *timeout_seconds*;
        exceeding it raises :class:`TimeoutError` so a Telegram user can never
        be left in an unbounded ``Working…`` state.

        Hermetic test harnesses may replace ``threading.Thread`` with an inline
        double that executes the target synchronously inside ``start()``; the
        helper detects that and behaves deterministically instead of crashing.
        """
        box: dict = {}

        def _runner():
            try:
                box["result"] = fn()
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                box["error"] = exc

        worker = threading.Thread(
            target=_runner, daemon=True, name="jarvis-capture",
        )
        started = True
        try:
            worker.start()
        except Exception:
            # Thread creation unavailable -> synchronous fallback.
            started = False
            if "result" not in box and "error" not in box:
                _runner()

        if started:
            join = getattr(worker, "join", None)
            if callable(join):
                join(timeout_seconds)
                is_alive = getattr(worker, "is_alive", None)
                alive = callable(is_alive) and is_alive()
                if alive:
                    raise TimeoutError(
                        f"Capture did not finish within "
                        f"{float(timeout_seconds):.0f}s."
                    )
            # else: inline execution already finished during start()

        if "error" in box:
            raise box["error"]
        if "result" not in box:
            raise TimeoutError(
                f"Capture produced no result within "
                f"{float(timeout_seconds):.0f}s."
            )
        return box["result"]

    def _validated_capture(self, r: Resource, out_path: str) -> dict:
        """Capture one application window; region fallback; bounded."""
        from jarvis_core import screenshot_service, windows_resource_manager

        started = time.time()

        def _capture():
            return screenshot_service.capture_window(r.window_handle, out_path)

        try:
            result = self._run_with_timeout(
                _capture, self.CAPTURE_TIMEOUT_SECONDS
            )
        except ValueError:
            rect = windows_resource_manager.window_rect(r.window_handle)
            if rect is None:
                raise ValueError("Could not capture that window (it may have closed).")
            result = self._run_with_timeout(
                lambda: screenshot_service.capture_region(
                    rect["x"], rect["y"], rect["width"], rect["height"], out_path
                ),
                self.CAPTURE_TIMEOUT_SECONDS,
            )
        logger.info(
            "Screenshot of %s captured in %.2fs (%s bytes)",
            r.resource_id, time.time() - started,
            result.get("bytes", "?"),
        )
        return result

    def screenshot(self, resource_id: str) -> dict:
        from jarvis_core import (
            browser_resource_manager,
            screenshot_service,
            windows_resource_manager,
        )

        r = self.get(resource_id)
        if r is None:
            return self._stale_result(resource_id)
        if not r.screenshot_allowed or screenshot_service.looks_sensitive(
            r.title, r.safe_display_target, r._raw_url
        ):
            logger.info("Screenshot blocked by sensitive guard: %s", r.resource_id)
            return {
                "status": "blocked",
                "message": (
                    "\U0001f512 Screenshot blocked because the selected resource "
                    "may expose sensitive information."
                ),
            }

        out_dir = SCREENSHOT_DIR / "resources"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = str(
            out_dir / f"res_{r.resource_type}_{stamp}_{uuid.uuid4().hex[:8]}.png"
        )
        logger.info(
            "Screenshot requested: %s (%s/%s)", r.resource_id,
            r.resource_type, r.control_mode or "window",
        )

        try:
            if r.resource_type == "browser_tab":
                if r.control_mode == "uia":
                    result = self._screenshot_uia_tab(r, out_path)
                else:
                    from jarvis_core import (
                        browser_resource_manager,
                        windows_resource_manager,
                    )

                    result = self._screenshot_cdp_tab(
                        r, out_path, browser_resource_manager,
                        screenshot_service, windows_resource_manager,
                    )
            else:
                hwnd = int(r.window_handle or 0)
                if hwnd <= 0:
                    # Exact-identity guard: an application resource without a
                    # valid HWND can never be captured. Fail honestly instead
                    # of falling back to another window or the whole desktop.
                    return {
                        "status": "error",
                        "message": (
                            "That window is no longer available "
                            "(no valid window handle)."
                        ),
                    }
                focused = windows_resource_manager.focus_window(hwnd)
                if not focused:
                    logger.info(
                        "Focus failed before capture for %s; capturing anyway",
                        r.resource_id,
                    )
                time.sleep(0.6)
                result = self._validated_capture(r, out_path)
        except TimeoutError as exc:
            return {
                "status": "error", "timed_out": True,
                "message": f"Screenshot timed out ({exc})",
            }
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        except Exception as exc:
            logger.exception("Screenshot failed for %s", resource_id)
            return {
                "status": "error",
                "message": f"Screenshot failed ({type(exc).__name__}).",
            }

        check = screenshot_service.validate_png_file(result["file"])
        if not check["ok"]:
            try:
                os.remove(result["file"])
            except OSError:
                pass
            return {
                "status": "error",
                "message": "Screenshot capture produced an invalid image.",
            }
        logger.info(
            "Capture completed for %s: %d bytes %dx%d",
            r.resource_id, check["bytes"], check["width"], check["height"],
        )
        return {
            "status": "ok", "action": "screenshot",
            "resource_id": r.resource_id, "resource": r.public(),
            "file": result["file"], "bytes": check["bytes"],
            "width": check["width"], "height": check["height"],
            "message": "Screenshot captured.",
        }

    def _screenshot_uia_tab(self, r: Resource, out_path: str) -> dict:
        """UIA tab: select the EXACT tab, wait briefly, capture its window."""
        from jarvis_core import screenshot_service, uia_browser_manager

        runtime_ids = [int(x) for x in (r.runtime_id or "").split(",") if x]
        focused = uia_browser_manager.focus_tab(
            int(r.host_hwnd or 0), runtime_ids, r.title, int(r.tab_index)
        )
        if not focused:
            raise ValueError(
                "Could not bring that exact tab to the front (UIA selection "
                "could not be verified)."
            )
        time.sleep(0.8)  # brief settle so the newly active tab renders
        hwnd = int(r.host_hwnd or 0)

        def _capture():
            return screenshot_service.capture_window(hwnd, out_path)

        try:
            return self._run_with_timeout(_capture, self.CAPTURE_TIMEOUT_SECONDS)
        except ValueError:
            def _region():
                from jarvis_core import windows_resource_manager

                rect = windows_resource_manager.window_rect(hwnd)
                if rect is None:
                    raise ValueError("The Chrome window is no longer available.")
                return screenshot_service.capture_region(
                    rect["x"], rect["y"], rect["width"], rect["height"], out_path
                )

            return self._run_with_timeout(_region, self.CAPTURE_TIMEOUT_SECONDS)

    def _screenshot_cdp_tab(self, r: Resource, out_path: str,
                            browser_resource_manager, screenshot_service,
                            windows_resource_manager) -> dict:
        """CDP tab: EXACT targetId capture ONLY - no fallback capture.

        The screenshot comes from the selected tab's own CDP target. There is
        deliberately NO "activate the tab then grab the focused Chrome
        window" fallback: with several Chrome windows/tabs open that can
        capture a different same-title tab or an unrelated window while
        pretending it is the selected one, and it would silently perform a
        Focus. When the exact capture is unavailable the user gets a visible
        honest failure instead of a wrong image.
        """

        def _cdp_shot():
            if browser_resource_manager.screenshot_tab(
                self._cdp_url or "", r.page_id, out_path, r._raw_url
            ):
                return {"file": out_path}
            raise ValueError(
                "Exact Chrome tab capture unavailable for the selected tab "
                "(the tab may have been closed or direct tab capture is not "
                "supported)."
            )

        try:
            return self._run_with_timeout(
                _cdp_shot, self.CAPTURE_TIMEOUT_SECONDS * 2
            )
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Tab screenshot failed ({type(exc).__name__}).")

    def close(self, resource_id: str, confirmed: bool = False) -> dict:
        """Dispatch a close request to the EXACT backend for its type.

        Kept as the single entry point so legacy callers keep working, but the
        two backends below are strictly separated and mutually rejecting.
        """
        r = self.get(resource_id)
        if r is not None and r.resource_type == "browser_tab":
            return self.close_browser_tab(resource_id, confirmed=confirmed)
        return self.close_application_window(resource_id, confirmed=confirmed)

    def close_application_window(self, resource_id: str,
                                 confirmed: bool = False) -> dict:
        """Close ONE application window by its exact HWND.

        Hard guard: refuses browser-tab ids - a tab id can never reach the
        window-level WM_CLOSE backend through this function.
        """
        from jarvis_core import windows_resource_manager

        r = self.get(resource_id)
        if r is None:
            return self._stale_result(resource_id)
        # ── Backend separation guard ─────────────────────────────────────
        if r.resource_type == "browser_tab":
            logger.warning(
                "Rejected application-window close for tab resource %s",
                resource_id,
            )
            return {
                "status": "rejected",
                "message": (
                    "Refused: that id is a Chrome TAB. Use Close Tab instead - "
                    "an application close would take down the whole browser."
                ),
            }
        if not r.close_allowed:
            return {
                "status": "error",
                "message": f"{resource_display_name(r)} is protected and stays open.",
            }
        if r.status != "open":
            return {
                "status": "ok",
                "message": f"{resource_display_name(r)} is already closed.",
            }
        if r.close_requires_confirmation and not confirmed:
            return {
                "status": "needs_confirmation",
                "resource_id": r.resource_id,
                "resource": r.public(),
                "message": (
                    "\u26a0\ufe0f CLOSE THIS?\n\n"
                    f"{resource_display_name(r)} may contain unsaved work."
                ),
            }
        ok = bool(windows_resource_manager.close_window(r.window_handle))
        if ok:
            # WM_CLOSE is only a graceful REQUEST. Give the application a
            # brief moment to honor it; if the EXACT HWND is still alive it
            # is showing its own (e.g. unsaved-work) confirmation dialog -
            # Jarvis NEVER dismisses it, never retries and never kills the
            # process. Report honestly instead.
            time.sleep(CLOSE_SETTLE_SECONDS)
            closed_confirmed = not windows_resource_manager.is_window_alive(
                r.window_handle)
            with self._lock:
                r.status = "closed" if closed_confirmed else "open"
            if closed_confirmed:
                return {
                    "status": "ok", "action": "closed",
                    "closed_confirmed": True,
                    "resource_id": r.resource_id, "resource": r.public(),
                    "message": f"\u2705 Closed: {resource_display_name(r)}",
                }
            return {
                "status": "ok", "action": "close_requested",
                "closed_confirmed": False,
                "resource_id": r.resource_id, "resource": r.public(),
                "message": (
                    "\u26a0\ufe0f Close requires confirmation on the laptop. "
                    f"{resource_display_name(r)} is still open and may be "
                    "showing a Save/unsaved-work dialog, which Jarvis never "
                    "dismisses."
                ),
            }
        return {
            "status": "error",
            "message": f"Could not close {resource_display_name(r)}.",
        }

    def close_browser_tab(self, resource_id: str,
                          confirmed: bool = False) -> dict:
        """Close EXACTLY one Chrome tab - never the window, never processes.

        Hard guard: refuses application resources. Verifies the removal where
        observable and reports honestly when it cannot be verified.
        """
        from jarvis_core import browser_resource_manager, uia_browser_manager

        r = self.get(resource_id)
        if r is None:
            return self._stale_result(resource_id)
        # ── Backend separation guard ─────────────────────────────────────
        if r.resource_type != "browser_tab":
            logger.warning(
                "Rejected tab close for non-tab resource %s", resource_id,
            )
            return {
                "status": "rejected",
                "message": (
                    "Refused: Close Tab can only be applied to a browser tab."
                ),
            }
        if not r.close_allowed:
            return {
                "status": "error",
                "message": f"{resource_display_name(r)} is protected and stays open.",
            }
        if r.status != "open":
            return {
                "status": "ok",
                "message": f"{resource_display_name(r)} is already closed.",
            }
        if r.close_requires_confirmation and not confirmed:
            return {
                "status": "needs_confirmation",
                "resource_id": r.resource_id,
                "resource": r.public(),
                "message": (
                    "\u26a0\ufe0f CLOSE THIS TAB?\n\n"
                    f"{resource_display_name(r)} may contain unsaved work."
                ),
            }

        verified = False
        if r.control_mode == "uia":
            outcome = uia_browser_manager.close_tab(
                int(r.host_hwnd or 0),
                [int(x) for x in (r.runtime_id or "").split(",") if x],
                r.title, int(r.tab_index),
            )
            ok = bool(outcome.get("ok"))
            verified = bool(outcome.get("verified"))
            if not ok and outcome.get("reason"):
                return {"status": "error", "message": str(outcome["reason"])}
        else:
            ok = bool(browser_resource_manager.close_tab(
                self._cdp_url or "", r.page_id,
            ))
            if ok:
                try:
                    remaining = browser_resource_manager.discover_tabs(
                        self._cdp_url
                    ).get("tabs") or []
                    verified = all(
                        t.get("target_id") != r.page_id for t in remaining
                    )
                except Exception:
                    verified = False
        if ok:
            with self._lock:
                r.status = "closed"
        if verified:
            message = f"\u2705 Closed tab: {r.title}"
        elif ok:
            message = "\u26a0\ufe0f Close requested but tab closure could not be verified."
        else:
            message = f"Could not close {resource_display_name(r)}."
        return {
            "status": "ok" if ok else "error", "action": "closed_tab",
            "verified": verified,
            "resource_id": r.resource_id, "resource": r.public(),
            "feature_id": r.feature_id,
            "message": message,
        }

    def screenshot_current_screen(self) -> dict:
        """📸 Current Screen - capture the real monitor(s) right now.

        A sensitive-surface check runs against the foreground window title and
        the currently selected Chrome tab titles BEFORE anything is sent.
        """
        from jarvis_core import screenshot_service, uia_browser_manager
        from jarvis_core.windows_resource_manager import (
            foreground_window,
            window_title,
        )

        surfaces: list[str] = []
        fg = foreground_window()
        if fg:
            surfaces.append(window_title(fg) or "")
        try:
            outcome = uia_browser_manager.enumerate_tabs()
            surfaces.extend(
                t.get("title", "") for t in outcome.get("tabs", [])
                if t.get("selected")
            )
        except Exception:
            logger.debug("UIA surface check skipped", exc_info=True)
        if screenshot_service.looks_sensitive(" ".join(surfaces), ""):
            return {
                "status": "blocked",
                "message": (
                    "\U0001f512 Screenshot blocked because the current screen "
                    "may expose sensitive information."
                ),
            }

        out_dir = SCREENSHOT_DIR / "resources"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = str(out_dir / f"screen_{stamp}_{uuid.uuid4().hex[:8]}.png")
        try:
            result = self._run_with_timeout(
                lambda: screenshot_service.capture_screen(out_path),
                self.CAPTURE_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            return {"status": "error", "timed_out": True,
                    "message": f"Screen capture timed out ({exc})"}
        except Exception as exc:
            logger.exception("Current-screen capture failed")
            return {
                "status": "error",
                "message": f"Screen capture failed ({type(exc).__name__}).",
            }
        check = screenshot_service.validate_png_file(result["file"])
        if not check["ok"]:
            try:
                os.remove(result["file"])
            except OSError:
                pass
            return {
                "status": "error",
                "message": "Screen capture produced an invalid image.",
            }
        return {
            "status": "ok", "action": "screenshot",
            "resource_id": "current-screen", "resource": {},
            "file": result["file"], "bytes": check["bytes"],
            "width": check["width"], "height": check["height"],
            "message": "Current screen captured.",
        }

    @staticmethod
    def _stale_result(resource_id: str) -> dict:
        return {
            "status": "stale",
            "resource_id": resource_id,
            "message": (
                "That resource is no longer in the current scan. "
                "Press \U0001f504 Refresh for a fresh list."
            ),
        }


resource_registry = ResourceRegistry()


class ReadOnlyResourceInspector:
    """READ-ONLY inspection facade that Resource Review / Apps-Tabs depend on.

    Deliberately exposes ONLY discovery and safe metadata capture:

    * ``scan`` / ``snapshot``         - fresh read-only enumeration
    * ``list_apps`` / ``list_windows`` / ``list_browser_tabs`` - pure filters
    * ``capture_metadata``            - safe structured metadata only

    It deliberately has NO ``focus`` / ``close`` / ``screenshot`` /
    ``click`` / ``type`` methods, so the Feature workflow's Resource Review can
    never reach the Desktop Controller (``ResourceRegistry.focus/close/...``).
    The controller remains available only to explicit Jarvis command paths
    (Action Engine), never to the review path.
    """

    def __init__(self, registry: "ResourceRegistry") -> None:
        self._registry = registry

    def scan(self, feature_id: Optional[str] = None, cdp_url=None,
             include_identity: bool = False) -> dict:
        """Fresh READ-ONLY discovery of running apps/windows + safe tab titles."""
        return self._registry.scan(
            feature_id=feature_id, cdp_url=cdp_url,
            include_identity=include_identity, read_only=True)

    def snapshot(self, feature_id: Optional[str] = None,
                 include_identity: bool = False) -> dict:
        """Current in-memory snapshot (generation-bound).

        ``feature_id`` is only forwarded when the caller actually supplies
        one, so the delegation stays transparent for callers (and existing
        tests) that never bind a feature context.
        """
        kwargs: dict = {"include_identity": include_identity}
        if feature_id is not None:
            kwargs["feature_id"] = feature_id
        return self._registry.scan_result(**kwargs)

    @staticmethod
    def list_apps(snapshot: dict) -> list[dict]:
        """Read-only filter: application rows of a snapshot."""
        return list((snapshot or {}).get("applications") or [])

    @staticmethod
    def list_windows(snapshot: dict) -> list[dict]:
        """Read-only filter: one representative entry per Chrome HWND."""
        windows: dict[int, dict] = {}
        for tab in (snapshot or {}).get("tabs") or []:
            if not tab.get("host_hwnd"):
                continue
            windows.setdefault(int(tab["host_hwnd"]), tab)
        for app in (snapshot or {}).get("applications") or []:
            if "chrome" in (app.get("display_name") or "").lower() \
                    and app.get("window_handle"):
                windows.setdefault(int(app["window_handle"]), app)
        return list(windows.values())

    @staticmethod
    def list_browser_tabs(snapshot: dict) -> list[dict]:
        """Read-only filter: browser tab rows of a snapshot."""
        return list((snapshot or {}).get("tabs") or [])

    @staticmethod
    def capture_metadata(snapshot: dict) -> dict:
        """Safe structured metadata; never URLs, profiles or secrets."""
        return {
            "generation": (snapshot or {}).get("generation"),
            "feature_id": (snapshot or {}).get("feature_id"),
            "scanned_at": (snapshot or {}).get("scanned_at"),
            "chrome_detected": bool((snapshot or {}).get("chrome_detected")),
            "chrome_controllable": bool(
                (snapshot or {}).get("chrome_controllable")),
            "applications": [
                {k: app.get(k) for k in (
                    "resource_id", "display_name", "title", "executable",
                    "window_handle", "pid", "control_mode")}
                for app in (snapshot or {}).get("applications") or []
            ],
            "tabs": [
                {k: tab.get(k) for k in (
                    "resource_id", "title", "safe_display_target",
                    "host_hwnd", "tab_index", "control_mode", "active")}
                for tab in (snapshot or {}).get("tabs") or []
            ],
        }


#: Shared read-only inspector used ONLY by Resource Review / Apps-Tabs.
resource_inspector = ReadOnlyResourceInspector(resource_registry)


# ── Telegram rendering helpers ────────────────────────────────────────────

def _short(label: str, limit: int = 36) -> str:
    label = " ".join(str(label or "").split())
    return label if len(label) <= limit else label[: limit - 1] + "\u2026"


def _feature_nav_callback(feature_id: Optional[str], action: str) -> str:
    return f"feature:{action}:{feature_id or ''}"


def build_resources_menu(feature_id: Optional[str], scan: dict) -> tuple[str, list]:
    """Text + inline-keyboard rows for the 'CURRENT LAPTOP RESOURCES' message."""
    applications = scan.get("applications") or []
    tabs = scan.get("tabs") or []
    lines = ["\U0001f9e9 CURRENT LAPTOP RESOURCES"]

    lines.append("\nAPPLICATIONS")
    if applications:
        for index, app in enumerate(applications, 1):
            label = app.get("display_name") or app.get("title") or "Application"
            if app.get("title") and app["title"].lower() != label.lower():
                label = f"{label} \u2014 {app['title']}"
            lines.append(f"{index}. {_short(label, 60)}")
    else:
        lines.append("No visible application windows.")

    lines.append("\nCHROME TABS")
    if tabs:
        for index, tab in enumerate(tabs, 1):
            title = tab.get("title") or "Chrome tab"
            target = tab.get("safe_display_target") or ""
            lines.append(f"{index}. {_short(title, 60)}")
            if target:
                lines.append(f"   {target}")
    elif scan.get("chrome_detected"):
        lines.append(
            "Chrome detected, but individual tabs could not be read "
            "(UIA/CDP both unavailable)."
        )
    else:
        lines.append("No Chrome browser detected.")

    rows = []
    for app in applications:
        rid = app.get("resource_id")
        if rid:
            label = app.get("display_name") or app.get("title")
            rows.append([{
                "text": f"\U0001f4c4 {_short(label)}",
                "callback_data": f"feature:resrc_show:{rid}",
            }])
    for tab in tabs:
        rid = tab.get("resource_id")
        if rid:
            title = tab.get("title") or "Chrome tab"
            rows.append([{
                "text": f"\U0001f310 {_short(title)}",
                "callback_data": f"feature:resrc_tab_show:{rid}",
            }])

    nav = [{
        "text": "\U0001f504 Refresh",
        "callback_data": _feature_nav_callback(feature_id, "resrc_refresh"),
    }]
    if scan.get("chrome_controllable"):
        # Grouped entry: ONE button that opens the real per-tab list - the
        # individual Chrome windows above remain application-level entries.
        group_label = (
            f"\U0001f310 Chrome \u2014 {len(tabs)} tabs"
            if tabs else "\U0001f310 Chrome"
        )
        nav.append({
            "text": group_label,
            "callback_data": _feature_nav_callback(feature_id, "resrc_tabs"),
        })
    nav.append({
        "text": "\U0001f4f8 Current Screen",
        "callback_data": _feature_nav_callback(feature_id, "resrc_screen"),
    })
    if feature_id:
        nav.append({
            "text": "\u25b6 Continue Feature",
            "callback_data": _feature_nav_callback(feature_id, "resrc_continue"),
        })
    rows.append(nav)
    if feature_id:
        rows.append([{
            "text": "\u274c Cancel Feature",
            "callback_data": _feature_nav_callback(feature_id, "resrc_cancel"),
        }])
    else:
        rows.append([{"text": "\u274c Close", "callback_data": "feature:noop"}])
    return "\n".join(lines), rows


def build_resource_detail(resource: Resource, *, read_only: bool = False) -> tuple[str, list]:
    """Text + inline-keyboard rows for the application action menu.
    Provides exactly 5 options:
    1. 🎯 Focus
    2. 📸 Screenshot
    3. ✏️ Edit
    4. ❌ Close
    5. ⬅️ Cancel/Back
    """
    rid = resource.resource_id
    is_tab = resource.resource_type == "browser_tab"
    lines = [
        "\U0001f4f1 APPLICATION ACTION MENU" if not is_tab else "\U0001f310 CHROME TAB ACTION MENU",
        f"Name: {resource_display_name(resource)}",
        f"Type: {resource_type_label(resource)}",
        "Status: Open",
    ]
    if is_tab:
        owner = (resource.account_email or resource.profile_label
                 or resource.identity_label or "").strip()
        if owner:
            lines.append(f"Account/Profile: {owner}")
        if resource.safe_display_target:
            lines.append(f"URL: {resource.safe_display_target}")
    if not resource.screenshot_allowed:
        lines.append("Screenshot: blocked (sensitive content)")

    rows = [
        [
            {"text": "\U0001f3af Focus", "callback_data": f"feature:resrc_focus:{rid}"},
            {"text": "\U0001f4f8 Screenshot", "callback_data": f"feature:resrc_shot:{rid}"},
        ],
        [
            {"text": "\u270f\ufe0f Edit", "callback_data": f"feature:resrc_edit:{rid}"},
            {"text": "\u2b05\ufe0f Cancel/Back", "callback_data": _feature_nav_callback(resource.feature_id, "resrc_back")},
        ],
        [
            {"text": "\u274c Close", "callback_data": f"feature:resrc_close:{rid}"},
        ],
    ]
    return "\n".join(lines), rows


def build_close_confirmation(resource: Resource) -> tuple[str, list]:
    """Text + inline-keyboard rows asking the user to confirm a risky close.

    Rendered IN PLACE on the SAME Resource Review message before any WM_CLOSE
    is posted. Jarvis never automates the application's own Save/Discard
    dialog - the warning says so explicitly.
    """
    rid = resource.resource_id
    lines = [
        "\u26a0\ufe0f Confirm closing?",
        f"Name: {resource_display_name(resource)}",
        "Unsaved work may exist. Jarvis sends a graceful close request to "
        "this exact window only \u2014 it never kills the process. If the "
        "application shows a Save/unsaved-work dialog, Jarvis leaves it "
        "untouched for you to answer on the laptop.",
    ]
    rows = [
        [
            {"text": "\u2705 Confirm Close",
             "callback_data": f"feature:resrc_confirm_close:{rid}"},
            {"text": "Keep Open",
             "callback_data": f"feature:resrc_show:{rid}"},
        ],
        [
            {"text": "\u2b05\ufe0f Cancel/Back",
             "callback_data": _feature_nav_callback(
                 resource.feature_id, "resrc_home")},
        ],
    ]
    return "\n".join(lines), rows


def build_chrome_tabs_menu(feature_id: Optional[str], scan: dict) -> tuple[str, list]:
    """Text + inline-keyboard rows for the dedicated '🌐 CHROME TABS' view."""
    tabs = scan.get("tabs") or []
    lines = ["\U0001f310 CHROME TABS"]
    if tabs:
        for index, tab in enumerate(tabs, 1):
            title = tab.get("title") or "Chrome tab"
            suffix = ""
            if tab.get("safe_display_target"):
                suffix = f" \u2014 {tab['safe_display_target']}"
            marker = " \u2022active" if tab.get("active") or tab.get("selected") else ""
            lines.append(f"{index}. {_short(title + suffix, 70)}{marker}")
    elif scan.get("chrome_detected"):
        lines.append("Chrome is running, but no individual tabs could be read.")
        if scan.get("chrome_reason"):
            lines.append(_short(scan["chrome_reason"], 160))
    else:
        lines.append("No Chrome browser is currently running.")

    mode = scan.get("chrome_control_mode")
    if not mode:
        # Infer from tab payloads when the snapshot predates the field.
        modes = {t.get("control_mode") for t in tabs if t.get("control_mode")}
        mode = modes.pop() if len(modes) == 1 else ""

    control_note = {
        "cdp": "Control mode: Chrome DevTools Protocol (exact targetIds).",
        "uia": "Control mode: Windows UI Automation (real tab strip).",
    }.get(mode)
    if control_note:
        lines.append(control_note)

    rows = []
    for tab in tabs:
        rid = tab.get("resource_id")
        if rid:
            title = tab.get("title") or "Chrome tab"
            rows.append([{
                "text": f"\U0001f5a5 {_short(title)}",
                "callback_data": f"feature:resrc_tab_show:{rid}",
            }])
    nav = [
        {
            "text": "\U0001f504 Refresh Tabs",
            "callback_data": _feature_nav_callback(feature_id, "resrc_tabs_refresh"),
        },
        {
            "text": "\U0001f4f8 Screenshot Current Chrome",
            "callback_data": _feature_nav_callback(feature_id, "resrc_screen"),
        },
        {
            "text": "\u2b05 Back",
            "callback_data": _feature_nav_callback(feature_id, "resrc_back"),
        },
    ]
    rows.append(nav)
    return "\n".join(lines), rows


def build_chrome_tab_detail(resource: Resource, *, read_only: bool = False) -> tuple[str, list]:
    """Text + inline-keyboard rows for 'CHROME TAB ACTION MENU'.
    Provides exactly 5 options:
    1. 🎯 Focus
    2. 📸 Screenshot
    3. ✏️ Edit
    4. ❌ Close Tab
    5. ⬅️ Cancel/Back
    """
    rid = resource.resource_id
    domain = resource.safe_display_target or "(domain not exposed)"
    owner = (resource.account_email or resource.identity_label or "").strip()
    lines = [
        "\U0001f310 CHROME TAB ACTION MENU",
        f"Title: {resource.title}",
        f"Domain: {domain}",
        f"Account/Profile: {owner or 'Chrome'}",
        f"Window: {resource.identity_label or owner or 'Chrome'}",
        "Status: Open",
    ]
    if not resource.screenshot_allowed:
        lines.append("Screenshot: blocked (sensitive content)")

    rows = [
        [
            {"text": "\U0001f3af Focus", "callback_data": f"feature:resrc_focus:{rid}"},
            {"text": "\U0001f4f8 Screenshot", "callback_data": f"feature:resrc_shot:{rid}"},
        ],
        [
            {"text": "\u270f\ufe0f Edit", "callback_data": f"feature:resrc_edit:{rid}"},
            {"text": "\u2b05\ufe0f Cancel/Back", "callback_data": _feature_nav_callback(resource.feature_id, "resrc_back")},
        ],
        [
            {"text": "\u274c Close Tab", "callback_data": f"feature:resrc_close:{rid}"},
        ],
    ]
    return "\n".join(lines), rows