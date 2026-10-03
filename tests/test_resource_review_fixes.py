"""Targeted regression tests for the Resource Review backend fixes."""
from __future__ import annotations

import os
from unittest.mock import Mock, patch

import PIL.Image
import pytest

from fastapi.testclient import TestClient

from jarvis_core import resource_manager
from jarvis_core.browser_resource_manager import discover_tabs
from jarvis_core.resource_manager import (
    Resource,
    resource_registry,
    _classify_application,
    _safe_app_identity,
)
from jarvis_core.telegram_bot_api import TelegramAPIError
from jarvis_core.telegram_service import TelegramService
from tests.test_telegram_integration import FakeAPI, _settings


def _png(path, size=(6, 4)):
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


class TestCalculatorRisk:
    def test_calc_exe_low_risk(self):
        allowed, needs_confirm = _classify_application(
            display_name="Calculator", process_name="CalculatorApp.exe",
            title="Calculator", protected=False,
            executable="C:\\Windows\\System32\\calc.exe",
        )
        assert allowed is True
        assert needs_confirm is False

    def test_safe_app_identity_modern_calculator(self):
        assert _safe_app_identity(
            "CalculatorApp.exe", "CalculatorApp.exe", "Calculator"
        ) is True

    def test_safe_app_identity_calc_alias(self):
        assert _safe_app_identity("calc.exe", "", "calc") is True


class TestUIAFallback:
    def test_uia_fallback_when_cdp_unreachable(self, monkeypatch):
        monkeypatch.setattr(resource_manager, "SCREENSHOT_DIR", "/tmp")
        reg = resource_registry
        reg._generation = 1
        reg._cdp_url = None

        fake_tabs = [
            {"title": "Tab One", "host_hwnd": 12345, "runtime_id": "1,2,3",
             "tab_index": 0, "selected": False, "control_mode": "uia"},
            {"title": "Tab Two", "host_hwnd": 12345, "runtime_id": "1,2,4",
             "tab_index": 1, "selected": True, "control_mode": "uia"},
        ]

        with patch("jarvis_core.browser_resource_manager.chrome_processes_running", return_value=True), \
             patch("jarvis_core.browser_resource_manager.cdp_reachable", return_value=False), \
             patch("jarvis_core.uia_browser_manager.enumerate_tabs",
                   return_value={"ok": True, "tabs": fake_tabs}):
            result = discover_tabs()

        assert result["chrome_detected"] is True
        assert result["controllable"] is True
        assert result["control_mode"] == "uia"
        assert len(result["tabs"]) == 2


class TestBackendSeparation:
    def test_close_app_rejects_browser_tab(self, monkeypatch, tmp_path):
        monkeypatch.setattr(resource_manager, "SCREENSHOT_DIR", tmp_path)
        reg = resource_registry
        reg._generation = 1
        reg._cdp_url = None
        reg._chrome_detected = True
        reg._chrome_controllable = True
        reg._chrome_control_mode = "uia"

        tab = Resource(
            resource_id="r1-t1", feature_id=None, resource_type="browser_tab",
            title="Test Tab", display_name="Test Tab", host_hwnd=12345,
            runtime_id="1,2,3", tab_index=0,
        )
        reg._resources = {tab.resource_id: tab}

        result = reg.close_application_window(tab.resource_id)
        assert result["status"] == "rejected"
        assert "tab" in result["message"].lower()

    def test_close_tab_rejects_application(self, monkeypatch, tmp_path):
        monkeypatch.setattr(resource_manager, "SCREENSHOT_DIR", tmp_path)
        reg = resource_registry
        reg._generation = 1

        app = Resource(
            resource_id="r1-a9", feature_id=None, resource_type="application",
            title="Calculator", display_name="Calculator", window_handle=901,
        )
        reg._resources = {app.resource_id: app}

        result = reg.close_browser_tab(app.resource_id)
        assert result["status"] == "rejected"
        assert "tab" in result["message"].lower()


class TestRuntimeInfoEndpoint:
    def test_runtime_info_returns_structure(self):
        from app import app
        client = TestClient(app)
        response = client.get("/api/runtime-info")
        assert response.status_code == 200
        data = response.json()
        assert "pid" in data
        assert "jarvis_modules" in data
        assert "dependencies" in data

    def test_runtime_info_dependencies(self):
        from app import app
        client = TestClient(app)
        response = client.get("/api/runtime-info")
        data = response.json()
        deps = data["dependencies"]
        assert "pywinauto" in deps
        assert "MISSING" not in deps["pywinauto"]


@pytest.fixture()
def reg_shot(monkeypatch, tmp_path):
    monkeypatch.setattr(resource_manager, "SCREENSHOT_DIR", tmp_path)
    reg = resource_registry
    reg._generation = 1
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
def svc_shot():
    api = FakeAPI()
    service = TelegramService(settings=_settings(123), api=api)
    service._start_task_watcher = Mock()
    return service, api


class TestScreenshotDelivery:
    def test_capture_produces_valid_image_bytes(self, reg_shot, monkeypatch):
        recorded = []
        monkeypatch.setattr("jarvis_core.windows_resource_manager.focus_window", lambda hwnd: True)
        monkeypatch.setattr("jarvis_core.screenshot_service.capture_window",
                            lambda hwnd, path: recorded.append(hwnd) or _png(path))
        out = reg_shot.screenshot("r1-a9")
        assert out["status"] == "ok"
        assert out["bytes"] > 0
        assert recorded == [901]

    def test_screenshot_success_sends_photo_then_deletes_temp(self, svc_shot, reg_shot, monkeypatch):
        monkeypatch.setattr("jarvis_core.windows_resource_manager.focus_window", lambda hwnd: True)
        monkeypatch.setattr("jarvis_core.screenshot_service.capture_window",
                            lambda hwnd, path: _png(path))
        service, api = svc_shot
        service.process_update(_cb("cb1", 123, "feature:resrc_shot:r1-a9"))
        assert len(api.photos) == 1
        photo = api.photos[0]
        assert photo["chat_id"] == 100
        assert not os.path.exists(photo["photo"])
        texts = [m["text"] for m in api.messages]
        assert any("Screenshot delivered" in t for t in texts)

    def test_sendphoto_failure_reports_error_with_retry(self, svc_shot, reg_shot, monkeypatch):
        monkeypatch.setattr("jarvis_core.windows_resource_manager.focus_window", lambda hwnd: True)
        monkeypatch.setattr("jarvis_core.screenshot_service.capture_window",
                            lambda hwnd, path: _png(path))

        def _fail(self, chat_id, photo_path, *, caption="", reply_markup=None):
            raise TelegramAPIError("upload rejected")

        monkeypatch.setattr(FakeAPI, "send_photo", _fail)
        service, api = svc_shot
        service.process_update(_cb("cb2", 123, "feature:resrc_shot:r1-a9"))

        texts = [m["text"] for m in api.messages]
        assert any("Screenshot captured but Telegram delivery failed" in t for t in texts)
        rows = [m.get("reply_markup", {}).get("inline_keyboard", []) for m in api.messages]
        flat = [btn["callback_data"] for row in rows for btn_row in row for btn in btn_row]
        assert "feature:resrc_shot:r1-a9" in flat
        assert api.photos == []

    def test_non_allowlisted_user_cannot_screenshot(self, svc_shot, reg_shot, monkeypatch):
        captured_app = []
        monkeypatch.setattr("jarvis_core.windows_resource_manager.focus_window", lambda h: True)
        monkeypatch.setattr("jarvis_core.screenshot_service.capture_window",
                            lambda hwnd, path: captured_app.append(hwnd) or _png(path))
        service, api = svc_shot
        service.process_update(_cb("cb10", 999, "feature:resrc_shot:r1-a9"))
        service.process_update(_cb("cb11", 999, "feature:resrc_screen:"))
        assert captured_app == []
        assert api.photos == []
        assert api.callbacks == [("cb10", "Unauthorized"), ("cb11", "Unauthorized")]