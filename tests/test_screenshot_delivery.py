"""Targeted tests: SCREENSHOT CAPTURE -> TELEGRAM sendPhoto DELIVERY.

Regression coverage for the real physical-test failure where pressing
Screenshot left the Telegram user in an unbounded ``Working…`` state and no
image ever arrived.

Root cause (fixed): ``capture_window`` imported a non-existent
``window_rect`` helper; the ImportError escaped every guard and died silently
inside the poll loop, while ``_send_photo`` swallowed upload errors and the
bot claimed "Screenshot sent." regardless.

All external/destructive effects are mocked - nothing is uploaded and no real
window content leaves the machine during these tests.
"""
from __future__ import annotations

import os
import time
from unittest.mock import Mock

import PIL.Image
import pytest

from jarvis_core import resource_manager
from jarvis_core.resource_manager import (
    Resource,
    ResourceRegistry,
    resource_registry,
)
from jarvis_core.telegram_bot_api import TelegramAPIError
from jarvis_core.telegram_service import TelegramService
from tests.test_telegram_integration import FakeAPI, _settings


# ── Helpers / fixtures ───────────────────────────────────────────────────────


def _png(path, size=(6, 4)):
    """Write a REAL valid PNG so production-grade validation passes."""
    PIL.Image.new("RGB", size, (10, 120, 200)).save(path, "PNG")
    return {"file": str(path), "width": size[0], "height": size[1]}


def _cb(cb_id, user_id, data, chat_id=100):
    return {
        "update_id": 10,
        "callback_query": {
            "id": cb_id,
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}},
            "data": data,
        },
    }


@pytest.fixture()
def reg(monkeypatch, tmp_path):
    """Fresh shared-registry generation holding one safe app resource."""
    monkeypatch.setattr(resource_manager, "SCREENSHOT_DIR", tmp_path)
    reg = resource_registry
    reg._generation = 1
    reg._feature_id = None
    reg._cdp_url = None
    reg._chrome_detected = False
    reg._chrome_controllable = False
    reg._chrome_reason = ""
    reg._chrome_control_mode = ""
    reg._last_scan_at = 0.0
    app = Resource(
        resource_id="r1-a9", feature_id=None, resource_type="application",
        title="Calculator", display_name="Calculator", window_handle=901,
        close_requires_confirmation=False,
    )
    reg._resources = {app.resource_id: app}
    yield reg
    reg._generation = 0
    reg._resources = {}


@pytest.fixture()
def svc():
    api = FakeAPI()
    service = TelegramService(settings=_settings(123), api=api)
    service._start_task_watcher = Mock()
    return service, api


@pytest.fixture()
def captured_app(monkeypatch):
    """Focus + PrintWindow-capture mocked like the real GDI backend."""
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.focus_window",
        lambda hwnd: True,
    )
    recorded = []

    def _capture(hwnd, path):
        recorded.append(hwnd)
        return _png(path)

    monkeypatch.setattr(
        "jarvis_core.screenshot_service.capture_window", _capture,
    )
    return recorded


# ── Capture validity ─────────────────────────────────────────────────────────


def test_capture_produces_valid_image_bytes(reg, captured_app):
    out = reg.screenshot("r1-a9")
    assert out["status"] == "ok"
    assert out["bytes"] > 0
    assert out["width"] == 6 and out["height"] == 4
    assert os.path.isfile(out["file"])
    assert captured_app == [901]


def test_invalid_capture_is_rejected_not_delivered(reg, monkeypatch):
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.focus_window", lambda h: True)
    monkeypatch.setattr(
        "jarvis_core.screenshot_service.capture_window",
        lambda hwnd, path: open(path, "wb").close() or {"file": path},
    )
    out = reg.screenshot("r1-a9")
    assert out["status"] == "error"
    assert "invalid" in out["message"].lower()


# ── Telegram delivery ────────────────────────────────────────────────────────


def test_screenshot_success_sends_photo_then_deletes_temp(svc, reg,
                                                          captured_app):
    service, api = svc
    service.process_update(_cb("cb1", 123, "feature:resrc_shot:r1-a9"))

    assert len(api.photos) == 1, "sendPhoto must be called exactly once"
    photo = api.photos[0]
    assert photo["chat_id"] == 100
    assert photo["existed"] is True, "temp file must exist DURING upload"
    assert photo["size"] > 0
    assert not os.path.exists(photo["photo"]), \
        "temp file must be deleted only AFTER upload completed"
    assert api.messages == [], \
        "delivery sends ONLY the screenshot photo - no extra success text"
    assert getattr(api, "edits", []) == [], \
        "the existing control panel message stays valid and untouched"


def test_upload_failure_explicit_message_with_retry(svc, reg, captured_app,
                                                    monkeypatch):
    service, api = svc

    def _fail(*args, **kwargs):
        raise TelegramAPIError(
            "Telegram API request failed (ConnectionError).",
            error_type="ConnectionError",
        )

    monkeypatch.setattr(api, "send_photo", _fail)
    service.process_update(_cb("cb2", 123, "feature:resrc_shot:r1-a9"))

    texts = [m["text"] for m in api.messages]
    assert any("Telegram delivery failed" in t for t in texts)
    retry_msgs = [m for m in api.messages if m.get("reply_markup")]
    flat = [
        b["callback_data"]
        for m in retry_msgs
        for row in m["reply_markup"]["inline_keyboard"]
        for b in row
    ]
    assert "feature:resrc_shot:r1-a9" in flat, "Retry re-triggers the shot"
    assert api.photos == []


# ── Failure honesty & bounded behavior ───────────────────────────────────────


def test_capture_failure_reports_safe_reason(svc, reg, monkeypatch):
    service, api = svc
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.focus_window", lambda h: True)
    monkeypatch.setattr(
        "jarvis_core.screenshot_service.capture_window",
        Mock(side_effect=ValueError("Window no longer exists.")),
    )
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.window_rect", lambda h: None,
    )
    service.process_update(_cb("cb4", 123, "feature:resrc_shot:r1-a9"))
    texts = [m["text"] for m in api.messages]
    assert any("Screenshot failed" in t for t in texts)
    assert any("Could not capture that window" in t for t in texts), \
        "the real reason is surfaced to the user"
    assert api.photos == []


def test_timeout_is_bounded_never_infinite(reg, captured_app, monkeypatch):
    monkeypatch.setattr(ResourceRegistry, "CAPTURE_TIMEOUT_SECONDS", 0.3)

    def _slow(hwnd, path):
        time.sleep(1.5)
        return _png(path)

    monkeypatch.setattr(
        "jarvis_core.screenshot_service.capture_window", _slow,
    )
    started = time.monotonic()
    out = reg.screenshot("r1-a9")
    elapsed = time.monotonic() - started
    assert out["status"] == "error"
    assert out.get("timed_out") is True
    assert "timed out" in out["message"].lower()
    assert elapsed < 3.0, "timeout must be bounded, not minutes"


def test_sensitive_screenshot_blocked_with_lock_message(monkeypatch, tmp_path):
    monkeypatch.setattr(resource_manager, "SCREENSHOT_DIR", tmp_path)
    reg = resource_registry
    reg._generation = 1
    secret = Resource(
        resource_id="r1-t9", feature_id=None, resource_type="browser_tab",
        title="Sign in - .env passwords", display_name="Chrome",
        page_id="T9", safe_display_target="https://accounts.google.com",
    )
    reg._resources = {secret.resource_id: secret}
    capture_calls = []
    monkeypatch.setattr(
        "jarvis_core.screenshot_service.capture_window",
        lambda hwnd, path: capture_calls.append(hwnd),
    )

    api = FakeAPI()
    service = TelegramService(settings=_settings(123), api=api)
    service._start_task_watcher = Mock()
    service.process_update(_cb("cb5", 123, "feature:resrc_shot:r1-t9"))

    assert capture_calls == [], "sensitive surfaces are never captured"
    assert api.photos == []
    assert any("\U0001f512" in m["text"] for m in api.messages)


# ── Current Screen ───────────────────────────────────────────────────────────


def test_current_screen_success_sends_photo(svc, monkeypatch, tmp_path):
    service, api = svc
    monkeypatch.setattr(resource_manager, "SCREENSHOT_DIR", tmp_path)
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.foreground_window",
        lambda: None,
    )
    monkeypatch.setattr(
        "jarvis_core.uia_browser_manager.enumerate_tabs",
        lambda *a, **k: {"ok": True, "reason": "", "tabs": [
            {"title": "Safe Reading", "selected": True},
            {"title": "Docs", "selected": False},
        ]},
    )
    screen_calls = []
    monkeypatch.setattr(
        "jarvis_core.screenshot_service.capture_screen",
        lambda path: screen_calls.append(path) or _png(path, (8, 5)),
    )

    service.process_update(_cb("cb7", 123, "feature:resrc_screen:"))

    assert len(screen_calls) == 1
    assert len(api.photos) == 1 and api.photos[0]["existed"]
    assert not os.path.exists(api.photos[0]["photo"])
    assert not any("delivered" in m["text"].lower() for m in api.messages), \
        "no extra success text message after the screenshot"
    assert any("FEATURE RESOURCE REVIEW" in m["text"] for m in api.messages), \
        "the existing control panel is re-rendered in place (unchanged home)"


def test_current_screen_sensitive_selected_tab_blocks_send(svc, monkeypatch,
                                                           tmp_path):
    service, api = svc
    monkeypatch.setattr(resource_manager, "SCREENSHOT_DIR", tmp_path)
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.foreground_window",
        lambda: None,
    )
    monkeypatch.setattr(
        "jarvis_core.uia_browser_manager.enumerate_tabs",
        lambda *a, **k: {"ok": True, "reason": "", "tabs": [
            {"title": ".env passwords - Keeper", "selected": True},
        ]},
    )
    screen_calls = []
    monkeypatch.setattr(
        "jarvis_core.screenshot_service.capture_screen",
        lambda path: screen_calls.append(path) or _png(path),
    )

    service.process_update(_cb("cb8", 123, "feature:resrc_screen:"))

    assert screen_calls == []
    assert api.photos == []
    assert any("\U0001f512" in m["text"] for m in api.messages)


# ── Authorization (allowlist stays mandatory) ────────────────────────────────


def test_unauthorized_user_cannot_trigger_screenshots(reg, captured_app):
    api = FakeAPI()
    service = TelegramService(settings=_settings(123), api=api)
    service._start_task_watcher = Mock()

    service.process_update(_cb("cb9", 999, "feature:resrc_shot:r1-a9"))
    service.process_update(_cb("cb10", 999, "feature:resrc_screen:"))

    assert captured_app == [], "no capture may run for a non-allowlisted user"
    assert api.photos == []
    assert api.callbacks == [
        ("cb9", "Unauthorized"), ("cb10", "Unauthorized"),
    ], "unauthorized callbacks are answered with Unauthorized only"
    texts = [m["text"] for m in api.messages]
    assert any("not authorized" in t.lower() for t in texts)


def test_unexpected_failure_clears_working_state(svc, reg, monkeypatch):
    """Even an unexpected crash answers the callback and sends a final text."""
    service, api = svc
    monkeypatch.setattr(
        resource_registry, "screenshot",
        Mock(side_effect=RuntimeError("boom")),
    )
    service.process_update(_cb("cb6", 123, "feature:resrc_shot:r1-a9"))
    assert len(api.callbacks) == 1, "callback answered exactly once"
    assert any("could not be completed" in m["text"] for m in api.messages), \
        "an honest terminal message always follows"


def test_upload_failure_still_deletes_temp_file(svc, reg, captured_app,
                                                monkeypatch, tmp_path):
    service, api = svc
    monkeypatch.setattr(
        api, "send_photo",
        Mock(side_effect=TelegramAPIError("upload rejected")),
    )
    service.process_update(_cb("cb3", 123, "feature:resrc_shot:r1-a9"))
    leftovers = list((tmp_path / "resources").glob("*.png"))
    assert leftovers == [], "temp files are cleaned even when upload fails"

