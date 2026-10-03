"""Part 7 - \u2b05\ufe0f Back performs a fresh READ-ONLY scan on the SAME message.

Back from application detail / Chrome-tab detail / application Edit
placeholder / Chrome-tab Edit placeholder must:

    * acknowledge the callback,
    * run ONE lightweight read-only resource scan (generation bumps through
      the existing scan behavior),
    * edit the SAME Telegram Resource Review control-panel message,
    * show the refreshed CURRENT RUNNING resource list.

Back is strictly read-only: it never focuses/closes/screenshots/types/mutates
anything, never starts Research, never changes the feature stage, and on scan
failure shows the last known resources with a visible warning on the SAME
message. All backends are mocked - the real system is untouched.
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

BACK_ACK = "\u2b05\ufe0f Back..."
CURRENT_RUNNING = "CURRENT RUNNING"
WARN = "Resource refresh failed. Showing the last known resources."


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
        # resources that are STILL open (closed ones simply disappear).
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


def _back_callback(api):
    """The \u2b05\ufe0f Back button on the LATEST rendered inline panel."""
    edits = getattr(api, "edits", [])
    assert edits, "at least one panel was rendered"
    rows = edits[-1]["reply_markup"]["inline_keyboard"]
    backs = [b["callback_data"] for row in rows
             for b in row if "Back" in b["text"]]
    assert len(backs) == 1, f"expected exactly one Back button, got {backs}"
    return backs[0]


def _do_open(service, api, cb_id, data, expected_text):
    before = len(getattr(api, "edits", []))
    service.process_update(_cb(cb_id, 123, data))
    edits = getattr(api, "edits", [])
    assert len(edits) == before + 1, "the action renders exactly ONE panel edit"
    assert expected_text in edits[-1]["text"]


def _open_app_detail(service, api, cb_id="cbD1"):
    _do_open(service, api, cb_id, f"feature:resrc_show:{APP_ID}",
             "APPLICATION ACTION MENU")


def _open_tab_detail(service, api, cb_id="cbD2"):
    _do_open(service, api, cb_id, f"feature:resrc_tab_show:{TAB_ID}",
             "CHROME TAB ACTION MENU")


def _open_edit_placeholder(service, api, cb_id="cbD3"):
    _do_open(service, api, cb_id, f"feature:resrc_edit:{APP_ID}",
             "later phase")


@pytest.fixture(autouse=True)
def _reset_registry():
    reg = resource_manager.resource_registry
    yield
    reg._generation = 0
    reg._resources = {}
    reg._feature_id = None


# ── 1-2. Detail Back performs ONE fresh read-only scan ────────────────────────


def test_app_detail_back_performs_fresh_scan(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    _seed_resources(resource_manager.resource_registry)

    _open_app_detail(service, api)
    assert scan_log == [], "opening the detail does not scan"
    service.process_update(_cb("cbB1", 123, _back_callback(api)))

    assert ("cbB1", BACK_ACK) in api.callbacks, "callback acknowledged"
    assert len(scan_log) == 1, "Back scans exactly once"
    assert scan_log[0]["read_only"] is True, "the scan is read-only"
    assert len(api.edits) == 2
    last = api.edits[-1]
    assert last["message_id"] == VERSION
    assert CURRENT_RUNNING in last["text"]
    assert "FEATURE RESOURCE REVIEW" not in last["text"]
    assert api.messages == [], "no duplicate dashboard message"


def test_chrome_tab_detail_back_performs_fresh_scan(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    _seed_resources(resource_manager.resource_registry)

    _open_tab_detail(service, api)
    service.process_update(_cb("cbB2", 123, _back_callback(api)))

    assert ("cbB2", BACK_ACK) in api.callbacks
    assert len(scan_log) == 1, "Back scans exactly once"
    assert len(api.edits) == 2
    last = api.edits[-1]
    assert last["message_id"] == VERSION
    assert CURRENT_RUNNING in last["text"]
    assert api.messages == []
# ── 3. Edit-placeholder Back performs ONE fresh read-only scan ───────────────


def test_edit_placeholder_back_performs_fresh_scan(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    _seed_resources(resource_manager.resource_registry)

    _open_edit_placeholder(service, api)
    back_cb = _back_callback(api)
    assert back_cb == "feature:resrc_back:", \
        "Edit Back is wired to the shared fresh-scan Back action"
    service.process_update(_cb("cbB3", 123, back_cb))

    assert ("cbB3", BACK_ACK) in api.callbacks
    assert len(scan_log) == 1, "Edit Back scans exactly once"
    assert len(api.edits) == 2
    last = api.edits[-1]
    assert last["message_id"] == VERSION
    assert CURRENT_RUNNING in last["text"]
    assert api.messages == []


# ── 4. Same control-panel message is preserved ───────────────────────────────


def test_same_message_id_preserved(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    reg = resource_manager.resource_registry

    # Flow 1: application detail -> Back (fresh scan bumps the generation).
    ids = _seed_resources(reg, generation=1)
    _do_open(service, api, "cbD1",
             f"feature:resrc_show:{ids['app']}", "APPLICATION ACTION MENU")
    service.process_update(_cb("cbB4a", 123, _back_callback(api)))

    # Flow 2: a fresh laptop snapshot + Edit placeholder -> Back - STILL the
    # same control-panel message (the real scanner would stamp new ids too).
    ids = _seed_resources(reg, generation=reg._generation)
    _do_open(service, api, "cbD2",
             f"feature:resrc_edit:{ids['app']}", "later phase")
    service.process_update(_cb("cbB4b", 123, _back_callback(api)))

    assert len(api.edits) == 4
    assert {e["message_id"] for e in api.edits} == {VERSION}, \
        "every render edits the SAME Telegram control-panel message"
    assert api.messages == [], "never a second Resource Review dashboard"


# ── 5-6. Stale closed resources disappear after Back ─────────────────────────


def test_closed_application_disappears_after_back(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    reg = resource_manager.resource_registry
    _seed_resources(reg)

    _open_app_detail(service, api)
    # The user closed File Explorer on the laptop while the detail was open.
    reg.get(APP_ID).status = "closed"
    service.process_update(_cb("cbB5", 123, _back_callback(api)))

    last = api.edits[-1]["text"]
    assert CURRENT_RUNNING in last
    assert APP_TITLE not in last, "closed File Explorer disappears after Back"
    assert APP2_TITLE in last, "still-open applications are kept"
    assert "feature:resrc_show:r1-a1" not in _flat_callbacks(
        api.edits[-1]["reply_markup"]["inline_keyboard"])
    assert len(scan_log) == 1


def test_closed_chrome_tab_disappears_after_back(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    reg = resource_manager.resource_registry
    _seed_resources(reg)

    _open_tab_detail(service, api)
    # The user manually closed the tab on the laptop before Back.
    reg.get(TAB_ID).status = "closed"
    service.process_update(_cb("cbB6", 123, _back_callback(api)))

    last = api.edits[-1]["text"]
    assert CURRENT_RUNNING in last
    assert TAB_TITLE not in last, "closed Chrome tab disappears after Back"
    assert len(scan_log) == 1
    assert "feature:resrc_wtabs:333@0" not in _flat_callbacks(
        api.edits[-1]["reply_markup"]["inline_keyboard"])


# ── 7. Generation advances through the normal scan behavior ──────────────────


def test_generation_changes_through_normal_scan_behavior(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    reg = resource_manager.resource_registry
    _seed_resources(reg, generation=1)

    _open_app_detail(service, api)
    assert reg._generation == 1
    service.process_update(_cb("cbB7", 123, _back_callback(api)))

    assert reg._generation == 2, "Back scan bumps the resource generation"
    assert len(scan_log) == 1
# ── 8. Scan failure: last snapshot + visible warning on the SAME message ─────


def test_scan_failure_uses_last_snapshot_and_warning(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    reg = resource_manager.resource_registry
    _seed_resources(reg)

    _open_app_detail(service, api)

    def _boom(*_a, **_kw):
        raise RuntimeError("laptop unreachable")

    monkeypatch.setattr(reg, "scan", _boom)
    service.process_update(_cb("cbB8", 123, _back_callback(api)))

    assert len(api.edits) == 2, "no crash - the SAME message is edited"
    last = api.edits[-1]
    assert last["message_id"] == VERSION
    assert WARN in last["text"], "visible scan-failure warning is shown"
    assert CURRENT_RUNNING in last["text"]
    assert APP_TITLE in last["text"], "last known snapshot is still rendered"
    assert api.messages == [], "fallback stays on the SAME message"


# ── 9. Read-only: no Focus / Close / Screenshot / mutation ───────────────────


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
    reg = resource_manager.resource_registry
    _seed_resources(reg)
    forbidden: list = []
    _record_forbidden_backends(monkeypatch, forbidden)

    _open_app_detail(service, api)
    service.process_update(_cb("cbB9", 123, _back_callback(api)))

    assert forbidden == [], f"Back must be read-only, fired: {forbidden}"
    assert api.photos == [], "no screenshot is ever delivered from Back"
    assert scan_log and all(e["read_only"] for e in scan_log), \
        "the Back scan is strictly read-only (read_only=True)"
    assert service._feature_orchestrator.mock_calls == [], \
        "Back never touches the feature orchestrator (no Research/stage change)"


# ── 10. State remains RESOURCE_REVIEW ────────────────────────────────────────


def test_state_remains_resource_review(monkeypatch):
    scan_log: list = []
    service, api = _make_service(monkeypatch, scan_log)
    reg = resource_manager.resource_registry

    ids = _seed_resources(reg, generation=1)
    _do_open(service, api, "cbD1",
             f"feature:resrc_show:{ids['app']}", "APPLICATION ACTION MENU")
    service.process_update(_cb("cbB10a", 123, _back_callback(api)))

    ids = _seed_resources(reg, generation=reg._generation)
    _do_open(service, api, "cbD2",
             f"feature:resrc_tab_show:{ids['tab']}", "CHROME TAB ACTION MENU")
    service.process_update(_cb("cbB10b", 123, _back_callback(api)))

    ids = _seed_resources(reg, generation=reg._generation)
    _do_open(service, api, "cbD3",
             f"feature:resrc_edit:{ids['app']}", "later phase")
    service.process_update(_cb("cbB10c", 123, _back_callback(api)))

    assert reg._feature_id is None, "feature context is untouched"
    assert any(
        "continue_after_resource_review" in str(c) for c in
        service._feature_orchestrator.mock_calls) is False, \
        "Back never calls continue_after_resource_review"
    assert service._feature_orchestrator.mock_calls == [], \
        "Back never starts research / never changes the feature stage"
    assert api.messages == [], \
        "state stays RESOURCE_REVIEW on the SAME panel - no new dashboard"