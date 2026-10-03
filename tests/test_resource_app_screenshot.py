"""Part 4 - 📸 Screenshot of ONE selected normal Windows application window.

Covers ONLY the application screenshot callback path inside RESOURCE_REVIEW:

    Select application → Application action menu → 📸 Screenshot
    → capture ONLY that exact HWND → sendPhoto to Telegram
    → remain in RESOURCE_REVIEW.

Every Windows capture primitive is mocked (``focus_window``,
``screenshot_service.capture_window``, ``PIL.ImageGrab.grab``); the fake
capture writes a REAL tiny PNG so production capture validation runs, but no
development tool is ever screenshotted and no real window is touched.

Identity contract under test: the target is the generation-stamped
``resource_id`` + exact ``window_handle`` - never a process/executable name.
"""

from __future__ import annotations

import os
from unittest.mock import Mock

import pytest

import jarvis_core.resource_manager as resource_manager
import jarvis_core.windows_resource_manager as windows_resource_manager
from jarvis_core.resource_manager import Resource
from jarvis_core.telegram_service import TelegramService
from tests.test_telegram_integration import FakeAPI, _settings

HWND_A = 111  # File Explorer "A"
HWND_B = 222  # File Explorer "B" (SAME application, second window)
HWND_C = 333  # Windows Terminal (unrelated app)

TITLE = "File Explorer"
FULL_TITLE = "File Explorer - Project Opus"
OTHER_TITLE = "Windows Terminal"

STALE_TEXT = "Resource list changed"
CAPTURE_ACK = "\U0001f4f8 Capturing..."


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


def _mock_success_capture(monkeypatch, tmp_path, focus_calls, capture_calls):
    """Mock the exact-HWND capture safely: REAL PNG bytes, zero real shots."""

    def _fake_focus(hwnd):
        focus_calls.append(hwnd)
        return True

    def _fake_capture(hwnd, path):
        capture_calls.append(hwnd)
        import PIL.Image

        PIL.Image.new("RGB", (6, 4), (200, 30, 30)).save(path, "PNG")
        return {"file": str(path)}

    monkeypatch.setattr(windows_resource_manager, "focus_window", _fake_focus)
    monkeypatch.setattr(
        "jarvis_core.screenshot_service.capture_window", _fake_capture)
    monkeypatch.setattr(resource_manager, "SCREENSHOT_DIR", tmp_path)
    monkeypatch.setattr(
        "jarvis_core.resource_manager.time.sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def _reset_registry():
    reg = resource_manager.resource_registry
    yield
    reg._generation = 0
    reg._resources = {}
    reg._feature_id = None


# ── 1. Exact HWND ────────────────────────────────────────────────────────────


def test_screenshot_captures_exact_selected_hwnd(monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False),
                     ("r1-a3", OTHER_TITLE, HWND_C, False)])
    focus_calls: list = []
    capture_calls: list = []
    _mock_success_capture(monkeypatch, tmp_path, focus_calls, capture_calls)

    service.process_update(_cb("cbS1", 123, "feature:resrc_shot:r1-a1"))

    assert capture_calls == [HWND_A], \
        "ONLY the exact selected File Explorer HWND is captured"
    assert focus_calls == [HWND_A], "focus (capture prerequisite) uses the HWND"
    assert len(api.photos) == 1
    assert api.photos[0]["chat_id"] == 100
    assert not scan_counter, "screenshot must not trigger a rescan"


# ── 2. Two same-name windows ─────────────────────────────────────────────────


def test_two_same_name_windows_capture_only_selected(monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False),
                     ("r1-a2", TITLE, HWND_B, False)])
    capture_calls: list = []
    _mock_success_capture(monkeypatch, tmp_path, [], capture_calls)

    service.process_update(_cb("cbS2", 123, "feature:resrc_shot:r1-a2"))

    assert capture_calls == [HWND_B], \
        "window B captured - never window A, never both, never the desktop"


# ── 3. Stale resource ────────────────────────────────────────────────────────


def test_stale_resource_never_captures_and_refreshes_panel(
        monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False),
                     ("r1-a3", OTHER_TITLE, HWND_C, False)])
    reg._generation = 2  # callback belongs to generation 1 -> stale

    def _forbidden(hwnd, path):
        raise AssertionError(f"capture attempted on stale hwnd {hwnd}")

    monkeypatch.setattr(
        "jarvis_core.screenshot_service.capture_window", _forbidden)

    service.process_update(_cb("cbS3", 123, "feature:resrc_shot:r1-a1"))

    assert api.photos == [], "stale resource must not produce any screenshot"
    assert any(STALE_TEXT in m["text"] for m in api.messages), \
        "visible stale warning is shown"
    assert len(api.edits) == 1
    edit = api.edits[0]
    assert edit["message_id"] == 777
    kb = _kb(edit)
    assert "applications" in edit["text"].lower(), \
        "stale refresh re-renders the panel with an Applications heading"
    assert "chrome" in edit["text"].lower(), \
        "stale refresh re-renders the panel with a Chrome heading"
    assert any("resrc_refresh_run" in b for b in kb), \
        "stale refresh offers the resrc_refresh_run button"
    assert not any("resrc_show" in b for b in kb), \
        "no feature:resrc_show callback remains after the stale refresh"
    assert scan_counter, "stale path re-scans to show the latest resources"


# ── 4. Missing HWND → no wrong fallback ──────────────────────────────────────


def test_missing_hwnd_fails_without_any_capture(monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a9", "Ghost App", None, False)])

    def _forbidden(*_a, **_kw):
        raise AssertionError("capture attempted without a valid HWND")

    monkeypatch.setattr(windows_resource_manager, "focus_window", _forbidden)
    monkeypatch.setattr(
        "jarvis_core.screenshot_service.capture_window", _forbidden)
    import PIL.ImageGrab

    monkeypatch.setattr(PIL.ImageGrab, "grab", _forbidden)

    service.process_update(_cb("cbS4", 123, "feature:resrc_shot:r1-a9"))

    assert api.photos == [], "no image may be delivered"
    texts = [m["text"] for m in api.messages]
    assert any("\u26a0\ufe0f Screenshot failed" in t for t in texts), \
        "visible failure message"
    assert any("no valid window handle" in t for t in texts), \
        "honest reason, no silent fallback"


# ── 5. Successful screenshot goes through Telegram ──────────────────────────


def test_success_sends_photo_and_cleans_temp(monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False)])
    capture_calls: list = []
    _mock_success_capture(monkeypatch, tmp_path, [], capture_calls)

    service.process_update(_cb("cbS5", 123, "feature:resrc_shot:r1-a1"))

    assert len(api.photos) == 1, "sendPhoto called exactly once"
    photo = api.photos[0]
    assert photo["chat_id"] == 100
    assert photo["existed"] is True, "temp file exists DURING upload"
    assert photo["size"] > 0
    assert not os.path.exists(photo["photo"]), \
        "temp file deleted after upload"
    assert api.messages == [], \
        "photo-only delivery: no extra success text message"
    assert getattr(api, "edits", []) == [], \
        "existing Resource Review control panel stays valid/untouched"


# ── 6. Caption carries the selected window title ────────────────────────────


def test_caption_contains_selected_window_title(monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", FULL_TITLE, HWND_A, False)])
    _mock_success_capture(monkeypatch, tmp_path, [], [])

    service.process_update(_cb("cbS6", 123, "feature:resrc_shot:r1-a1"))

    assert len(api.photos) == 1
    assert api.photos[0]["caption"] == \
        f"\U0001f4f8 Screenshot: {FULL_TITLE}"


# ── 7. State remains RESOURCE_REVIEW ─────────────────────────────────────────


def test_state_remains_resource_review_after_screenshot(
        monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False)])
    _mock_success_capture(monkeypatch, tmp_path, [], [])

    service.process_update(_cb("cbS7", 123, "feature:resrc_shot:r1-a1"))

    assert service._feature_orchestrator.mock_calls == [], \
        "screenshot must not advance/cancel the feature (stays RESOURCE_REVIEW)"
    assert getattr(api, "edits", []) == [], \
        "the Resource Review control panel message stays valid in place"
    assert reg.get("r1-a1") is not None, "resource stays tracked and usable"


# ── 8. Focus / Close actions are not triggered ───────────────────────────────


def test_focus_and_close_actions_not_triggered(monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False)])
    close_calls: list = []
    monkeypatch.setattr(
        windows_resource_manager, "close_window",
        lambda hwnd: close_calls.append(hwnd) or True)
    _mock_success_capture(monkeypatch, tmp_path, [], [])

    service.process_update(_cb("cbS8", 123, "feature:resrc_shot:r1-a1"))

    assert close_calls == [], "Close must never run from a Screenshot press"
    assert ("cbS8", CAPTURE_ACK) in api.callbacks, \
        "the callback is acknowledged as a screenshot capture"
    assert not any(
        text == "\U0001f441 Focusing..." for _, text in api.callbacks), \
        "the Focus action flow is not triggered"
    all_texts = [m["text"] for m in api.messages] + \
        [e["text"] for e in getattr(api, "edits", [])]
    assert not any("Focused:" in t for t in all_texts), \
        "no Focus outcome message - this was a screenshot"


# ── 9. No whole-desktop fallback on a dead HWND ──────────────────────────────


def test_dead_hwnd_honest_failure_no_desktop_fallback(
        monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_apps(reg, [("r1-a1", TITLE, HWND_A, False)])
    grab_calls: list = []
    _mock_success_capture(monkeypatch, tmp_path, [], [])

    def _dead_window(hwnd, path):
        raise ValueError("Window no longer exists.")

    def _forbidden_grab(*_a, **_kw):
        grab_calls.append(1)
        raise AssertionError("whole-desktop grab attempted")

    monkeypatch.setattr(
        "jarvis_core.screenshot_service.capture_window", _dead_window)
    monkeypatch.setattr(
        windows_resource_manager, "window_rect", lambda hwnd: None)
    import PIL.ImageGrab

    monkeypatch.setattr(PIL.ImageGrab, "grab", _forbidden_grab)

    service.process_update(_cb("cbS9", 123, "feature:resrc_shot:r1-a1"))

    assert grab_calls == [], "never falls back to a desktop capture"
    assert api.photos == [], "no wrong image is delivered"
    texts = [m["text"] for m in api.messages]
    assert any("\u26a0\ufe0f Screenshot failed" in t for t in texts)
    assert any("Could not capture that window" in t for t in texts), \
        "the real reason is surfaced, never silently swallowed"
