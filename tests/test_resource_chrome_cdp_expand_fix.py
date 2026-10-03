"""Regression: expanding Chrome must list CDP tabs (no host_hwnd) and show
their per-tab action sub-options through the REAL callback dispatch path.

Reported bug: tapping "Chrome (N)" showed the count but no tab rows and no
Focus / Screenshot / Close options. CDP-discovered tabs have a page id but no
Windows ``host_hwnd``; the expanded-Chrome builder used to drop every hwnd-less
tab. This test drives the actual Telegram callback handler end-to-end.
"""
from __future__ import annotations

from unittest.mock import Mock

import jarvis_core.resource_manager as resource_manager
from jarvis_core.resource_manager import Resource
from jarvis_core.telegram_service import TelegramService
from tests.test_telegram_integration import FakeAPI, _settings


def _seed_cdp_tabs(registry, n=3):
    """Seed real CDP Chrome tabs (page ids, NO host_hwnd) in generation 1."""
    registry._generation = 1
    registry._feature_id = None
    registry._cdp_url = "http://127.0.0.1:9222"
    registry._chrome_detected = True
    registry._chrome_controllable = True
    registry._chrome_reason = ""
    registry._chrome_control_mode = "cdp"
    registry._last_scan_at = 0.0
    resources = {}
    for i in range(n):
        rid = f"r1-t{i}"
        resources[rid] = Resource(
            resource_id=rid, feature_id=None, resource_type="browser_tab",
            title=f"CDP Tab {i}", display_name="Chrome",
            safe_display_target=f"site-{i}.example",
            page_id=f"PAGE{i}", executable="chrome.exe",
            control_mode="cdp", tab_index=i,   # NOTE: no host_hwnd
        )
    registry._resources = resources


def _make_service():
    api = FakeAPI()
    service = TelegramService(settings=_settings(123), api=api)
    service._start_task_watcher = Mock()
    service._feature_orchestrator = Mock()
    return service, api


def _cb(cb_id, data, user_id=123, chat_id=100, message_id=777):
    return {
        "update_id": 10,
        "callback_query": {
            "id": cb_id,
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}, "message_id": message_id},
            "data": data,
        },
    }


def _kb(edit):
    rm = edit.get("reply_markup") or {}
    return [b["callback_data"]
            for row in rm.get("inline_keyboard", []) for b in row]


def test_expand_chrome_lists_cdp_tabs_via_handler(monkeypatch):
    service, api = _make_service()
    _seed_cdp_tabs(resource_manager.resource_registry, n=3)

    service.process_update(_cb("cb1", "feature:resrc_expand_chrome:"))

    assert api.edits, "expanding Chrome edits the SAME panel"
    edit = api.edits[-1]
    kb = _kb(edit)
    # New UX: Chrome opens the account list. These CDP tabs have no resolvable
    # account identity, so they group under a single "Other / Unknown" account
    # (never dropped). Clicking it reveals all 3 tabs.
    assert "CHROME" in edit["text"]
    acct = [c for c in kb if "resrc_chrome_account:" in c]
    assert len(acct) == 1, "one Other/Unknown account group for CDP tabs"
    service.process_update(_cb("cb2", acct[0]))
    kb2 = _kb(api.edits[-1])
    for i in range(3):
        assert any(f"resrc_tab_actions:r1-t{i}" in cb for cb in kb2), (
            f"CDP tab r1-t{i} must be listed under its account")


def test_tap_cdp_tab_shows_action_suboptions_via_handler(monkeypatch):
    service, api = _make_service()
    _seed_cdp_tabs(resource_manager.resource_registry, n=3)

    # Expand Chrome -> open the (Other/Unknown) account -> tap ONE tab.
    service.process_update(_cb("cb1", "feature:resrc_expand_chrome:"))
    acct = next(c for c in _kb(api.edits[-1]) if "resrc_chrome_account:" in c)
    service.process_update(_cb("cb2", acct))
    service.process_update(_cb("cb3", "feature:resrc_tab_actions:r1-t1"))

    edit = api.edits[-1]
    kb = _kb(edit)
    # Focus / Screenshot / Close target the exact tab.
    assert any("resrc_focus:r1-t1" in cb for cb in kb)
    assert any("resrc_shot:r1-t1" in cb for cb in kb)
    assert any("resrc_close:r1-t1" in cb for cb in kb)


# ── Regression: keyboard-only edits must not spam a new message ──────────────


def test_edit_message_treats_not_modified_as_success():
    """Telegram '400 message is not modified' must be treated as a successful
    in-place edit, never a fallback that posts a brand-new panel."""
    from jarvis_core.telegram_bot_api import TelegramAPIError
    from jarvis_core.telegram_service import TelegramService
    from tests.test_telegram_integration import _settings

    class _NotModifiedAPI:
        def edit_message_text(self, chat_id, message_id, text, *,
                              reply_markup=None):
            raise TelegramAPIError(
                "Bad Request: message is not modified", status_code=400)

    service = TelegramService(settings=_settings(123), api=_NotModifiedAPI())
    ok = service._edit_message(100, 777, "same text",
                               reply_markup={"inline_keyboard": []})
    assert ok is True, "'not modified' is a successful no-op edit, not a resend"
