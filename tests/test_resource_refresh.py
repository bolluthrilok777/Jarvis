"""Part 8 - \U0001f504 Refresh performs ONE fresh READ-ONLY scan on the SAME message.

Covers ONLY the \U0001f504 Refresh action on the CURRENT RUNNING / Resource
Review resource list:

    CURRENT RUNNING list
    → \U0001f504 Refresh
    → ONE lightweight read-only fresh scan (generation +1 through the
      existing normal scan behavior)
    → editMessageText on the SAME Resource Review message
    → refreshed CURRENT RUNNING list (closed apps/tabs disappear, newly
      available resources appear through the normal scan)
    → scan failure shows the existing Part-7 warning + the last known
      snapshot on the SAME message
    → the \U0001f504 Refresh button stays available afterwards

Refresh is strictly read-only: it never focuses/closes/screenshots/types or
mutates anything, never starts Research, and the feature stage stays
RESOURCE_REVIEW (Continue/orchestrator is never called). All backends are
mocked - the real system is untouched.
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

VERSION = 777  # the triggering Resource Review control-panel message id

APP_ID = "r1-a1"
APP2_ID = "r1-a2"
TAB_ID = "r1-t1"

APP_TITLE = "File Explorer"
APP2_TITLE = "Windows Terminal"
TAB_TITLE = "Docs"
NEW_APP_ID = "r2-a9"
NEW_APP_TITLE = "Calculator"

REFRESH_ACK = "\u23f3 Refreshing..."
CURRENT_RUNNING = "CURRENT RUNNING"
WARN = "Resource refresh failed. Showing the last known resources."
REFRESH_TEXT = "\U0001f504 Refresh"

def _seed_resources(registry, *, generation=1):
    gen = generation
    app_id = f"r{gen}-a1"
    app2_id = f"r{gen}-a2"
    tab_id = f"r{gen}-t1"
    registry._generation = gen
    registry._feature_id = None
    registry._cdp_url = "http://127.0.0.1:9222"
    registry._chrome_detected = True
    registry._chrome_controllable = True
    registry._chrome_reason = ""
    registry._chrome_control_mode = "cdp"
    registry._last_scan_at = 0.0
    registry._resources = {
        app_id: Resource(
            resource_id=app_id, feature_id=None, resource_type="application",
            title=APP_TITLE, display_name=APP_TITLE, window_handle=111,
        ),
        app2_id: Resource(
            resource_id=app2_id, feature_id=None, resource_type="application",
            title=APP2_TITLE, display_name=APP2_TITLE, window_handle=222,
        ),
        tab_id: Resource(
            resource_id=tab_id, feature_id=None, resource_type="browser_tab",
            title=TAB_TITLE, display_name="Chrome", page_id="T9",
            executable="chrome.exe", control_mode="cdp", host_hwnd=333,
        ),
    }
    return {"app": app_id, "app2": app2_id, "tab": tab_id}


def _make_service(monkeypatch, scan_log):
    reg = resource_manager.resource_registry

    def _fake_scan(feature_id=None, cdp_url=None, include_identity=False,
                   read_only=False):
        scan_log.append({"read_only": bool(read_only)})
        reg._generation += 1
        # Existing scan behavior: the refreshed snapshot only keeps the
        # resources that are STILL open (closed ones simply disappear; newly
        # available open ones are kept).
        reg._resources = {
            rid: r for rid, r in reg._resources.items() if r.status == "open"}
        return {"generation": reg._generation, "feature_id": feature_id,
                "applications": [], "tabs": [], "chrome_detected": True,
                "chrome_controllable": True, "chrome_reason": "",
                "chrome_control_mode": "cdp", "scanned_at": ""}

    monkeypatch.setattr(reg, "scan", _fake_scan)
    api = FakeAPI()
    service = TelegramService(settings=_settings(123), api=api)
    service._start_task_watcher = Mock()
    service._feature_orchestrator = Mock()
    return service, api

def _cb(cb_id, user_id, data, chat_id=100, message_id=VERSION):
    return {
        "update_id": 10,
        "callback_query": {
            "id": cb_id,
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}, "message_id": message_id},
            "data": data,
        },
    }


def _flat_callbacks(inline_keyboard):
    return [b["callback_data"] for row in inline_keyboard for b in row]


def _refresh_callback(api):
    """The \U0001f504 Refresh button on the LATEST rendered CURRENT RUNNING panel."""
    edits = getattr(api, "edits", [])
    assert edits, "at least one panel was rendered"
    rows = edits[-1]["reply_markup"]["inline_keyboard"]
    refreshes = [b["callback_data"] for row in rows
                 for b in row if "Refresh" in b["text"]]
    assert len(refreshes) == 1, f"expected exactly one Refresh button, got {refreshes}"
    return refreshes[0]


def _do_open(service, api, cb_id, data, expected_text):
    before = len(getattr(api, "edits", []))
    service.process_update(_cb(cb_id, 123, data))
    edits = getattr(api, "edits", [])
    assert len(edits) == before + 1, "the action renders exactly ONE panel edit"
    assert expected_text in edits[-1]["text"]


def _open_current_running(service, api, cb_id="cbR0"):
    _do_open(service, api, cb_id, "feature:resrc_run:", "CURRENT RUNNING")


@pytest.fixture(autouse=True)
def _reset_registry():
    reg = resource_manager.resource_registry
    yield
    reg._generation = 0
    reg._resources = {}
    reg._feature_id = None


# ── 1-2. Refresh performs exact ONE fresh read-only scan, generation +1 ───────


def test_refresh_performs_exactly_one_fresh_read_only_scan(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    _seed_resources(resource_manager.resource_registry)

    _open_current_running(service, api)
    assert scan_log == [], "opening the CURRENT RUNNING panel does not scan"
    service.process_update(_cb("cbF1", 123, _refresh_callback(api)))

    assert ("cbF1", REFRESH_ACK) in api.callbacks, "callback acknowledged"
    assert len(scan_log) == 1, "Refresh scans exactly once"
    assert scan_log[0]["read_only"] is True, "the Refresh scan is read-only"
    assert len(api.edits) == 2
    last = api.edits[-1]
    assert last["message_id"] == VERSION
    assert CURRENT_RUNNING in last["text"]
    assert api.messages == [], "no duplicate dashboard message"


def test_generation_increments_once_through_normal_scan_behavior(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    reg = resource_manager.resource_registry
    _seed_resources(reg, generation=1)

    _open_current_running(service, api)
    assert reg._generation == 1
    service.process_update(_cb("cbF2", 123, _refresh_callback(api)))

    assert reg._generation == 2, "Refresh bumps the generation exactly once"
    assert len(scan_log) == 1

# ── 3-4. Same message preserved; no duplicate dashboard ──────────────────────


def test_same_telegram_message_id_preserved(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    _seed_resources(resource_manager.resource_registry)

    _open_current_running(service, api)
    service.process_update(_cb("cbF3a", 123, _refresh_callback(api)))
    service.process_update(_cb("cbF3b", 123, _refresh_callback(api)))

    assert len(api.edits) == 3
    assert {e["message_id"] for e in api.edits} == {VERSION}, \
        "every render edits the SAME Telegram control-panel message"
    assert len(scan_log) == 2, "one fresh scan per Refresh press"
    assert api.messages == [], "never a second Resource Review dashboard"


def test_no_duplicate_dashboard_or_new_message_sent(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    _seed_resources(resource_manager.resource_registry)

    _open_current_running(service, api)
    service.process_update(_cb("cbF4", 123, _refresh_callback(api)))

    last = api.edits[-1]
    assert "FEATURE RESOURCE REVIEW" not in last["text"], \
        "the refreshed panel is the CURRENT RUNNING list, not the dashboard"
    assert CURRENT_RUNNING in last["text"]
    assert api.messages == [], "no extra message was sent"


# ── 5-6. Closed resources disappear after Refresh ────────────────────────────


def test_closed_application_disappears_after_refresh(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    reg = resource_manager.resource_registry
    _seed_resources(reg)

    _open_current_running(service, api)
    # The user manually closed File Explorer on the laptop before Refresh.
    reg.get(APP_ID).status = "closed"
    reg._last_scan_at = 0.0
    service.process_update(_cb("cbF5", 123, _refresh_callback(api)))

    last = api.edits[-1]["text"]
    assert CURRENT_RUNNING in last
    assert "Applications (1)" in last, \
        "one app closed -> the Applications count drops to 1"
    assert "Chrome (1)" in last, "Chrome window count stays 1"
    assert len(scan_log) == 1


def test_closed_chrome_tab_disappears_after_refresh(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    reg = resource_manager.resource_registry
    _seed_resources(reg)

    _open_current_running(service, api)
    # The user manually closed the tab on the laptop before Refresh.
    reg.get(TAB_ID).status = "closed"
    reg._last_scan_at = 0.0
    service.process_update(_cb("cbF6", 123, _refresh_callback(api)))

    last = api.edits[-1]["text"]
    assert CURRENT_RUNNING in last
    assert TAB_TITLE not in last, "closed Chrome tab disappears after Refresh"
    kb = api.edits[-1]["reply_markup"]["inline_keyboard"]
    assert "feature:resrc_wtabs:333@0" not in _flat_callbacks(kb)
    assert len(scan_log) == 1

# ── 7. Newly available resource appears after Refresh ────────────────────────


def test_newly_available_resource_appears_after_refresh(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    reg = resource_manager.resource_registry
    _seed_resources(reg)

    _open_current_running(service, api)
    # A new application starts on the laptop before Refresh.
    reg._resources[NEW_APP_ID] = Resource(
        resource_id=NEW_APP_ID, feature_id=None, resource_type="application",
        title=NEW_APP_TITLE, display_name=NEW_APP_TITLE, window_handle=999,
    )
    service.process_update(_cb("cbF7", 123, _refresh_callback(api)))

    last = api.edits[-1]["text"]
    assert "Applications (3)" in last, \
        "newly available app appears -> the Applications count becomes 3"
    assert "Chrome (1)" in last, "Chrome window count stays 1"
    assert CURRENT_RUNNING in last
    assert len(scan_log) == 1


# ── 8. Scan failure: last snapshot + existing warning on the SAME message ─────


def test_scan_failure_shows_last_snapshot_and_warning(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    reg = resource_manager.resource_registry
    _seed_resources(reg)

    _open_current_running(service, api)

    def _boom(*_a, **_kw):
        raise RuntimeError("laptop unreachable")

    monkeypatch.setattr(reg, "scan", _boom)
    service.process_update(_cb("cbF8", 123, _refresh_callback(api)))

    assert len(api.edits) == 2, "no crash - the SAME message is edited"
    last = api.edits[-1]
    assert last["message_id"] == VERSION
    assert WARN in last["text"], "visible scan-failure warning is shown"
    assert CURRENT_RUNNING in last["text"]
    assert "Applications (2)" in last["text"], \
        "last-known Applications count is still rendered"
    assert "Chrome (1)" in last["text"], \
        "last-known Chrome count is still rendered"
    assert api.messages == [], "fallback stays on the SAME message"

# ── 9-10. Refresh button stays available (success + failure) ─────────────────


def test_refresh_button_remains_available_after_successful_refresh(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    _seed_resources(resource_manager.resource_registry)

    _open_current_running(service, api)
    service.process_update(_cb("cbF9", 123, _refresh_callback(api)))

    kb = api.edits[-1]["reply_markup"]["inline_keyboard"]
    buttons = [b for row in kb for b in row]
    refresh_buttons = [b for b in buttons if "Refresh" in b["text"]]
    assert len(refresh_buttons) == 1, "the refreshed list keeps the Refresh button"
    assert refresh_buttons[0]["callback_data"] == "feature:resrc_refresh_run:"


def test_refresh_button_remains_available_after_failed_refresh(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    reg = resource_manager.resource_registry
    _seed_resources(reg)

    _open_current_running(service, api)

    def _boom(*_a, **_kw):
        raise RuntimeError("laptop unreachable")

    monkeypatch.setattr(reg, "scan", _boom)
    service.process_update(_cb("cbF10", 123, _refresh_callback(api)))

    kb = api.edits[-1]["reply_markup"]["inline_keyboard"]
    buttons = [b for row in kb for b in row]
    refresh_buttons = [b for b in buttons if "Refresh" in b["text"]]
    assert len(refresh_buttons) == 1, \
        "Refresh stays available after a failed scan so the user can retry"


# ── 11. Read-only: no Focus / Close / Screenshot / mutation ──────────────────


def _record_forbidden_backends(monkeypatch, calls):
    def _recorder(name):
        def _f(*_a, **_kw):
            calls.append(name)
            return False
        return _f

    monkeypatch.setattr(
        windows_resource_manager, "focus_window", _recorder("focus_window"))
    monkeypatch.setattr(
        browser_resource_manager, "focus_tab", _recorder("focus_tab"))
    monkeypatch.setattr(
        uia_browser_manager, "focus_tab", _recorder("uia_focus_tab"))
    monkeypatch.setattr(
        browser_resource_manager, "screenshot_tab", _recorder("screenshot_tab"))
    monkeypatch.setattr(
        browser_resource_manager, "close_tab", _recorder("close_tab"))
    monkeypatch.setattr(
        windows_resource_manager, "close_window", _recorder("close_window"))
    import PIL.ImageGrab

    monkeypatch.setattr(PIL.ImageGrab, "grab", _recorder("imagegrab_grab"))


def test_no_focus_close_screenshot_or_mutation(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    _seed_resources(resource_manager.resource_registry)
    forbidden: list = []
    _record_forbidden_backends(monkeypatch, forbidden)

    _open_current_running(service, api)
    service.process_update(_cb("cbF11", 123, _refresh_callback(api)))

    assert forbidden == [], f"Refresh must be read-only, fired: {forbidden}"
    assert api.photos == [], "no screenshot is ever delivered from Refresh"
    assert scan_log and all(e["read_only"] for e in scan_log), \
        "the Refresh scan is strictly read-only (read_only=True)"
    assert service._feature_orchestrator.mock_calls == [], \
        "Refresh never touches the feature orchestrator (no Research/stage change)"

# ── 12-13. State remains RESOURCE_REVIEW; Continue never called ───────────────


def test_state_remains_resource_review(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    reg = resource_manager.resource_registry
    _seed_resources(reg)

    _open_current_running(service, api)
    service.process_update(_cb("cbF12", 123, _refresh_callback(api)))

    assert reg._feature_id is None, "feature context is untouched"
    assert service._feature_orchestrator.mock_calls == [], \
        "Refresh never starts research / never changes the feature stage"
    assert api.messages == [], \
        "state stays RESOURCE_REVIEW on the SAME panel - no new dashboard"
    assert CURRENT_RUNNING in api.edits[-1]["text"]


def test_continue_orchestrator_never_called(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    _seed_resources(resource_manager.resource_registry)

    _open_current_running(service, api)
    service.process_update(_cb("cbF13", 123, _refresh_callback(api)))

    assert any(
        "continue_after_resource_review" in str(c) for c in
        service._feature_orchestrator.mock_calls) is False, \
        "Refresh never calls continue_after_resource_review"
    assert service._feature_orchestrator.mock_calls == [], \
        "Continue / Research / Planning is never invoked by Refresh"