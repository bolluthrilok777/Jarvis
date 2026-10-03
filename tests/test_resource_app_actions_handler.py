"""Part 8C-2A-WIRE: the app-actions inline callbacks are wired in the handler.

``feature:resrc_app_actions:<rid>`` (tap ONE app in the expanded Applications
list) edits the SAME Telegram message and renders that app's action buttons
(Focus / Screenshot / Close / Edit / Hide Actions) inline while Applications
stays expanded; ``feature:resrc_hide_app_actions:<feature_id>`` edits the SAME
message back to the plain expanded Applications list with NO actions open.
No new Telegram messages are ever sent and no other resource-review behaviour
changes.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import jarvis_core.browser_resource_manager as browser_resource_manager
import jarvis_core.resource_manager as resource_manager
import jarvis_core.uia_browser_manager as uia_browser_manager
import jarvis_core.windows_resource_manager as windows_resource_manager
from jarvis_core.resource_manager import Resource
from jarvis_core.telegram_service import TelegramService
from tests.test_telegram_integration import FakeAPI, _settings

HWND_A = 111  # File Explorer
HWND_C = 333  # Windows Terminal


def _seed_apps(registry, apps_spec):
    registry._generation = 1
    registry._feature_id = None
    registry._cdp_url = None
    registry._chrome_detected = False
    registry._chrome_controllable = False
    registry._chrome_reason = ""
    registry._chrome_control_mode = ""
    registry._last_scan_at = 0.0
    resources = {}
    for rid, name, hwnd, risky in apps_spec:
        resources[rid] = Resource(
            resource_id=rid, feature_id=None, resource_type="application",
            title=name, display_name=name, window_handle=hwnd,
            close_requires_confirmation=risky,
        )
    registry._resources = resources


def _make_service(monkeypatch):
    api = FakeAPI()
    service = TelegramService(settings=_settings(123), api=api)
    service._start_task_watcher = Mock()
    service._feature_orchestrator = Mock()
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


def _kb(edit):
    return [b["callback_data"] for row in
            (edit["reply_markup"]["inline_keyboard"] if edit["reply_markup"]
             else [])
            for b in row]


def _seed_tabs(registry, tabs_spec):
    """Seed Chrome tab resources: (resource_id, title, hwnd, mode, page_id)."""
    registry._generation = 1
    registry._feature_id = None
    registry._cdp_url = "http://127.0.0.1:9222"
    registry._chrome_detected = True
    registry._chrome_controllable = True
    registry._chrome_reason = ""
    registry._chrome_control_mode = "cdp"
    registry._last_scan_at = 0.0
    resources = {}
    for rid, title, hwnd, mode, page_id in tabs_spec:
        resources[rid] = Resource(
            resource_id=rid, feature_id=None, resource_type="browser_tab",
            title=title, display_name="Chrome", window_handle=hwnd,
            host_hwnd=hwnd, control_mode=mode, page_id=page_id,
            runtime_id="4,5,6" if mode == "uia" else "",
            tab_index=0 if mode == "uia" else -1,
        )
    registry._resources = resources


@pytest.fixture(autouse=True)
def _reset_registry():
    reg = resource_manager.resource_registry
    yield
    reg._generation = 0
    reg._resources = {}
    reg._feature_id = None


def test_app_actions_callback_expands_selected_app_inline(monkeypatch):
    """Tapping one app edits the SAME message and shows THAT app's actions."""
    service, api = _make_service(monkeypatch)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", "File Explorer", HWND_A, False),
                     ("r1-a3", "Windows Terminal", HWND_C, False)])

    service.process_update(_cb("cbA1", 123, "feature:resrc_app_actions:r1-a1"))

    # SAME message edited in place - never a new Telegram message.
    assert api.messages == [], "no new message is sent"
    assert len(api.edits) == 1
    edit = api.edits[0]
    assert edit["message_id"] == 777

    # Applications stays expanded; the tapped app's actions appear inline as
    # BUTTONS (clean UI contract: names/actions live in the keyboard, not
    # duplicated into the message body). The selected app shows a small status
    # line in the body.
    assert "CURRENT RUNNING" in edit["text"]
    assert "File Explorer" in edit["text"]  # small selected status line
    kb = _kb(edit)
    kb_labels = [b["text"] for row in
                 (edit["reply_markup"]["inline_keyboard"] if edit["reply_markup"]
                  else []) for b in row]
    assert any("File Explorer" in t for t in kb_labels)
    assert any("Windows Terminal" in t for t in kb_labels)
    assert any("\U0001f3af Focus" in t for t in kb_labels)
    assert any("\U0001f4f8 Screenshot" in t for t in kb_labels)
    assert any("\u274c Close" in t for t in kb_labels)
    assert any("\u270f\ufe0f Edit" in t for t in kb_labels)
    assert any("\U0001f53c Hide Actions" in t for t in kb_labels)

    assert "feature:resrc_focus:r1-a1" in kb
    assert "feature:resrc_shot:r1-a1" in kb
    assert "feature:resrc_close:r1-a1" in kb
    assert "feature:resrc_edit:r1-a1" in kb
    assert any("resrc_hide_app_actions" in c for c in kb)
    # Only the tapped app carries the inline actions.
    assert "feature:resrc_focus:r1-a3" not in kb


def test_hide_app_actions_collapses_inline(monkeypatch):
    """Hide Actions edits the SAME message back to no open actions."""
    service, api = _make_service(monkeypatch)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", "File Explorer", HWND_A, False),
                     ("r1-a3", "Windows Terminal", HWND_C, False)])

    # Expand the first app's actions on the SAME message first.
    service.process_update(_cb("cbB1", 123, "feature:resrc_app_actions:r1-a1"))
    assert len(api.edits) == 1
    assert any("resrc_hide_app_actions" in c for c in _kb(api.edits[0]))

    # Hide Actions collapses them - SAME message, Applications still expanded.
    service.process_update(_cb("cbB2", 123, "feature:resrc_hide_app_actions:"))

    assert api.messages == [], "no new message is sent"
    assert len(api.edits) == 2
    edit = api.edits[-1]
    assert edit["message_id"] == 777, "SAME message edited in place"

    text = edit["text"]
    assert "CURRENT RUNNING" in text
    kb = _kb(edit)
    kb_labels = [b["text"] for row in
                 (edit["reply_markup"]["inline_keyboard"] if edit["reply_markup"]
                  else []) for b in row]
    # Both apps remain as clickable buttons after Hide Actions.
    assert any("File Explorer" in t for t in kb_labels)
    assert any("Windows Terminal" in t for t in kb_labels)
    assert not any("resrc_focus" in c for c in kb)
    assert not any("resrc_shot" in c for c in kb)
    assert not any("resrc_close" in c for c in kb)
    assert not any("resrc_edit" in c for c in kb)
    assert not any("resrc_hide_app_actions" in c for c in kb)


# ── Application Focus stays in the expanded Applications panel ───────────────


def test_focus_app_stays_on_selected_inline_app(monkeypatch):
    """Successful app Focus re-renders the SAME panel: Applications stays
    expanded, the focused app stays selected, its inline action buttons stay
    visible, and the old APPLICATION ACTION MENU is never opened."""
    service, api = _make_service(monkeypatch)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", "File Explorer", HWND_A, False),
                     ("r1-a3", "Windows Terminal", HWND_C, False)])
    monkeypatch.setattr(windows_resource_manager, "focus_window",
                        lambda hwnd: True)

    # Expand File Explorer's inline actions on the SAME message first.
    service.process_update(_cb("cbF1", 123, "feature:resrc_app_actions:r1-a1"))
    assert len(api.edits) == 1

    # Focus the selected app - the panel must be re-rendered IN PLACE.
    service.process_update(_cb("cbF2", 123, "feature:resrc_focus:r1-a1"))

    assert api.messages == [], "no new Telegram message is sent"
    assert len(api.edits) == 2
    edit = api.edits[-1]
    assert edit["message_id"] == 777, "SAME message edited in place"

    text = edit["text"]
    # ✅ Focused acknowledgement on the CURRENT RUNNING panel (never the menu).
    assert "\u2705 Focused: File Explorer" in text
    assert "CURRENT RUNNING" in text
    assert "APPLICATION ACTION MENU" not in text

    # Applications stays expanded; names + actions live in the keyboard buttons.
    kb_labels = [b["text"] for row in
                 (edit["reply_markup"]["inline_keyboard"] if edit["reply_markup"]
                  else []) for b in row]
    assert any("File Explorer" in t for t in kb_labels)
    assert any("Windows Terminal" in t for t in kb_labels)
    assert any("\U0001f3af Focus" in t for t in kb_labels)
    assert any("\U0001f4f8 Screenshot" in t for t in kb_labels)
    assert any("\u274c Close" in t for t in kb_labels)
    assert any("\u270f\ufe0f Edit" in t for t in kb_labels)
    assert any("\U0001f53c Hide Actions" in t for t in kb_labels)

    kb = _kb(edit)
    # Only the focused app carries the inline actions.
    assert "feature:resrc_focus:r1-a1" in kb
    assert "feature:resrc_shot:r1-a1" in kb
    assert "feature:resrc_close:r1-a1" in kb
    assert "feature:resrc_edit:r1-a1" in kb
    assert any("resrc_hide_app_actions" in c for c in kb)
    assert "feature:resrc_focus:r1-a3" not in kb


def test_focus_app_same_message_and_actions_remain(monkeypatch):
    """Successful app Focus keeps the SAME message_id and the action buttons."""
    service, api = _make_service(monkeypatch)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", "File Explorer", HWND_A, False),
                     ("r1-a3", "Windows Terminal", HWND_C, False)])
    monkeypatch.setattr(windows_resource_manager, "focus_window",
                        lambda hwnd: True)

    service.process_update(_cb("cbF3", 123, "feature:resrc_focus:r1-a1"))

    assert api.messages == [], "SAME message edited, no duplicate panel"
    assert len(api.edits) == 1
    edit = api.edits[0]
    assert edit["message_id"] == 777
    assert "\u2705 Focused: File Explorer" in edit["text"]
    assert "CURRENT RUNNING" in edit["text"]

    kb = _kb(edit)
    assert "feature:resrc_focus:r1-a1" in kb
    assert "feature:resrc_shot:r1-a1" in kb
    assert "feature:resrc_close:r1-a1" in kb
    assert "feature:resrc_edit:r1-a1" in kb
    assert any("resrc_hide_app_actions" in c for c in kb)
# ── Chrome-tab Focus stays in the expanded Chrome panel ──────────────────────


def test_focus_uia_tab_stays_on_selected_inline_tab(monkeypatch):
    """Successful UIA Chrome-tab Focus keeps the SAME panel: Chrome stays
    expanded, the focused tab stays selected with its inline action buttons
    visible, and no separate Telegram message is sent."""
    service, api = _make_service(monkeypatch)
    reg = resource_manager.resource_registry
    _seed_tabs(reg, [
        ("r1-t1", "Docs", 333, "uia", None),
        ("r1-t2", "Sheets", 444, "uia", None),
    ])
    monkeypatch.setattr(uia_browser_manager, "focus_tab",
                        lambda *args, **kwargs: True)

    # Expand Docs' inline actions on the SAME message first.
    service.process_update(_cb("cbT1", 123, "feature:resrc_tab_actions:r1-t1"))
    assert len(api.edits) == 1

    # Focus the selected tab - the panel must be re-rendered IN PLACE.
    service.process_update(_cb("cbT2", 123, "feature:resrc_focus:r1-t1"))

    assert api.messages == [], "no new Telegram message is sent"
    assert len(api.edits) == 2
    edit = api.edits[-1]
    assert edit["message_id"] == 777, "SAME message edited in place"

    text = edit["text"]
    # ✅ Focused acknowledgement, staying inside the tab's Gmail/profile
    # account view (new account-grouped UX; header is the account label, e.g.
    # "Other / Unknown" for tabs with no resolvable identity).
    assert "\u2705 Focused: Docs" in text
    assert "\U0001f310" in text  # Chrome account header

    # Account view; tab names + actions live in the keyboard buttons.
    kb_labels = [b["text"] for row in
                 (edit["reply_markup"]["inline_keyboard"] if edit["reply_markup"]
                  else []) for b in row]
    assert any("Docs" in t for t in kb_labels)
    assert any("Sheets" in t for t in kb_labels)
    assert any("\U0001f3af Focus" in t for t in kb_labels)
    assert any("\U0001f4f8 Screenshot" in t for t in kb_labels)
    assert any("\u274c Close" in t for t in kb_labels)
    assert any("\u270f\ufe0f Edit" in t for t in kb_labels)
    assert any("\U0001f53c Hide Actions" in t for t in kb_labels)

    kb = _kb(edit)
    # Only the focused tab carries the inline actions.
    assert "feature:resrc_focus:r1-t1" in kb
    assert "feature:resrc_shot:r1-t1" in kb
    assert "feature:resrc_close:r1-t1" in kb
    assert "feature:resrc_edit:r1-t1" in kb
    assert any("resrc_hide_tab_actions" in c or "resrc_chrome_account:" in c for c in kb)  # Hide Actions returns to account view
    assert "feature:resrc_focus:r1-t2" not in kb


def test_focus_cdp_tab_stays_on_same_message_with_actions(monkeypatch):
    """Successful CDP Chrome-tab Focus edits the SAME message and keeps the
    tab's action buttons visible - never a separate Telegram message."""
    service, api = _make_service(monkeypatch)
    reg = resource_manager.resource_registry
    _seed_tabs(reg, [
        ("r1-t1", "Docs", 333, "cdp", "T9"),
        ("r1-t2", "Sheets", 444, "cdp", "T10"),
    ])
    monkeypatch.setattr(
        browser_resource_manager, "focus_tab",
        lambda cdp_url, page_id: page_id == "T9")

    service.process_update(_cb("cbT3", 123, "feature:resrc_focus:r1-t1"))

    assert api.messages == [], "SAME message edited, no separate message"
    assert len(api.edits) == 1
    edit = api.edits[0]
    assert edit["message_id"] == 777

    text = edit["text"]
    assert "\u2705 Focused: Docs" in text
    assert "\U0001f310" in text  # Chrome account header (account-grouped UX)
    kb_labels = [b["text"] for row in
                 (edit["reply_markup"]["inline_keyboard"] if edit["reply_markup"]
                  else []) for b in row]
    assert any("Docs" in t for t in kb_labels)
    assert any("Sheets" in t for t in kb_labels)
    assert any("\U0001f3af Focus" in t for t in kb_labels)
    assert any("\U0001f4f8 Screenshot" in t for t in kb_labels)
    assert any("\u274c Close" in t for t in kb_labels)
    assert any("\u270f\ufe0f Edit" in t for t in kb_labels)
    assert any("\U0001f53c Hide Actions" in t for t in kb_labels)

    kb = _kb(edit)
    assert "feature:resrc_focus:r1-t1" in kb
    assert "feature:resrc_shot:r1-t1" in kb
    assert "feature:resrc_close:r1-t1" in kb
    assert "feature:resrc_edit:r1-t1" in kb
    assert any("resrc_hide_tab_actions" in c or "resrc_chrome_account:" in c for c in kb)  # Hide Actions returns to account view
    assert "feature:resrc_focus:r1-t2" not in kb
