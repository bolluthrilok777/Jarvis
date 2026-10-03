"""Windows UI Automation fallback for REAL Chrome tab control.

Used when the user's ALREADY-RUNNING Chrome does not expose a DevTools/CDP
endpoint (i.e. it was not launched with ``--remote-debugging-port``). Instead
of stopping at "tabs not controllable", this module inspects the real Chrome
tab strip through Windows UI Automation (pywinauto UIA backend):

* every visible ``TabItem`` control of every real Chrome top-level window
* identity = Chrome HWND + UIA runtime-id + tab index + title
  (title alone is NEVER an identity)
* exact tab focus via SelectionItem ``Select()``, verified afterwards
* exact tab close preferring each TabItem's own ``Close`` button child,
  falling back to selecting the tab first and sending Ctrl+W as
  controlled window-specific input - NEVER WM_CLOSE to the Chrome HWND
  and NEVER any process-wide kill.

Every call is bounded by deadlines and fails honestly instead of guessing.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("jarvis.resources.uia")

#: Window classes that are real Chrome top-level frames.
_CHROME_WINDOW_CLASSES = {"Chrome_WidgetWin_1"}
_CHROME_PROCESS_NAMES = {"chrome.exe", "chrome"}

#: Upper bound for one UIA discovery round-trip (seconds).
_UIA_DEADLINE_SECONDS = 8.0
#: How long to wait for a tab to actually become selected after Select().
_ACTIVATE_SETTLE_SECONDS = 2.0


class UIATabError(RuntimeError):
    """Raised when the tab strip cannot be safely identified or acted upon."""


# ── Host windows ─────────────────────────────────────────────────────────────


def chrome_host_windows() -> list[dict]:
    """REAL visible Chrome top-level windows (hwnd/title/pid), nothing else."""
    from jarvis_core.windows_resource_manager import enumerate_windows

    hosts: list[dict] = []
    for window in enumerate_windows() or []:
        process = (window.get("process_name") or "").lower()
        cls = window.get("class_name") or ""
        if process not in _CHROME_PROCESS_NAMES:
            continue
        if cls not in _CHROME_WINDOW_CLASSES:
            continue
        hosts.append({
            "hwnd": int(window["hwnd"]),
            "title": (window.get("title") or "").strip(),
            "pid": window.get("pid"),
        })
    return hosts


# ── Discovery ────────────────────────────────────────────────────────────────


def _runtime_id(element) -> list[int]:
    try:
        return [int(x) for x in element.element_info.runtime_id or []]
    except Exception:
        return []


def _is_selected(element) -> bool:
    try:
        return bool(element.is_selected())
    except Exception:
        return False
def _attach(hwnd: int):
    from pywinauto import Desktop

    return Desktop(backend="uia").window(handle=int(hwnd))

def _find_tab_strip(window) -> Optional[Any]:
    """Locate the REAL Chrome tab strip container.

    Chrome's tab strip is the top-most control that DIRECTLY contains TabItem
    controls. In most Chrome versions that container is a Pane; in newer
    versions it is a TabControl (or another control type). We first scan Panes
    (exact legacy behavior); when no Pane matches we fall back to ANY control
    in the window tree whose direct children include TabItems, choosing the
    one with the MOST tab children (ties broken by widest rectangle, then by
    lowest top) - this keeps unrelated TabItems elsewhere (DevTools panels,
    dropdowns) from being mistaken for the strip.
    """
    def _children_tabitems(ctrl) -> list:
        try:
            return list(ctrl.children(control_type="TabItem"))
        except Exception:
            return []

    # 1) Precise pass: a Pane whose DIRECT children include TabItems.
    try:
        for pane in window.descendants(control_type="Pane"):
            if _children_tabitems(pane):
                return pane
    except Exception:
        pass

    # 2) Robust fallback: the REAL strip is exposed as a TabControl (or
    #    another non-Pane control) in newer Chrome builds. Prefer the control
    #    hosting the most direct TabItems, widest rectangle first (the strip
    #    spans the whole tab area; DevTools/side panels are narrower and often
    #    show fewer tabs).
    try:
        best = None
        best_score = (-1, -1, -1)  # (tab_count, width, -top)
        for ctrl in window.descendants():
            children = _children_tabitems(ctrl)
            if not children:
                continue
            try:
                rect = ctrl.rectangle()
                width = max(0, int(rect.right) - int(rect.left))
                top = int(rect.top)
            except Exception:
                width = -1
                top = -1
            score = (len(children), width, -top)
            if score > best_score:
                best, best_score = ctrl, score
        return best
    except Exception:
        pass
    return None

def enumerate_tabs(host_hwnd: Optional[int] = None) -> dict:
    """Enumerate the REAL visible tabs of running Chrome window(s).

    ``host_hwnd`` restricts discovery to one exact Chrome window; otherwise
    every visible Chrome frame contributes its tabs. Returns
    ``{"ok", "reason", "tabs"}``; each tab carries its full identity tuple
    (host hwnd + UIA runtime-id + index + title) and whether it is the
    currently selected tab of its window. Fails honestly - never guesses.
    """
    deadline = time.monotonic() + _UIA_DEADLINE_SECONDS
    try:
        import pywinauto  # noqa: F401 - availability probe
    except Exception as exc:  # pragma: no cover - depends on env
        return {
            "ok": False,
            "reason": f"UIA backend unavailable ({type(exc).__name__}).",
            "tabs": [],
        }

    hosts = chrome_host_windows()
    if host_hwnd is not None:
        hosts = [h for h in hosts if h["hwnd"] == int(host_hwnd)]
        if not hosts:
            return {
                "ok": False,
                "reason": "That Chrome window no longer exists.",
                "tabs": [],
            }
    elif not hosts:
        return {
            "ok": False,
            "reason": "No visible Chrome window is available for UIA inspection.",
            "tabs": [],
        }

    tabs: list[dict] = []
    failures: list[str] = []
    for host in hosts:
        hwnd = host["hwnd"]
        try:
            window = _attach(hwnd)
            # Find the actual tab strip container
            tab_strip = _find_tab_strip(window)
            if tab_strip is None:
                failures.append(f"Chrome window {hwnd} has no tab strip container")
                continue
            # Get TabItems ONLY from the tab strip, not the whole window
            items = tab_strip.children(control_type="TabItem")
            if not items:
                failures.append(f"Chrome window {hwnd} tab strip exposes no TabItem controls")
                continue
            for index, element in enumerate(items):
                title = (element.window_text() or "").strip()
                if not title:
                    continue
                tabs.append({
                    "host_hwnd": hwnd,
                    "title": title[:120],
                    "runtime_id": _runtime_id(element),
                    "tab_index": index,
                    "selected": _is_selected(element),
                    "control_mode": "uia",
                })
        except Exception as exc:
            failures.append(f"Chrome window {hwnd}: {type(exc).__name__}")
        if time.monotonic() > deadline:
            failures.append("UIA enumeration exceeded its deadline")
            break

    if tabs:
        return {"ok": True, "reason": "", "tabs": tabs}
    return {
        "ok": False,
        "reason": "; ".join(failures) or "The Chrome tab strip could not be read.",
        "tabs": [],
    }

def _find_element(window, runtime_id: list[int], title: str, index: int):
    """Resolve ONE exact TabItem from the full identity tuple.

    Strongest match wins: exact UIA runtime-id first; then a UNIQUE title
    match whose position is adjacent to the recorded index. Title alone is
    never sufficient. Raises :class:`UIATabError` instead of guessing.
    """
    items = window.descendants(control_type="TabItem")
    if runtime_id:
        wanted = [int(x) for x in runtime_id]
        for position, element in enumerate(items):
            if _runtime_id(element) == wanted:
                return element, position, len(items)
    candidates = []
    for position, element in enumerate(items):
        if ((element.window_text() or "").strip()
                == (title or "").strip()):
            candidates.append((position, element))
    if len(candidates) == 1 and abs(candidates[0][0] - int(index)) <= 1:
        return candidates[0][1], candidates[0][0], len(items)
    raise UIATabError(
        "Could not safely identify that tab in the current Chrome tab strip "
        "(the strip changed since the last scan - refresh and retry)."
    )


def _wait_until_selected(element, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if _is_selected(element):
            return True
        time.sleep(0.1)
    return _is_selected(element)


# ── Actions ──────────────────────────────────────────────────────────────────


def focus_tab(host_hwnd: int, runtime_id: list[int], title: str,
              tab_index: int) -> bool:
    """Select EXACTLY this tab and verify the selection really moved."""
    try:
        window = _attach(host_hwnd)
        element, _position, _count = _find_element(
            window, runtime_id, title, tab_index
        )
        element.select()
        if _wait_until_selected(
            element, time.monotonic() + _ACTIVATE_SETTLE_SECONDS
        ):
            logger.info("UIA focused tab '%s' in Chrome window %s", title, host_hwnd)
            return True
        logger.warning(
            "UIA select() did not move selection for tab '%s' in %s", title, host_hwnd
        )
        return False
    except UIATabError:
        raise
    except Exception as exc:
        logger.warning("UIA focus_tab failed for %s: %s", title, exc)
        return False


def read_tab_url(host_hwnd: int, runtime_id: list[int], *,
                 title: str = "", tab_index: int = -1) -> dict:
    """Read the URL of EXACTLY the tab identified by *runtime_id* in the given
    Chrome window, safely and fail-closed.

    Flow:
      1. attach to the exact window (must still be a real Chrome host);
      2. resolve the EXACT ``TabItem`` by runtime-id (never a title guess) —
         if it is gone, return ``code="TAB_IDENTITY_STALE"``;
      3. select that exact tab and VERIFY the selection moved to it;
      4. bring THAT window to the foreground (never type into another window);
      5. save the clipboard, Ctrl+L (focus address bar) → Ctrl+C (copy URL),
         Escape (leave the address bar), then RESTORE the clipboard;
      6. return the URL bound to that runtime-id, or
         ``code="TAB_URL_UNAVAILABLE"`` when nothing could be positively read.

    Returns ``{"ok": bool, "url": str, "code": str, "reason": str}``. Never
    raises for the expected failure modes (they are reported as codes). This
    reads only; it changes the active tab of the SELECTED window (required to
    read its address bar) but never navigates, never opens/closes a tab, and
    restores the clipboard.
    """
    out = {"ok": False, "url": "", "code": "", "reason": ""}
    # The window must still be one of THIS machine's real Chrome frames.
    hosts = {h["hwnd"] for h in chrome_host_windows()}
    if int(host_hwnd) not in hosts:
        out["code"] = "TAB_IDENTITY_STALE"
        out["reason"] = "Chrome window no longer exists."
        return out
    try:
        window = _attach(host_hwnd)
    except Exception as exc:
        out["code"] = "TAB_IDENTITY_STALE"
        out["reason"] = f"attach failed ({type(exc).__name__})."
        return out

    # Resolve the EXACT tab by runtime-id only (no title fallback for reads —
    # a title guess could read the wrong tab's URL).
    element = None
    try:
        wanted = [int(x) for x in (runtime_id or [])]
        for el in window.descendants(control_type="TabItem"):
            if wanted and _runtime_id(el) == wanted:
                element = el
                break
    except Exception as exc:
        out["code"] = "TAB_IDENTITY_STALE"
        out["reason"] = f"tab enumeration failed ({type(exc).__name__})."
        return out
    if element is None:
        out["code"] = "TAB_IDENTITY_STALE"
        out["reason"] = "The exact tab (runtime-id) is no longer present."
        return out

    # Select the exact tab and confirm the selection actually moved.
    try:
        element.select()
    except Exception as exc:
        out["code"] = "TAB_IDENTITY_STALE"
        out["reason"] = f"select failed ({type(exc).__name__})."
        return out
    if not _wait_until_selected(element, time.monotonic() + _ACTIVATE_SETTLE_SECONDS):
        out["code"] = "TAB_IDENTITY_STALE"
        out["reason"] = "The exact tab could not be made active."
        return out

    # Foreground THAT window (never read from another window).
    from jarvis_core.windows_resource_manager import foreground_window
    try:
        window.set_focus()
    except Exception:
        pass
    if foreground_window() != int(host_hwnd):
        out["code"] = "TAB_URL_UNAVAILABLE"
        out["reason"] = "Selected Chrome window is not foreground; URL not read."
        return out

    # Save clipboard, read the address bar, restore clipboard.
    #
    # SAFETY: a bare Ctrl+C can leave the PREVIOUS clipboard contents intact
    # (e.g. the address bar had no selection, or focus did not land there),
    # which would make stale clipboard text masquerade as a URL. To prevent
    # that we first write a unique SENTINEL to the clipboard; the read is only
    # trusted if the clipboard changed AWAY from the sentinel to something that
    # actually looks like a URL. Otherwise we fail closed.
    import uuid
    sentinel = f"__JARVIS_URL_SENTINEL__{uuid.uuid4().hex}__"
    prev_clip = None
    copied = ""
    try:
        try:
            import pyperclip
            prev_clip = pyperclip.paste()
            pyperclip.copy(sentinel)
        except Exception:
            prev_clip = None
        window.type_keys("^l")          # focus + select the address bar
        time.sleep(0.15)
        window.type_keys("^a")          # ensure the full URL is selected
        time.sleep(0.05)
        window.type_keys("^c")          # copy the selected URL
        time.sleep(0.2)
        try:
            import pyperclip
            copied = (pyperclip.paste() or "").strip()
        except Exception:
            copied = ""
        # Leave the address bar without changing the page.
        try:
            window.type_keys("{ESC}")
        except Exception:
            pass
    finally:
        if prev_clip is not None:
            try:
                import pyperclip
                pyperclip.copy(prev_clip)
            except Exception:
                pass

    # The copy must have changed the clipboard away from the sentinel.
    if not copied or copied == sentinel:
        out["code"] = "TAB_URL_UNAVAILABLE"
        out["reason"] = "Address bar produced no copyable URL (clipboard unchanged)."
        return out
    url = copied
    if "://" not in url and not url.startswith("about:"):
        url = "https://" + url
    # POSITIVELY validate it looks like a URL (single-line, valid scheme/host)
    # so stale clipboard text can never be mistaken for a URL.
    if not _looks_like_url(url):
        out["code"] = "TAB_URL_UNAVAILABLE"
        out["reason"] = "Copied clipboard value is not a valid URL."
        return out
    out["ok"] = True
    out["url"] = url
    return out


def _looks_like_url(value: str) -> bool:
    """True only for a single-line string that parses as an http(s)/about URL
    with a hostname (http/https). Rejects multi-line or free text (guards
    against stale clipboard contents being mistaken for a URL)."""
    from urllib.parse import urlsplit
    s = (value or "").strip()
    if not s or "\n" in s or "\r" in s or len(s) > 2048:
        return False
    if s.startswith("about:"):
        return True
    try:
        p = urlsplit(s)
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    return bool(p.hostname)


def close_tab(host_hwnd: int, runtime_id: list[int], title: str,
              tab_index: int) -> dict:
    """Close EXACTLY one tab; verify the strip shrank by exactly one.

    Preferred strategy: invoke THIS tab's own ``Close`` button child. Fallback:
    select the tab, verify it is active, then send Ctrl+W as controlled input
    to that specific window. WM_CLOSE to the Chrome HWND and any process-wide
    termination are never used.
    """
    outcome = {"ok": False, "verified": False, "reason": "", "method": ""}
    try:
        window = _attach(host_hwnd)
        element, _position, count_before = _find_element(
            window, runtime_id, title, tab_index
        )

        # 1) Per-tab Close button (safest - scoped to exactly this tab).
        close_button = None
        try:
            for child in element.children(control_type="Button"):
                label = (child.window_text() or "").strip().lower()
                if label in ("close", "close tab"):
                    close_button = child
                    break
        except Exception:
            close_button = None
        if close_button is not None:
            close_button.invoke()
            outcome["method"] = "uia_close_button"
        else:
            # 2) Select the exact tab, VERIFY active, then Ctrl+W on that
            #    specific window (never global keys, never WM_CLOSE).
            element.select()
            if not _wait_until_selected(
                element, time.monotonic() + _ACTIVATE_SETTLE_SECONDS
            ):
                outcome["reason"] = (
                    "Tab could not be activated first; close was NOT attempted."
                )
                return outcome
            from jarvis_core.windows_resource_manager import foreground_window

            if foreground_window() != int(host_hwnd):
                outcome["reason"] = (
                    "Chrome did not take foreground; close was NOT attempted."
                )
                return outcome
            window.set_focus()
            window.type_keys("^w")
            outcome["method"] = "ctrl_w_active_tab"

        # Verify: same window alive AND tab count decreased by exactly one.
        # Use the tab strip container to avoid counting unrelated TabItem controls.
        deadline = time.monotonic() + 3.0
        count_after = count_before
        while time.monotonic() < deadline:
            try:
                win = _attach(host_hwnd)
                tab_strip = _find_tab_strip(win)
                if tab_strip is None:
                    count_after = 0
                    break
                count_after = len(tab_strip.children(control_type="TabItem"))
            except Exception:
                # If the whole window vanished, more than one tab changed;
                # treat as unverified rather than success.
                outcome["reason"] = "Chrome window disappeared during verification."
                return outcome
            if count_after < count_before:
                break
            time.sleep(0.1)
        from jarvis_core.windows_resource_manager import is_window_alive

        window_alive = is_window_alive(int(host_hwnd))
        outcome["ok"] = count_after < count_before
        outcome["verified"] = (
            outcome["ok"] and window_alive and (count_before - count_after) == 1
        )
        if not outcome["ok"]:
            outcome["reason"] = "The tab was still present after the close attempt."
        elif not outcome["verified"]:
            outcome["reason"] = "More than the selected tab appears to have changed."
        logger.info(
            "UIA close_tab '%s' (%s): ok=%s verified=%s %d->%d",
            title, outcome["method"], outcome["ok"], outcome["verified"],
            count_before, count_after,
        )
        return outcome
    except UIATabError as exc:
        outcome["reason"] = str(exc)
        return outcome
    except Exception as exc:
        logger.warning("UIA close_tab failed for %s: %s", title, exc)
        outcome["reason"] = f"UIA close failed ({type(exc).__name__})."
        return outcome


def create_tab(host_hwnd: int) -> dict:
    """Open EXACTLY ONE new tab in the given Chrome window and return its
    identity. Never overwrites an existing tab.

    Strategy: snapshot the window's current TabItem runtime-ids, bring THAT
    exact window to the foreground, send Ctrl+T (scoped to that window, never a
    global key), then re-enumerate and return the ONE new tab (the runtime-id
    that appeared). Fails honestly instead of guessing when the new tab cannot
    be uniquely resolved.

    Returns ``{"ok", "reason", "host_hwnd", "runtime_id", "tab_index",
    "title", "control_mode": "uia"}``.
    """
    out = {"ok": False, "reason": "", "host_hwnd": int(host_hwnd),
           "runtime_id": [], "tab_index": -1, "title": "", "control_mode": "uia"}
    try:
        import pywinauto  # noqa: F401 - availability probe
    except Exception as exc:  # pragma: no cover - depends on env
        out["reason"] = f"UIA backend unavailable ({type(exc).__name__})."
        return out

    def _strip_runtime_ids(win) -> list[list[int]]:
        strip = _find_tab_strip(win)
        if strip is None:
            return []
        ids = []
        for el in strip.children(control_type="TabItem"):
            rid = _runtime_id(el)
            if rid:
                ids.append(rid)
        return ids

    try:
        window = _attach(host_hwnd)
        before = _strip_runtime_ids(window)
        from jarvis_core.windows_resource_manager import foreground_window

        window.set_focus()
        if foreground_window() != int(host_hwnd):
            out["reason"] = "Chrome did not take foreground; new tab NOT created."
            return out
        window.type_keys("^t")

        deadline = time.monotonic() + 3.0
        new_rid = None
        new_index = -1
        while time.monotonic() < deadline:
            win = _attach(host_hwnd)
            strip = _find_tab_strip(win)
            if strip is not None:
                items = strip.children(control_type="TabItem")
                for index, el in enumerate(items):
                    rid = _runtime_id(el)
                    if rid and rid not in before:
                        new_rid = rid
                        new_index = index
                        try:
                            out["title"] = (el.window_text() or "").strip()[:120]
                        except Exception:
                            out["title"] = ""
                        break
            if new_rid is not None:
                break
            time.sleep(0.1)

        if new_rid is None:
            out["reason"] = "Could not resolve the newly created tab uniquely."
            return out
        out["ok"] = True
        out["runtime_id"] = new_rid
        out["tab_index"] = new_index
        logger.info("UIA created new tab in Chrome window %s (index %d)",
                    host_hwnd, new_index)
        return out
    except UIATabError as exc:
        out["reason"] = str(exc)
        return out
    except Exception as exc:
        logger.warning("UIA create_tab failed for %s: %s", host_hwnd, exc)
        out["reason"] = f"UIA create failed ({type(exc).__name__})."
        return out
