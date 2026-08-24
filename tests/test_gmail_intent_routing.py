from __future__ import annotations

from datetime import date
from unittest.mock import Mock, patch

import pytest

from jarvis_core.gmail_intent import GmailIntentRouter
from jarvis_core.orchestrator import Orchestrator
from jarvis_core.task_manager import task_manager
from jarvis_core.telegram_config import TelegramSettings
from jarvis_core.telegram_service import TelegramService


@pytest.mark.parametrize(
    "message",
    [
        "what about my gmail",
        "check my mail",
        "any important mails",
        "what emails came today",
        "check inbox",
        "anything important in gmail",
        "any recruiter mails today",
        "any job mails",
        "any JD received",
        "show today's mails",
        "what happened in my gmail",
        "do I have any drafts",
        "show my drafts",
        "any interview mails",
        "check mails from yesterday",
        "what mails came 2 days ago",
        "check last 3 days emails",
        "any important messages from recruiters",
        "did any company contact me",
        "gmail status",
        "mail status",
    ],
)
def test_natural_gmail_examples_resolve_to_real_gmail_commands(message):
    resolution = GmailIntentRouter().resolve(message, conversation_id="web")
    assert resolution is not None
    assert resolution.command.startswith("gmail ")


@pytest.mark.parametrize(
    "message",
    [
        "what is the postal mail service",
        "show my mailing address",
        "mail this package tomorrow",
        "send email to Alex",
        "compose an email",
        "open gmail",
        "explain Python email validation",
    ],
)
def test_unrelated_or_protected_mail_phrases_are_not_rewritten(message):
    assert GmailIntentRouter().resolve(message, conversation_id="web") is None


@pytest.mark.parametrize(
    ("message", "start", "end"),
    [
        ("check gmail today", date(2026, 8, 22), date(2026, 8, 22)),
        ("check mails yesterday", date(2026, 8, 21), date(2026, 8, 21)),
        ("what mails came 2 days ago", date(2026, 8, 20), date(2026, 8, 20)),
        ("check mail 3 days ago", date(2026, 8, 19), date(2026, 8, 19)),
        ("check last 2 days emails", date(2026, 8, 21), date(2026, 8, 22)),
        ("check last 3 days emails", date(2026, 8, 20), date(2026, 8, 22)),
        ("check gmail last week", date(2026, 8, 16), date(2026, 8, 22)),
        ("check mail from 3 days ago", date(2026, 8, 19), date(2026, 8, 22)),
        ("check gmail Aug 19 to Aug 22", date(2026, 8, 19), date(2026, 8, 22)),
    ],
)
def test_semantic_gmail_dates_use_existing_parser(message, start, end):
    router = GmailIntentRouter()
    with patch("jarvis_core.gmail_reporting.local_today", return_value=date(2026, 8, 22)):
        resolution = router.resolve(message, conversation_id="web")
    assert resolution is not None
    assert resolution.period.start == start
    assert resolution.period.end == end


def test_general_gmail_defaults_to_today_and_followups_keep_context():
    router = GmailIntentRouter()
    with patch("jarvis_core.gmail_reporting.local_today", return_value=date(2026, 8, 22)):
        first = router.resolve("check my gmail", conversation_id="web")
        important = router.resolve("show important ones", conversation_id="web")
        last_three = router.resolve("what about last 3 days", conversation_id="web")
        drafts = router.resolve("show the drafts", conversation_id="web")

    assert first.command == "gmail status today"
    assert important.command == "gmail details important 2026-08-22 to 2026-08-22"
    assert last_three.period.start == date(2026, 8, 20)
    assert last_three.period.end == date(2026, 8, 22)
    assert drafts.command == "gmail details drafts 2026-08-20 to 2026-08-22"


def test_unrelated_request_clears_gmail_followup_context():
    router = GmailIntentRouter()
    with patch("jarvis_core.gmail_reporting.local_today", return_value=date(2026, 8, 22)):
        assert router.resolve("check my gmail", conversation_id="web") is not None
        assert router.resolve("tell me about planets", conversation_id="web") is None
        assert router.resolve("show important ones", conversation_id="web") is None


def test_context_is_isolated_between_web_and_telegram_users():
    router = GmailIntentRouter()
    with patch("jarvis_core.gmail_reporting.local_today", return_value=date(2026, 8, 22)):
        router.resolve("check my gmail", conversation_id="telegram:123")
        assert router.resolve("show important ones", conversation_id="telegram:999") is None
        assert router.resolve("show important ones", conversation_id="web") is None
        assert router.resolve("show important ones", conversation_id="telegram:123") is not None


def test_orchestrator_routes_natural_gmail_before_generic_conversation():
    task_manager.clear_all_tasks()
    orchestrator = Orchestrator()
    try:
        with patch("jarvis_core.gmail_reporting.local_today", return_value=date(2026, 8, 22)):
            result = orchestrator.submit("what about my gmail", conversation_id="web")
        task = task_manager.get_task(result["task_id"])
        assert result["action"] == "created"
        assert task.interpreted_command == "gmail status today"
        assert task.task_type == "command"
        assert task.routing["execution"] == "direct"
    finally:
        task_manager.clear_all_tasks()


def test_natural_gmail_execution_uses_real_gmail_skill_not_ollama_answer():
    task_manager.clear_all_tasks()
    orchestrator = Orchestrator()
    gmail_result = {
        "status": "done",
        "kind": "gmail_report",
        "summary": "real Gmail report",
        "report": {"emails_checked": 3},
    }
    try:
        with patch("jarvis_core.gmail_reporting.local_today", return_value=date(2026, 8, 22)), patch(
            "jarvis_core.gmail_reporting.run_gmail_report", return_value=gmail_result
        ) as real_gmail, patch("jarvis_core.ollama_service.ask_ollama") as ask_ollama:
            submitted = orchestrator.submit("what emails came today", conversation_id="web")
            result = orchestrator.execute_task(submitted["task_id"])

        assert result["status"] == "completed"
        real_gmail.assert_called_once_with("gmail status today")
        ask_ollama.assert_not_called()
    finally:
        task_manager.clear_all_tasks()


def test_web_task_route_uses_web_scoped_gmail_context():
    from jarvis_core.models import TaskSubmitRequest
    from jarvis_core.routes import tasks_submit

    with patch("jarvis_core.orchestrator.orchestrator.submit_multiple", return_value=[]) as submit:
        result = tasks_submit(TaskSubmitRequest(text="check my gmail"))

    assert result["status"] == "ok"
    submit.assert_called_once_with(["check my gmail"], conversation_id="web")


def test_telegram_uses_user_scoped_shared_gmail_context():
    settings = TelegramSettings(bot_token="token", allowed_user_ids=frozenset({123}))
    fake_api = Mock()
    fake_orchestrator = Mock()
    fake_orchestrator.submit.return_value = {
        "task_id": "task-1", "status": "queued", "action": "created", "message": "queued"
    }
    service = TelegramService(settings=settings, api=fake_api, orchestrator_instance=fake_orchestrator)
    service._start_task_watcher = Mock()

    service._submit_command(123, 100, "check my gmail")

    fake_orchestrator.submit.assert_called_once_with(
        "check my gmail",
        source="Telegram",
        track_conversation=True,
        conversation_id="telegram:123",
    )
