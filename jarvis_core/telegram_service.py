"""Authorized Telegram remote control for Jarvis using Bot API long polling."""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime
from typing import Optional

from jarvis_core.task_models import Task, TaskPublic, TaskStatus
from jarvis_core.telegram_bot_api import TelegramAPIError, TelegramBotAPI
from jarvis_core.telegram_config import TelegramSettings

logger = logging.getLogger("jarvis.telegram")

# Module-level registry of active long-polling workers (one per bot token).
# start() uses it to refuse spawning a second worker for the same token within
# this process, which would otherwise cause duplicate-update processing.
_ACTIVE_POLLERS: dict[str, "TelegramService"] = {}
_ACTIVE_POLLERS_LOCK = threading.Lock()

TERMINAL_STATUSES = {
    TaskStatus.completed.value,
    TaskStatus.partially_completed.value,
    TaskStatus.failed.value,
    TaskStatus.cancelled.value,
}


class TelegramService:
    """Long-polling Telegram adapter around the existing Jarvis orchestrator."""

    # Exponential backoff bounds for temporary Telegram/network failures.
    BACKOFF_BASE_SECONDS = 2.0
    BACKOFF_MAX_SECONDS = 60.0

    def __init__(
        self,
        settings: Optional[TelegramSettings] = None,
        api: Optional[TelegramBotAPI] = None,
        orchestrator_instance=None,
        task_manager_instance=None,
    ) -> None:
        self._settings = settings
        self._api = api
        self._api_was_injected = api is not None
        self._orchestrator = orchestrator_instance
        self._task_manager = task_manager_instance
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()
        self._active_token: Optional[str] = None
        self._task_owners: dict[str, int] = {}
        self._task_chats: dict[str, int] = {}
        self._mapping_lock = threading.Lock()
        self._gmail_custom_users: set[int] = set()
        self._authorized_chats: dict[int, int] = {}
        self._notified_draft_ids: set[str] = set()
        self._last_gmail_event_at = datetime.now().astimezone().isoformat()

    @property
    def settings(self) -> TelegramSettings:
        if self._settings is None:
            self._settings = TelegramSettings.from_env()
        return self._settings

    @property
    def orchestrator(self):
        if self._orchestrator is None:
            from jarvis_core.orchestrator import orchestrator

            self._orchestrator = orchestrator
        return self._orchestrator

    @property
    def task_manager(self):
        if self._task_manager is None:
            from jarvis_core.task_manager import task_manager

            self._task_manager = task_manager
        return self._task_manager

    @property
    def running(self) -> bool:
        return self._poll_thread is not None and self._poll_thread.is_alive()

    def start(self) -> bool:
        """Start long polling when both token and allowlist are configured.

        Idempotent and single-flight: if this service (or another service in
        this process) already runs a poller for the same bot token, a second
        long-poll worker is not spawned -- avoiding duplicate-update delivery
        and Telegram's HTTP 409 conflict.
        """
        try:
            settings = self.settings
        except ValueError as exc:
            logger.error("Telegram integration is disabled: %s", exc)
            return False

        if not settings.enabled:
            logger.info(
                "Telegram integration is disabled; configure TELEGRAM_BOT_TOKEN "
                "and TELEGRAM_ALLOWED_USER_IDS in .env to enable it."
            )
            return False

        with self._start_lock:
            if self._poll_thread is not None and self._poll_thread.is_alive():
                return True

            with _ACTIVE_POLLERS_LOCK:
                owner = _ACTIVE_POLLERS.get(settings.bot_token)
                if owner is not None and owner is not self:
                    logger.warning(
                        "Telegram long polling is already active for this bot "
                        "token; skipping duplicate worker to avoid duplicate updates."
                    )
                    return False
                _ACTIVE_POLLERS[settings.bot_token] = self
                self._active_token = settings.bot_token

            if self._api is None:
                self._api = TelegramBotAPI(settings.bot_token)
            self._stop_event.clear()
            self._poll_thread = threading.Thread(
                target=self._poll_loop,
                name="telegram-long-poll",
                daemon=True,
            )
            try:
                self._poll_thread.start()
            except Exception:
                with _ACTIVE_POLLERS_LOCK:
                    _ACTIVE_POLLERS.pop(settings.bot_token, None)
                self._active_token = None
                raise
            logger.info("Telegram long polling started")
            return True

    def stop(self, timeout: float = 5.0) -> None:
        """Stop polling gracefully without affecting the Jarvis task worker."""
        self._stop_event.set()
        if self._api is not None:
            self._api.close()
            if not self._api_was_injected:
                self._api = None
        if self._poll_thread is not None and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=timeout)
            if self._poll_thread.is_alive():
                logger.warning("Telegram polling did not stop within %.1f seconds", timeout)
        self._poll_thread = None
        with _ACTIVE_POLLERS_LOCK:
            if self._active_token is not None:
                if _ACTIVE_POLLERS.get(self._active_token) is self:
                    _ACTIVE_POLLERS.pop(self._active_token, None)
                self._active_token = None

    def _poll_loop(self) -> None:
        """Long-poll for updates, retrying temporary failures with backoff.

        ``consecutive_failures`` drives an exponential backoff that resets on
        the first successful request. Log noise is suppressed: a warning is
        emitted only when the retry tier changes or polling recovers, so the
        terminal is not spammed with the same message every few seconds.
        """
        offset: Optional[int] = None
        consecutive_failures = 0
        last_logged_tier: Optional[float] = None
        while not self._stop_event.is_set():
            try:
                updates = self._require_api().get_updates(
                    offset=offset,
                    timeout=self.settings.poll_timeout_seconds,
                )
                if consecutive_failures:
                    logger.info(
                        "Telegram long polling recovered after %d failure(s).",
                        consecutive_failures,
                    )
                consecutive_failures = 0
                last_logged_tier = None
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    self.process_update(update)
                self._notify_new_gmail_drafts()
            except TelegramAPIError as exc:
                if self._stop_event.is_set():
                    break
                consecutive_failures += 1
                delay = min(
                    self.BACKOFF_MAX_SECONDS,
                    self.BACKOFF_BASE_SECONDS * (2 ** (consecutive_failures - 1)),
                )
                if exc.is_duplicate_poller:
                    if delay != last_logged_tier:
                        logger.warning(
                            "Telegram long polling rejected for this bot token "
                            "(another worker is already polling). Backing off %.0fs.",
                            delay,
                        )
                        last_logged_tier = delay
                elif delay != last_logged_tier:
                    logger.warning(
                        "Telegram API request failed (HTTP %s / %s). Retrying in %.0fs.",
                        exc.status_code if exc.status_code is not None else "unknown",
                        exc.error_type or type(exc).__name__,
                        delay,
                    )
                    last_logged_tier = delay
                if not self._stop_event.is_set():
                    self._stop_event.wait(delay)
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                consecutive_failures += 1
                delay = min(
                    self.BACKOFF_MAX_SECONDS,
                    self.BACKOFF_BASE_SECONDS * (2 ** (consecutive_failures - 1)),
                )
                if delay != last_logged_tier:
                    logger.warning(
                        "Unexpected Telegram polling error (%s). Retrying in %.0fs.",
                        type(exc).__name__,
                        delay,
                    )
                    last_logged_tier = delay
                if not self._stop_event.is_set():
                    self._stop_event.wait(delay)

    def process_update(self, update: dict) -> None:
        """Process one Telegram update. Public for deterministic unit tests."""
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self._handle_callback(callback)
            return

        message = update.get("message")
        if not isinstance(message, dict):
            return
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        user_id = sender.get("id")
        chat_id = chat.get("id")
        text = message.get("text")
        if not isinstance(user_id, int) or not isinstance(chat_id, int) or not isinstance(text, str):
            return
        if not self._is_authorized(user_id):
            self._send(chat_id, "You are not authorized to control Jarvis.")
            return
        with self._mapping_lock:
            self._authorized_chats[user_id] = chat_id
        self._dispatch_text(user_id, chat_id, text.strip())

    def _handle_callback(self, callback: dict) -> None:
        sender = callback.get("from") or {}
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        user_id = sender.get("id")
        chat_id = chat.get("id")
        callback_id = callback.get("id")
        data = callback.get("data")
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            return
        if not self._is_authorized(user_id):
            self._answer_callback(callback_id, "Unauthorized")
            self._send(chat_id, "You are not authorized to control Jarvis.")
            return
        with self._mapping_lock:
            self._authorized_chats[user_id] = chat_id
        if not isinstance(data, str) or ":" not in data:
            self._answer_callback(callback_id, "Unknown action")
            return
        if data.startswith("gmail_"):
            self._handle_gmail_callback(user_id, chat_id, callback_id, data)
            return
        action, task_id = data.split(":", 1)
        if action == "confirm":
            if not self._owns_task(user_id, task_id):
                self._answer_callback(callback_id, "Task unavailable")
                self._send(chat_id, "That task is not available for confirmation.")
                return
            self._answer_callback(callback_id, "Request received")
            self._confirm_task(user_id, chat_id, task_id)
        elif action == "cancel":
            if not self._owns_task(user_id, task_id):
                self._answer_callback(callback_id, "Task unavailable")
                self._send(chat_id, "That task is not available for cancellation.")
                return
            self._answer_callback(callback_id, "Request received")
            self._cancel_task(user_id, chat_id, task_id)
        else:
            self._answer_callback(callback_id, "Unknown action")

    def _dispatch_text(self, user_id: int, chat_id: int, text: str) -> None:
        if not text:
            return
        head, *rest = text.split(maxsplit=1)
        command = head.split("@", 1)[0].lower() if head.startswith("/") else ""
        argument = rest[0].strip() if rest else ""

        if not command and user_id in self._gmail_custom_users:
            from jarvis_core.gmail_reporting import GmailPeriodError, parse_gmail_period

            try:
                parse_gmail_period(text)
            except GmailPeriodError as exc:
                self._send(chat_id, f"I could not read that range. {exc}")
                return
            self._gmail_custom_users.discard(user_id)
            self._submit_command(user_id, chat_id, f"gmail status {text}")
            return

        if not command and re.fullmatch(r"gmail\s+status\s*[?.!]*", text, flags=re.IGNORECASE):
            self._submit_command(user_id, chat_id, "gmail status today")
            return

        if command == "/start":
            self._send(
                chat_id,
                "Jarvis remote control is ready. Send a natural-language command or use /help.",
            )
            return
        if command == "/help":
            self._send(chat_id, self._help_text())
            return
        if command == "/status":
            self._submit_command(user_id, chat_id, "system status")
            return
        if command == "/confirm":
            if not argument:
                self._send(chat_id, "Use /confirm <task_id>, or tap the Confirm button.")
            else:
                self._confirm_task(user_id, chat_id, argument)
            return
        if command == "/cancel":
            if not argument:
                self._send(chat_id, "Use /cancel <task_id>, or tap the Cancel button.")
            else:
                self._cancel_task(user_id, chat_id, argument)
            return
        if command:
            self._send(chat_id, "Unknown command. Use /help to see available commands.")
            return
        self._submit_command(user_id, chat_id, text)

    def _send_gmail_period_menu(self, chat_id: int) -> None:
        from jarvis_core.gmail_reporting import get_authoritative_gmail_status

        status = get_authoritative_gmail_status()
        icon = "✅" if status.get("healthy") else "⏸" if status.get("monitoring_paused") else "⚠️"
        self._send(
            chat_id,
            f"📧 Gmail status\n{icon} {status.get('message', 'Choose a report period.')}\n\nChoose a report period:",
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "Today", "callback_data": "gmail_period:today"},
                        {"text": "Last 2 Days", "callback_data": "gmail_period:last2"},
                    ],
                    [
                        {"text": "Last 3 Days", "callback_data": "gmail_period:last3"},
                        {"text": "Custom Range", "callback_data": "gmail_period:custom"},
                    ],
                ]
            },
        )

    def _handle_gmail_callback(
        self,
        user_id: int,
        chat_id: int,
        callback_id,
        data: str,
    ) -> None:
        action, payload = data.split(":", 1)
        self._answer_callback(callback_id, "Request received")
        if action == "gmail_period":
            if payload == "custom":
                self._gmail_custom_users.add(user_id)
                self._send(
                    chat_id,
                    "Type the Gmail start and end dates, for example:\nAug 19 to Aug 22\nor\n19/08/2026 to 22/08/2026",
                )
                return
            commands = {
                "today": "gmail status today",
                "last2": "gmail status last 2 days",
                "last3": "gmail status last 3 days",
            }
            command = commands.get(payload)
            if not command:
                self._send(chat_id, "Unknown Gmail period selection.")
                return
            self._submit_command(user_id, chat_id, command)
            return

        parts = payload.split("|")
        if action == "gmail_details" and len(parts) == 3:
            category, start, end = parts
            self._submit_command(user_id, chat_id, f"gmail details {category} {start} to {end}")
            return
        if action == "gmail_refresh" and len(parts) == 2:
            start, end = parts
            self._submit_command(user_id, chat_id, f"gmail refresh {start} to {end}")
            return
        if action == "gmail_draft" and len(parts) == 2:
            draft_action, draft_id = parts
            if not re.fullmatch(r"[A-Za-z0-9_-]+", draft_id):
                self._send(chat_id, "Invalid Gmail draft selection.")
                return
            commands = {
                "view": f"gmail draft view {draft_id}",
                "keep": f"gmail draft keep {draft_id}",
                "skip": f"gmail draft keep {draft_id}",
                "send": f"gmail draft send {draft_id}",
            }
            command = commands.get(draft_action)
            if not command:
                self._send(chat_id, "Unknown Gmail draft action.")
                return
            self._submit_command(user_id, chat_id, command)
            return
        self._send(chat_id, "Unknown Gmail action.")

    def _submit_command(self, user_id: int, chat_id: int, text: str) -> None:
        try:
            result = self.orchestrator.submit(
                text,
                source="Telegram",
                track_conversation=True,
                conversation_id=f"telegram:{user_id}",
            )
        except Exception:
            logger.exception("Telegram command submission failed")
            self._send(chat_id, "Jarvis could not accept that command.")
            return

        task_id = result.get("task_id")
        if isinstance(task_id, str):
            with self._mapping_lock:
                self._task_owners[task_id] = user_id
                self._task_chats[task_id] = chat_id

        status = result.get("status")
        if status == TaskStatus.waiting_for_confirmation.value and task_id:
            self._send_confirmation_for_task(task_id)
            return
        if status == "blocked":
            self._send(chat_id, result.get("message") or "This action is blocked for safety.")
            if task_id:
                self._forget_task(task_id)
            return
        if status in ("queued", TaskStatus.running.value) and task_id:
            self._send(chat_id, f"Queued in Jarvis LIVE TASKS.\nTask: {task_id}")
            self._start_task_watcher(task_id)
            return
        self._send(chat_id, result.get("message") or "Jarvis could not process that request.")

    def _confirm_task(self, user_id: int, chat_id: int, task_id: str) -> None:
        if not self._owns_task(user_id, task_id):
            self._send(chat_id, "That task is not available for confirmation.")
            return
        result = self.orchestrator.confirm(task_id)
        if result.get("status") == "queued":
            self._send(chat_id, "Confirmed. Jarvis has queued the task for execution.")
            self._start_task_watcher(task_id)
        else:
            self._send(chat_id, result.get("message") or "The task could not be confirmed.")

    def _cancel_task(self, user_id: int, chat_id: int, task_id: str) -> None:
        if not self._owns_task(user_id, task_id):
            self._send(chat_id, "That task is not available for cancellation.")
            return
        result = self.orchestrator.cancel(task_id)
        self._send(chat_id, result.get("message") or "Task cancellation requested.")
        if result.get("status") == "cancelled":
            self._forget_task(task_id)

    def _owns_task(self, user_id: int, task_id: str) -> bool:
        with self._mapping_lock:
            return self._task_owners.get(task_id) == user_id

    def _start_task_watcher(self, task_id: str) -> None:
        threading.Thread(
            target=self._watch_task,
            args=(task_id,),
            name=f"telegram-task-{task_id[:8]}",
            daemon=True,
        ).start()

    def _watch_task(self, task_id: str) -> None:
        deadline = time.monotonic() + self.settings.completion_timeout_seconds
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            task = self.task_manager.get_task(task_id)
            if task is None:
                self._send_for_task(task_id, "Jarvis could not find the submitted task.")
                self._forget_task(task_id)
                return
            if task.status == TaskStatus.waiting_for_confirmation.value:
                self._send_confirmation_for_task(task_id)
                return
            if task.status in TERMINAL_STATUSES:
                if not self._send_specialized_task_result(task_id, task):
                    self._send_for_task(task_id, self._format_task_result(task))
                self._forget_task(task_id)
                return
            self._stop_event.wait(0.25)

        if not self._stop_event.is_set():
            self._send_for_task(
                task_id,
                f"The task is still running in Jarvis LIVE TASKS.\nTask: {task_id}",
            )

    def _send_for_task(self, task_id: str, text: str) -> None:
        with self._mapping_lock:
            chat_id = self._task_chats.get(task_id)
        if chat_id is not None:
            self._send(chat_id, text)

    def _send_confirmation_for_task(self, task_id: str) -> None:
        with self._mapping_lock:
            chat_id = self._task_chats.get(task_id)
        if chat_id is None:
            return
        task = self.task_manager.get_task(task_id)
        if task is not None:
            match = re.fullmatch(
                r"gmail\s+draft\s+send\s+([A-Za-z0-9_-]+)",
                task.original_input.strip(),
                flags=re.IGNORECASE,
            )
            if match:
                from jarvis_core.gmail_reporting import view_gmail_draft

                preview = view_gmail_draft(match.group(1))
                draft = preview.get("draft") or {}
                body = str(draft.get("body") or "").strip().replace("\n", " ")
                body_preview = body[:300] + ("…" if len(body) > 300 else "")
                self._send(
                    chat_id,
                    "⚠️ CONFIRM GMAIL SEND\n"
                    f"To: {draft.get('to') or 'Unknown'}\n"
                    f"Subject: {draft.get('subject') or '(No subject)'}\n"
                    f"Preview: {body_preview or '(Empty draft body)'}",
                    reply_markup={
                        "inline_keyboard": [[
                            {"text": "✅ Confirm Send", "callback_data": f"confirm:{task_id}"},
                            {"text": "❌ Cancel", "callback_data": f"cancel:{task_id}"},
                        ]]
                    },
                )
                return
        self._send(
            chat_id,
            f"Confirmation required before Jarvis executes this action.\nTask: {task_id}",
            reply_markup={
                "inline_keyboard": [[
                    {"text": "Confirm", "callback_data": f"confirm:{task_id}"},
                    {"text": "Cancel", "callback_data": f"cancel:{task_id}"},
                ]]
            },
        )

    @staticmethod
    def _task_primary_output(task: Task) -> dict:
        result = task.result or {}
        if isinstance(result, dict) and result.get("kind"):
            return result
        for step in result.get("results", []) if isinstance(result, dict) else []:
            output = step.get("output", {})
            if isinstance(output, dict) and output.get("kind"):
                return output
        return {}

    def _send_specialized_task_result(self, task_id: str, task: Task) -> bool:
        output = self._task_primary_output(task)
        kind = output.get("kind")
        if not kind:
            return False
        with self._mapping_lock:
            chat_id = self._task_chats.get(task_id)
        if chat_id is None:
            return True

        if kind == "gmail_report":
            period = output.get("period") or (output.get("report") or {}).get("period") or {}
            start, end = period.get("start", ""), period.get("end", "")
            report = output.get("report") or {}
            rows = []
            if start and end:
                rows.extend([
                    [{"text": "Important Mails", "callback_data": f"gmail_details:important|{start}|{end}"}],
                    [{"text": "JD / Recruiter Mails", "callback_data": f"gmail_details:jd|{start}|{end}"}],
                    [
                        {"text": "Drafts", "callback_data": f"gmail_details:drafts|{start}|{end}"},
                        {"text": "Needs Review", "callback_data": f"gmail_details:needs_review|{start}|{end}"},
                    ],
                ])
            if report.get("errors") or (report.get("gmail_status") or {}).get("safe_error"):
                rows.append([{"text": "Errors", "callback_data": f"gmail_details:errors|{start}|{end}"}])
            if start and end:
                rows.append([{"text": "Refresh / Rescan", "callback_data": f"gmail_refresh:{start}|{end}"}])
                rows.append([
                    {"text": "Today", "callback_data": "gmail_period:today"},
                    {"text": "Last 2 Days", "callback_data": "gmail_period:last2"},
                    {"text": "Last 3 Days", "callback_data": "gmail_period:last3"},
                ])
                rows.append([{"text": "Custom Range", "callback_data": "gmail_period:custom"}])
            self._send(chat_id, output.get("summary") or output.get("message") or "Gmail report unavailable.", reply_markup={"inline_keyboard": rows} if rows else None)
            drafts = output.get("drafts") or []
            for index, draft in enumerate(drafts, start=1):
                self._send_gmail_draft_card(chat_id, draft, index, len(drafts))
            return True

        if kind == "gmail_details":
            self._send(chat_id, output.get("summary") or "No Gmail details found.")
            if output.get("category") in {"important", "jd", "needs_review"}:
                for event in (output.get("events") or [])[:10]:
                    self._send_gmail_event_card(chat_id, event)
            drafts = output.get("drafts") or []
            for index, draft in enumerate(drafts, start=1):
                self._send_gmail_draft_card(chat_id, draft, index, len(drafts))
            return True

        if kind == "gmail_draft":
            draft = output.get("draft") or {}
            draft_id = draft.get("id", "")
            self._send(
                chat_id,
                output.get("summary") or "Gmail draft loaded.",
                reply_markup={"inline_keyboard": [[
                    {"text": "✅ Send Draft", "callback_data": f"gmail_draft:send|{draft_id}"},
                    {"text": "✏️ Keep as Draft", "callback_data": f"gmail_draft:keep|{draft_id}"},
                ]]},
            )
            return True

        self._send(chat_id, output.get("summary") or output.get("message") or "Gmail task completed.")
        return True

    def _send_gmail_event_card(self, chat_id: int, event: dict) -> None:
        gmail_id = str(event.get("gmail_thread_id") or event.get("gmail_message_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", gmail_id):
            return
        buttons = [{"text": "📨 Open in Gmail", "url": f"https://mail.google.com/mail/u/0/#all/{gmail_id}"}]
        draft_id = str(event.get("gmail_draft_id") or "")
        if re.fullmatch(r"[A-Za-z0-9_-]+", draft_id):
            buttons.append({"text": "👁 View Full Draft", "callback_data": f"gmail_draft:view|{draft_id}"})
        self._send(
            chat_id,
            f"📧 {str(event.get('subject') or '(No subject)')[:160]}\n"
            f"From: {str(event.get('from') or 'Unknown sender')[:120]}\n"
            f"Received (Jarvis time): {str(event.get('received_at_display') or event.get('email_date') or 'Unknown')[:100]}",
            reply_markup={"inline_keyboard": [buttons]},
        )

    def _send_gmail_draft_card(self, chat_id: int, draft: dict, index: int, total: int) -> None:
        draft_id = str(draft.get("id") or "")
        if not draft_id:
            return
        self._notified_draft_ids.add(draft_id)
        body = str(draft.get("body") or "").strip().replace("\n", " ")
        preview = body[:280] + ("…" if len(body) > 280 else "")
        heading = f"Draft {index} of {total}\n" if total > 1 else ""
        source_sender = draft.get("source_sender")
        date_label = "Received (Jarvis time)" if draft.get("received") else "Draft created (Jarvis time)"
        date_value = (
            draft.get("received_at_display")
            or draft.get("received")
            or draft.get("created_at_display")
            or draft.get("created_at")
            or draft.get("date")
            or "Unknown"
        )
        self._send(
            chat_id,
            f"{heading}{'💼 JD EMAIL' if source_sender else '📝 GMAIL DRAFT'}\n"
            f"{'From' if source_sender else 'To'}: {source_sender or draft.get('to') or 'Unknown'}\n"
            f"Subject: {draft.get('source_subject') or draft.get('subject') or '(No subject)'}\n"
            f"{date_label}: {date_value}\n\n"
            f"📝 DRAFT\n{preview or '(Use View Full Draft to load the complete body.)'}",
            reply_markup={"inline_keyboard": [
                [
                    {"text": "👁 View Full Draft", "callback_data": f"gmail_draft:view|{draft_id}"},
                    {"text": "✅ Send Draft", "callback_data": f"gmail_draft:send|{draft_id}"},
                ],
                [
                    {"text": "✏️ Keep as Draft", "callback_data": f"gmail_draft:keep|{draft_id}"},
                    {"text": "Skip", "callback_data": f"gmail_draft:skip|{draft_id}"},
                ],
            ]},
        )

    def _notify_new_gmail_drafts(self) -> None:
        from jarvis_core.gmail_reporting import list_new_draft_events
        from jarvis_core.gmail_service import get_gmail_draft

        events = list_new_draft_events(self._last_gmail_event_at)
        self._last_gmail_event_at = datetime.now().astimezone().isoformat()
        if not events:
            return
        with self._mapping_lock:
            chat_ids = list(set(self._authorized_chats.values()))
        for event in events:
            draft_id = str(event.get("gmail_draft_id") or "")
            if not draft_id or draft_id in self._notified_draft_ids:
                continue
            draft = get_gmail_draft(draft_id) or {"id": draft_id, "body": ""}
            draft.update({
                "source_sender": event.get("from", ""),
                "source_subject": event.get("subject", ""),
                "received": event.get("email_date", ""),
            })
            for chat_id in chat_ids:
                self._send_gmail_draft_card(chat_id, draft, 1, 1)

    @staticmethod
    def _format_task_result(task: Task) -> str:
        if task.status == TaskStatus.cancelled.value:
            return "Task cancelled."
        if task.status == TaskStatus.failed.value:
            return f"Jarvis task failed: {task.error or 'Execution failed.'}"

        public = TaskPublic.from_task(task)
        result = task.result or {}
        reply = result.get("reply") if isinstance(result, dict) else None
        details = [
            step.get("output_summary", "")
            for step in (public.step_results or [])
            if step.get("output_summary")
        ]
        content = str(reply or "").strip()
        if not content and details:
            content = "\n".join(dict.fromkeys(details))
        if not content:
            content = str(public.result_summary or "Task completed.").strip()
        prefix = "Jarvis partially completed the task:" if task.status == TaskStatus.partially_completed.value else "Jarvis completed the task:"
        return f"{prefix}\n{content}"

    def _forget_task(self, task_id: str) -> None:
        with self._mapping_lock:
            self._task_owners.pop(task_id, None)
            self._task_chats.pop(task_id, None)

    def _is_authorized(self, user_id: int) -> bool:
        return user_id in self.settings.allowed_user_ids

    def _send(self, chat_id: int, text: str, *, reply_markup: Optional[dict] = None) -> None:
        chunks = self._split_message(text)
        for index, chunk in enumerate(chunks):
            try:
                self._require_api().send_message(
                    chat_id,
                    chunk,
                    reply_markup=reply_markup if index == 0 else None,
                )
            except TelegramAPIError as exc:
                logger.warning("Telegram reply failed: %s", exc)
                return

    def _answer_callback(self, callback_id, text: str) -> None:
        if not isinstance(callback_id, str):
            return
        try:
            self._require_api().answer_callback_query(callback_id, text)
        except TelegramAPIError as exc:
            logger.warning("Telegram callback acknowledgement failed: %s", exc)

    def _require_api(self) -> TelegramBotAPI:
        if self._api is None:
            raise RuntimeError("Telegram API client has not been initialized")
        return self._api

    @staticmethod
    def _split_message(text: str, limit: int = 4000) -> list[str]:
        clean = str(text or "").strip() or "Jarvis returned an empty response."
        return [clean[index:index + limit] for index in range(0, len(clean), limit)]

    @staticmethod
    def _help_text() -> str:
        return (
            "Jarvis Telegram commands:\n"
            "/start - check the remote control\n"
            "/help - show this help\n"
            "/status - run Jarvis system status\n"
            "/confirm <task_id> - confirm a protected task\n"
            "/cancel <task_id> - cancel one of your tasks\n\n"
            "Gmail: send 'gmail status' for period choices, or use phrases like "
            "'gmail status yesterday' and 'gmail status last 3 days'.\n\n"
            "You can also send normal natural-language Jarvis commands. "
            "Protected actions always require confirmation."
        )


telegram_service = TelegramService()
