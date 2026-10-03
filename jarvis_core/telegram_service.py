"""Authorized Telegram remote control for Jarvis using Bot API long polling."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import Optional

from jarvis_core.resource_manager import (
    build_chrome_tab_detail,
    build_chrome_tabs_menu,
    build_resource_detail,
    build_resources_menu,
    resource_inspector,
    resource_registry,
)
from jarvis_core.resource_menus import (
    build_all_apps_menu,
    build_chrome_accounts_menu,
    build_current_running_menu,
    build_resource_review_home,
    build_window_tabs_menu,
)
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
        self._feature_orchestrator = orchestrator_instance
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
        self._mode_pending: dict = {}
        self._feature_review_pending: dict = {}
        self._feature_changes_pending: dict = {}
        # Part 5: pending workflow-command disambiguation (many waiting).
        self._workflow_command_pending: dict = {}
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
    def feature_orchestrator(self):
        if self._feature_orchestrator is None:
            from jarvis_core.feature_orchestrator import feature_orchestrator

            self._feature_orchestrator = feature_orchestrator
        # If the injected instance doesn't implement feature detection, fall back
        # to the real singleton (this keeps test doubles working without changes).
        if not hasattr(self._feature_orchestrator, "is_feature_request"):
            from jarvis_core.feature_orchestrator import feature_orchestrator

            return feature_orchestrator
        return self._feature_orchestrator

    def _wire_feature_orchestrator(self) -> None:
        """Give the feature orchestrator safe Telegram send/auth hooks.

        Without this, `feature_orchestrator._telegram_send` remains None
        (stage messages are silently dropped) and `_authorize()` returns
        False, which makes the automatic resource-review continuation return
        an unnoticed error and park the feature at the review stage forever.
        """
        try:
            self.feature_orchestrator.set_telegram_channels(
                send=lambda chat_id, text, markup=None: self._send(
                    chat_id, text, reply_markup=markup,
                ),
                is_authorized=self._is_authorized,
            )
            logger.info(
                "Feature orchestrator wired to Telegram send/auth channels"
            )
        except Exception:
            logger.exception("Could not wire the feature orchestrator to Telegram")

    def _wire_bootstrap_notifier(self) -> None:
        """Give the Part 4 bootstrap service a Telegram notify hook so it can
        tell the authorized user 'GPT is ready' on WAITING_FOR_COMMAND, and
        recover any missed notifications for already-waiting workflows."""
        try:
            from jarvis_core.workflow_bootstrap_service import workflow_bootstrap_service
            workflow_bootstrap_service.set_telegram_channels(
                notify=lambda text, markup=None: self.notify_authorized(
                    text, reply_markup=markup))
            recovered = workflow_bootstrap_service.recover_awaiting_command_notifications()
            if recovered:
                logger.info("Recovered %d WAITING_FOR_COMMAND notification(s)",
                            len(recovered))
            logger.info("Bootstrap WAITING_FOR_COMMAND notifier wired to Telegram")
        except Exception:
            logger.exception("Could not wire the bootstrap notifier to Telegram")

    def _wire_workflow_command_service(self) -> None:
        """Give the Part 5 WorkflowCommandService Telegram send/edit/auth hooks.

        The service sends ONE status message per command and edits it in place
        (never floods the chat). ``send_status`` returns the message id so the
        service can edit that exact message; ``edit_status`` performs the edit.
        """
        try:
            from jarvis_core.workflow_command_service import workflow_command_service
            workflow_command_service.set_telegram_channels(
                send_status=self._send_returning_id,
                edit_status=lambda chat_id, msg_id, text: self._edit_message(
                    chat_id, msg_id, text),
                is_authorized=self._is_authorized,
            )
            workflow_command_service.start_worker()
            logger.info("Workflow command service wired to Telegram + worker started")
        except Exception:
            logger.exception("Could not wire the workflow command service to Telegram")

    def _route_workflow_command(self, user_id: int, chat_id: int, text: str,
                                message_id: Optional[int]) -> bool:
        """Route free text to the Part 5 command service when appropriate.

        Returns True when the message was consumed as a workflow command (or a
        disambiguation prompt was shown). Returns False when NO workflow is
        waiting, so the caller continues with the generic feature/normal flow.
        """
        try:
            from jarvis_core.workflow_command_service import (
                workflow_command_service,
                AmbiguousWorkflowError,
                CommandBusyError,
                NoActiveWorkflowError,
                CommandError,
            )
        except Exception:
            logger.debug("workflow command service unavailable", exc_info=True)
            return False

        # Only intercept when at least one workflow is genuinely waiting; this
        # keeps the existing bot behaviour intact when Part 5 is idle.
        try:
            waiting = workflow_command_service.list_waiting_workflows()
        except Exception:
            logger.debug("list_waiting_workflows failed", exc_info=True)
            return False
        if not waiting:
            return False

        try:
            result = workflow_command_service.submit_command(
                text=text,
                telegram_message_id=message_id,
                telegram_chat_id=chat_id,
                telegram_user_id=user_id,
            )
            self._audit_workflow_route(
                user_id=user_id, chat_id=chat_id, message_id=message_id,
                text=text, result=result, candidates=None)
        except AmbiguousWorkflowError as exc:
            lines = ["Multiple workflows are waiting for a command. Reply with "
                     "the number to choose one:"]
            for idx, cand in enumerate(exc.candidates, start=1):
                label = cand.get("project_name") or cand.get("workflow_id", "")[:8]
                lines.append(f"{idx}. {label}")
            # Remember the candidates so a follow-up numeric reply resolves.
            self._workflow_command_pending[(user_id, chat_id)] = {
                "text": text, "message_id": message_id,
                "candidates": exc.candidates,
            }
            self._send(chat_id, "\n".join(lines))
            return True
        except CommandBusyError:
            self._send(
                chat_id,
                "\u23f3 A command is already running for this workflow. Please "
                "wait for the current GPT response to finish.",
            )
            return True
        except NoActiveWorkflowError:
            # Should not happen (we checked waiting), but fall through safely.
            return False
        except CommandError as exc:
            self._send(chat_id, f"Could not send that command: {exc}")
            return True
        except Exception:
            logger.exception("workflow command submit failed")
            self._send(chat_id, "Could not send that command to ChatGPT.")
            return True
        return True

    def _audit_workflow_route(self, *, user_id, chat_id, message_id, text,
                              result, candidates) -> None:
        """Append a diagnostic JSON line proving the REAL Telegram → Part 5 route.

        Diagnostic only (no behaviour change). Records the exact Telegram
        update_id/message_id, the authorized user/chat, and the workflow the
        command service selected, so a live Telegram gate can prove the path
        without direct Python/service invocation.
        """
        try:
            import json as _json
            import hashlib as _hashlib
            from jarvis_core.timezone_utils import jarvis_now
            entry = {
                "ts": jarvis_now().isoformat(),
                "update_id": getattr(self, "_last_update_id", None),
                "message_id": message_id,
                "user_id": user_id,
                "chat_id": chat_id,
                "authorized": self._is_authorized(user_id),
                "workflow_id": (result or {}).get("workflow_id") if isinstance(result, dict) else None,
                "command_hash": _hashlib.sha256((text or "").encode("utf-8")).hexdigest(),
                "candidates": candidates,
            }
            with open("C:\\Project Opus\\.p5tg_route_audit.log", "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(entry) + "\n")
            logger.info("PART5_TELEGRAM_ROUTE update_id=%s message_id=%s workflow_id=%s",
                        entry["update_id"], message_id, entry["workflow_id"])
        except Exception:
            logger.debug("route audit write failed", exc_info=True)

    def _wire_approval_service(self) -> None:
        """Give the Part 6 WorkflowApprovalService Telegram send/edit/auth hooks
        and start its worker. Reuses the EXISTING bot (no second poller)."""
        try:
            from jarvis_core.workflow_approval_service import workflow_approval_service
            workflow_approval_service.set_telegram_channels(
                send=lambda chat_id, text, markup=None: self._send_returning_id(
                    chat_id, text, reply_markup=markup),
                edit=lambda chat_id, msg_id, text, markup=None: self._edit_message(
                    chat_id, msg_id, text, reply_markup=markup),
                is_authorized=self._is_authorized,
            )
            workflow_approval_service.start_worker()
            logger.info("Workflow approval service wired to Telegram + worker started")
        except Exception:
            logger.exception("Could not wire the workflow approval service to Telegram")
        # Wire the Kiro session-mismatch confirmation service (reuses the same
        # bot: send + auth). Its callbacks arrive as kmm:<decision>:<mismatch_id>.
        try:
            from jarvis_core.kiro_mismatch_service import kiro_mismatch_service
            kiro_mismatch_service.wire(
                tg_send=lambda chat_id, text, markup=None: self._send_returning_id(
                    chat_id, text, reply_markup=markup),
                tg_is_authorized=self._is_authorized,
            )
            logger.info("Kiro session-mismatch service wired to Telegram")
        except Exception:
            logger.exception("Could not wire the Kiro mismatch service to Telegram")

    def _handle_approval_callback(self, user_id: int, chat_id: int,
                                  callback_id, data: str,
                                  message_id: Optional[int]) -> None:
        """Delegate a Part 6 approval callback to WorkflowApprovalService.

        Callback data: ``appr:<action>:<token>`` where action is
        proceed/edit/cancel/eproceed. TelegramService only routes + answers; all
        approval logic (auth, idempotency, stale rejection) lives in the service.
        """
        try:
            from jarvis_core.workflow_approval_service import workflow_approval_service
        except Exception:
            self._answer_callback(callback_id, "Unavailable")
            return
        parts = data.split(":", 2)
        if len(parts) < 3:
            self._answer_callback(callback_id, "Bad request")
            return
        _, action, token = parts[0], parts[1], parts[2]
        try:
            if action == "eproceed":
                result = workflow_approval_service.handle_edit_proceed(
                    user_id=user_id, chat_id=chat_id, token=token,
                    message_id=message_id)
            else:
                result = workflow_approval_service.handle_callback(
                    user_id=user_id, chat_id=chat_id, action=action, token=token,
                    callback_id=callback_id, message_id=message_id)
        except Exception:
            logger.exception("approval callback failed")
            self._answer_callback(callback_id, "Error")
            return
        # Acknowledge the callback (short toast) and, when not delivered by the
        # service's own send, surface the message.
        msg = (result or {}).get("message") or "Done"
        self._answer_callback(callback_id, msg[:180])

    def _handle_kiro_mismatch_callback(self, user_id: int, chat_id: int,
                                       callback_id, data: str,
                                       message_id: Optional[int]) -> None:
        """Delegate a Kiro session-mismatch decision to KiroMismatchService.

        Callback data: ``kmm:<decision>:<mismatch_id>`` where decision is
        continue/new/pause/cancel. TelegramService only routes + answers.
        """
        try:
            from jarvis_core.kiro_mismatch_service import kiro_mismatch_service
        except Exception:
            self._answer_callback(callback_id, "Unavailable")
            return
        parts = data.split(":", 2)
        if len(parts) < 3:
            self._answer_callback(callback_id, "Bad request")
            return
        _, decision, mismatch_id = parts[0], parts[1], parts[2]
        try:
            result = kiro_mismatch_service.handle_callback(
                user_id=user_id, chat_id=chat_id, decision=decision,
                mismatch_id=mismatch_id, callback_id=callback_id,
                message_id=message_id)
        except Exception:
            logger.exception("kiro mismatch callback failed")
            self._answer_callback(callback_id, "Error")
            return
        msg = (result or {}).get("message") or "Done"
        self._answer_callback(callback_id, msg[:180])

    def _route_workflow_command_to(self, user_id: int, chat_id: int, text: str,
                                   message_id: Optional[int],
                                   workflow_id: Optional[str]) -> None:
        """Submit a command to an EXPLICIT workflow (after disambiguation)."""
        try:
            from jarvis_core.workflow_command_service import (
                workflow_command_service, CommandBusyError, CommandError,
            )
        except Exception:
            self._send(chat_id, "Workflow command service is unavailable.")
            return
        try:
            workflow_command_service.submit_command(
                text=text,
                telegram_message_id=message_id,
                telegram_chat_id=chat_id,
                telegram_user_id=user_id,
                workflow_id=workflow_id,
            )
        except CommandBusyError:
            self._send(
                chat_id,
                "\u23f3 A command is already running for this workflow.",
            )
        except CommandError as exc:
            self._send(chat_id, f"Could not send that command: {exc}")
        except Exception:
            logger.exception("workflow command submit (explicit) failed")
            self._send(chat_id, "Could not send that command to ChatGPT.")

    def _send_returning_id(self, chat_id: int, text: str,
                           *, reply_markup: Optional[dict] = None) -> Optional[int]:
        """Send a message and return its Telegram message id (or None).

        Used by the Part 5 command service (single editable status message) and
        the Part 6 approval service (approval buttons). Errors are swallowed
        (best-effort UX).
        """
        try:
            result = self._require_api().send_message(
                chat_id, text, reply_markup=reply_markup)
        except Exception:
            logger.debug("send_returning_id failed", exc_info=True)
            return None
        if isinstance(result, dict):
            mid = result.get("message_id")
            return mid if isinstance(mid, int) else None
        return None

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

            # Wire the Autonomous Feature Orchestrator's Telegram send/auth
            # hooks BEFORE polling starts. Without this the orchestrator's
            # resource-review / approval messages are silently dropped and
            # `_authorize()` returns False, so an auto-continuing stage would
            # return an unnoticed error and park the feature forever.
            self._wire_feature_orchestrator()
            # Wire the Part 5 Autonomous Workflow command service so a Telegram
            # command reaches the SAME ChatGPT conversation via its worker.
            self._wire_workflow_command_service()
            # Wire the Part 6 approval service (GPT output → Telegram approval).
            self._wire_approval_service()
            # Wire the Part 4 bootstrap service's WAITING_FOR_COMMAND readiness
            # notification (reuses this bot; no second poller) + recover any
            # pending notifications for already-waiting workflows.
            self._wire_bootstrap_notifier()

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
        # Diagnostic only (no behaviour change): remember the last update_id so
        # the Part 5 routing audit can correlate a routed command with the exact
        # Telegram update that carried it.
        try:
            self._last_update_id = update.get("update_id")
        except Exception:
            self._last_update_id = None
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
        message_id = message.get("message_id")
        if not isinstance(user_id, int) or not isinstance(chat_id, int) or not isinstance(text, str):
            return
        if not self._is_authorized(user_id):
            self._send(chat_id, "You are not authorized to control Jarvis.")
            return
        with self._mapping_lock:
            self._authorized_chats[user_id] = chat_id
        self._dispatch_text(user_id, chat_id, text.strip(), message_id=message_id)

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
        if data.startswith("feature:"):
            self._handle_feature_callback(
                user_id, chat_id, callback_id, data,
                message_id=(
                    message.get("message_id")
                    if isinstance(message, dict) else None
                ),
            )
            return
        # ── Part 6: GPT-output approval callbacks (appr:<action>:<token>) ────
        if data.startswith("appr:"):
            self._handle_approval_callback(
                user_id, chat_id, callback_id, data,
                message_id=(message.get("message_id")
                            if isinstance(message, dict) else None),
            )
            return
        # ── Kiro session-mismatch decisions (kmm:<decision>:<mismatch_id>) ────
        if data.startswith("kmm:"):
            self._handle_kiro_mismatch_callback(
                user_id, chat_id, callback_id, data,
                message_id=(message.get("message_id")
                            if isinstance(message, dict) else None),
            )
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

    def _handle_feature_callback(self, user_id, chat_id, callback_id, callback,
                                 message_id=None):
        if not isinstance(callback, str):
            return
        if callback == "feature:noop" or callback.startswith("feature:noop:"):
            return
        route = callback[len("feature:"):]
        action, _, param = route.partition(":")
        if action.startswith("resrc"):
            try:
                from jarvis_core.resource_review_handler import (
                    handle_resource_review_action,
                )
                handle_resource_review_action(
                    self, user_id, chat_id, callback_id, route,
                    message_id=message_id,
                )
            except Exception:
                logger.exception(
                    "Resource review action failed for %s", route[:40])
                self._send(
                    chat_id,
                    "Something went wrong completing that action. Use Refresh.")
            return
        if action == "mode":
            self._handle_mode_choice(user_id, chat_id, callback_id, param)
            return
        if action == "review":
            self._arm_review_capture(user_id, chat_id, callback_id, param)
            return
        if action == "review_cancel":
            self._clear_review_capture(user_id, chat_id, callback_id, param)
            return
        if action == "cancel":
            self._cancel_feature_action(user_id, chat_id, callback_id, param)
            return
        if action == "chatgpt_skip":
            self._skip_chatgpt_action(user_id, chat_id, callback_id, param)
            return
        if action == "chatgpt_show":
            self._chatgpt_show_action(user_id, chat_id, callback_id, param)
            return
        if action == "proof":
            self._proof_action(user_id, chat_id, callback_id, param)
            return
        if action == "apps":
            self._apps_action(user_id, chat_id, callback_id, param)
            return
        if action == "apps_refresh":
            self._apps_refresh_action(user_id, chat_id, callback_id, param)
            return
        if action == "approve":
            self._approve_action(user_id, chat_id, callback_id, param)
            return
        if action == "plan_view":
            self._model_view_action(chat_id, callback_id, param, "plan")
            return
        if action == "prompt_view":
            self._model_view_action(chat_id, callback_id, param, "prompt")
            return
        if action == "confirm_high_risk":
            self._high_risk_action(user_id, chat_id, callback_id, param,
                                   confirm=True)
            return
        if action == "cancel_high_risk":
            self._high_risk_action(user_id, chat_id, callback_id, param,
                                   confirm=False)
            return
        if action == "changes":
            self._arm_changes(user_id, chat_id, callback_id, param)
            return
        if action == "missing":
            self._missing_action(chat_id, callback_id, param)
            return
        if action == "reresearch":
            self._reresearch_action(user_id, chat_id, callback_id, param)
            return
        if action == "noop":
            self._answer_callback(callback_id, "Kept open")
            return
        self._answer_callback(callback_id, "Unknown action")

    def _handle_mode_choice(self, user_id: int, chat_id: int, callback_id,
                            choice: str) -> None:
        key = (user_id, chat_id)
        text = self._mode_pending.pop(key, None)
        self._answer_callback(callback_id, "Request received")
        if not text:
            self._send(chat_id, "No pending request to act on.")
            return
        if choice == "normal":
            self._submit_command(user_id, chat_id, text)
            return
        if choice == "cancel":
            self._send(chat_id, "Cancelled.")
            return
        if choice == "feature":
            self._submit_feature_request(user_id, chat_id, text)
            return
        self._send(chat_id, "Unknown mode selection.")

    def _submit_feature_request(self, user_id: int, chat_id: int,
                                text: str) -> None:
        try:
            result = self.feature_orchestrator.submit_feature_request(
                text,
                source="Telegram",
                telegram_user_id=user_id,
                telegram_chat_id=chat_id,
            )
        except Exception:
            logger.exception("Feature request submission failed")
            self._send(chat_id, "Jarvis could not accept that feature request.")
            return
        message = result.get("message")
        self._send(chat_id, message or "Feature request accepted.")

    def _arm_review_capture(self, user_id: int, chat_id: int, callback_id,
                            feature_id: str) -> None:
        self._answer_callback(callback_id, "Prompt capture armed")
        try:
            feature = self.feature_orchestrator.get_feature(feature_id) or {}
        except Exception:
            feature = {}
        if not feature or feature.get("status") != "chatgpt_review_pending":
            self._send(chat_id,
                       "That feature is not waiting for a ChatGPT review.")
            return
        key = (user_id, chat_id)
        previous = self._feature_review_pending.get(key)
        self._feature_review_pending[key] = feature_id
        title = (feature.get("title")
                 or feature.get("interpreted_objective")
                 or feature_id)
        note = ""
        if previous and previous != feature_id:
            note = ("\n\nReplaced your pending review entry - the older one "
                    "is still waiting for its own submission.")
        self._send(
            chat_id,
            "Paste the exact prompt you want Jarvis to use for Cline.\n\n"
            f"It will be attached to:\n{title}\n\n"
            "Send /cancel to exit prompt entry mode." + note,
        )

    def _clear_review_capture(self, user_id: int, chat_id: int, callback_id,
                              feature_id: str) -> None:
        self._answer_callback(callback_id, "Exited prompt capture")
        key = (user_id, chat_id)
        if self._feature_review_pending.get(key) == feature_id:
            del self._feature_review_pending[key]
        self._send(
            chat_id,
            "ChatGPT review is still waiting to be submitted. Use the "
            "\"Submit / Replace Cline Prompt\" button again.",
        )

    def _cancel_feature_action(self, user_id: int, chat_id: int, callback_id,
                               feature_id: str) -> None:
        self._answer_callback(callback_id, "Cancelling feature")
        try:
            result = self.feature_orchestrator.cancel(
                feature_id, telegram_user_id=user_id)
        except Exception:
            logger.exception("Feature cancel failed for %s", feature_id)
            self._send(chat_id, "Could not cancel that feature.")
            return
        self._send(chat_id, result.get("message") or "Feature cancelled.")

    def _skip_chatgpt_action(self, user_id: int, chat_id: int, callback_id,
                             feature_id: str) -> None:
        self._answer_callback(callback_id, "Skipping ChatGPT")
        try:
            result = self.feature_orchestrator.skip_chatgpt(feature_id)
        except Exception:
            logger.exception("Skip ChatGPT failed for %s", feature_id)
            self._send(chat_id, "Could not skip ChatGPT for that feature.")
            return
        self._send(chat_id, result.get("message") or "ChatGPT skipped.")

    def _chatgpt_show_action(self, user_id: int, chat_id: int, callback_id,
                             feature_id: str) -> None:
        self._answer_callback(callback_id, "Showing request")
        try:
            feature = self.feature_orchestrator.get_feature(feature_id) or {}
        except Exception:
            feature = {}
        prompt = (feature.get("chatgpt_prompt")
                  or feature.get("interpreted_objective")
                  or "No ChatGPT request on file for that feature.")
        self._send(chat_id, f"ChatGPT request:\n\n{prompt}")

    def _apps_action(self, user_id: int, chat_id: int, callback_id,
                     feature_id: str) -> None:
        """Render the persisted resource snapshot for this feature.

        Apps/Tabs is informational only - it NEVER changes feature stage. The
        snapshot is read from the feature record (persisted at resource-review
        time) so it works indefinitely after the scan thread exits. A Refresh
        re-scans real-time and updates the stored snapshot.
        """
        self._answer_callback(callback_id, "Apps / Tabs")
        logger.info("Apps/Tabs callback received feature=%s", feature_id[:8])
        try:
            feature = self.feature_orchestrator.get_feature(feature_id) or {}
        except Exception:
            logger.exception("Apps/Tabs feature load failed feature=%s",
                             feature_id[:8])
            feature = {}
        if not feature:
            logger.warning("Apps/Tabs feature not found feature=%s",
                           feature_id[:8])
            self._send(chat_id, "⚠️ This action is no longer available for "
                        "this feature.")
            return
        snapshot = feature.get("resource_snapshot") or {}
        self._render_apps_tabs(
            chat_id, feature_id, feature, snapshot, is_refresh=False)

    def _apps_refresh_action(self, user_id: int, chat_id: int,
                             callback_id, feature_id: str) -> None:
        """Refresh Apps / Tabs: do a NEW read-only scan + update persisted
        snapshot. Info-only: never restarts research or changes feature stage.
        """
        self._answer_callback(callback_id, "Refreshing")
        logger.info("Resource refresh callback received feature=%s",
                    feature_id[:8])
        try:
            feature = self.feature_orchestrator.get_feature(feature_id) or {}
        except Exception:
            logger.exception("Apps/Tabs refresh load failed feature=%s",
                             feature_id[:8])
            feature = {}
        if not feature:
            self._send(chat_id, "No longer available for this feature.")
            return
        snapshot = feature.get("resource_snapshot") or {}
        logger.info("Resource refresh scan started feature=%s", feature_id[:8])
        try:
            fresh = resource_inspector.scan(
                feature_id=feature_id, include_identity=True)
            logger.info(
                "Resource refresh scan completed feature=%s apps=%d tabs=%d",
                feature_id[:8],
                len(fresh.get("applications") or []),
                len(fresh.get("tabs") or []),
            )
            try:
                self.feature_orchestrator.update_resource_snapshot(
                    feature_id, fresh)
                logger.info("Resource snapshot updated feature=%s",
                            feature_id[:8])
            except Exception:
                logger.exception("Persist refreshed snapshot failed feature=%s",
                                 feature_id[:8])
            snapshot = fresh
        except Exception:
            logger.exception("Resource refresh scan failed feature=%s",
                             feature_id[:8])
            self._send(chat_id,
                       "Unable to refresh resources right now.\n"
                       "Showing the last captured snapshot.")
        self._render_apps_tabs(
            chat_id, feature_id, feature, snapshot, is_refresh=True)

    def _render_apps_tabs(self, chat_id: int, feature_id: str,
                          feature: dict, snapshot: dict,
                          is_refresh: bool = False) -> None:
        """Render the Apps / Tabs view (informational only, read-only).

        For Chrome tabs the real tab/window title takes priority over
        ``display_name`` (which is always "Chrome" for every tab Resource).
        Falls back to an explicit placeholder when a title is unavailable.
        """
        title = (feature.get("interpreted_objective")
                 or feature.get("original_request") or feature_id)
        apps = snapshot.get("applications") or []
        tabs = snapshot.get("tabs") or []
        captured = snapshot.get("scanned_at") or ""
        logger.info(
            "Apps/Tabs rendering apps=%s tabs=%s feature=%s refresh=%s",
            len(apps), len(tabs), feature_id[:8], is_refresh,
        )
        lines = ["\U0001f9e9 RESOURCES USED FOR THIS FEATURE",
                 f"\nFeature: {title}"]
        if captured:
            lines.append(f"Captured: {str(captured)[:19].replace('T', ' ')}")
        if apps or tabs:
            if apps:
                lines.append("\n\U0001f4bb Open Apps")
                _seen: set[str] = set()
                for app in apps:
                    name = (app.get("title")
                            or app.get("display_name")
                            or app.get("executable") or "App")
                    key = name.lower()
                    if key in _seen:
                        continue
                    _seen.add(key)
                    lines.append(f"{len(_seen)}. {name}")
            if tabs:
                lines.append("\n\U0001f310 Chrome Tabs")
                _seen: set[str] = set()
                for tab in tabs:
                    name = (tab.get("title")
                            or tab.get("safe_display_target")
                            or "Chrome tab title unavailable")
                    key = name.lower()
                    if key in _seen:
                        continue
                    _seen.add(key)
                    lines.append(f"{len(_seen)}. {name}")
        else:
            lines.append(
                "\nNo relevant open apps or Chrome tabs were detected.")
        text = "\n".join(lines)
        rows = [
            [{"text": "\U0001f504 Refresh Apps / Tabs",
              "callback_data": f"feature:apps_refresh:{feature_id}"}],
            [{"text": "\u2b05\ufe0f Back",
              "callback_data": f"feature:chatgpt_show:{feature_id}"}],
            [{"text": "\U0001f6ab Cancel Feature",
              "callback_data": f"feature:cancel:{feature_id}"}],
        ]
        self._send(chat_id, text, reply_markup={"inline_keyboard": rows})

    def _proof_action(self, user_id: int, chat_id: int, callback_id,
                      feature_id: str) -> None:
        """\U0001f4f8 Current-work proof for the feature (text evidence only)."""
        self._answer_callback(callback_id, "Proof")
        try:
            feature = self.feature_orchestrator.get_feature(feature_id) or {}
        except Exception:
            logger.exception("Proof load failed feature=%s", feature_id[:8])
            feature = {}
        if not feature:
            self._send(
                chat_id,
                "\u26a0\ufe0f This action is no longer available for this feature.",
            )
            return
        title = (feature.get("interpreted_objective")
                 or feature.get("original_request") or feature_id)
        lines = [
            "\U0001f4f8 CURRENT WORK PROOF",
            "",
            f"Feature: {title}",
        ]
        history = feature.get("proof_history") or []
        if not history:
            lines += [
                "Phase: (not started)",
                "Evidence: research has not captured a snapshot yet.",
            ]
        else:
            entry = history[-1]
            paths = entry.get("related_paths") or []
            path_lines = "\n".join(f"- {p}" for p in paths) \
                or "- (no repository paths in this stage)"
            lines += [
                f"Phase: {entry.get('phase')}",
                f"Current: {entry.get('stage')}",
                f"Time: {str(entry.get('at', ''))[:19].replace('T', ' ')}",
                f"Evidence:\n{path_lines}",
                "",
                f"{entry.get('description', '')}",
                "",
                f"Proof type: {entry.get('proof_type')} (text evidence; "
                "screenshot skipped to protect secrets)",
            ]
        self._send(chat_id, "\n".join(lines))

    def _approve_action(self, user_id: int, chat_id: int, callback_id,
                        feature_id: str) -> None:
        """First approval click; high-risk features still need a second
        confirmation (sent by the orchestrator with its own buttons)."""
        self._answer_callback(callback_id, "Approval received")
        try:
            result = self.feature_orchestrator.approve_cline(feature_id, user_id)
        except Exception:
            logger.exception("Feature approval failed for %s", feature_id)
            self._send(chat_id, "Could not approve that feature.")
            return
        status = result.get("status")
        if status == "error":
            self._send(chat_id, result.get(
                "message") or "That feature is not awaiting approval.")
            return
        if status == "high_risk_confirmation_required":
            # The orchestrator already sent the high-risk confirmation card
            # with its own Confirm / Cancel buttons; mirror the notice.
            self._send(chat_id, result.get(
                "message") or "High-risk feature: confirm implementation.")
            return
        self._send(chat_id, result.get("message") or "Approved.")

    def _model_view_action(self, chat_id: int, callback_id,
                           feature_id: str, kind: str) -> None:
        """Render the full plan or the exact Cline prompt (never mutates)."""
        self._answer_callback(callback_id, "Showing view")
        try:
            feature = self.feature_orchestrator.get_feature(feature_id) or {}
        except Exception:
            logger.exception("Model view load failed feature=%s", feature_id[:8])
            feature = {}
        if not feature:
            self._send(
                chat_id,
                "\u26a0\ufe0f This action is no longer available for this feature.",
            )
            return
        if kind == "plan":
            plan = feature.get("plan") or {}
            if not plan:
                self._send(
                    chat_id,
                    "Plan unavailable - no plan has been prepared for this "
                    "feature yet.",
                )
                return
            from jarvis_core.feature_plan import format_plan_text

            self._send(
                chat_id,
                "\U0001f4cb FULL FEATURE PLAN\n\n" + format_plan_text(plan),
            )
            return
        prompt = (feature.get("cline_candidate_prompt")
                  or feature.get("chatgpt_prompt") or "")
        if not prompt:
            self._send(chat_id, "No candidate Cline prompt stored yet.")
            return
        self._send(
            chat_id,
            "\U0001f4cb EXACT CLINE PROMPT\n\n" + str(prompt)[:3500],
        )

    def _high_risk_action(self, user_id: int, chat_id: int, callback_id,
                          feature_id: str, confirm: bool) -> None:
        """Second, stronger confirmation gate for high-risk features."""
        if confirm:
            self._answer_callback(callback_id, "Confirmation received")
        else:
            self._answer_callback(callback_id, "Request received")
        try:
            if confirm:
                result = self.feature_orchestrator.confirm_high_risk(
                    feature_id, user_id)
            else:
                result = self.feature_orchestrator.cancel(
                    feature_id, telegram_user_id=user_id)
        except Exception:
            logger.exception(
                "High-risk action failed feature=%s confirm=%s",
                feature_id, confirm,
            )
            self._send(chat_id, "Could not complete that action.")
            return
        if result.get("status") == "error":
            self._send(chat_id, result.get(
                "message") or "Could not complete that action.")
            return
        self._send(chat_id, result.get(
            "message") or ("Confirmed." if confirm else "Cancelled."))

    def _arm_changes(self, user_id: int, chat_id: int, callback_id,
                     feature_id: str) -> None:
        """Arm the next text message as a plan revision request."""
        self._answer_callback(callback_id, "Describe changes")
        try:
            feature = self.feature_orchestrator.get_feature(feature_id) or {}
        except Exception:
            feature = {}
        if not feature:
            self._send(
                chat_id,
                "\u26a0\ufe0f This action is no longer available for this feature.",
            )
            return
        if feature.get("status") not in (
                "approval_pending", "planning", "chatgpt_review_pending"):
            self._send(
                chat_id,
                "That feature is not open to change requests right now.",
            )
            return
        self._feature_changes_pending[(user_id, chat_id)] = feature_id
        title = (feature.get("interpreted_objective")
                 or feature.get("original_request") or feature_id)
        self._send(
            chat_id,
            "\u270f\ufe0f Describe what should change.\n\n"
            f"It will be attached to:\n{title}\n\n"
            "Send it as your next message, or /cancel to abort.",
        )

    def _missing_action(self, chat_id: int, callback_id,
                        feature_id: str) -> None:
        self._answer_callback(callback_id, "Missing evidence")
        try:
            feature = self.feature_orchestrator.get_feature(feature_id) or {}
        except Exception:
            feature = {}
        if not feature:
            self._send(
                chat_id,
                "\u26a0\ufe0f This feature is no longer available.",
            )
            return
        facts = feature.get("missing_evidence") or []
        body = "\n".join(f"- {str(x)[:180]}" for x in facts[:10]) \
            or "- (none recorded)"
        self._send(chat_id, "\U0001f4cb MISSING EVIDENCE\n\n" + body)

    def _reresearch_action(self, user_id: int, chat_id: int, callback_id,
                           feature_id: str) -> None:
        self._answer_callback(callback_id, "Re-running research")
        try:
            result = self.feature_orchestrator.request_changes(
                feature_id, "re-run research and refresh evidence",
                telegram_user_id=user_id, kind="research_required",
            )
        except Exception:
            logger.exception("Re-research failed for %s", feature_id)
            self._send(chat_id, "Could not re-run research for that feature.")
            return
        self._send(chat_id, result.get(
            "message") or "Research re-run requested.")

    def _attach_review_text(self, user_id: int, chat_id: int,
                             feature_id: str, text: str) -> None:
        key = (user_id, chat_id)
        self._feature_review_pending.pop(key, None)
        try:
            result = self.feature_orchestrator.submit_chatgpt_review(
                feature_id, text)
        except Exception:
            logger.exception("ChatGPT review attach failed for %s", feature_id)
            self._send(chat_id, "Could not attach the ChatGPT review.")
            return
        if result.get("status") != "ok":
            self._send(chat_id, result.get(
                "message") or "Could not attach the ChatGPT review.")
            return
        import hashlib

        digest = hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:16]
        self._send(
            chat_id,
            "CHATGPT OUTPUT RECEIVED\n\n"
            "Source: Submitted by authorized user\n"
            f"Content hash: {digest}\n\n"
            "Running the repository evidence check and analysis...",
        )

    def _dispatch_text(self, user_id: int, chat_id: int, text: str,
                       *, message_id: Optional[int] = None) -> None:
        if not text:
            return
        head, *rest = text.split(maxsplit=1)
        command = head.split("@", 1)[0].lower() if head.startswith("/") else ""
        argument = rest[0].strip() if rest else ""

        # ── Part 5: workflow-command disambiguation (numeric reply) ──────────
        pending_cmd = self._workflow_command_pending.get((user_id, chat_id))
        if pending_cmd:
            self._workflow_command_pending.pop((user_id, chat_id), None)
            if command == "/cancel":
                self._send(chat_id, "Command cancelled. No workflow was changed.")
                return
            candidates = pending_cmd.get("candidates") or []
            choice = None
            if text.strip().isdigit():
                idx = int(text.strip())
                if 1 <= idx <= len(candidates):
                    choice = candidates[idx - 1]
            if choice is None:
                self._send(
                    chat_id,
                    "That wasn't a valid choice. Send your command again to "
                    "pick a workflow.",
                )
                return
            self._route_workflow_command_to(
                user_id, chat_id,
                pending_cmd.get("text", ""),
                pending_cmd.get("message_id"),
                choice.get("workflow_id"),
            )
            return

        # ── Part 6: edit-mode capture (Approval "Edit" button) ───────────────
        # If the approval service is awaiting an edited prompt from THIS user,
        # the next authorized text is the edited prompt for THAT workflow — it
        # is NOT routed through Part 5 as a new GPT requirement.
        try:
            from jarvis_core.workflow_approval_service import workflow_approval_service
            if workflow_approval_service.is_edit_pending(user_id, chat_id):
                if command == "/cancel":
                    self._send(chat_id, "Edit cancelled. The approval is still pending.")
                    # leave edit latch; user can tap Edit again or Proceed/Cancel
                    return
                workflow_approval_service.handle_edit_text(
                    user_id=user_id, chat_id=chat_id, text=text)
                return
        except Exception:
            logger.debug("approval edit-capture check failed", exc_info=True)

        # ── Plan-changes session ("Request Changes" button) ─────────────────
        changes_fid = self._feature_changes_pending.get((user_id, chat_id))
        if changes_fid:
            self._feature_changes_pending.pop((user_id, chat_id), None)
            if command == "/cancel":
                self._send(
                    chat_id,
                    "Change request cancelled. The plan is still waiting for "
                    "your decision.",
                )
                return
            orch = self.feature_orchestrator
            if orch is None:
                self._send(chat_id, "Feature orchestrator is unavailable.")
                return
            result = orch.request_changes(
                changes_fid, text, telegram_user_id=user_id)
            self._send(chat_id, result.get("message") or "Change request received.")
            return

        # ── Review-capture session (explicit "Submit / Replace Cline Prompt") ──
        capture_fid = self._feature_review_pending.get((user_id, chat_id))
        if capture_fid:
            if command == "/cancel":
                self._feature_review_pending.pop((user_id, chat_id), None)
                self._send(
                    chat_id,
                    "ChatGPT review is still waiting to be submitted. Use the "
                    "\"Submit / Replace Cline Prompt\" button again, "
                    "or keep typing your review.",
                )
            else:
                self._attach_review_text(user_id, chat_id, capture_fid, text)
            return

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
        lower = text.lower().strip()
        if lower.startswith("feature:"):
            self._submit_feature_request(
                user_id, chat_id, text[len("feature:"):].strip())
            return
        if lower.startswith("normal:"):
            self._submit_command(user_id, chat_id, text[len("normal:"):].strip())
            return
        # ── Part 5: Telegram command → SAME ChatGPT conversation ─────────────
        # If an Autonomous Workflow is waiting for a command, this free-text
        # message IS that command: hand it to the WorkflowCommandService, which
        # persists it, restores the SAME conversation and submits it once. This
        # takes precedence over the generic feature/normal request classifier
        # so a waiting workflow is not mistaken for a brand-new feature request.
        if self._route_workflow_command(user_id, chat_id, text, message_id):
            return
        feature_orch = self.feature_orchestrator
        if feature_orch is not None and feature_orch.is_feature_request(text):
            self._mode_pending[(user_id, chat_id)] = text
            self._send(
                chat_id,
                "How should I handle this request?\n\n"
                f"\"{text}\"",
                reply_markup={"inline_keyboard": [
                    [{"text": "\U0001f9e9 Feature Development",
                      "callback_data": "feature:mode:feature"}],
                    [{"text": "\u2699\ufe0f Normal Command",
                      "callback_data": "feature:mode:normal"}],
                    [{"text": "\u274c Cancel",
                      "callback_data": "feature:mode:cancel"}],
                ]},
            )
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

    def authorized_chat_ids(self) -> list[int]:
        """Resolve chat ids to notify the authorized user(s).

        Prefers chat ids observed from prior authorized messages; falls back to
        the configured allowlist user ids (in a private chat the chat id equals
        the user id). Used for unsolicited notifications (e.g. Part 4 →
        WAITING_FOR_COMMAND) via the EXISTING bot; no second poller.
        """
        with self._mapping_lock:
            known = list({int(c) for c in self._authorized_chats.values()})
        if known:
            return known
        try:
            return [int(u) for u in self.settings.allowed_user_ids]
        except Exception:
            return []

    def notify_authorized(self, text: str, *, reply_markup: Optional[dict] = None) -> Optional[int]:
        """Send an unsolicited message to the authorized chat; return message id.

        Uses the existing bot/poller. Returns the first sent message id (or None
        when no authorized chat is resolvable / send fails). Best-effort.
        """
        for chat_id in self.authorized_chat_ids():
            mid = self._send_returning_id(chat_id, text, reply_markup=reply_markup)
            if mid is not None:
                return mid
        return None

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

    def _edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: Optional[dict] = None,
    ) -> bool:
        """Edit an existing bot message in place; ``False`` when it must be
        re-sent instead (unknown/expired message or API rejection)."""
        try:
            self._require_api().edit_message_text(
                chat_id, message_id, text, reply_markup=reply_markup)
            return True
        except TelegramAPIError as exc:
            # "message is not modified" means the SAME message already shows
            # this exact text+keyboard: treat it as a successful in-place edit
            # so we never spam a brand-new message for a no-op change.
            if "not modified" in str(exc).lower():
                return True
            logger.warning("Telegram message edit fell back to send: %s", exc)
        except Exception:
            logger.exception("Telegram message edit failed unexpectedly")
        return False

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
