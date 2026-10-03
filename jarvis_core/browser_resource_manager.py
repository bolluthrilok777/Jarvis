"""Real Chrome tab discovery and control via the Chrome DevTools Protocol.

Discovery NEVER guesses tabs from the Chrome process list. If the running
Chrome exposes a CDP endpoint (launched with ``--remote-debugging-port``) we
enumerate REAL page targets through ``/json/list`` and act on an exact
``targetId`` (activate / close / screenshot). Otherwise we report honestly that
Chrome was detected but individual tabs are not controllable, and never
fabricate tab names, and never relaunch or force-kill the user's browser.

The CDP endpoint is configured with the ``JARVIS_CHROME_CDP_URL`` environment
variable (default ``http://127.0.0.1:9222``). Set it empty to disable.

Only the *safe* display domain (scheme + host + path, no query string) is
exposed to Telegram; full URLs are kept in memory only, never persisted.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Optional
from urllib.parse import urlsplit

import requests

logger = logging.getLogger("jarvis.resources.browser")

#: CDP endpoint used to attach to the user's already-running Chrome.
DEFAULT_CDP_URL = (
    os.getenv("JARVIS_CHROME_CDP_URL", "http://127.0.0.1:9222").strip()
    or None
)

_CDP_VERSION_TIMEOUT = 1.5
_CDP_ACTION_TIMEOUT = 5.0
_CHROME_PROCESS_NAMES = {"chrome.exe", "chrome"}


def chrome_processes_running() -> bool:
    """True when at least one real chrome.exe process exists (only used to
    produce an honest "Chrome detected" message when CDP is unavailable)."""
    try:
        import psutil

        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if name in _CHROME_PROCESS_NAMES:
                return True
    except Exception:
        logger.debug("Could not inspect Chrome processes", exc_info=True)
    return False


def cdp_reachable(cdp_url: Optional[str], timeout: float = _CDP_VERSION_TIMEOUT) -> bool:
    """True when a real Chrome DevTools endpoint answers /json/version."""
    if not cdp_url:
        return False
    try:
        response = requests.get(f"{cdp_url.rstrip('/')}/json/version", timeout=timeout)
        data = response.json()
        return isinstance(data, dict) and bool(data.get("Browser"))
    except Exception:
        return False


def safe_domain_display(raw_url: str) -> str:
    """Safe, human-friendly URL display: scheme + host + short path only.

    Query strings (which may carry session tokens), fragments and embedded
    credentials are NEVER shown or persisted.
    """
    try:
        parts = urlsplit(raw_url or "")
    except ValueError:
        return ""
    host = parts.hostname or ""
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    path = (parts.path or "")[:40]
    if len(parts.path or "") > 40:
        path = path + "\u2026"
    return f"{parts.scheme}://{host}{path}"


def discover_tabs(cdp_url: Optional[str] = None) -> dict:
    """Enumerate REAL Chrome page targets.

    Layered strategy:

    1. CDP (``--remote-debugging-port``): every page target with a stable
       ``targetId`` - exact activate/close/screenshot support.
    2. Windows UI Automation fallback: the REAL visible tab strip of the
       already-running Chrome when no CDP endpoint exists - exact
       focus/screenshot/close via per-tab UIA controls.

    Returns a dict with ``chrome_detected``, ``controllable``, optional
    ``reason``, and ``tabs`` (each carrying its ``control_mode``). Never
    guesses tabs and never relaunches or kills the user's browser.
    """
    url = DEFAULT_CDP_URL if cdp_url is None else cdp_url
    base = (url or "").rstrip("/")

    def _uia_fallback(chrome_detected: bool, reason: str, cdp_base: str) -> dict:
        from jarvis_core import uia_browser_manager

        # Bounded retries: a single UIA round-trip can transiently miss the tab
        # strip (tree still settling) while Chrome windows are clearly open. Up
        # to 3 attempts with a short settle; each attempt is itself deadline-
        # bounded, so this never runs away.
        outcome = uia_browser_manager.enumerate_tabs()
        if not outcome.get("ok"):
            try:
                _hosts = uia_browser_manager.chrome_host_windows()
            except Exception:
                _hosts = []
            if _hosts:
                import time as _t
                for _attempt in range(2):
                    _t.sleep(0.4)
                    retry = uia_browser_manager.enumerate_tabs()
                    if retry.get("ok"):
                        outcome = retry
                        break
        if outcome.get("ok"):
            logger.info(
                "Chrome tabs discovered via UIA fallback: %d tab(s)",
                len(outcome["tabs"]),
            )
            return {
                "chrome_detected": True,
                "controllable": True,
                "control_mode": "uia",
                "reason": reason,
                "tabs": outcome["tabs"],
                "cdp_url": cdp_base,
            }
        combined = "; ".join(
            part for part in [reason, outcome.get("reason", "")] if part
        )
        return {
            "chrome_detected": chrome_detected,
            "controllable": False,
            "control_mode": "",
            "reason": combined or (
                "Chrome was detected but individual tabs are not controllable."
            ),
            "tabs": [],
            "cdp_url": cdp_base,
        }

    if not base:
        return _uia_fallback(
            chrome_processes_running(),
            "",
            None,
        )
    if not cdp_reachable(base):
        return _uia_fallback(
            chrome_processes_running(),
            (
                "Chrome remote-debugging endpoint not reachable; "
                "individual tabs are managed through the Windows UI "
                "Automation fallback instead."
                if chrome_processes_running()
                else ""
            ),
            base,
        )
    try:
        targets = requests.get(f"{base}/json/list", timeout=_CDP_ACTION_TIMEOUT).json()
    except Exception as exc:
        logger.warning("Chrome /json/list failed: %s", exc)
        return {
            "chrome_detected": True,
            "controllable": False,
            "reason": "Chrome answered but tab enumeration failed.",
            "tabs": [],
            "cdp_url": base,
        }
    tabs: list[dict] = []
    for entry in targets or []:
        if not isinstance(entry, dict) or entry.get("type") != "page":
            continue
        title = (entry.get("title") or "").strip()
        raw_url = entry.get("url") or ""
        target_id = entry.get("id") or ""
        if not target_id or (not title and not raw_url):
            continue
        tabs.append({
            "title": (title[:120] or safe_domain_display(raw_url)),
            "safe_display_target": safe_domain_display(raw_url),
            "raw_url": raw_url,          # in-memory only; never persisted/shown
            "target_id": target_id,
            "active": False,
            "control_mode": "cdp",
        })
    return {
        "chrome_detected": True,
        "controllable": True,
        "control_mode": "cdp",
        "reason": "",
        "tabs": tabs,
        "cdp_url": base,
    }


def focus_tab(cdp_url: str, target_id: str) -> bool:
    """Activate the EXACT tab by its CDP targetId (brings it to the front)."""
    base = (cdp_url or "").rstrip("/")
    if not base or not target_id:
        return False
    try:
        response = requests.get(
            f"{base}/json/activate/{target_id}", timeout=_CDP_ACTION_TIMEOUT
        )
        return response.status_code == 200
    except Exception as exc:
        logger.warning("Could not activate tab %s: %s", target_id, exc)
        return False


def close_tab(cdp_url: str, target_id: str) -> bool:
    """Close ONLY the given tab. Other tabs and the browser stay untouched."""
    base = (cdp_url or "").rstrip("/")
    if not base or not target_id:
        return False
    try:
        response = requests.get(
            f"{base}/json/close/{target_id}", timeout=_CDP_ACTION_TIMEOUT
        )
        return response.status_code == 200
    except Exception as exc:
        logger.warning("Could not close tab %s: %s", target_id, exc)
        return False


def create_tab(cdp_url: str, url: str = "about:blank") -> Optional[dict]:
    """Create a REAL new Chrome tab via CDP and return its exact identity.

    Uses the DevTools ``PUT /json/new`` endpoint (with the target URL as the
    query) which creates a brand-new page target and returns its JSON,
    including the new ``id`` (the CDP ``targetId``). This NEVER navigates or
    overwrites an existing tab — it always opens an additional tab, so the
    user's Gmail / personal ChatGPT / Jarvis tabs are untouched.

    Returns ``{"target_id", "raw_url", "control_mode": "cdp"}`` on success,
    or ``None`` when no CDP endpoint is available / the create failed (the
    caller must then fail closed or try the UIA path — never downgrade to
    overwriting the active tab).
    """
    base = (cdp_url or "").rstrip("/")
    if not base:
        return None
    open_url = url or "about:blank"
    try:
        # Newer Chrome requires PUT; the URL is passed as the path query.
        from urllib.parse import quote
        endpoint = f"{base}/json/new?{quote(open_url, safe=':/?=&%')}"
        response = requests.put(endpoint, timeout=_CDP_ACTION_TIMEOUT)
        if response.status_code in (404, 405):
            # Older Chrome builds accept GET on /json/new.
            response = requests.get(endpoint, timeout=_CDP_ACTION_TIMEOUT)
        if response.status_code != 200:
            logger.warning("CDP create_tab HTTP %s", response.status_code)
            return None
        data = response.json()
        if not isinstance(data, dict) or not data.get("id"):
            return None
        return {
            "target_id": data.get("id"),
            "raw_url": data.get("url") or open_url,
            "control_mode": "cdp",
        }
    except Exception as exc:
        logger.warning("Could not create tab (%s): %s", open_url, exc)
        return None


def _live_page_target_url(base: str, target_id: str) -> Optional[str]:
    """Return the live URL of *target_id*, or None when that exact target is
    no longer a live page target (tab closed, Chrome restarted).

    A tab may only be captured while its tracked targetId still exists. If
    the check itself fails (CDP momentarily unreachable) the answer is None:
    refusing to capture is always safe; guessing never is.
    """
    try:
        response = requests.get(
            f"{base.rstrip('/')}/json/list", timeout=_CDP_ACTION_TIMEOUT
        )
        targets = response.json()
    except Exception:
        return None
    if not isinstance(targets, list):
        return None
    for target in targets:
        if not isinstance(target, dict):
            continue
        if target.get("id") == target_id and target.get("type") == "page":
            return target.get("url") or ""
    return None


def _playwright_page_for_target(browser, target_id: str):
    """Resolve the ONE Playwright page whose CDP targetId is *target_id*.

    Matching is exact-by-targetId only - never by title, URL or "first
    non-chrome page" - so two same-title tabs can never be confused and no
    other page is ever touched. Returns None when no page matches.
    """
    for context in getattr(browser, "contexts", None) or []:
        for page in getattr(context, "pages", None) or []:
            session = None
            try:
                session = context.new_cdp_session(page)
                info = session.send("Target.getTargetInfo") or {}
                target_info = info.get("targetInfo") or {}
                if target_info.get("targetId") == target_id:
                    return page
            except Exception:
                logger.debug("Could not resolve a page targetId", exc_info=True)
            finally:
                if session is not None:
                    try:
                        session.detach()
                    except Exception:
                        pass
    return None


def screenshot_tab(cdp_url: str, target_id: str, out_path: str,
                   raw_url: str = "") -> bool:
    """Capture a REAL screenshot of the ONE exact tracked tab to *out_path*.

    The tab is identified ONLY by its CDP ``targetId`` (``raw_url`` is kept
    for call compatibility and is never used to pick a target). When the
    exact target is gone or cannot be captured directly, this captures
    NOTHING and returns False so the caller reports an honest failure
    instead of pretending some other image is this tab.
    """
    base = (cdp_url or "").rstrip("/")
    if not base or not target_id:
        return False
    # The tracked target must still be a live page target RIGHT NOW. If it
    # vanished (tab closed / Chrome restarted) nothing else may be captured.
    if _live_page_target_url(base, target_id) is None:
        logger.info(
            "Tracked tab target %s is no longer a live page target; "
            "capturing nothing", target_id,
        )
        return False
    # 1) Native CDP page screenshot endpoint (exact targetId; Chrome M125+).
    try:
        response = requests.get(
            f"{base}/json/{target_id}/screenshot", timeout=15.0
        )
        if response.status_code == 200:
            data = response.json()
            encoded = data.get("data") or data.get("screenshot") or ""
            if encoded:
                png = base64.b64decode(encoded)
                with open(out_path, "wb") as handle:
                    handle.write(png)
                logger.info(
                    "Captured tab %s via CDP screenshot endpoint", target_id)
                return True
    except Exception:
        logger.debug("CDP screenshot endpoint unavailable", exc_info=True)
    # 2) Playwright connect_over_cdp (attaches; never relaunches Chrome).
    #    The page is matched by its EXACT targetId only - never by title,
    #    URL prefix or "first non-chrome page".
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(base)
            try:
                page = _playwright_page_for_target(browser, target_id)
                if page is None:
                    logger.info(
                        "No Playwright page matches target %s; capturing "
                        "nothing", target_id,
                    )
                    return False
                page.screenshot(path=out_path, timeout=15000)
                logger.info("Captured tab %s via Playwright CDP", target_id)
                return True
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as exc:
        logger.warning(
            "Playwright tab screenshot failed for %s: %s", target_id, exc)
        return False