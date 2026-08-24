from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from jarvis_core.gmail_models import GmailMonitorState
from jarvis_core.gmail_reporting import (
    build_authoritative_gmail_status,
    keep_gmail_draft,
    parse_gmail_period,
    run_gmail_report,
    view_gmail_draft,
)
from jarvis_core.orchestrator import Orchestrator
from jarvis_core.task_manager import task_manager
from jarvis_core.task_models import Task, TaskStatus
from jarvis_core.telegram_config import TelegramSettings
from jarvis_core.telegram_service import TelegramService


TODAY = date(2026, 8, 22)
JARVIS_TZ = timezone(timedelta(hours=5, minutes=30))


class FakeAPI:
    def __init__(self):
        self.messages = []
        self.callbacks = []

    def send_message(self, chat_id, text, *, reply_markup=None):
        self.messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {}

    def answer_callback_query(self, callback_query_id, text=""):
        self.callbacks.append((callback_query_id, text))

    def close(self):
        return None


class FakeOrchestrator:
    def __init__(self):
        self.submit_calls = []
        self.confirm_calls = []
        self.cancel_calls = []

    def submit(self, text, **kwargs):
        self.submit_calls.append((text, kwargs))
        return {"task_id": "task-1", "status": "queued", "action": "created", "message": "queued"}

    def confirm(self, task_id):
        self.confirm_calls.append(task_id)
        return {"status": "queued", "message": "confirmed"}

    def cancel(self, task_id):
        self.cancel_calls.append(task_id)
        return {"status": "cancelled", "message": "cancelled"}


def _settings(*ids):
    return TelegramSettings(bot_token="test-token", allowed_user_ids=frozenset(ids), completion_timeout_seconds=1)


def _message(user_id, text, chat_id=100):
    return {"update_id": 1, "message": {"from": {"id": user_id}, "chat": {"id": chat_id}, "text": text}}


def _callback(user_id, data, callback_id="cb", chat_id=100):
    return {
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}},
            "data": data,
        }
    }


@pytest.mark.parametrize(
    ("phrase", "start", "end"),
    [
        ("gmail status today", date(2026, 8, 22), date(2026, 8, 22)),
        ("gmail status yesterday", date(2026, 8, 21), date(2026, 8, 21)),
        ("gmail status 2 days ago", date(2026, 8, 20), date(2026, 8, 20)),
        ("gmail status last 2 days", date(2026, 8, 21), date(2026, 8, 22)),
        ("gmail status last 3 days", date(2026, 8, 20), date(2026, 8, 22)),
        ("gmail status from 3 days ago", date(2026, 8, 19), date(2026, 8, 22)),
        ("gmail status from Aug 19 to Aug 22", date(2026, 8, 19), date(2026, 8, 22)),
        ("gmail status 19/08/2026 to 22/08/2026", date(2026, 8, 19), date(2026, 8, 22)),
    ],
)
def test_gmail_period_interpretation(phrase, start, end):
    parsed = parse_gmail_period(phrase, today=TODAY)
    assert parsed.start == start
    assert parsed.end == end


def test_jarvis_today_changes_at_jarvis_midnight_not_utc_midnight():
    from jarvis_core.gmail_reporting import local_today

    with patch("jarvis_core.timezone_utils.jarvis_timezone", return_value=JARVIS_TZ):
        assert local_today(datetime(2026, 8, 21, 18, 29, 59, tzinfo=timezone.utc)) == date(2026, 8, 21)
        assert local_today(datetime(2026, 8, 21, 18, 30, 0, tzinfo=timezone.utc)) == date(2026, 8, 22)


@pytest.mark.parametrize(
    ("phrase", "start", "end"),
    [
        ("gmail status today", date(2026, 8, 22), date(2026, 8, 22)),
        ("gmail status yesterday", date(2026, 8, 21), date(2026, 8, 21)),
        ("gmail status 2 days ago", date(2026, 8, 20), date(2026, 8, 20)),
        ("gmail status last 2 days", date(2026, 8, 21), date(2026, 8, 22)),
        ("gmail status last 3 days", date(2026, 8, 20), date(2026, 8, 22)),
        ("gmail status from 3 days ago", date(2026, 8, 19), date(2026, 8, 22)),
        ("gmail status Aug 19 to Aug 22", date(2026, 8, 19), date(2026, 8, 22)),
    ],
)
def test_telegram_periods_share_jarvis_timezone_after_local_midnight(phrase, start, end):
    jarvis_midnight = datetime(2026, 8, 21, 18, 30, 0, tzinfo=timezone.utc).astimezone(JARVIS_TZ)
    with patch("jarvis_core.gmail_reporting.jarvis_now", return_value=jarvis_midnight):
        parsed = parse_gmail_period(phrase)

    assert parsed.start == start
    assert parsed.end == end


def test_received_email_date_uses_jarvis_midnight_boundary(tmp_path):
    from jarvis_core.gmail_reporting import GmailDateRange, list_events_for_range

    events = [
        {"gmail_message_id": "before", "email_date": "Fri, 21 Aug 2026 18:29:59 +0000"},
        {"gmail_message_id": "at", "email_date": "Fri, 21 Aug 2026 18:30:00 +0000"},
    ]
    (tmp_path / "gmail_monitor_log_boundary.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8"
    )
    period = GmailDateRange(date(2026, 8, 22), date(2026, 8, 22), "TODAY")
    with patch("jarvis_core.gmail_monitor.NOTES_DIR", tmp_path), patch(
        "jarvis_core.timezone_utils.jarvis_timezone", return_value=JARVIS_TZ
    ):
        selected = list_events_for_range(period)

    assert [event["gmail_message_id"] for event in selected] == ["at"]


def test_gmail_details_display_received_timestamp_in_jarvis_timezone(tmp_path):
    from jarvis_core.gmail_reporting import get_gmail_details

    event = {
        "gmail_message_id": "at-midnight",
        "email_date": "Fri, 21 Aug 2026 18:30:00 +0000",
        "subject": "Developer JD",
        "category": "jd_received",
    }
    (tmp_path / "gmail_monitor_log_display.jsonl").write_text(json.dumps(event), encoding="utf-8")
    with patch("jarvis_core.gmail_monitor.NOTES_DIR", tmp_path), patch(
        "jarvis_core.timezone_utils.jarvis_timezone", return_value=JARVIS_TZ
    ):
        result = get_gmail_details("jd", "2026-08-22", "2026-08-22")

    assert result["events"][0]["received_at_jarvis"].startswith("2026-08-22T00:00:00+05:30")
    assert "Received (Jarvis time): Aug 22, 2026 12:00:00 AM +0530" in result["summary"]


def test_gmail_api_query_uses_jarvis_midnight_epoch_boundaries():
    from jarvis_core.gmail_service import list_messages_for_date_range

    service = Mock()
    message_list = service.users.return_value.messages.return_value.list
    message_list.return_value.execute.return_value = {}
    with patch("jarvis_core.gmail_service._build_service", return_value=service), patch(
        "jarvis_core.gmail_service.jarvis_timezone", return_value=JARVIS_TZ
    ):
        list_messages_for_date_range(date(2026, 8, 22), date(2026, 8, 22))

    start = datetime(2026, 8, 22, tzinfo=JARVIS_TZ)
    end = datetime(2026, 8, 23, tzinfo=JARVIS_TZ)
    assert message_list.call_args.kwargs["q"] == (
        f"in:inbox after:{int(start.timestamp()) - 1} before:{int(end.timestamp())}"
    )


def test_draft_internal_date_uses_same_jarvis_midnight_boundary():
    from jarvis_core.gmail_reporting import GmailDateRange, _gmail_drafts_for_period

    def millis(value: datetime) -> str:
        return str(int(value.timestamp() * 1000))

    drafts = [
        {"id": "before", "internal_date": millis(datetime(2026, 8, 21, 18, 29, 59, tzinfo=timezone.utc))},
        {"id": "at", "internal_date": millis(datetime(2026, 8, 21, 18, 30, 0, tzinfo=timezone.utc))},
        {"id": "next", "internal_date": millis(datetime(2026, 8, 22, 18, 30, 0, tzinfo=timezone.utc))},
    ]
    period = GmailDateRange(date(2026, 8, 22), date(2026, 8, 22), "TODAY")
    with patch("jarvis_core.gmail_service.list_gmail_drafts", return_value=drafts), patch(
        "jarvis_core.gmail_reporting.jarvis_timezone", return_value=JARVIS_TZ
    ):
        selected = _gmail_drafts_for_period(period)

    assert [draft["id"] for draft in selected] == ["at"]
    assert selected[0]["created_at"].startswith("2026-08-22T00:00:00")


def test_bare_gmail_status_defaults_to_today_through_shared_pipeline():
    api, orchestrator = FakeAPI(), FakeOrchestrator()
    service = TelegramService(settings=_settings(123), api=api, orchestrator_instance=orchestrator)
    service._start_task_watcher = Mock()

    service.process_update(_message(123, "gmail status"))

    assert orchestrator.submit_calls == [(
        "gmail status today",
        {"source": "Telegram", "track_conversation": True, "conversation_id": "telegram:123"},
    )]


def test_gmail_period_button_submits_through_shared_orchestrator():
    api, orchestrator = FakeAPI(), FakeOrchestrator()
    service = TelegramService(settings=_settings(123), api=api, orchestrator_instance=orchestrator)
    service._start_task_watcher = Mock()

    service.process_update(_callback(123, "gmail_period:last3"))

    assert orchestrator.submit_calls == [(
        "gmail status last 3 days",
        {"source": "Telegram", "track_conversation": True, "conversation_id": "telegram:123"},
    )]
    service._start_task_watcher.assert_called_once_with("task-1")


def test_custom_range_prompt_and_submission():
    api, orchestrator = FakeAPI(), FakeOrchestrator()
    service = TelegramService(settings=_settings(123), api=api, orchestrator_instance=orchestrator)
    service._start_task_watcher = Mock()

    service.process_update(_callback(123, "gmail_period:custom"))
    assert "start and end" in api.messages[-1]["text"]

    service.process_update(_message(123, "Aug 19 to Aug 22"))
    assert orchestrator.submit_calls[-1] == (
        "gmail status Aug 19 to Aug 22",
        {"source": "Telegram", "track_conversation": True, "conversation_id": "telegram:123"},
    )


def test_gmail_api_failure_is_not_reported_as_healthy():
    connected = {"connected": True, "healthy": True, "message": "current"}
    failed = {
        "connected": True,
        "healthy": False,
        "safe_error": "Gmail API failed while reading the requested range.",
        "message": "Gmail API failed while reading the requested range.",
        "last_successful_scan": "2026-08-21T16:00:00+05:30",
    }
    with patch(
        "jarvis_core.gmail_reporting.get_authoritative_gmail_status",
        side_effect=[connected, failed],
    ), patch(
        "jarvis_core.gmail_monitor.run_date_range_scan",
        side_effect=RuntimeError("unsafe internal transport detail"),
    ):
        result = run_gmail_report("gmail status today")

    assert result["status"] == "error"
    assert "GMAIL SCAN FAILED" in result["summary"]
    assert "Gmail API failed" in result["summary"]
    assert "functioning normally" not in result["summary"]


def test_ui_and_telegram_surface_the_same_gmail_api_failure():
    from jarvis_core.gmail_routes import gmail_status

    failure = "Gmail API failed while reading the requested range."
    state = GmailMonitorState(
        running=False,
        connected=True,
        account_email="user@example.com",
        scan_phase="failed",
        scan_status=failure,
    )
    authoritative = build_authoritative_gmail_status(
        state=state,
        connected=True,
        credentials_configured=True,
        token_configured=True,
        token_health={"valid": True},
    )

    api = FakeAPI()
    service = TelegramService(settings=_settings(123), api=api)
    with patch(
        "jarvis_core.gmail_reporting.get_authoritative_gmail_status",
        return_value=authoritative,
    ):
        service._send_gmail_period_menu(100)

    with patch("jarvis_core.gmail_routes.get_state", return_value=state), patch(
        "jarvis_core.gmail_routes.credentials_exist", return_value=True
    ), patch("jarvis_core.gmail_routes.get_credentials_error", return_value=None), patch(
        "jarvis_core.gmail_routes.token_exists", return_value=True
    ), patch("jarvis_core.gmail_routes.is_connected", return_value=True), patch(
        "jarvis_core.gmail_routes.validate_token_health", return_value={"valid": True}
    ), patch("jarvis_core.gmail_routes.is_token_revoked", return_value=False), patch(
        "jarvis_core.gmail_routes.get_last_scan_timestamp", return_value=""
    ), patch("jarvis_core.gmail_routes.is_full_scan_done", return_value=False), patch(
        "jarvis_core.gmail_reporting.build_authoritative_gmail_status",
        return_value=authoritative,
    ):
        ui_status = gmail_status()

    assert ui_status["safe_error"] == failure
    assert ui_status["status_message"] == failure
    assert failure in api.messages[-1]["text"]


def test_paused_monitoring_is_authoritative_status():
    status = build_authoritative_gmail_status(
        state=GmailMonitorState(running=False, connected=True),
        connected=True,
        credentials_configured=True,
        token_configured=True,
        token_health={"valid": True},
    )
    assert status["operational_status"] == "paused"
    assert status["healthy"] is False
    assert "paused" in status["message"].lower()


def test_report_uses_real_jd_draft_and_review_values():
    event = {
        "from": "recruiter@example.com",
        "subject": "Java Developer JD",
        "email_date": "Sat, 22 Aug 2026 15:42:00 +0530",
        "category": "jd_received",
        "importance": "high",
        "action_required": "Review the role",
        "attachments_saved": ["jd.pdf"],
        "jd_saved_path": "jd.pdf",
        "gmail_draft_id": "draft123",
        "gmail_message_id": "message123",
    }
    status = {"connected": True, "healthy": True, "message": "current", "monitoring_paused": False}
    draft = {"id": "draft123", "to": "recruiter@example.com", "subject": "Re: Java Developer JD", "body": "Thank you."}
    with patch("jarvis_core.gmail_reporting.get_authoritative_gmail_status", return_value=status), patch(
        "jarvis_core.gmail_monitor.run_date_range_scan",
        return_value={"emails_scanned": 27, "errors": 0, "checked_at": "2026-08-22T16:31:00+05:30"},
    ), patch("jarvis_core.gmail_reporting.list_events_for_range", return_value=[event]), patch(
        "jarvis_core.gmail_reporting._gmail_drafts_for_period", return_value=[draft]
    ):
        result = run_gmail_report("gmail status today")

    report = result["report"]
    assert report["emails_checked"] == 27
    assert report["important"] == 1
    assert report["jd_emails"] == 1
    assert report["jd_files_saved"] == 1
    assert report["drafts_created"] == 1
    assert report["needs_review"] == 1
    assert report["email_date_basis"] == "received_date_jarvis_timezone"
    assert report["draft_date_basis"] == "gmail_internal_date_jarvis_timezone"
    assert "Email counts use received date" in result["summary"]
    assert "Draft counts use draft created date" in result["summary"]


def test_full_draft_preview_and_keep_use_existing_draft_id():
    draft = {"id": "draft123", "to": "recruiter@example.com", "subject": "Re: JD", "body": "Full draft body."}
    with patch("jarvis_core.gmail_service.get_gmail_draft", return_value=draft) as get_draft:
        viewed = view_gmail_draft("draft123")
        kept = keep_gmail_draft("draft123")

    assert "To: recruiter@example.com" in viewed["summary"]
    assert "Full draft body." in viewed["summary"]
    assert kept["kind"] == "gmail_draft_kept"
    assert get_draft.call_args_list[0].args == ("draft123",)


def test_draft_details_include_real_gmail_api_drafts_for_period():
    from jarvis_core.gmail_reporting import get_gmail_details

    api_draft = {
        "id": "full_exact_draft_id_123456",
        "to": "recruiter@example.com",
        "subject": "Re: JD",
        "internal_date": "1787404200000",
    }
    with patch("jarvis_core.gmail_reporting.list_events_for_range", return_value=[]), patch(
        "jarvis_core.gmail_service.list_gmail_drafts", return_value=[api_draft]
    ), patch(
        "jarvis_core.gmail_reporting.jarvis_timezone", return_value=JARVIS_TZ
    ):
        result = get_gmail_details("drafts", "2026-08-22", "2026-08-22")

    assert result["drafts"][0]["id"] == api_draft["id"]
    assert result["drafts"][0]["created_at"].startswith("2026-08-22")
    assert "draft created date (Jarvis timezone)" in result["summary"]
    assert "Draft created (Jarvis time):" in result["summary"]


def test_exact_date_range_query_raises_instead_of_faking_empty_success():
    from jarvis_core.gmail_service import list_messages_for_date_range

    service = Mock()
    service.users.return_value.messages.return_value.list.return_value.execute.side_effect = RuntimeError("API down")
    with patch("jarvis_core.gmail_service._build_service", return_value=service):
        with pytest.raises(RuntimeError, match="requested date range"):
            list_messages_for_date_range(date(2026, 8, 19), date(2026, 8, 22))


def test_confirmed_send_calls_gmail_drafts_send_with_exact_id():
    from jarvis_core.gmail_service import send_gmail_draft

    service = Mock()
    drafts = service.users.return_value.drafts.return_value
    drafts.get.return_value.execute.return_value = {"id": "draft123"}
    drafts.send.return_value.execute.return_value = {"id": "message123", "threadId": "thread123"}
    with patch("jarvis_core.gmail_service._build_service", return_value=service):
        result = send_gmail_draft("draft123")

    assert result["status"] == "done"
    drafts.get.assert_called_once_with(userId="me", id="draft123", format="metadata")
    drafts.send.assert_called_once_with(userId="me", body={"id": "draft123"})


def test_send_draft_requires_confirmation_and_cancel_preserves_it():
    task_manager.clear_all_tasks()
    api = FakeAPI()
    orchestrator = Orchestrator()
    service = TelegramService(
        settings=_settings(123), api=api, orchestrator_instance=orchestrator,
        task_manager_instance=task_manager,
    )
    draft = {"id": "draft123", "to": "recruiter@example.com", "subject": "Re: JD", "body": "Preview body"}
    try:
        with patch("jarvis_core.gmail_reporting.view_gmail_draft", return_value={"status": "done", "draft": draft}):
            service.process_update(_callback(123, "gmail_draft:send|draft123"))

        task = task_manager.get_all_tasks()[0]
        assert task.source == "Telegram"
        assert task.status == TaskStatus.waiting_for_confirmation.value
        assert "CONFIRM GMAIL SEND" in api.messages[-1]["text"]
        assert api.messages[-1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ Confirm Send"

        service.process_update(_callback(123, f"cancel:{task.task_id}", callback_id="cancel"))
        assert task_manager.get_task(task.task_id).status == TaskStatus.cancelled.value
    finally:
        task_manager.clear_all_tasks()


def test_confirmed_send_uses_existing_draft_and_completes_live_task():
    task_manager.clear_all_tasks()
    orchestrator = Orchestrator()
    submission = orchestrator.submit("gmail draft send draft123", source="Telegram")
    task_id = submission["task_id"]
    try:
        assert submission["status"] == TaskStatus.waiting_for_confirmation.value
        assert orchestrator.confirm(task_id)["status"] == TaskStatus.queued.value
        with patch("jarvis_core.gmail_reporting.send_existing_gmail_draft", return_value={
            "status": "done", "kind": "gmail_draft_sent", "summary": "sent"
        }) as send:
            result = orchestrator.execute_task(task_id)

        task = task_manager.get_task(task_id)
        assert result["status"] == TaskStatus.completed.value
        assert task.status == TaskStatus.completed.value
        assert task.started_at is not None
        assert task.completed_at is not None
        send.assert_called_once_with("draft123")
    finally:
        task_manager.clear_all_tasks()


def test_multiple_drafts_are_presented_individually():
    api = FakeAPI()
    service = TelegramService(settings=_settings(123), api=api)
    task = Task(original_input="gmail status today", source="Telegram")
    task.status = TaskStatus.completed.value
    task.result = {
        "results": [{"output": {
            "status": "done",
            "kind": "gmail_report",
            "summary": "report",
            "period": {"start": "2026-08-22", "end": "2026-08-22"},
            "report": {"drafts_created": 2},
            "drafts": [
                {"id": "draft1", "subject": "One", "body": "First"},
                {"id": "draft2", "subject": "Two", "body": "Second"},
            ],
        }}]
    }
    service._task_chats[task.task_id] = 100

    assert service._send_specialized_task_result(task.task_id, task) is True
    assert any("Draft 1 of 2" in item["text"] for item in api.messages)
    assert any("Draft 2 of 2" in item["text"] for item in api.messages)


def test_jd_details_can_open_mail_and_draft_from_telegram():
    api = FakeAPI()
    service = TelegramService(settings=_settings(123), api=api)
    task = Task(original_input="gmail details jd 2026-08-22 to 2026-08-22", source="Telegram")
    task.status = TaskStatus.completed.value
    task.result = {
        "results": [{"output": {
            "status": "done",
            "kind": "gmail_details",
            "category": "jd",
            "summary": "JD details",
            "events": [{
                "subject": "Senior Engineer JD",
                "from": "recruiter@example.com",
                "email_date": "2026-08-22",
                "gmail_thread_id": "thread123",
                "gmail_message_id": "message123",
                "gmail_draft_id": "draft123",
            }],
        }}]
    }
    service._task_chats[task.task_id] = 100

    assert service._send_specialized_task_result(task.task_id, task) is True
    event_message = next(item for item in api.messages if "Senior Engineer JD" in item["text"])
    buttons = event_message["reply_markup"]["inline_keyboard"][0]
    assert buttons[0] == {
        "text": "📨 Open in Gmail",
        "url": "https://mail.google.com/mail/u/0/#all/thread123",
    }
    assert buttons[1] == {
        "text": "👁 View Full Draft",
        "callback_data": "gmail_draft:view|draft123",
    }


def test_unauthorized_user_cannot_use_gmail_callbacks():
    api, orchestrator = FakeAPI(), FakeOrchestrator()
    service = TelegramService(settings=_settings(123), api=api, orchestrator_instance=orchestrator)

    service.process_update(_callback(999, "gmail_period:today"))

    assert orchestrator.submit_calls == []
    assert "not authorized" in api.messages[-1]["text"].lower()


def test_existing_gmail_ui_status_contract_is_preserved():
    from jarvis_core.gmail_routes import gmail_status

    state = GmailMonitorState(running=False, connected=True)
    with patch("jarvis_core.gmail_routes.get_state", return_value=state), patch(
        "jarvis_core.gmail_routes.credentials_exist", return_value=True
    ), patch("jarvis_core.gmail_routes.get_credentials_error", return_value=None), patch(
        "jarvis_core.gmail_routes.token_exists", return_value=True
    ), patch("jarvis_core.gmail_routes.is_connected", return_value=True), patch(
        "jarvis_core.gmail_routes.get_account_email", return_value="user@example.com"
    ), patch("jarvis_core.gmail_routes.validate_token_health", return_value={"valid": True}), patch(
        "jarvis_core.gmail_routes.is_token_revoked", return_value=False
    ), patch("jarvis_core.gmail_routes.get_last_scan_timestamp", return_value=None), patch(
        "jarvis_core.gmail_routes.is_full_scan_done", return_value=False
    ):
        result = gmail_status()

    for existing_key in (
        "connected", "running", "monitoring_active", "last_checked",
        "important_mails_today", "jd_files_saved_today", "drafts_created_today",
        "scan_in_progress", "scan_status", "poll_healthy", "poll_error",
    ):
        assert existing_key in result
    assert result["operational_status"] == "paused"
