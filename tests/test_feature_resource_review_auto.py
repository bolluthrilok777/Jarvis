"""Regression tests: the Telegram Feature-Development resource review must
NEVER leave a task parked at 0% in "Waiting for resource review".

Flow proven here:
  submit -> resources phase -> resource scan -> review marked complete
         -> auto-continue -> "Feature request analyzed" (research stage).

A resource-scan failure must degrade to an empty snapshot and continue
(non-critical review), never wait for a user action that has no button.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from jarvis_core import resource_manager
from jarvis_core.feature_models import FeatureStatus
from jarvis_core.feature_orchestrator import (
    ACTION_RESEARCH,
    FeatureOrchestrator,
)
from jarvis_core.feature_store import FeatureStore
from jarvis_core.task_manager import TaskManager


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


@pytest.fixture()
def orch(tmp_path, monkeypatch):
    from tests.test_telegram_integration import patch_inline_feature_threads

    patch_inline_feature_threads(monkeypatch)
    store = FeatureStore(root=tmp_path / "features")
    orchestrator = FeatureOrchestrator(store=store, task_manager_instance=TaskManager())
    orchestrator.start_worker = lambda: None  # type: ignore[method-assign]
    orchestrator.set_telegram_channels(
        send=lambda chat_id, text, markup=None: None,
        is_authorized=lambda uid: uid == 123,
    )
    yield orchestrator
    orchestrator.stop_worker(timeout=1)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Reset the shared resource singleton between tests."""
    reg = resource_manager.resource_registry
    reg._generation = 0
    reg._resources = {}
    reg._feature_id = None
    reg._chrome_detected = False
    reg._chrome_controllable = False
    reg._chrome_reason = ""
    reg._last_scan_at = 0.0
    yield
    reg._generation = 0
    reg._resources = {}
    reg._feature_id = None


def _submit(orch, text="add a notes monitor feature"):
    return orch.submit_feature_request(
        text, source="Telegram",
        telegram_user_id=123, telegram_chat_id=100,
    )["feature_id"]


def test_resource_review_completes_and_auto_continues(orch):
    feature_id = _submit(orch)

    feature = orch.get_feature(feature_id)
    model = orch._store.get(feature_id)
    # Scan ran (inline thread) and the orchestrator auto-continued WITHOUT
    # any user action - no indefinite "waiting for user action" state.
    assert feature["resource_review_completed"] is True
    assert feature["status"] == FeatureStatus.received.value
    assert orch.list_resource_review_features(telegram_user_id=123) == []
    assert any("Resource review completed" in e.message
               for e in model.history)
    assert not orch._queue.empty(), "research must be enqueued after review"

    # Continuing the existing workflow reaches the analysis stage.
    with patch("jarvis_core.ollama_service.ask_ollama", _FakeOllama()), \
            patch("jarvis_core.feature_research.web_research", _fake_web):
        orch._run_action(ACTION_RESEARCH, feature_id)
    analyzed_model = orch._store.get(feature_id)
    assert any("Analyzing feature request" in e.message
               for e in analyzed_model.history)
    assert orch.get_feature(feature_id)[
        "status"] == FeatureStatus.chatgpt_review_pending.value


def test_resource_scan_failure_does_not_wait_for_user(orch):
    with patch("jarvis_core.resource_manager.resource_registry.scan",
               side_effect=RuntimeError("UIA exploded")):
        feature_id = _submit(orch)

    feature = orch.get_feature(feature_id)
    # Non-critical review: degrade to an empty snapshot and continue.
    assert feature["resource_review_completed"] is True
    assert feature["status"] == FeatureStatus.received.value
    assert orch.list_resource_review_features() == []


def test_resource_scan_timeout_degrades_and_continues(orch, monkeypatch):
    """Even a hung scan must time out and continue - never stall at 0%."""
    import threading as real_threading
    import jarvis_core.feature_orchestrator as fo_mod

    _OriginalThread = real_threading.Thread  # captured BEFORE patching

    class _RealThread:
        def __init__(self, target=None, name=None, daemon=None, **_kw):
            self._t = _OriginalThread(target=target, daemon=True)

        def start(self):
            self._t.start()

        def join(self, timeout=None):
            self._t.join(timeout)

        def is_alive(self):
            return self._t.is_alive()

    monkeypatch.setattr(fo_mod.threading, "Thread", _RealThread)
    monkeypatch.setattr(orch, "RESOURCE_SCAN_TIMEOUT_SECONDS", 0.05)
    reg = resource_manager.resource_registry

    def slow_scan(feature_id=None, cdp_url=None):
        time.sleep(0.5)  # far beyond the 0.05s budget
        return reg.scan_result(feature_id=feature_id)

    monkeypatch.setattr(reg, "scan", slow_scan)

    feature_id = _submit(orch)
    deadline = time.time() + 5
    while time.time() < deadline:
        if orch.get_feature(feature_id)[
                "status"] != FeatureStatus.resource_review_pending.value:
            break
        time.sleep(0.05)

    feature = orch.get_feature(feature_id)
    assert feature["resource_review_completed"] is True
    assert feature["status"] == FeatureStatus.received.value
    assert orch.list_resource_review_features() == []