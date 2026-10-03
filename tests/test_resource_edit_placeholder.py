"""Part 6 - ✏️ Edit placeholder (application + Chrome tab).

Covers ONLY the Edit callback path inside RESOURCE_REVIEW:

    Select application / Chrome tab → tab action menu → ✏️ Edit
    → placeholder notice on the SAME control-panel message
    → ⬅️ Back button (existing per-type detail callback wiring)
    → no resource is ever modified → remain RESOURCE_REVIEW.

Edit is PLACEHOLDER ONLY. These tests guarantee it never types/clicks/sends
keyboard input, never mutates a file/webpage, never runs JavaScript or CDP
mutation commands, and never focuses/closes/screenshots anything or changes
the feature stage. All backends are mocked - the real system is untouched.
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

APP_ID = "r1-a1"
TAB_ID = "r1-t1"
VERSION = 777  # the triggering Resource Review control-panel message id

APP_TITLE = "File Explorer"
TAB_TITLE = "Docs"

PLACEHOLDER = "\u270f\ufe0f Edit controls will be implemented in a later phase."
BACK_LABEL = "\u2b05\ufe0f Back"
EDIT_ACK = "\u270f\ufe0f Edit mode"
STALE_TEXT = "Resource list changed"
FOCUS_ACK = "\U0001f441 Focusing..."
CLOSE_ACK = "\u274c Closing..."
CAPTURE_ACK = "\U0001f4f8 Capturing..."


def _seed_resources(registry, *, generation=1):
    registry._generation = generation
    registry._feature_id = None
    registry._cdp_url = "http://127.0.0.1:9222"
    registry._chrome_detected = True
    registry._chrome_controllable = True
    registry._chrome_reason = ""
    registry._chrome_control_mode = "cdp"
    registry._last_scan_at = 0.0
    registry._resources = {
        APP_ID: Resource(
            resource_id=APP_ID, feature_id=None, resource_type="application",
            title=APP_TITLE, display_name=APP_TITLE, window_handle=111,
        ),
        TAB_ID: Resource(
            resource_id=TAB_ID, feature_id=None, resource_type="browser_tab",
            title=TAB_TITLE, display_name="Chrome", page_id="T9",
            executable="chrome.exe", control_mode="cdp",
        ),
    }


def _make_service(monkeypatch, scan_counter):
    reg = resource_manager.resource_registry

    def _fake_scan(feature_id=None, cdp_url=None, include_identity=False,
                   read_only=False):
        scan_counter.append(1)
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


def _single_edit(api):
    edits = getattr(api, "edits", [])
    assert len(edits) == 1, f"expected exactly ONE editMessageText, got {len(edits)}"
    return edits[0]


@pytest.fixture(autouse=True)
def _reset_registry():
    reg = resource_manager.resource_registry
    yield
    reg._generation = 0
    reg._resources = {}
    reg._feature_id = None
    reg._cdp_url = None
    reg._chrome_detected = False
    reg._chrome_controllable = False
    # ── 1-2. Placeholder text for application and Chrome tab ──────────────────────


def _press_edit(service, api, rid, cb_id):
    service.process_update(_cb(cb_id, 123, f"feature:resrc_edit:{rid}"))
    return _single_edit(api)


def test_application_edit_shows_placeholder_text(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    _seed_resources(resource_manager.resource_registry)

    edit = _press_edit(service, api, APP_ID, "cbE1")

    assert edit["message_id"] == VERSION
    assert edit["text"] == PLACEHOLDER
    assert ("cbE1", EDIT_ACK) in api.callbacks, \
        "the callback is acknowledged as an Edit-mode press"
    assert api.messages == [], \
        "the placeholder is rendered as an inline edit, not a new message"


def test_chrome_tab_edit_shows_placeholder_text(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    _seed_resources(resource_manager.resource_registry)

    edit = _press_edit(service, api, TAB_ID, "cbE2")

    assert edit["message_id"] == VERSION
    assert edit["text"] == PLACEHOLDER
    assert ("cbE2", EDIT_ACK) in api.callbacks
    assert api.messages == []


# ── 3. Back button is present (existing per-type detail callback) ──────────────


def test_back_button_present_on_application_edit(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    _seed_resources(resource_manager.resource_registry)

    edit = _press_edit(service, api, APP_ID, "cbE3a")
    kb = edit["reply_markup"]["inline_keyboard"]
    assert _flat_callbacks(kb) == ["feature:resrc_back:"], \
        "Back is wired to the Part-7 fresh-scan action (refreshed resource list)"
    assert any(b["text"] == BACK_LABEL for row in kb for b in row)


def test_back_button_present_on_chrome_tab_edit(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    _seed_resources(resource_manager.resource_registry)

    edit = _press_edit(service, api, TAB_ID, "cbE3b")
    kb = edit["reply_markup"]["inline_keyboard"]
    assert _flat_callbacks(kb) == ["feature:resrc_back:"], \
        "Back is wired to the Part-7 fresh-scan action (refreshed resource list)"
    assert any(b["text"] == BACK_LABEL for row in kb for b in row)


# ── 4. Same Telegram message_id is preserved ───────────────────────────────────


def test_same_message_id_preserved(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    _seed_resources(resource_manager.resource_registry)

    edit = _press_edit(service, api, APP_ID, "cbE4")

    assert edit["message_id"] == VERSION, \
        "the panel is edited in place; no second Resource Review dashboard"
    assert edit["chat_id"] == 100
    assert api.messages == [], "no duplicate dashboard is ever created"
    assert len(getattr(api, "edits", [])) == 1


# ── 5. State stays RESOURCE_REVIEW ─────────────────────────────────────────────


def test_state_stays_resource_review(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_resources(reg)

    _press_edit(service, api, TAB_ID, "cbE5")

    assert service._feature_orchestrator.mock_calls == [], \
        "Edit must not advance/cancel the feature (stays RESOURCE_REVIEW)"
    # ── 6-8. Focus / Close / Screenshot are never triggered ────────────────────────


def _record_forbidden_backends(monkeypatch, calls):
    """Any focus/close/screenshot/grab backend firing is a Part-6 violation."""

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


def _press_both_edits(service, api):
    service.process_update(_cb("cbE6a", 123, f"feature:resrc_edit:{APP_ID}"))
    service.process_update(_cb("cbE6b", 123, f"feature:resrc_edit:{TAB_ID}"))
    assert len(getattr(api, "edits", [])) == 2


def test_no_focus_triggered(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    _seed_resources(resource_manager.resource_registry)
    forbidden: list = []
    _record_forbidden_backends(monkeypatch, forbidden)

    _press_both_edits(service, api)

    assert not any(
        c in ("focus_window", "focus_tab", "uia_focus_tab")
        for c in forbidden), f"Focus fired: {forbidden}"
    assert not any(
        text == FOCUS_ACK for _, text in api.callbacks), "no Focus ack"


def test_no_close_triggered(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    _seed_resources(resource_manager.resource_registry)
    forbidden: list = []
    _record_forbidden_backends(monkeypatch, forbidden)

    _press_both_edits(service, api)

    assert not any(
        c in ("close_tab", "close_window") for c in forbidden), \
        f"Close fired: {forbidden}"
    assert not any(
        text == CLOSE_ACK for _, text in api.callbacks), "no Close ack"


def test_no_screenshot_triggered(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    _seed_resources(resource_manager.resource_registry)
    forbidden: list = []
    _record_forbidden_backends(monkeypatch, forbidden)

    _press_both_edits(service, api)

    assert "screenshot_tab" not in forbidden, \
        "no tab screenshot capture on an Edit press"
    assert "imagegrab_grab" not in forbidden, \
        "no desktop screenshot on an Edit press"
    assert api.photos == [], "no image is ever delivered from Edit"
    assert not any(
        text == CAPTURE_ACK for _, text in api.callbacks), "no Capture ack"


# ── 9. No typing / clicking / mutation ─────────────────────────────────────────


def test_no_typing_or_mutation(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_resources(reg)
    before_app = reg.get(APP_ID)
    before_tab = reg.get(TAB_ID)
    forbidden: list = []
    _record_forbidden_backends(monkeypatch, forbidden)

    _press_both_edits(service, api)

    assert forbidden == [], \
        "no backend primitive (focus/close/screenshot/grab) was touched"
    assert service._feature_orchestrator.mock_calls == [], \
        "no feature/orchestration mutation - Edit cannot start Research or a stage"
    after_app = reg.get(APP_ID)
    after_tab = reg.get(TAB_ID)
    assert after_app is before_app and after_tab is before_tab, \
        "the Resource objects are the SAME instances (nothing recreated)"
    for resource in (after_app, after_tab):
        for field in ("status", "title", "window_handle", "page_id",
                      "control_mode", "focused", "active"):
            assert getattr(resource, field, None) == \
                getattr(before_app if resource is after_app else before_tab,
                        field, None), \
                f"resource field mutated: {field}"
    # The ONLY side effects are one inline panel edit per press + the ack.
    assert len(getattr(api, "edits", [])) == 2
    assert api.messages == [], "no new message was created"


# ── Stale resource → existing stale behavior, nothing performed ────────────────


def test_stale_edit_uses_existing_stale_behavior(monkeypatch):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    _seed_resources(resource_manager.resource_registry, generation=2)

    service.process_update(_cb("cbE10", 123, f"feature:resrc_edit:{APP_ID}"))

    assert api.photos == []
    assert any(STALE_TEXT in m["text"] for m in api.messages), \
        "the existing Resource-list-changed notice is shown"
    assert scan_counter, "stale path re-scans to show the latest resources"