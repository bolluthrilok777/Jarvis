from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from jarvis_core.action_engine import ActionEngine
from jarvis_core.action_models import ActionPlan, ActionStep
from jarvis_core.models import CommandRequest
from jarvis_core.normalizer import normalize
from jarvis_core.orchestrator import Orchestrator
from jarvis_core.intent_classifier import classify
from jarvis_core.task_manager import TaskManager
from jarvis_core.task_models import TaskStatus
from jarvis_core.telegram_bot_api import TelegramBotAPI
from jarvis_core.telegram_config import TelegramSettings, parse_allowed_user_ids
from jarvis_core.telegram_service import TelegramService


class FakeAPI:
    def __init__(self):
        self.messages = []
        self.callbacks = []
        self.closed = False

    def send_message(self, chat_id, text, *, reply_markup=None):
        self.messages.append({
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
        })
        return {}

    def answer_callback_query(self, callback_query_id, text=""):
        self.callbacks.append((callback_query_id, text))

    def close(self):
        self.closed = True


class FakeOrchestrator:
    def __init__(self, submission=None):
        self.submission = submission or {
            "task_id": "task-1",
            "status": "queued",
            "action": "created",
            "message": "queued",
        }
        self.submit_calls = []
        self.confirm_calls = []
        self.cancel_calls = []

    def submit(self, text, **kwargs):
        self.submit_calls.append((text, kwargs))
        return dict(self.submission)

    def confirm(self, task_id):
        self.confirm_calls.append(task_id)
        return {"status": "queued", "message": "confirmed"}

    def cancel(self, task_id):
        self.cancel_calls.append(task_id)
        return {"status": "cancelled", "message": "cancelled"}


def _settings(*user_ids):
    return TelegramSettings(
        bot_token="test-token",
        allowed_user_ids=frozenset(user_ids),
        completion_timeout_seconds=1,
    )


def _message(user_id, text, chat_id=100):
    return {
        "update_id": 1,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": chat_id},
            "text": text,
        },
    }


def test_allowed_user_ids_parse_multiple_separators():
    assert parse_allowed_user_ids("123, 456;789\n900") == frozenset({123, 456, 789, 900})


def test_allowed_user_ids_reject_invalid_values():
    with pytest.raises(ValueError):
        parse_allowed_user_ids("123,not-a-user")
    with pytest.raises(ValueError):
        parse_allowed_user_ids("0")


def test_settings_repr_never_exposes_token():
    settings = _settings(123)
    assert "test-token" not in repr(settings)


def test_disabled_service_does_not_start_polling():
    service = TelegramService(settings=TelegramSettings())
    assert service.start() is False
    assert service.running is False


def test_unauthorized_user_cannot_submit_commands():
    api = FakeAPI()
    orchestrator = FakeOrchestrator()
    service = TelegramService(
        settings=_settings(123),
        api=api,
        orchestrator_instance=orchestrator,
    )

    service.process_update(_message(999, "open calculator"))

    assert orchestrator.submit_calls == []
    assert "not authorized" in api.messages[-1]["text"].lower()


def test_natural_language_uses_existing_orchestrator_with_telegram_source():
    api = FakeAPI()
    orchestrator = FakeOrchestrator()
    service = TelegramService(
        settings=_settings(123),
        api=api,
        orchestrator_instance=orchestrator,
    )
    service._start_task_watcher = Mock()

    service.process_update(_message(123, "open calculator"))

    assert orchestrator.submit_calls == [(
        "open calculator",
        {"source": "Telegram", "track_conversation": True, "conversation_id": "telegram:123"},
    )]
    service._start_task_watcher.assert_called_once_with("task-1")
    assert "LIVE TASKS" in api.messages[-1]["text"]


@pytest.mark.parametrize(
    "message",
    [
        "Hello",
        "Hello!",
        "Hello Jarvis",
        "Hello, Jarvis!",
        "Hi",
        "Hi Jarvis",
        "Hey",
        "Hey Jarvis!",
        "Good morning Jarvis",
    ],
)
def test_greeting_variants_are_conversational_without_weakening_actions(message):
    assert classify(normalize(message), message) == "conversational_query"


@pytest.mark.parametrize(
    "message",
    [
        "open calculator",
        "Hello Jarvis, open calculator",
        "delete the file",
        "what is system status",
    ],
)
def test_explicit_actions_remain_executable(message):
    assert classify(normalize(message), message) == "executable_command"


def test_telegram_greeting_uses_shared_conversation_task_lifecycle():
    from jarvis_core.task_manager import task_manager

    task_manager.clear_all_tasks()
    api = FakeAPI()
    orchestrator = Orchestrator()
    service = TelegramService(
        settings=_settings(123),
        api=api,
        orchestrator_instance=orchestrator,
        task_manager_instance=task_manager,
    )
    service._start_task_watcher = Mock()

    try:
        service.process_update(_message(123, "Hello Jarvis!"))

        tasks = task_manager.get_all_tasks()
        assert len(tasks) == 1
        task = tasks[0]
        assert task.source == "Telegram"
        assert task.task_type == "conversation"
        assert task.status == TaskStatus.queued.value
        service._start_task_watcher.assert_called_once_with(task.task_id)

        with patch(
            "jarvis_core.conversation_service.ask_ollama",
            return_value=("Hello! How can I help?", "test-model", None),
        ):
            result = orchestrator.execute_task(task.task_id)

        completed = task_manager.get_task(task.task_id)
        assert result["status"] == TaskStatus.completed.value
        assert completed.status == TaskStatus.completed.value
        assert completed.started_at is not None
        assert completed.result["reply"] == "Hello! How can I help?"
        assert TelegramService._format_task_result(completed) == (
            "Jarvis completed the task:\nHello! How can I help?"
        )
    finally:
        task_manager.clear_all_tasks()


def test_status_command_runs_existing_system_status_flow():
    api = FakeAPI()
    orchestrator = FakeOrchestrator()
    service = TelegramService(
        settings=_settings(123),
        api=api,
        orchestrator_instance=orchestrator,
    )
    service._start_task_watcher = Mock()

    service.process_update(_message(123, "/status"))

    assert orchestrator.submit_calls[0] == (
        "system status",
        {"source": "Telegram", "track_conversation": True, "conversation_id": "telegram:123"},
    )


def test_protected_command_requires_owner_confirmation():
    api = FakeAPI()
    orchestrator = FakeOrchestrator({
        "task_id": "protected-task",
        "status": "waiting_for_confirmation",
        "action": "created",
        "message": "confirm",
    })
    service = TelegramService(
        settings=_settings(123, 456),
        api=api,
        orchestrator_instance=orchestrator,
    )
    service._start_task_watcher = Mock()

    service.process_update(_message(123, "close notepad"))
    markup = api.messages[-1]["reply_markup"]
    assert markup["inline_keyboard"][0][0]["callback_data"] == "confirm:protected-task"

    service.process_update({
        "callback_query": {
            "id": "callback-other",
            "from": {"id": 456},
            "message": {"chat": {"id": 100}},
            "data": "confirm:protected-task",
        }
    })
    assert orchestrator.confirm_calls == []

    service.process_update({
        "callback_query": {
            "id": "callback-owner",
            "from": {"id": 123},
            "message": {"chat": {"id": 100}},
            "data": "confirm:protected-task",
        }
    })
    assert orchestrator.confirm_calls == ["protected-task"]
    service._start_task_watcher.assert_called_once_with("protected-task")


def test_task_source_is_exposed_to_live_tasks_payload():
    manager = TaskManager()
    task = manager.create_task("system status", source="Telegram")

    public = manager.get_public_task(task.task_id)

    assert public["source"] == "Telegram"


def test_completed_conversation_response_is_returned_to_telegram():
    manager = TaskManager()
    task = manager.create_task(
        "Who are you?",
        source="Telegram",
        task_type="conversation",
    )
    manager.set_result(task.task_id, {
        "status": "done",
        "reply": "I am Jarvis.",
        "summary": "I am Jarvis.",
    })
    manager.set_status(task.task_id, TaskStatus.completed.value)

    assert TelegramService._format_task_result(task) == (
        "Jarvis completed the task:\nI am Jarvis."
    )


def test_confirmed_action_engine_plan_does_not_request_confirmation_again():
    executor = Mock(return_value={"status": "done", "message": "closed"})
    skill = SimpleNamespace(executor=executor)
    plan = ActionPlan(
        steps=[ActionStep(
            skill_id="app.close",
            parameters={"app_name": "notepad"},
            description="Close Notepad",
            requires_confirmation=True,
        )],
        raw_command="close notepad",
        normalized_command="close notepad",
        confidence=1.0,
    )

    with patch("jarvis_core.skill_registry.registry.get_skill", return_value=skill):
        report = ActionEngine().execute(plan, confirmed=True)

    assert report.all_succeeded is True
    executor.assert_called_once_with(app_name="notepad")


def test_late_action_engine_confirmation_returns_to_task_confirmation_flow():
    from jarvis_core.orchestrator import Orchestrator
    from jarvis_core.task_manager import task_manager

    task_manager.clear_all_tasks()
    task = task_manager.create_task("close notepad", source="Telegram")
    task_manager.set_status(task.task_id, TaskStatus.queued.value)
    orchestrator = Orchestrator()

    with patch("jarvis_core.orchestrator.action_engine.run", return_value={
        "status": "confirmation_required",
        "action_id": "engine-action",
        "message": "Please confirm",
    }), patch.dict(
        "jarvis_core.command_dispatcher.pending_actions",
        {"engine-action": {"command": "close notepad", "created_at": 0}},
        clear=True,
    ) as pending:
        result = orchestrator.execute_task(task.task_id)

    assert result["status"] == TaskStatus.waiting_for_confirmation.value
    assert task_manager.get_task(task.task_id).status == TaskStatus.waiting_for_confirmation.value
    assert "engine-action" not in pending
    task_manager.clear_all_tasks()


def test_bot_api_uses_long_polling_parameters_without_logging_token():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"ok": True, "result": []}
    session = Mock()
    session.get.return_value = response
    api = TelegramBotAPI("secret-token", session=session)

    assert api.get_updates(offset=9, timeout=20) == []

    _, kwargs = session.get.call_args
    assert kwargs["params"]["offset"] == 9
    assert kwargs["params"]["timeout"] == 20
    assert kwargs["timeout"][1] > 20


def test_existing_ui_conditionally_renders_telegram_source():
    from jarvis_core.config import UI_FILE

    html = UI_FILE.read_text(encoding="utf-8")
    assert "Source: Telegram" in html
    assert 'String(t.source || "").toLowerCase() === "telegram"' in html
