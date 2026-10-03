"""Targeted regression tests: Feature Orchestrator work must NOT break ordinary
Web commands or Telegram commands.

Covers the Web path used by ui.html submitCommand():
  Web UI -> /tasks/submit -> feature detection split -> chat | task | feature
Focus: normal commands must never be swallowed by the Feature Orchestrator,
feature requests must still return feature_created, normal tasks must still be
created/polled/renderable, and Telegram routing must remain untouched.
"""
from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from jarvis_core.feature_detector import detect_feature_request
from jarvis_core.feature_store import FeatureStore
from jarvis_core.task_manager import TaskManager


# ── Web /tasks/submit split ──────────────────────────────────────────────────


@pytest.fixture()
def web_client(tmp_path):
    """TestClient with the feature-orchestrator singleton isolated onto a temp
    store, its worker disabled (mirrors tests/test_feature_orchestrator.py's
    web_client fixture so neither tests nor the real app state leak)."""
    import jarvis_core.feature_orchestrator as fo_module
    from app import app

    fo = fo_module.feature_orchestrator
    original_store = fo._store
    original_tm = fo._task_manager_ref
    original_start = fo.start_worker
    original_enqueue = fo.enqueue
    fo._store = FeatureStore(root=tmp_path / "features")
    fo._task_manager_ref = TaskManager()
    fo.start_worker = lambda: None  # type: ignore[method-assign]
    fo.enqueue = lambda action, fid: None  # type: ignore[method-assign]
    try:
        from fastapi.testclient import TestClient

        yield TestClient(app)
    finally:
        fo.stop_worker(timeout=1)
        fo.start_worker = original_start  # type: ignore[method-assign]
        fo.enqueue = original_enqueue  # type: ignore[method-assign]
        fo._store = original_store
        fo._task_manager_ref = original_tm


def test_web_hello_routes_to_existing_chat_not_feature(web_client):
    """The exact regression case: Hello must go to the conversational path."""
    response = web_client.post("/tasks/submit", json={"text": "Hello"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    result = data["results"][0]
    assert result["action"] == "chat"
    assert result["task_id"] is None
    assert result["message"] == "Routed to AI chat"


def test_web_hi_routes_to_existing_chat(web_client):
    response = web_client.post("/tasks/submit", json={"text": "Hi"})
    result = response.json()["results"][0]
    assert result["action"] == "chat"


def test_web_hello_jarvis_routes_to_existing_chat(web_client):
    response = web_client.post("/tasks/submit", json={"text": "Hello Jarvis"})
    result = response.json()["results"][0]
    assert result["action"] == "chat"


def test_web_normal_question_routes_to_chat(web_client):
    """A question is conversational — it must not become a Feature request."""
    response = web_client.post("/tasks/submit", json={"text": "What is the system status?"})
    result = response.json()["results"][0]
    assert result["action"] == "chat"


def test_web_system_status_command_creates_normal_task(web_client):
    """'system status' is a real command (not a question) -> normal task."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        with patch("jarvis_core.orchestrator.classify_risk", return_value="SAFE"):
            with patch("jarvis_core.orchestrator.classify_concurrency", return_value="background"):
                with patch(
                    "jarvis_core.orchestrator.build_interpreted_command",
                    return_value="system status",
                ):
                    response = web_client.post("/tasks/submit", json={"text": "system status"})
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["action"] == "created"
    assert result["task_id"]
    assert result["status"] == "queued"
def test_web_executable_command_creates_normal_task(web_client):
    """Ordinary executable commands still create LIVE TASKS."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        with patch("jarvis_core.orchestrator.classify_risk", return_value="SAFE"):
            with patch("jarvis_core.orchestrator.classify_concurrency", return_value="background"):
                with patch(
                    "jarvis_core.orchestrator.build_interpreted_command",
                    side_effect=lambda x: x,
                ):
                    response = web_client.post("/tasks/submit", json={"text": "open calculator"})
    result = response.json()["results"][0]
    assert result["action"] == "created"
    assert result["task_id"]


def test_web_feature_request_returns_feature_created(web_client):
    """A genuine feature-development request still reaches the Feature path."""
    response = web_client.post(
        "/tasks/submit", json={"text": "Jarvis add a simple notes export feature"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    result = data["results"][0]
    assert result["action"] == "feature_created"
    assert result["feature_id"]
    assert result["task_id"]
    assert "FEATURE REQUEST RECEIVED" in result["message"]


@pytest.mark.parametrize(
    "text",
    [
        "Hello",
        "Hi",
        "Hey",
        "Hello Jarvis",
        "What is the system status?",
        "Open calculator",
        "calculate 25*4",
        "system status",
        "show system status",
        "gmail status",
        # Fused-verb safety: a feature verb prefix must NOT auto-promote a word
        # that merely starts with that verb (no feature noun present).
        "addition",
        "address the package",
    ],
)
def test_normal_commands_are_not_feature_requests(text):
    """Detection-level guarantee: ordinary Web commands never become Features."""
    detection = detect_feature_request(text)
    assert detection["is_feature"] is False, text


@pytest.mark.parametrize(
    "text",
    [
        "add a note feature",
        "adda note feature",  # dropped-space/fused verb typo (T-1 regression)
        "add note feature",
        "create a note feature",
        "build a note feature",
        "implement a note feature",
        "add gmail analyser feature",
        "add a gmail analyser feature",
    ],
)
def test_feature_request_with_typo_variants_detected(text):
    """Regression T-1: a dropped-space/fused verb ('adda') must NOT be silently
    treated as a normal command and queued as a LIVE TASK. It must still be
    detected as a feature request so the Telegram clarification gate fires."""
    detection = detect_feature_request(text)
    assert detection["is_feature"] is True, text
    assert detection["objective"], text


def test_feature_request_still_detected():
    assert detect_feature_request("Jarvis add calendar integration")["is_feature"] is True
    assert (
        detect_feature_request("Jarvis add a simple notes export feature")["is_feature"] is True
    )
def test_normal_task_response_creates_and_lists_pollable_task(web_client):
    """The task the UI polls (GET /tasks, GET /tasks/{id}) must contain it."""
    from jarvis_core.task_manager import task_manager

    task_manager.clear_all_tasks()
    try:
        with patch("jarvis_core.orchestrator.classify", return_value="command"):
            with patch("jarvis_core.orchestrator.classify_risk", return_value="SAFE"):
                with patch(
                    "jarvis_core.orchestrator.classify_concurrency", return_value="background"
                ):
                    with patch(
                        "jarvis_core.orchestrator.build_interpreted_command",
                        side_effect=lambda x: x,
                    ):
                        submitted = web_client.post(
                            "/tasks/submit", json={"text": "Open calculator"}
                        )
        task_id = submitted.json()["results"][0]["task_id"]

        listing = web_client.get("/tasks").json()
        assert listing["status"] == "ok"
        assert task_id in {t["task_id"] for t in listing["tasks"]}

        single = web_client.get(f"/tasks/{task_id}").json()
        assert single["status"] == "ok"
        assert single["task"]["task_type"] == "command"
    finally:
        task_manager.clear_all_tasks()


def test_final_normal_response_renders_via_chat_endpoint(web_client):
    """The frontend renders the final greeting by calling /chat with the echoed
    line — that endpoint must return a reply for a normal greeting."""
    with patch(
        "jarvis_core.conversation_service.ask_ollama",
        return_value=("Hello! How can I help you today?", "test-model", None),
    ):
        response = web_client.post("/chat", json={"message": "Hello", "mode": "auto"})
    assert response.status_code == 200
    data = response.json()
    assert data.get("reply") == "Hello! How can I help you today?"


# ── Telegram regression guard ─────────────────────────────────────────────────


class _FakeTelegramAPI:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, *, reply_markup=None):
        self.messages.append({"chat_id": chat_id, "text": text})
        return {}

    def answer_callback_query(self, callback_query_id, text=""):
        return {}

    def close(self):
        return None


class _FakeOrchestrator:
    """Minimal stand-in that records normal submits (not features)."""

    def __init__(self):
        self.submit_calls = []

    def submit(self, text, **kwargs):
        self.submit_calls.append((text, kwargs))
        return {
            "task_id": "task-1",
            "status": "queued",
            "action": "created",
            "message": "queued",
        }


def test_telegram_normal_command_remains_unaffected():
    """Telegram still routes normal commands via the existing orchestrator, and
    never sends them into the Feature Orchestrator."""
    from jarvis_core.telegram_config import TelegramSettings
    from jarvis_core.telegram_service import TelegramService

    api = _FakeTelegramAPI()
    orchestrator = _FakeOrchestrator()
    feature = Mock()
    feature.is_feature_request.return_value = False

    service = TelegramService(
        settings=TelegramSettings(bot_token="test-token", allowed_user_ids=frozenset({123})),
        api=api,
        orchestrator_instance=orchestrator,
    )
    service._start_task_watcher = Mock()

    greeting = {
        "update_id": 1,
        "message": {
            "from": {"id": 123},
            "chat": {"id": 100},
            "text": "Hello",
        },
    }
    # The service stores the injected feature orchestrator as a direct
    # attribute (property-backed); patch the attribute itself.
    with patch.object(service, "_feature_orchestrator", feature):
        service.process_update(greeting)

    # Normal command went through the shared orchestrator, untouched.
    assert orchestrator.submit_calls == [(
        "Hello",
        {"source": "Telegram", "track_conversation": True, "conversation_id": "telegram:123"},
    )]
    feature.submit_feature_request.assert_not_called()
# ── Explicit Web intent-prefix bypass (Section B) ────────────────────────────


def test_web_feature_prefix_bypasses_to_feature_created(web_client):
    response = web_client.post(
        "/tasks/submit", json={"text": "feature: add calendar integration"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    result = data["results"][0]
    assert result["action"] == "feature_created"
    assert result["feature_id"]


def test_web_normal_prefix_routes_to_chat_not_feature(web_client):
    response = web_client.post(
        "/tasks/submit", json={"text": "normal: Hello"}
    )
    assert response.status_code == 200
    data = response.json()
    result = data["results"][0]
    # The prefix strips to "Hello" and goes to the normal conversational path —
    # never the Feature Orchestrator.
    assert result.get("action") == "chat"