"""Part 3 - Windows APPLICATION Focus/Close on the exact HWND.

Covers only the application-resource action path (Chrome tab Focus/Close is
verified in ``test_resource_menu_ux.py`` / ``test_chrome_window_grouping.py``).

Every Windows control primitive is mocked (``focus_window``, ``close_window``,
``is_window_alive``); no real window is ever touched. The registry rescan is
faked the same way the Chrome tests do it: only resources whose status is
still ``open`` survive the re-scan, so a confirmed close really disappears.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import jarvis_core.resource_manager as resource_manager
import jarvis_core.windows_resource_manager as windows_resource_manager
from jarvis_core.resource_manager import Resource
from jarvis_core.telegram_service import TelegramService
from tests.test_telegram_integration import FakeAPI, _settings

HWND_A = 111  # File Explorer "A"
HWND_B = 222  # File Explorer "B" (same application, second window)
HWND_C = 333  # Windows Terminal (unrelated app)

TITLE = "File Explorer"
OTHER_TITLE = "Windows Terminal"

STALE_TEXT = "Resource list changed"


# ── Harness ──────────────────────────────────────────────────────────────────


def _seed_apps(registry, apps_spec):
    """Seed application resources: (resource_id, name, hwnd, risky)."""
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


def _make_service(monkeypatch, scan_counter):
    reg = resource_manager.resource_registry

    def _fake_scan(feature_id=None, cdp_url=None, include_identity=False,
                   read_only=False):
        scan_counter.append(1)
        reg._resources = {
            rid: r for rid, r in reg._resources.items() if r.status == "open"}
        return {"generation": reg._generation, "feature_id": feature_id,
                "applications": [], "tabs": [], "chrome_detected": False,
                "chrome_controllable": False, "chrome_reason": "",
                "chrome_control_mode": "", "scanned_at": ""}

    monkeypatch.setattr(reg, "scan", _fake_scan)
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


@pytest.fixture(autouse=True)
def _reset_registry():
    reg = resource_manager.resource_registry
    yield
    reg._generation = 0
    reg._resources = {}
    reg._feature_id = None


# ── Focus ────────────────────────────────────────────────────────────────────


def test_focus_exact_application_hwnd(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False),
                     ("r1-a3", OTHER_TITLE, HWND_C, False)])
    calls: list = []
    monkeypatch.setattr(
        windows_resource_manager, "focus_window",
        lambda hwnd: calls.append(hwnd) or True)

    service.process_update(_cb("cbA1", 123, "feature:resrc_focus:r1-a1"))

    assert calls == [HWND_A], "ONLY the selected window's HWND is focused"
    assert ("cbA1", "\U0001f441 Focusing...") in api.callbacks
    assert len(api.edits) == 1
    edit = api.edits[0]
    assert edit["message_id"] == 777
    assert "\u2705 Focused: File Explorer" in edit["text"]
    kb = _kb(edit)
    assert "feature:resrc_focus:r1-a1" in kb      # SAME detail panel
    assert "feature:resrc_close:r1-a1" in kb
    assert "APPLICATION ACTION MENU" in edit["text"]
    assert not scan_counter, "focus must not trigger a rescan"


def test_focus_same_app_two_windows_targets_exact_hwnd(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False),
                     ("r1-a2", TITLE, HWND_B, False)])
    calls: list = []
    monkeypatch.setattr(
        windows_resource_manager, "focus_window",
        lambda hwnd: calls.append(hwnd) or True)

    service.process_update(_cb("cbA2", 123, "feature:resrc_focus:r1-a2"))

    assert calls == [HWND_B], "window B focused, never window A"
    edit = api.edits[0]
    assert "\u2705 Focused: File Explorer" in edit["text"]
    kb = _kb(edit)
    assert "feature:resrc_focus:r1-a2" in kb
    assert not any(":r1-a1" in d for d in kb), "window A untouched in panel"


def test_focus_stale_resource_nothing_focused(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False),
                     ("r1-a3", OTHER_TITLE, HWND_C, False)])
    reg._generation = 2  # callback belongs to generation 1 -> stale
    calls: list = []
    monkeypatch.setattr(
        windows_resource_manager, "focus_window",
        lambda hwnd: calls.append(hwnd) or True)

    service.process_update(_cb("cbA3", 123, "feature:resrc_focus:r1-a1"))

    assert calls == [], "stale resource must not focus ANY window"
    # Existing stale handling: visible warning, then the SAME panel is
    # re-rendered with the latest resources.
    assert any(STALE_TEXT in m["text"] for m in api.messages)
    assert len(api.edits) == 1
    edit = api.edits[0]
    assert edit["message_id"] == 777
    kb = _kb(edit)
    assert "feature:resrc_show:r1-a1" in kb   # current list re-rendered
    assert "feature:resrc_show:r1-a3" in kb
    assert scan_counter, "stale path re-scans to show the latest resources"


def test_focus_keeps_resource_review_state(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False)])
    monkeypatch.setattr(windows_resource_manager, "focus_window",
                        lambda hwnd: True)

    service.process_update(_cb("cbA4", 123, "feature:resrc_focus:r1-a1"))

    assert service._feature_orchestrator.mock_calls == [], \
        "focus must not advance/cancel the feature (stays RESOURCE_REVIEW)"
    assert api.messages == [], "no new dashboard message is sent"


def test_focus_preserves_same_telegram_panel(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False)])
    monkeypatch.setattr(windows_resource_manager, "focus_window",
                        lambda hwnd: True)

    service.process_update(_cb("cbA5", 123, "feature:resrc_focus:r1-a1"))

    assert len(api.edits) == 1 and api.edits[0]["message_id"] == 777
    assert api.messages == [], "SAME message edited, no duplicate panel"


def test_focus_refused_reports_visible_failure(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False)])
    monkeypatch.setattr(windows_resource_manager, "focus_window",
                        lambda hwnd: False)  # Windows refuses activation

    service.process_update(_cb("cbA6", 123, "feature:resrc_focus:r1-a1"))

    assert len(api.edits) == 1
    edit = api.edits[0]
    assert edit["message_id"] == 777
    assert "\u26a0\ufe0f Unable to focus: File Explorer" in edit["text"]
    assert "feature:resrc_focus:r1-a1" in _kb(edit), "panel stays usable"


# ── Close ────────────────────────────────────────────────────────────────────


def test_close_exact_application_hwnd_and_disappears_after_rescan(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False),
                     ("r1-a3", OTHER_TITLE, HWND_C, False)])
    close_calls: list = []
    monkeypatch.setattr(
        windows_resource_manager, "close_window",
        lambda hwnd: close_calls.append(hwnd) or True)
    monkeypatch.setattr(windows_resource_manager, "is_window_alive",
                        lambda hwnd: hwnd != HWND_A)  # A gone after WM_CLOSE
    monkeypatch.setattr(resource_manager, "CLOSE_SETTLE_SECONDS", 0)

    service.process_update(_cb("cbC1", 123, "feature:resrc_close:r1-a1"))

    assert close_calls == [HWND_A], "graceful WM_CLOSE to the exact HWND only"
    assert ("cbC1", "\u274c Closing...") in api.callbacks
    assert len(api.edits) == 1
    edit = api.edits[0]
    assert edit["message_id"] == 777
    assert "\u2705 Closed: File Explorer" in edit["text"]
    assert "CURRENT RUNNING" in edit["text"]
    kb = _kb(edit)
    assert "feature:resrc_show:r1-a1" not in kb, "closed app disappeared"
    assert "feature:resrc_show:r1-a3" in kb, "unrelated app remains"
    assert scan_counter, "fresh scan after successful close"


def test_close_two_same_app_windows_only_selected_receives_close(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False),
                     ("r1-a2", TITLE, HWND_B, False)])
    close_calls: list = []
    monkeypatch.setattr(
        windows_resource_manager, "close_window",
        lambda hwnd: close_calls.append(hwnd) or True)
    monkeypatch.setattr(windows_resource_manager, "is_window_alive",
                        lambda hwnd: hwnd != HWND_A)  # only A closes
    monkeypatch.setattr(resource_manager, "CLOSE_SETTLE_SECONDS", 0)

    service.process_update(_cb("cbC2", 123, "feature:resrc_close:r1-a1"))

    assert close_calls == [HWND_A], "window B must receive NOTHING"
    edit = api.edits[0]
    kb = _kb(edit)
    assert "feature:resrc_show:r1-a2" in kb, "same-app window B remains"
    assert "feature:resrc_show:r1-a1" not in kb


def test_close_never_kills_process_or_other_windows(monkeypatch):
    import os
    import subprocess

    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False),
                     ("r1-a2", TITLE, HWND_B, False)])

    def _forbidden(*_a, **_kw):
        raise AssertionError("process-wide kill attempted")

    monkeypatch.setattr(os, "system", _forbidden)
    monkeypatch.setattr(os, "kill", _forbidden, raising=False)
    for name in ("run", "call", "check_call", "check_output", "Popen"):
        monkeypatch.setattr(subprocess, name, _forbidden)
    close_calls: list = []
    monkeypatch.setattr(
        windows_resource_manager, "close_window",
        lambda hwnd: close_calls.append(hwnd) or True)
    monkeypatch.setattr(windows_resource_manager, "is_window_alive",
                        lambda hwnd: hwnd != HWND_A)
    monkeypatch.setattr(resource_manager, "CLOSE_SETTLE_SECONDS", 0)

    service.process_update(_cb("cbC3", 123, "feature:resrc_close:r1-a1"))

    assert close_calls == [HWND_A], "one graceful close request, nothing else"
    assert len(api.edits) == 1, "completed without any process kill"


def test_close_stale_resource_nothing_closed(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False),
                     ("r1-a3", OTHER_TITLE, HWND_C, False)])
    reg._generation = 2  # callback belongs to generation 1 -> stale
    close_calls: list = []
    monkeypatch.setattr(
        windows_resource_manager, "close_window",
        lambda hwnd: close_calls.append(hwnd) or True)

    service.process_update(_cb("cbC4", 123, "feature:resrc_close:r1-a1"))

    assert close_calls == [], "stale resource must not close ANY window"
    # Existing stale handling: visible warning, then the SAME panel is
    # re-rendered with the latest resources.
    assert any(STALE_TEXT in m["text"] for m in api.messages)
    assert len(api.edits) == 1
    edit = api.edits[0]
    assert edit["message_id"] == 777
    kb = _kb(edit)
    assert "feature:resrc_show:r1-a1" in kb   # current list re-rendered
    assert "feature:resrc_show:r1-a3" in kb
    assert scan_counter, "stale path re-scans to show the latest resources"


def test_close_keeps_resource_review_state_and_same_panel(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False),
                     ("r1-a3", OTHER_TITLE, HWND_C, False)])
    monkeypatch.setattr(windows_resource_manager, "close_window",
                        lambda hwnd: True)
    monkeypatch.setattr(windows_resource_manager, "is_window_alive",
                        lambda hwnd: hwnd != HWND_A)
    monkeypatch.setattr(resource_manager, "CLOSE_SETTLE_SECONDS", 0)

    service.process_update(_cb("cbC5", 123, "feature:resrc_close:r1-a1"))

    assert service._feature_orchestrator.mock_calls == [], \
        "close must not advance/cancel the feature (stays RESOURCE_REVIEW)"
    assert api.messages == [], "no duplicate dashboard message"
    assert len(api.edits) == 1 and api.edits[0]["message_id"] == 777, \
        "SAME Resource Review message edited in place"


def test_close_risky_app_asks_confirmation_before_any_close(monkeypatch):
    """Unsaved-work safety: a risky app shows a confirm panel FIRST; Jarvis
    sends NO close request until the user explicitly presses Confirm Close,
    and the confirm panel itself documents that Save/unsaved dialogs are
    never automated."""
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, True)])  # risky app
    close_calls: list = []
    monkeypatch.setattr(
        windows_resource_manager, "close_window",
        lambda hwnd: close_calls.append(hwnd) or True)
    monkeypatch.setattr(windows_resource_manager, "is_window_alive",
                        lambda hwnd: False)
    monkeypatch.setattr(resource_manager, "CLOSE_SETTLE_SECONDS", 0)

    # Step 1: first ❌ Close press must NOT post WM_CLOSE.
    service.process_update(_cb("cbC6", 123, "feature:resrc_close:r1-a1"))
    assert close_calls == [], "no close request before explicit confirmation"
    assert len(api.edits) == 1
    edit = api.edits[0]
    assert edit["message_id"] == 777
    assert "Confirm closing?" in edit["text"]
    assert "unsaved" in edit["text"].lower()
    kb = _kb(edit)
    assert "feature:resrc_confirm_close:r1-a1" in kb
    assert "feature:resrc_show:r1-a1" in kb      # Keep Open escape hatch
    assert "feature:resrc_home:" in kb           # Cancel/Back escape hatch
    assert scan_counter == [], "confirmation view does not rescan"

    # Step 2: explicit Confirm Close now performs the graceful close.
    service.process_update(_cb("cbC7", 123, "feature:resrc_confirm_close:r1-a1"))
    assert close_calls == [HWND_A]
    assert ("cbC7", "\u274c Closing...") in api.callbacks
    assert "\u2705 Closed: File Explorer" in api.edits[-1]["text"]


def test_close_confirmation_never_auto_dismisses_unsaved_work(monkeypatch):
    """If the app keeps its exact HWND alive after WM_CLOSE (its own Save /
    unsaved-work dialog), Jarvis must leave that dialog alone: no retry, no
    process kill, no second WM_CLOSE - just a visible report."""
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False),
                     ("r1-a3", OTHER_TITLE, HWND_C, False)])
    close_calls: list = []
    monkeypatch.setattr(
        windows_resource_manager, "close_window",
        lambda hwnd: close_calls.append(hwnd) or True)
    monkeypatch.setattr(windows_resource_manager, "is_window_alive",
                        lambda hwnd: True)  # app is showing its own dialog
    monkeypatch.setattr(resource_manager, "CLOSE_SETTLE_SECONDS", 0)

    service.process_update(_cb("cbC8", 123, "feature:resrc_close:r1-a1"))

    assert close_calls == [HWND_A], "exactly ONE graceful request - no retry"
    # Visible report: Jarvis never dismisses the app's own dialog.
    assert len(api.messages) == 1
    assert "\u26a0\ufe0f Close requires confirmation on the laptop." in \
        api.messages[0]["text"]
    assert "never" in api.messages[0]["text"].lower(), \
        "must say Jarvis never dismisses it"
    # No panel edit was destroyed, and the still-open app remains tracked
    # and actionable in the registry (user answers the dialog on the laptop).
    assert getattr(api, "edits", []) == []
    assert reg.get("r1-a1") is not None
    assert reg.get("r1-a3") is not None
    assert scan_counter == [], "nothing closed -> no snapshot re-render"



