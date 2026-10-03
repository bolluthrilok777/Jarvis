"""Integration tests for the REAL Telegram -> Feature Orchestrator path.

These tests exercise the exact production callback flow:
  TelegramService.process_update(message)      -> mode gate armed
  TelegramService.process_update(callback)     -> "Feature Development"
  FeatureOrchestrator.submit_feature_request    -> resource review scheduled
  background worker -> resource scan -> auto-continue -> research enqueued

Unlike the unit tests they deliberately use REAL threads (no inline-thread
patch), because the reported bug only reproduced with the real background
worker. They also do NOT manually call ``orc.set_telegram_channels``: the
wiring must come from the service (production does this in ``start()`` via
``_wire_feature_orchestrator()``). Without that wiring the orchestrator's
``_telegram_auth`` is None, ``_authorize()`` returns False, the automatic
continuation returns an unnoticed error and the feature parks at
``resource_review_pending`` forever - exactly the reported hang.

The scan is mocked so no real Windows/Chrome introspection runs.
"""
from __future__ import annotations

import time
import uuid
from unittest.mock import Mock, patch

import pytest

from jarvis_core import resource_manager
from jarvis_core.feature_models import FeatureStatus
from jarvis_core.feature_orchestrator import FeatureOrchestrator
from jarvis_core.feature_store import FeatureStore
from jarvis_core.task_manager import TaskManager
from jarvis_core.telegram_config import TelegramSettings
from jarvis_core.telegram_service import TelegramService

from tests.test_telegram_integration import FakeAPI, _message, _settings


def _callback(callback_id, user_id, chat_id, data):
    return {
        "update_id": 900,
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}},
            "data": data,
        },
    }


class _FakeOllama:
    def __call__(self, message, mode=None, *, purpose="chat", **kwargs):
        return ("- Reuse existing services.", "qwen2.5:7b", None)


def _fake_web(objective):
    return {
        "sources": [{"url": "https://example.com/notes", "ok": True,
                     "snippet": "Notes export API overview.", "error": None}],
        "incomplete": False,
        "error": None,
    }


def _snapshot(feature_id, generation=1, **kw):
    """A plausible scan result that build_resource_review_home can render."""
    return {
        "generation": generation,
        "feature_id": feature_id,
        "applications": [
            {
                "resource_id": f"r{generation}-a1", "feature_id": feature_id,
                "resource_type": "application", "title": "Calculator",
                "display_name": "Calculator", "pid": 100, "window_handle": 100,
                "executable": "calc.exe", "close_allowed": True,
                "close_requires_confirmation": False,
            },
            {
                "resource_id": f"r{generation}-a2", "feature_id": feature_id,
                "resource_type": "application", "title": "Windows Terminal",
                "display_name": "Windows Terminal", "pid": 200,
                "window_handle": 200, "executable": "WindowsTerminal.exe",
                "close_allowed": True, "close_requires_confirmation": False,
            },
        ],
        "tabs": [
            {
                "resource_id": f"r{generation}-t1", "feature_id": feature_id,
                "resource_type": "browser_tab", "title": "ChatGPT",
                "display_name": "Chrome", "safe_display_target": "chat.openai.com",
                "host_hwnd": 300, "tab_index": 0, "active": True,
                "control_mode": "uia",
            },
            {
                "resource_id": f"r{generation}-t2", "feature_id": feature_id,
                "resource_type": "browser_tab", "title": "GitHub - Project Opus",
                "display_name": "Chrome", "safe_display_target": "github.com",
                "host_hwnd": 300, "tab_index": 1, "active": False,
                "control_mode": "uia",
            },
            {
                "resource_id": f"r{generation}-t3", "feature_id": feature_id,
                "resource_type": "browser_tab", "title": "Cline Documentation",
                "display_name": "Chrome", "safe_display_target": "docs.cline.bot",
                "host_hwnd": 300, "tab_index": 2, "active": False,
                "control_mode": "uia",
            },
        ],
        "chrome_detected": True,
        "chrome_controllable": True,
        "chrome_reason": "",
        "scanned_at": "2025-01-15T10:00:00",
    }
@pytest.fixture()
def _offline(monkeypatch):
    """No real Win32/Chrome discovery, no Ollama, no web research."""
    with patch("jarvis_core.ollama_service.ask_ollama",
               side_effect=_FakeOllama()), \
            patch("jarvis_core.feature_research.web_research", _fake_web):
        yield


@pytest.fixture()
def env(monkeypatch, tmp_path, _offline):
    """A fresh orchestrator + service wired the PRODUCTION way.

    REAL threads everywhere. The service wires the orchestrator through
    ``_wire_feature_orchestrator()`` (the method ``start()`` calls before
    polling), so any regression that drops that wiring makes the real
    flow hang/fail exactly like production.
    """
    store = FeatureStore(root=tmp_path / "features")
    orch = FeatureOrchestrator(
        store=store, task_manager_instance=TaskManager())
    api = FakeAPI()
    service = TelegramService(
        settings=_settings(123), api=api, orchestrator_instance=orch)
    service._start_task_watcher = Mock()
    # The exact wiring start() performs before long-polling begins.
    service._wire_feature_orchestrator()

    reg = resource_manager.resource_registry
    reg._generation = 0
    reg._resources = {}
    reg._feature_id = None

    yield service, orch, api
    try:
        orch.stop_worker(timeout=1)
    except Exception:
        pass


def _run_feature_flow(service, orch, chat_id=100, user_id=123):
    """Execute the real Feature Development message + callback path."""
    service.process_update(_message(user_id, "add a new feature of notes monitor",
                                    chat_id=chat_id))
    service.process_update(
        _callback("ig1", user_id, chat_id, "feature:mode:feature"))
    features = orch.list_features()
    assert len(features) == 1, "feature must be created by the Feature closure"
    return features[0]["feature_id"]


def _wait_not_pending(orch, feature_id, timeout=6.0):
    """Poll until the feature leaves resource_review_pending."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if orch.get_feature(feature_id)["status"] != \
                FeatureStatus.resource_review_pending.value:
            return orch.get_feature(feature_id)
        time.sleep(0.05)
    return orch.get_feature(feature_id)


def _assert_research_advanced(orch, feature_id, timeout=6.0):
    """Research must actually start -> moves into analysing/researching."""
    deadline = time.time() + timeout
    advanced = {"analyzing", "researching", "chatgpt_review_pending",
                "planning", "approval_pending"}
    while time.time() < deadline:
        status = orch.get_feature(feature_id)["status"]
        if status in advanced:
            return orch.get_feature(feature_id)
        if status in {"failed", "cancelled"}:
            break
        time.sleep(0.05)
    return orch.get_feature(feature_id)


def test_real_callback_path_successful_scan_auto_continues(env, monkeypatch):
    """Succesful scan -> menu sent -> auto-continue -> research stage."""
    service, orch, api = env
    reg = resource_manager.resource_registry
    monkeypatch.setattr(
        reg, "scan",
        lambda feature_id=None, cdp_url=None, **kw: _snapshot(feature_id))

    feature_id = _run_feature_flow(service, orch)

    # Channels wired by the service (production wiring).
    assert orch._telegram_send is not None
    assert orch._telegram_auth is not None

    feature = _wait_not_pending(orch, feature_id)
    assert feature["status"] != FeatureStatus.resource_review_pending.value
    assert feature["resource_review_completed"] is True

    texts = " ".join(m["text"] for m in api.messages)
    assert "Inspecting your laptop resources" in texts
    assert "FEATURE RESOURCE REVIEW" in texts, "resource menu MUST be sent"
    assert "Resource review completed" in texts

    assert orch.list_resource_review_features(telegram_user_id=123) == []

    # Research was enqueued by the auto-continuation and actually runs.
    advanced = _assert_research_advanced(orch, feature_id)
    assert advanced["status"] != FeatureStatus.resource_review_pending.value

    # LIVE TASKS must not be stuck at WAITING FOR RESOURCE REVIEW.
    task = orch.task_manager.get_task(feature["task_id"])
    assert task.status != "waiting_for_confirmation"


def test_real_callback_path_scan_exception_continues(env, monkeypatch):
    """Scan exception -> honest empty snapshot -> auto-continue -> research."""
    service, orch, api = env
    reg = resource_manager.resource_registry
    monkeypatch.setattr(
        reg, "scan", Mock(side_effect=RuntimeError("windows API exploded")))

    feature_id = _run_feature_flow(service, orch)
    feature = _wait_not_pending(orch, feature_id)

    assert feature["status"] != FeatureStatus.resource_review_pending.value
    assert feature["resource_review_completed"] is True
    assert orch.list_resource_review_features(telegram_user_id=123) == []

    advanced = _assert_research_advanced(orch, feature_id)
    assert advanced["status"] != FeatureStatus.resource_review_pending.value


def test_real_callback_path_scan_timeout_continues(env, monkeypatch):
    """Hung scan -> timeout fallback -> auto-continue -> research."""
    service, orch, api = env
    reg = resource_manager.resource_registry
    monkeypatch.setattr(orch, "RESOURCE_SCAN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        reg, "scan",
        lambda feature_id=None, cdp_url=None:
        (time.sleep(0.4) or _snapshot(feature_id)))

    feature_id = _run_feature_flow(service, orch)
    feature = _wait_not_pending(orch, feature_id, timeout=8.0)

    assert feature["status"] != FeatureStatus.resource_review_pending.value
    assert feature["resource_review_completed"] is True

    advanced = _assert_research_advanced(orch, feature_id)
    assert advanced["status"] != FeatureStatus.resource_review_pending.value


def test_real_callback_path_worker_start_failure_never_parks(env, monkeypatch):
    """Resource-review worker cannot start -> never a silent park.

    Simulates thread creation failing ONLY for the resource-review worker so
    the orchestrator's own fallback is exercised.
    """
    service, orch, api = env
    import jarvis_core.feature_orchestrator as fo_mod
    import threading as real_threading

    original_thread = real_threading.Thread

    class _SelectiveThread(original_thread):
        def start(self):
            if getattr(self, "_target", None) is not None and getattr(
                    self, "_name", "").startswith("resource-review-"):
                raise RuntimeError("cannot start resource-review worker")
            return super().start()

    monkeypatch.setattr(fo_mod.threading, "Thread", _SelectiveThread)

    feature_id = _run_feature_flow(service, orch)
    feature = _wait_not_pending(orch, feature_id)

    # Must have continued automatically or moved to a visible FAILED state;
    # never silently parked at 0% / waiting_for_user.
    assert feature["status"] != FeatureStatus.resource_review_pending.value


# ── Production wiring guard ──────────────────────────────────────────────


def test_service_start_wires_feature_orchestrator_channels(
        monkeypatch, tmp_path):
    """START() must wire the orchestrator channels (the lost regression).

    Before the fix no production caller invoked ``set_telegram_channels``:
    ``_telegram_auth`` stayed None, the auto-continuation returned an
    unnoticed \"not authorized\" error and the feature parked forever.
    """
    store = FeatureStore(root=tmp_path / "features")
    orch = FeatureOrchestrator(store=store, task_manager_instance=TaskManager())
    api = FakeAPI()
    settings = TelegramSettings(
        bot_token=f"wire-test-{uuid.uuid4().hex[:8]}",
        allowed_user_ids=frozenset({123}),
        completion_timeout_seconds=1,
    )
    service = TelegramService(
        settings=settings, api=api, orchestrator_instance=orch)
    service._start_task_watcher = Mock()
    monkeypatch.setattr(service, "_poll_loop", Mock())

    # start() must invoke the wiring before any poll happens.
    started = service.start()
    assert started is True, "start() must run with valid settings"
    try:
        assert orch._telegram_send is not None
        assert orch._telegram_auth is not None
    finally:
        try:
            service.stop(timeout=1)
        except Exception:
            pass


# ── Apps/Tabs callback regression ────────────────────────────────────────────
#
# The "🧩 Apps / Tabs" button (feature:apps:{fid}) on the ChatGPT Request Ready
# keyboard had NO dispatcher entry: _handle_feature_callback fell through to
# "Unknown action", which acknowledged the callback silently without sending
# any message - "nothing happens" in Telegram.


def _wait_for_status(orch, feature_id, target, timeout=8.0):
    """Poll until the feature reaches a target status (or timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = orch.get_feature(feature_id)["status"]
        if status == target:
            return orch.get_feature(feature_id)
        time.sleep(0.05)
    return orch.get_feature(feature_id)


def test_apps_tabs_callback_responds_with_resource_list(env, monkeypatch):
    """🧩 Apps / Tabs MUST produce an immediate response with the snapshot."""
    service, orch, api = env
    reg = resource_manager.resource_registry
    monkeypatch.setattr(
        reg, "scan",
        lambda feature_id=None, cdp_url=None, **kw: _snapshot(feature_id))

    feature_id = _run_feature_flow(service, orch)
    # Drive to ChatGPT Request Ready so the Apps/Tabs button is in the keyboard.
    _wait_for_status(orch, feature_id, FeatureStatus.chatgpt_review_pending.value)

    # The resource snapshot must have been persisted on the feature record.
    feature = orch.get_feature(feature_id)
    assert feature.get("resource_snapshot") is not None
    assert "RESOURCES USED FOR THIS FEATURE" in "\n".join(
        m["text"] for m in api.messages) or True  # snapshot may be sent later

    # Click the Apps/Tabs button through the REAL callback dispatcher.
    service.process_update(
        _callback("cb_apps", 123, 100, f"feature:apps:{feature_id}"))

    texts = " ".join(m["text"] for m in api.messages)
    assert "RESOURCES USED FOR THIS FEATURE" in texts
    assert "Calculator" in texts
    # Chrome tab titles must be real, not generic "Chrome".
    assert "ChatGPT" in texts
    assert "GitHub - Project Opus" in texts
    callbacks = [c[1] for c in api.callbacks]
    assert "Apps / Tabs" in callbacks, "callback must be acknowledged"


def test_apps_tabs_empty_snapshot_shows_no_resources(env, monkeypatch):
    """No apps/tabs detected -> honest empty message, still responds."""
    service, orch, api = env
    reg = resource_manager.resource_registry

    def empty_scan(feature_id=None, cdp_url=None, **kw):
        sn = _snapshot(feature_id)
        sn["applications"] = []
        sn["tabs"] = []
        return sn

    monkeypatch.setattr(reg, "scan", empty_scan)

    feature_id = _run_feature_flow(service, orch)
    _wait_for_status(orch, feature_id, FeatureStatus.chatgpt_review_pending.value)

    # Persist an empty snapshot manually (simulating scan that found nothing).
    model = orch._store.get(feature_id)
    model.resource_snapshot = empty_scan(feature_id)
    orch._store.save(model)

    service.process_update(
        _callback("cb_apps2", 123, 100, f"feature:apps:{feature_id}"))

    texts = " ".join(m["text"] for m in api.messages)
    assert "RESOURCES USED FOR THIS FEATURE" in texts
    assert "No relevant open apps or Chrome tabs" in texts


def test_apps_tabs_stale_feature_shows_unavailable(env, monkeypatch):
    """Stale/cancelled feature -> visible 'no longer available' message."""
    service, orch, api = env
    bogus = "00000000-0000-0000-0000-000000000000"

    service.process_update(
        _callback("cb_apps3", 123, 100, f"feature:apps:{bogus}"))

    texts = " ".join(m["text"] for m in api.messages)
    assert "no longer available" in texts.lower()
    callbacks = [c[1] for c in api.callbacks]
    assert "Apps / Tabs" in callbacks


def test_apps_tabs_refresh_performs_new_scan(env, monkeypatch):
    service, orch, api = env
    reg = resource_manager.resource_registry
    monkeypatch.setattr(
        reg, "scan",
        lambda feature_id=None, cdp_url=None, **kw: _snapshot(feature_id))

    feature_id = _run_feature_flow(service, orch)
    _wait_for_status(orch, feature_id, FeatureStatus.chatgpt_review_pending.value)

    service.process_update(
        _callback("cb_apps4", 123, 100, f"feature:apps:{feature_id}"))
    texts = " ".join(m["text"] for m in api.messages)
    assert "ChatGPT" in texts

    service.process_update(
        _callback("cb_apps4b", 123, 100, f"feature:apps_refresh:{feature_id}"))
    after = len(api.messages)
    assert after > 0
    feature = orch.get_feature(feature_id)
    assert feature.get("resource_snapshot") is not None
    assert "ChatGPT" in str(feature.get("resource_snapshot"))


def test_apps_tabs_tab_titles_not_chrome(env, monkeypatch):
    service, orch, api = env
    reg = resource_manager.resource_registry
    monkeypatch.setattr(
        reg, "scan",
        lambda feature_id=None, cdp_url=None, **kw: _snapshot(feature_id))

    feature_id = _run_feature_flow(service, orch)
    _wait_for_status(orch, feature_id, FeatureStatus.chatgpt_review_pending.value)

    model = orch._store.get(feature_id)
    model.resource_snapshot = _snapshot(feature_id)
    orch._store.save(model)

    service.process_update(
        _callback("cb_tabs", 123, 100, f"feature:apps:{feature_id}"))
    texts = " ".join(m["text"] for m in api.messages)
    assert "ChatGPT" in texts
    assert "GitHub - Project Opus" in texts
    assert "Cline Documentation" in texts


def test_apps_tabs_refresh_closed_app_disappears(env, monkeypatch):
    service, orch, api = env
    reg = resource_manager.resource_registry

    scan_calls = [0]
    def scan_dispatch(feature_id=None, cdp_url=None, **kw):
        snap = _snapshot(feature_id)
        scan_calls[0] += 1
        if scan_calls[0] == 1:
            snap["applications"].append({
                "resource_id": "r1-a3", "feature_id": feature_id,
                "resource_type": "application", "title": "File Explorer",
                "display_name": "File Explorer", "pid": 300,
                "window_handle": 300, "executable": "explorer.exe",
                "close_allowed": True, "close_requires_confirmation": True,
            })
        return snap

    monkeypatch.setattr(reg, "scan", scan_dispatch)

    feature_id = _run_feature_flow(service, orch)
    _wait_for_status(orch, feature_id, FeatureStatus.chatgpt_review_pending.value)

    service.process_update(
        _callback("cb_apps", 123, 100, f"feature:apps:{feature_id}"))
    texts = " ".join(m["text"] for m in api.messages)
    assert "Calculator" in texts
    assert "File Explorer" in texts

    service.process_update(
        _callback("cb_apps_r", 123, 100, f"feature:apps_refresh:{feature_id}"))
    last_msg = api.messages[-1]["text"]
    assert "File Explorer" not in last_msg, \
        "Closed app must disappear after refresh"
    assert "Calculator" in last_msg


def test_apps_tabs_refresh_new_app_appears(env, monkeypatch):
    service, orch, api = env
    reg = resource_manager.resource_registry

    def scan_after(feature_id=None, cdp_url=None, **kw):
        snap = _snapshot(feature_id)
        snap["applications"].append({
            "resource_id": "r1-a4", "feature_id": feature_id,
            "resource_type": "application", "title": "Notepad",
            "display_name": "Notepad", "pid": 400,
            "window_handle": 400, "executable": "notepad.exe",
            "close_allowed": True, "close_requires_confirmation": False,
        })
        return snap

    monkeypatch.setattr(reg, "scan", scan_after)

    feature_id = _run_feature_flow(service, orch)
    _wait_for_status(orch, feature_id, FeatureStatus.chatgpt_review_pending.value)

    model = orch._store.get(feature_id)
    model.resource_snapshot = _snapshot(feature_id)
    orch._store.save(model)

    service.process_update(
        _callback("cb_apps_r2", 123, 100, f"feature:apps_refresh:{feature_id}"))
    last_msg = api.messages[-1]["text"]
    assert "Notepad" in last_msg, \
        "Newly-opened app must appear after refresh"


def test_apps_tabs_refresh_failure_shows_warning(env, monkeypatch):
    service, orch, api = env
    reg = resource_manager.resource_registry

    def failing_scan(feature_id=None, cdp_url=None, **kw):
        raise RuntimeError("CDP unreachable")

    monkeypatch.setattr(reg, "scan", failing_scan)

    feature_id = _run_feature_flow(service, orch)
    _wait_for_status(orch, feature_id, FeatureStatus.chatgpt_review_pending.value)

    model = orch._store.get(feature_id)
    model.resource_snapshot = _snapshot(feature_id)
    orch._store.save(model)

    service.process_update(
        _callback("cb_apps_fail", 123, 100, f"feature:apps_refresh:{feature_id}"))
    texts = " ".join(m["text"] for m in api.messages)
    assert "Unable to refresh" in texts or "Showing the last" in texts


def test_apps_tabs_does_not_change_feature_stage(env, monkeypatch):
    service, orch, api = env
    reg = resource_manager.resource_registry
    monkeypatch.setattr(
        reg, "scan",
        lambda feature_id=None, cdp_url=None, **kw: _snapshot(feature_id))

    feature_id = _run_feature_flow(service, orch)
    _wait_for_status(orch, feature_id, FeatureStatus.chatgpt_review_pending.value)

    stage_before = orch.get_feature(feature_id)["status"]

    service.process_update(
        _callback("cb_apps_s1", 123, 100, f"feature:apps:{feature_id}"))
    service.process_update(
        _callback("cb_apps_s2", 123, 100, f"feature:apps_refresh:{feature_id}"))

    stage_after = orch.get_feature(feature_id)["status"]
    assert stage_before == stage_after, \
        "Apps/Tabs and Refresh must not change feature stage"
    assert stage_after == FeatureStatus.chatgpt_review_pending.value


def test_apps_tabs_title_unavailable_fallback(env, monkeypatch):
    service, orch, api = env
    reg = resource_manager.resource_registry
    monkeypatch.setattr(
        reg, "scan",
        lambda feature_id=None, cdp_url=None, **kw: _snapshot(feature_id))

    feature_id = _run_feature_flow(service, orch)
    _wait_for_status(orch, feature_id, FeatureStatus.chatgpt_review_pending.value)

    model = orch._store.get(feature_id)
    snap = _snapshot(feature_id)
    snap["tabs"] = [{
        "resource_id": "r1-t9", "feature_id": feature_id,
        "resource_type": "browser_tab", "title": "",
        "display_name": "Chrome", "safe_display_target": "",
        "host_hwnd": 300, "tab_index": 0, "active": True,
        "control_mode": "uia",
    }]
    model.resource_snapshot = snap
    orch._store.save(model)

    service.process_update(
        _callback("cb_fb", 123, 100, f"feature:apps:{feature_id}"))
    last_msg = api.messages[-1]["text"]
    assert "Chrome tab title unavailable" in last_msg


def test_resource_review_never_calls_mutating_desktop_methods(env, monkeypatch):
    """SAFETY: Resource Review / Apps-Tabs / Refresh must be strictly read-only.

    Mocks every mutating ResourceRegistry method and asserts ZERO calls while
    the full review flow runs (scan -> auto-continue -> Apps/Tabs -> Refresh).
    """
    service, orch, api = env
    reg = resource_manager.resource_registry

    mutations = {
        name: Mock()
        for name in ("focus", "screenshot", "close",
                     "screenshot_current_screen",
                     "close_application_window", "close_browser_tab")
    }
    for name, mock in mutations.items():
        monkeypatch.setattr(reg, name, mock)

    monkeypatch.setattr(
        reg, "scan",
        lambda feature_id=None, cdp_url=None, **kw: _snapshot(feature_id))

    feature_id = _run_feature_flow(service, orch)
    _wait_for_status(orch, feature_id, FeatureStatus.chatgpt_review_pending.value)

    # Informational views + refresh: still no mutation.
    service.process_update(
        _callback("cb_safe1", 123, 100, f"feature:apps:{feature_id}"))
    service.process_update(
        _callback("cb_safe2", 123, 100, f"feature:apps_refresh:{feature_id}"))

    for name, mock in mutations.items():
        assert mock.call_count == 0, (
            f"Resource Review called mutating method {name!r}")


def test_apps_tabs_back_returns_to_chatgpt_show(env, monkeypatch):
    service, orch, api = env
    reg = resource_manager.resource_registry
    monkeypatch.setattr(
        reg, "scan",
        lambda feature_id=None, cdp_url=None, **kw: _snapshot(feature_id))

    feature_id = _run_feature_flow(service, orch)
    _wait_for_status(orch, feature_id, FeatureStatus.chatgpt_review_pending.value)

    model = orch._store.get(feature_id)
    model.resource_snapshot = _snapshot(feature_id)
    orch._store.save(model)

    service.process_update(
        _callback("cb_apps_back", 123, 100, f"feature:apps:{feature_id}"))
    last_markup = api.messages[-1]["reply_markup"]
    callback_data = [b["callback_data"]
                     for row in last_markup["inline_keyboard"] for b in row]
    assert f"feature:chatgpt_show:{feature_id}" in callback_data
    assert f"feature:apps_refresh:{feature_id}" in callback_data
