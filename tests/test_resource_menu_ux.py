"""Targeted UX tests: Telegram resource menu navigation (menu-only task).

Covers ONLY the five requested behaviours:
1. Current Running button set (icon'd apps + Chrome account rows + Refresh/Back)
2. Chrome account grouping with live tab counts
3. Tab icons (YouTube/Gmail/ChatGPT/New Tab/Jarvis/unknown)
4. Same-page navigation after Close (edit the SAME message, same window)
5. Contextual Refresh (Current Running refresh vs Refresh Tabs)

All backends are faked - no real windows, Chrome, or Telegram traffic.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from jarvis_core import resource_manager, uia_browser_manager
from jarvis_core.resource_manager import Resource
from jarvis_core.resource_menus import (
    build_current_running_menu,
    build_resource_review_home,
    build_window_tabs_menu,
)
from tests.test_telegram_integration import FakeAPI, _settings

EMAIL_1 = "gmail1@gmail.com"
EMAIL_2 = "gmail2@gmail.com"


def _tab(rid, hwnd, title, idx=0, email="", label=""):
    return {
        "resource_id": rid, "feature_id": None, "type": "browser_tab",
        "title": title, "display_name": "Chrome", "safe_display_target": "",
        "pid": 42, "window_handle": 0, "page_id": "", "executable": "",
        "control_mode": "uia", "host_hwnd": hwnd,
        "runtime_id": f"1,{hwnd},{idx}",
        "tab_index": idx, "opened_by_jarvis": False, "visible": True,
        "focused": False, "active": False, "status": "open",
        "close_allowed": True, "close_requires_confirmation": False,
        "screenshot_allowed": True, "discovered_at": "",
        "account_email": email, "profile_label": "", "identity_source": "uia",
        "identity_label": label or email,
    }


def _app(rid, name, hwnd):
    return {
        "resource_id": rid, "feature_id": None, "type": "application",
        "title": name, "display_name": name, "safe_display_target": "",
        "pid": 7, "window_handle": hwnd, "page_id": "",
        "executable": f"C:\\Apps\\{name}.exe", "control_mode": "",
        "host_hwnd": hwnd, "runtime_id": "", "tab_index": 0,
        "opened_by_jarvis": False, "visible": True, "focused": False,
        "active": False, "status": "open", "close_allowed": True,
        "close_requires_confirmation": False, "screenshot_allowed": True,
        "discovered_at": "", "account_email": "", "profile_label": "",
        "identity_source": "", "identity_label": "",
    }


def _scan(apps=(), tabs=()):
    return {
        "generation": 1, "feature_id": None,
        "applications": list(apps), "tabs": list(tabs),
        "chrome_detected": True, "chrome_controllable": True,
        "chrome_reason": "", "chrome_control_mode": "uia", "scanned_at": "",
    }


def _buttons(rows):
    return [b for row in rows for b in row]


def _flat_callbacks(rows):
    return [b["callback_data"] for b in _buttons(rows)]


# ── 1. Home menu buttons ─────────────────────────────────────────────────────


def test_home_menu_buttons():
    text, rows = build_resource_review_home("f1", _scan())
    datas = _flat_callbacks(rows)
    assert "feature:resrc_run:f1" in datas
    assert "feature:resrc_screen:f1" not in datas
    assert "feature:resrc_refresh:f1" not in datas
    assert "feature:resrc_continue:f1" in datas
    assert "feature:resrc_cancel:f1" in datas
    texts = [b["text"] for b in _buttons(rows)]
    assert "\U0001f9e9 Current Running" in texts
    assert "\U0001f4f8 Current Screen" not in texts
    assert "\U0001f504 Refresh" not in texts
    assert "\u25b6\ufe0f Continue Feature" in texts
    assert "\u274c Cancel" in texts


# ── 2. Current Running: Chrome accounts with counts + app icons ──────────────


def test_current_running_menu_buttons():
    scan = _scan(
        apps=[_app("r1-a1", "Antigravity", 10),
              _app("r1-a2", "WhatsApp", 11),
              _app("r1-a3", "Notepad", 12),
              _app("r1-a4", "Terminal", 13)],
        tabs=[_tab("r1-t1", 200, "YouTube", 0, email=EMAIL_1),
              _tab("r1-t2", 200, "Gmail", 1, email=EMAIL_1),
              _tab("r2-t1", 300, "ChatGPT", 0, email=EMAIL_2)],
    )
    text, rows = build_current_running_menu("f1", scan)
    assert "CURRENT RUNNING" in text
    # Collapsed: the list reads as group COUNTS, not individual rows.
    combined = text + " " + " ".join(b["text"] for b in _buttons(rows))
    assert "Applications (4)" in combined
    assert "Chrome (3)" in combined  # 3 real tabs
    # Refresh and Back each sit on their OWN row.
    datas = _flat_callbacks(rows)
    assert any("resrc_expand_apps" in d for d in datas)
    assert any("resrc_expand_chrome" in d for d in datas)
    # Nav callbacks are feature-id-free to stay within Telegram's 64-byte
    # callback_data limit (the handler resolves the feature from context).
    assert "feature:resrc_refresh_run:" in datas
    assert "feature:resrc_home:" in datas
    refresh_rows = [row for row in rows if any(
        b["callback_data"] == "feature:resrc_refresh_run:" for b in row)]
    back_rows = [row for row in rows if any(
        b["callback_data"] == "feature:resrc_home:" for b in row)]
    assert len(refresh_rows) == 1 and len(refresh_rows[0]) == 1
    assert len(back_rows) == 1 and len(back_rows[0]) == 1
    # Collapsed: NO account/profile rows, NO per-window / per-app rows.
    texts = [b["text"] for b in _buttons(rows)]
    assert not any(EMAIL_1 in t or EMAIL_2 in t for t in texts), \
        "no per-account/profile rows in the collapsed menu"
    assert not any("resrc_wtabs" in d for d in datas), \
        "no drill-down window rows in the collapsed menu"
    assert not any("resrc_show" in d for d in datas), \
        "no per-app detail rows in the collapsed menu"


# ── 3. Chrome account menu shows ONLY that window's tabs ─────────────────────


def test_chrome_account_menu_shows_only_that_windows_tabs():
    scan = _scan(
        tabs=[_tab("r1-t1", 200, "YouTube", 0, email=EMAIL_1),
              _tab("r1-t2", 200, "Gmail", 1, email=EMAIL_1),
              _tab("r2-t1", 300, "ChatGPT", 0, email=EMAIL_2)],
    )
    built = build_window_tabs_menu("f1", scan, 200, 0)
    assert built is not None
    text, rows = built
    assert f"\U0001f310 {EMAIL_1}" in text          # header: the account
    assert "ChatGPT" not in text                    # other window excluded
    datas = _flat_callbacks(rows)
    assert "feature:resrc_tab_show:r1-t1" in datas
    assert "feature:resrc_tab_show:r1-t2" in datas
    assert not any("r2-t1" in d for d in datas)


# ── 4. Tab icons + Refresh Tabs / Back rows ──────────────────────────────────


def test_tab_icons_and_refresh_tabs_row():
    scan = _scan(tabs=[
        _tab("r1-t1", 200, "YouTube - Mix", 0, email=EMAIL_1),
        _tab("r1-t2", 200, "Inbox (9) - Gmail", 1, email=EMAIL_1),
        _tab("r1-t3", 200, "ChatGPT", 2, email=EMAIL_1),
        _tab("r1-t4", 200, "New Tab", 3, email=EMAIL_1),
        _tab("r1-t5", 200, "Jarvis dashboard", 4, email=EMAIL_1),
        _tab("r1-t6", 200, "Totally unknown site", 5, email=EMAIL_1),
    ])
    text, rows = build_window_tabs_menu("f1", scan, 200, 0)
    texts = [b["text"] for b in _buttons(rows)]
    assert "\u25b6\ufe0f YouTube - Mix" in texts          # YouTube
    assert "\U0001f4e7 Inbox (9) - Gmail" in texts        # Gmail
    assert "\U0001f916 ChatGPT" in texts                  # ChatGPT
    assert "\u2795 New Tab" in texts                      # New Tab
    assert "\U0001f3e0 Jarvis dashboard" in texts         # Jarvis
    assert "\U0001f310 Totally unknown site" in texts     # unknown
    assert "feature:resrc_refresh_tabs:200@0" in _flat_callbacks(rows)
    assert "feature:resrc_run:f1" in _flat_callbacks(rows)  # Back


# ── Telegram-level harness for close / refresh navigation ────────────────────


def _seed(registry, tabs_spec, apps_spec=()):
    """Seed the shared registry like a real UIA scan (identity attached)."""
    registry._generation = 1
    registry._feature_id = None
    registry._cdp_url = None
    registry._chrome_detected = True
    registry._chrome_controllable = True
    registry._chrome_reason = ""
    registry._chrome_control_mode = "uia"
    registry._last_scan_at = 0.0
    resources = {}
    for rid, title, idx, email, hwnd in tabs_spec:
        res = Resource(
            resource_id=rid, feature_id=None, resource_type="browser_tab",
            title=title, display_name="Chrome", control_mode="uia",
            host_hwnd=hwnd, runtime_id=f"42,{hwnd},{idx}", tab_index=idx,
        )
        res.account_email = email
        res.identity_label = email
        resources[rid] = res
    for rid, name, hwnd in apps_spec:
        resources[rid] = Resource(
            resource_id=rid, feature_id=None, resource_type="application",
            title=name, display_name=name, window_handle=hwnd,
            close_requires_confirmation=False,
        )
    registry._resources = resources


def _make_service(monkeypatch, scan_counter):
    """Service + FakeAPI whose registry rescan mimics a real re-discovery:
    only still-open resources survive, so closed tabs really disappear."""
    from jarvis_core.telegram_service import TelegramService

    reg = resource_manager.resource_registry

    def _fake_scan(feature_id=None, cdp_url=None, include_identity=False,
                   read_only=False):
        scan_counter.append(1)
        reg._resources = {
            rid: r for rid, r in reg._resources.items() if r.status == "open"}
        return {"generation": reg._generation, "feature_id": feature_id,
                "applications": [], "tabs": [], "chrome_detected": True,
                "chrome_controllable": True, "chrome_reason": "",
                "chrome_control_mode": "uia", "scanned_at": ""}

    monkeypatch.setattr(reg, "scan", _fake_scan)
    api = FakeAPI()
    service = TelegramService(settings=_settings(123), api=api)
    service._start_task_watcher = Mock()
    return service, api


def _cb(cb_id, user_id, data, chat_id=100, message_id=777):
    return {
        "update_id": 10,
        "callback_query": {
            "id": cb_id,
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}, "message_id": message_id},
            "data": data,
        },
    }


@pytest.fixture(autouse=True)
def _reset_registry():
    reg = resource_manager.resource_registry
    yield
    reg._generation = 0
    reg._resources = {}
    reg._feature_id = None


# ── 5. Close: SAME message, SAME Chrome window, closed tab removed ───────────


def test_close_stays_in_same_chrome_window_same_message(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed(reg, [("r1-t1", "Docs", 0, EMAIL_1, 555),
                ("r1-t2", "Keep", 1, EMAIL_1, 555),
                ("r2-t1", "Other", 0, EMAIL_2, 777)])
    monkeypatch.setattr(
        uia_browser_manager, "close_tab",
        lambda hwnd, rtid, title, idx:
            {"ok": True, "verified": True, "method": "uia_close_button"},
    )
    service._feature_orchestrator = Mock()
    service.process_update(_cb("cbC1", 123, "feature:resrc_close:r1-t1"))

    # Toast, then the SAME message was edited (no new menu message).
    assert ("cbC1", "\u274c Closing...") in api.callbacks
    assert len(api.edits) == 1
    edit = api.edits[0]
    assert edit["message_id"] == 777
    # No "Closed tab" success banner in the body - the panel becomes the
    # refreshed remaining-tabs view directly (feedback is a callback toast).
    assert "\u2705 Closed tab" not in edit["text"]
    kb_texts = [b["text"] for row in (edit["reply_markup"]["inline_keyboard"]
                                      if edit["reply_markup"] else []) for b in row]
    assert any("Keep" in t for t in kb_texts), \
        "the remaining tab is listed as an inline button"
    assert not any("Docs" in t for t in kb_texts), \
        "the closed tab no longer appears as an inline button"
    kb = _flat_callbacks(
        edit["reply_markup"]["inline_keyboard"] if edit["reply_markup"] else [])
    assert "feature:resrc_tab_actions:r1-t2" in kb, \
        "remaining tab shows its inline actions (stays in the account view)"
    assert not any("r1-t1" in d for d in kb), "closed tab callback absent"
    # New account-grouped UX: after closing a tab we stay INSIDE the same
    # Gmail/profile account view (Back returns to the account list); we never
    # fall back to All Apps.
    assert any("resrc_expand_chrome" in d for d in kb), \
        "account tabs view Back returns to the Chrome account list"
    assert not any("resrc_allapps" in d for d in kb)
    assert scan_counter, "a rescan happened so the list reflects reality"


def test_focus_stays_in_same_chrome_window_same_message(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed(reg, [("r1-t1", "Docs", 0, EMAIL_1, 555),
                ("r1-t2", "Keep", 1, EMAIL_1, 555)])
    monkeypatch.setattr(
        uia_browser_manager, "focus_tab",
        lambda hwnd, rtid, title, idx: True,
    )
    service._feature_orchestrator = Mock()
    service.process_update(_cb("cbF1", 123, "feature:resrc_focus:r1-t1"))

    assert ("cbF1", "\U0001f441 Focusing...") in api.callbacks
    assert len(api.edits) == 1
    edit = api.edits[0]
    assert edit["message_id"] == 777
    kb_texts = [b["text"] for row in (edit["reply_markup"]["inline_keyboard"]
                                      if edit["reply_markup"] else []) for b in row]
    assert any("Docs" in t for t in kb_texts), \
        "the focused tab is listed as an inline button"
    kb = _flat_callbacks(
        edit["reply_markup"]["inline_keyboard"] if edit["reply_markup"] else [])
    assert "feature:resrc_tab_actions:r1-t1" in kb, \
        "menu opens the focused tab's inline action panel"
    assert "feature:resrc_focus:r1-t1" in kb
    assert "feature:resrc_shot:r1-t1" in kb
    assert "feature:resrc_close:r1-t1" in kb
    assert "feature:resrc_edit:r1-t1" in kb
    # Hide Actions returns to the account view (account-grouped UX).
    assert any("resrc_hide_tab_actions" in d or "resrc_chrome_account:" in d
               for d in kb)
    assert not any("resrc_allapps" in d for d in kb)


# ── 6. Contextual refresh: each menu refreshes only itself ───────────────────


def test_refresh_run_rerenders_current_running_only(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed(reg, [("r1-t1", "Docs", 0, EMAIL_1, 555),
                ("r2-t1", "Other", 0, EMAIL_2, 777)],
          apps_spec=[("r1-a1", "Notepad", 11)])
    before = len(scan_counter)
    service._feature_orchestrator = Mock()
    service.process_update(_cb("cbR1", 123, "feature:resrc_refresh_run:"))

    assert ("cbR1", "\u23f3 Refreshing...") in api.callbacks
    assert len(api.edits) == 1
    assert len(scan_counter) == before + 1, "refresh re-scans exactly once"
    edit = api.edits[0]
    assert "CURRENT RUNNING" in edit["text"]
    assert "FEATURE RESOURCE REVIEW" not in edit["text"]
    texts = [b["text"] for b in _buttons(
        edit["reply_markup"]["inline_keyboard"])]
    assert any("Applications (1)" in t for t in texts), \
        "collapsed: one app count"
    assert any("Chrome (2)" in t for t in texts), \
        "collapsed: two Chrome windows count"
    kb = _flat_callbacks(
        edit["reply_markup"]["inline_keyboard"] if edit["reply_markup"] else [])
    assert any("resrc_expand_apps" in d for d in kb)
    assert any("resrc_expand_chrome" in d for d in kb)
    assert any("resrc_refresh_run" in d for d in kb)
    assert not any("resrc_wtabs" in d for d in kb)
    assert not any("resrc_show" in d for d in kb)


def test_refresh_tabs_rerenders_same_window_only(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed(reg, [("r1-t1", "Alpha", 0, EMAIL_1, 555),
                ("r1-t2", "Beta", 1, EMAIL_1, 555),
                ("r2-t1", "Gamma", 0, EMAIL_2, 777)])
    before = len(scan_counter)
    service._feature_orchestrator = Mock()
    service.process_update(
        _cb("cbR2", 123, "feature:resrc_refresh_tabs:555@0"))

    assert ("cbR2", "\u23f3 Refreshing...") in api.callbacks
    assert len(api.edits) == 1
    assert len(scan_counter) == before + 1, "Refresh Tabs re-scans once"
    edit = api.edits[0]
    assert EMAIL_1 in edit["text"]
    assert "Alpha" in edit["text"] and "Beta" in edit["text"]
    assert "Gamma" not in edit["text"], "other window stays out"
    assert "FEATURE RESOURCE REVIEW" not in edit["text"]
    kb = _flat_callbacks(
        edit["reply_markup"]["inline_keyboard"] if edit["reply_markup"] else [])
    assert "feature:resrc_refresh_tabs:555@0" in kb
    assert "feature:resrc_tab_show:r1-t1" in kb
    assert "feature:resrc_tab_show:r1-t2" in kb
    assert not any("r2-t1" in d for d in kb)