"""WorkflowCommandService — Part 5 coordinator.

Owns the "Telegram command → SAME ChatGPT conversation" sequence:

    (Telegram) command received
      → resolve the active WAITING_FOR_COMMAND workflow (disambiguate)
      → persist the EXACT command durably
      → WAITING_FOR_COMMAND → GPT_RUNNING (only after durable persist)
      → focus + verify the EXACT selected Chrome profile (Part 3)
      → restore the SAME saved ChatGPT conversation URL + verify same
      → focus the composer, paste the EXACT command, submit ONCE
      → detect the follow-up generation START (accessibility, NOT URL)
      → wait until it ACTUALLY finishes (Stop gone + Copy/Regenerate, stable)
      → GPT_RUNNING → GPT_OUTPUT_READY
      → persist latest_gpt_response metadata for Part 6
                                              (FAILED on any error)

What Part 5 deliberately does NOT do (later Parts):
- It NEVER clicks Copy / extracts the response text.
- It NEVER forwards the GPT response to Telegram.
- It NEVER asks "Proceed?" and NEVER launches Kiro.

Coordinates the locked lower layers WITHOUT reimplementing them:
- ``WorkflowEngine`` (Part 1)          — persistence, transitions, idempotent actions.
- ``ChromeProfileController`` (Part 3) — exact-profile focus + ownership proof
                                         (reached through the ChatGPT controller).
- ``ChatGPTBrowserController`` (Part 4/5) — restore/verify + follow-up submit +
                                         targeted follow-up generation probes.
- ``workflow_event_bus`` (Part 2)      — realtime progress.

The blocking browser work runs on a single dedicated daemon worker thread
(mirroring ``WorkflowBootstrapService``). Submitting a command validates +
schedules and returns immediately; progress streams over the WebSocket and a
single, in-place-edited Telegram status message.

Idempotency & recovery: the one real side effect (pasting + submitting the
command) is guarded by a Part 1 action keyed by the ORIGINATING Telegram
message id:

    chatgpt-user-command:<workflow_id>:<telegram_message_id>

so a crash/restart mid-flight NEVER resends the same command. A follow-up whose
submit action is already RUNNING/COMPLETED resumes to detection/completion
instead of re-pasting.

Concurrency rule: while a workflow is GPT_RUNNING a second command for the same
workflow is REJECTED (the safest choice; no silent queue/merge).
"""
from __future__ import annotations

import hashlib
import logging
import queue
import threading
from typing import Callable, Optional

from jarvis_core.timezone_utils import jarvis_now

logger = logging.getLogger("jarvis.command")

# Realtime event types (published via the Part 2 bus).
EV_COMMAND_RECEIVED = "telegram_command_received"
EV_COMMAND_PERSISTED = "telegram_command_persisted"
EV_CONVERSATION_RESTORING = "chatgpt_conversation_restoring"
EV_CONVERSATION_VERIFIED = "chatgpt_conversation_verified"
EV_COMMAND_SUBMITTING = "chatgpt_command_submitting"
EV_COMMAND_SUBMITTED = "chatgpt_command_submitted"
EV_GENERATION_STARTED = "chatgpt_generation_started"
EV_GENERATION_COMPLETE = "chatgpt_generation_complete"
EV_OUTPUT_READY = "gpt_output_ready"
EV_COMMAND_FAILED = "chatgpt_command_failed"

# Idempotent action id prefix for the single follow-up submission.
ACTION_USER_COMMAND = "chatgpt-user-command"


def _now_iso() -> str:
    return jarvis_now().isoformat()


class CommandError(Exception):
    """Raised for command-request validation problems (mapped to user replies)."""


class NoActiveWorkflowError(CommandError):
    """No workflow is waiting for a command."""


class AmbiguousWorkflowError(CommandError):
    """More than one workflow is waiting; the user must disambiguate.

    Carries the list of candidate workflow ids/labels so the caller can present
    a disambiguation prompt.
    """

    def __init__(self, candidates: list[dict]):
        self.candidates = candidates
        super().__init__("AMBIGUOUS_ACTIVE_WORKFLOW")


class CommandBusyError(CommandError):
    """The target workflow is already running a command (GPT_RUNNING)."""


class WorkflowCommandService:
    """Schedules and runs Telegram → same-ChatGPT-conversation commands."""

    def __init__(
        self,
        *,
        engine=None,
        chatgpt_controller=None,
        profile_controller=None,
        publish_fn: Optional[Callable] = None,
        run_inline: bool = False,
    ) -> None:
        self._engine_ref = engine
        self._chatgpt_ref = chatgpt_controller
        self._profile_ref = profile_controller
        self._publish_fn = publish_fn
        # run_inline runs the command synchronously in the calling thread
        # (used by tests). Production uses the background worker.
        self._run_inline = run_inline

        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._start_lock = threading.RLock()
        self._active: set[str] = set()
        self._active_lock = threading.Lock()

        # Telegram status channel (single in-place-edited message per command).
        self._tg_send_status: Optional[Callable] = None   # (chat_id, text) -> message_id|None
        self._tg_edit_status: Optional[Callable] = None    # (chat_id, msg_id, text) -> bool
        self._tg_is_authorized: Optional[Callable] = None   # (user_id) -> bool

    # ── Dependencies (lazy singletons by default) ────────────────────────────

    @property
    def engine(self):
        if self._engine_ref is not None:
            return self._engine_ref
        from jarvis_core.workflow_engine import workflow_engine
        return workflow_engine

    @property
    def chatgpt(self):
        if self._chatgpt_ref is not None:
            return self._chatgpt_ref
        from jarvis_core.chatgpt_browser_controller import ChatGPTBrowserController
        self._chatgpt_ref = ChatGPTBrowserController(
            profile_controller=self._profile_ref
        )
        return self._chatgpt_ref

    @property
    def profile_controller(self):
        if self._profile_ref is not None:
            return self._profile_ref
        from jarvis_core.chrome_profile_controller import chrome_profile_controller
        return chrome_profile_controller

    # ── Telegram channel wiring (mirrors feature orchestrator pattern) ───────

    def set_telegram_channels(
        self,
        *,
        send_status: Optional[Callable] = None,
        edit_status: Optional[Callable] = None,
        is_authorized: Optional[Callable] = None,
    ) -> None:
        """Give the service safe Telegram send/edit/auth hooks.

        ``send_status(chat_id, text) -> message_id|None`` sends the initial
        status line and returns its message id. ``edit_status(chat_id, msg_id,
        text) -> bool`` edits that SAME message in place (so we never flood the
        chat). ``is_authorized(user_id) -> bool`` reuses the existing bot auth.
        """
        if send_status is not None:
            self._tg_send_status = send_status
        if edit_status is not None:
            self._tg_edit_status = edit_status
        if is_authorized is not None:
            self._tg_is_authorized = is_authorized

    # ── Realtime publish ─────────────────────────────────────────────────────

    def _publish(self, workflow_id: str, event_type: str, payload: Optional[dict] = None) -> None:
        if self._publish_fn is not None:
            try:
                self._publish_fn(workflow_id, event_type, payload or {})
            except Exception:
                logger.debug("command publish failed", exc_info=True)
            return
        try:
            from jarvis_core.workflow_events_bus import workflow_event_bus
            workflow_event_bus.publish(
                workflow_id,
                {"type": event_type, "workflow_id": workflow_id, "command": payload or {}},
            )
        except Exception:
            logger.debug("command publish failed", exc_info=True)

    # ── Worker lifecycle ──────────────────────────────────────────────────────

    def start_worker(self) -> None:
        if self._run_inline:
            return
        with self._start_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._worker_loop, name="workflow-command", daemon=True
            )
            self._worker.start()
            logger.info("Workflow command worker started")

    def stop_worker(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=timeout)
        self._worker = None

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            workflow_id = job.get("workflow_id")
            try:
                self._run_command(job)
            except Exception:
                logger.exception("Command run failed for %s", workflow_id)
            finally:
                if workflow_id:
                    with self._active_lock:
                        self._active.discard(workflow_id)

    # ── Workflow resolution + disambiguation ─────────────────────────────────

    def list_waiting_workflows(self) -> list:
        """Workflows currently in WAITING_FOR_COMMAND (ready for a command)."""
        return [
            wf for wf in self.engine.list_active_workflows()
            if wf.state == "WAITING_FOR_COMMAND"
        ]

    def _candidate_label(self, wf) -> dict:
        return {
            "workflow_id": wf.workflow_id,
            "project_name": wf.project_name or "",
            "conversation_url": wf.chatgpt_conversation_url or "",
        }

    def resolve_target_workflow(self, *, workflow_id: Optional[str] = None):
        """Resolve which workflow a command targets.

        - explicit ``workflow_id`` → that workflow (must be WAITING_FOR_COMMAND);
        - exactly one waiting workflow → use it;
        - none waiting → :class:`NoActiveWorkflowError`;
        - many waiting → :class:`AmbiguousWorkflowError` (with candidates).
        """
        if workflow_id:
            wf = self.engine.get_workflow(workflow_id)
            if wf is None:
                raise NoActiveWorkflowError("Workflow not found")
            if wf.state == "GPT_RUNNING":
                raise CommandBusyError("GPT_ALREADY_RUNNING")
            if wf.state != "WAITING_FOR_COMMAND":
                raise CommandError(
                    f"Workflow is not waiting for a command (state={wf.state})"
                )
            return wf
        waiting = self.list_waiting_workflows()
        if not waiting:
            raise NoActiveWorkflowError("NO_ACTIVE_WORKFLOW")
        if len(waiting) > 1:
            raise AmbiguousWorkflowError(
                [self._candidate_label(wf) for wf in waiting]
            )
        return waiting[0]

    # ── Public API: submit a command ──────────────────────────────────────────

    def submit_command(
        self,
        *,
        text: str,
        telegram_message_id,
        telegram_chat_id=None,
        telegram_user_id=None,
        workflow_id: Optional[str] = None,
    ) -> dict:
        """Validate + persist + schedule a Telegram command.

        Resolves the target workflow (disambiguation as above), persists the
        EXACT command durably, transitions WAITING_FOR_COMMAND → GPT_RUNNING
        (only after the persist commit), then schedules the browser work.

        Returns a snapshot dict immediately. Raises the ``*Error`` types above
        for validation problems so the caller can reply appropriately.
        """
        if not text or not text.strip():
            raise CommandError("EMPTY_COMMAND")
        command = text.strip()

        wf = self.resolve_target_workflow(workflow_id=workflow_id)
        workflow_id = wf.workflow_id

        # Reject a second command while one is already running (single-flight).
        with self._active_lock:
            if workflow_id in self._active:
                raise CommandBusyError("GPT_ALREADY_RUNNING")

        if not wf.selected_chrome_profile_key:
            raise CommandError("NO_CHROME_PROFILE_SELECTED")
        if not wf.chatgpt_conversation_url:
            raise CommandError("NO_CONVERSATION_SAVED")

        received_at = _now_iso()
        command_hash = hashlib.sha256(command.encode("utf-8")).hexdigest()
        message_id = telegram_message_id

        # 1) Persist the EXACT command + provenance DURABLY *before* moving state.
        self.engine.update_workflow_context(
            workflow_id,
            current_user_command=command,
        )
        self.engine.set_metadata(workflow_id, "current_command", {
            "text": command,
            "telegram_message_id": message_id,
            "telegram_chat_id": telegram_chat_id,
            "telegram_user_id": telegram_user_id,
            "received_at": received_at,
            "command_hash": command_hash,
        })
        self._publish(workflow_id, EV_COMMAND_RECEIVED, {
            "text": command, "telegram_message_id": message_id,
        })

        # 2) WAITING_FOR_COMMAND → GPT_RUNNING (only after durable persist).
        self.engine.transition(
            workflow_id, "GPT_RUNNING",
            message="Telegram command received; sending to ChatGPT",
        )
        self._publish(workflow_id, EV_COMMAND_PERSISTED, {
            "text": command, "command_hash": command_hash,
        })

        # 3) Schedule the browser work.
        with self._active_lock:
            self._active.add(workflow_id)
        job = {
            "workflow_id": workflow_id,
            "command": command,
            "telegram_message_id": message_id,
            "telegram_chat_id": telegram_chat_id,
            "telegram_user_id": telegram_user_id,
            "command_hash": command_hash,
        }
        if self._run_inline:
            try:
                self._run_command(job)
            finally:
                with self._active_lock:
                    self._active.discard(workflow_id)
        else:
            self.start_worker()
            self._queue.put(job)
        return {"scheduled": True, "workflow_id": workflow_id}

    def is_active(self, workflow_id: str) -> bool:
        with self._active_lock:
            return workflow_id in self._active

    # ── Telegram status helpers (single, in-place-edited message) ────────────

    def _status_open(self, chat_id, text: str):
        if chat_id is None or self._tg_send_status is None:
            return None
        try:
            return self._tg_send_status(chat_id, text)
        except Exception:
            logger.debug("status open failed", exc_info=True)
            return None

    def _status_update(self, chat_id, message_id, text: str) -> None:
        if chat_id is None:
            return
        if message_id is not None and self._tg_edit_status is not None:
            try:
                if self._tg_edit_status(chat_id, message_id, text):
                    return
            except Exception:
                logger.debug("status edit failed", exc_info=True)
        # Fall back to a fresh send only if editing is impossible.
        if self._tg_send_status is not None:
            try:
                self._tg_send_status(chat_id, text)
            except Exception:
                logger.debug("status fallback send failed", exc_info=True)

    # ── The command sequence ──────────────────────────────────────────────────

    def _run_command(self, job: dict) -> None:
        engine = self.engine
        workflow_id = job["workflow_id"]
        command = job["command"]
        message_id = job.get("telegram_message_id")
        chat_id = job.get("telegram_chat_id")

        wf = engine.get_workflow(workflow_id)
        if wf is None:
            return
        profile_key = wf.selected_chrome_profile_key
        conversation_url = wf.chatgpt_conversation_url
        expected_email = wf.selected_chrome_account_email
        tab_binding = (wf.metadata or {}).get("chatgpt_tab_binding") or {}
        tab_identity = tab_binding.get("tab_identity")
        window_binding = (wf.metadata or {}).get("jarvis_browser_window") or {}

        # Single, updating Telegram status message. UX per requirement:
        #   "🟢 Jarvis Ready"  →  "🧠 Sending requirement to GPT..."
        status_msg_id = self._status_open(chat_id, "\U0001f7e2 Jarvis Ready")

        try:
            chatgpt = self.chatgpt

            # Step 1 — restore the SAME conversation using the PERSISTED
            # Jarvis-owned window binding (launch-provenance). Live binding →
            # reuse the exact HWND/tab; dead binding → hardened token recovery.
            # NEVER legacy profile discovery (ensure_selected_profile/
            # open_or_focus/verify_profile_open/launch_profile) on the workflow
            # path — that legacy verifier cannot verify on this Chrome setup.
            self._publish(workflow_id, EV_CONVERSATION_RESTORING, {
                "profile_key": profile_key, "url": conversation_url,
            })
            restore = self._restore_workflow_binding(
                chatgpt, workflow_id=workflow_id, conversation_url=conversation_url,
                window_binding=window_binding, tab_identity=tab_identity,
                profile_key=profile_key, expected_email=expected_email)
            hwnd = restore.get("hwnd")
            if not restore.get("same"):
                raise CommandError(
                    "CONVERSATION_MISMATCH: restored a different conversation "
                    f"(requested {conversation_url!r}, observed "
                    f"{restore.get('observed_url')!r})"
                )
            self._persist_tab_identity(workflow_id, restore)
            self._publish(workflow_id, EV_CONVERSATION_VERIFIED, {
                "hwnd": hwnd, "url": restore.get("observed_url"), "same": True,
            })

            self._status_update(chat_id, status_msg_id,
                                 "\U0001f9e0 Sending requirement to GPT...")

            # Step 2 — submit the EXACT command ONCE (idempotent by message id).
            action_id = f"{ACTION_USER_COMMAND}:{workflow_id}:{message_id}"
            started, action = engine.begin_action(
                workflow_id, action_id, action_type="chatgpt_user_command",
                metadata={"command_hash": job.get("command_hash")},
            )
            if started:
                self._publish(workflow_id, EV_COMMAND_SUBMITTING, {})
                chatgpt.submit_command(hwnd, command)
                self._publish(workflow_id, EV_COMMAND_SUBMITTED, {})
                engine.complete_action(workflow_id, action_id,
                                       result_ref="submitted")
            else:
                # Recovery: the command was already submitted in a prior run.
                # Do NOT resend; fall through to detection/completion so we can
                # still observe the reply and reach GPT_OUTPUT_READY.
                logger.info(
                    "Command already submitted for %s (msg %s); not resending",
                    workflow_id, message_id,
                )

            # Step 3 — positive follow-up generation START (accessibility only).
            # The conversation URL does NOT change for a follow-up, so we look
            # for the Stop/streaming control. A very fast reply may finish
            # before we observe Stop; a start-timeout is therefore NOT fatal —
            # we proceed to the completion wait which confirms via Copy/Regen.
            started_gen = chatgpt.wait_for_followup_generation_start(hwnd)
            self._publish(workflow_id, EV_GENERATION_STARTED, {
                "observed_stop": bool(started_gen),
            })

            # Step 4 — wait until it ACTUALLY finishes (Stop gone + Copy/Regen,
            # stable across observations). Raises GenerationTimeoutError → FAILED.
            completion = chatgpt.wait_for_followup_complete(hwnd)
            self._publish(workflow_id, EV_GENERATION_COMPLETE, completion)

            # Step 5 — persist "output ready" metadata for Part 6, then
            # GPT_RUNNING → GPT_OUTPUT_READY. NOTE: Part 5 does NOT read/copy the
            # response text — only records that a completed response is ready.
            ready_at = _now_iso()
            engine.set_metadata(workflow_id, "latest_gpt_response", {
                "ready_at": ready_at,
                "iteration": (wf.iteration_number or 0) + 1,
                "copy_available": bool(completion.get("had_controls")),
                "command_hash": job.get("command_hash"),
                "telegram_message_id": message_id,
            })
            engine.update_workflow_context(
                workflow_id,
                iteration_number=(wf.iteration_number or 0) + 1,
            )
            engine.transition(
                workflow_id, "GPT_OUTPUT_READY",
                message="ChatGPT response completed; output ready",
            )
            self._publish(workflow_id, EV_OUTPUT_READY, {
                "ready_at": ready_at, "state": "GPT_OUTPUT_READY",
            })

            # Final Telegram status (no output text, no Proceed button).
            self._status_update(
                chat_id, status_msg_id,
                "\U0001f9e0 GPT response completed. Preparing output...",
            )
            logger.info("Command complete for %s → GPT_OUTPUT_READY", workflow_id)

            # ── Part 5 → Part 6 HANDOFF ──────────────────────────────────────
            # Automatically hand the completed GPT_OUTPUT_READY workflow to the
            # Part 6 approval service, which captures the latest response, sends
            # the exact output to Telegram and requests Proceed/Edit/Cancel. This
            # is idempotent (Part 6 will not recopy if output is already
            # persisted) and best-effort (never fails the Part 5 completion).
            self._trigger_output_capture(workflow_id)
        except Exception as exc:
            code = type(exc).__name__
            msg = str(exc)
            logger.warning("Command error for %s: %s (%s)", workflow_id, msg, code)
            try:
                engine.fail(workflow_id, error=msg, error_code=code)
            except Exception:
                logger.debug("fail() transition failed", exc_info=True)
            self._publish(workflow_id, EV_COMMAND_FAILED, {"error": msg, "code": code})
            self._status_update(
                chat_id, status_msg_id,
                f"\u26a0\ufe0f Command failed: {msg}",
            )

    # ── Owned-tab-aware restore ──────────────────────────────────────────────

    @staticmethod
    def _restore_workflow_binding(chatgpt, *, workflow_id, conversation_url,
                                  window_binding, tab_identity, profile_key,
                                  expected_email):
        """Restore via the PERSISTED Jarvis-owned window binding (launch
        provenance). Never uses legacy profile discovery on the workflow path.

        Uses ``restore_workflow_owned_conversation`` when the controller
        supports it (the migrated authority). Only if a controller/fake lacks it
        do we fall back to the older owned/plain restore (kept for non-workflow
        callers and legacy test doubles)."""
        fn = getattr(chatgpt, "restore_workflow_owned_conversation", None)
        if callable(fn):
            return fn(workflow_id=workflow_id, conversation_url=conversation_url,
                      window_binding=window_binding or {}, tab_identity=tab_identity,
                      selected_profile_key=profile_key,
                      selected_account_email=expected_email)
        # Legacy fallbacks (non-workflow controllers / older fakes only).
        legacy = getattr(chatgpt, "restore_owned_conversation", None)
        if callable(legacy):
            return legacy(profile_key, conversation_url, workflow_id=workflow_id,
                          identity=tab_identity, expected_email=expected_email)
        return chatgpt.restore_conversation(
            profile_key, conversation_url, expected_email=expected_email)

    def _persist_tab_identity(self, workflow_id: str, restore: dict) -> None:
        """Persist the (possibly refreshed) owned-tab identity + window binding
        after a restore that reused/recovered the workflow tab. Never raises."""
        if not isinstance(restore, dict) or "identity" not in restore:
            return
        try:
            wf = self.engine.get_workflow(workflow_id)
            binding = dict((wf.metadata or {}).get("chatgpt_tab_binding") or {})
            binding["tab_identity"] = restore.get("identity")
            binding["verified_at"] = _now_iso()
            if restore.get("hwnd"):
                binding["window_hwnd"] = int(restore["hwnd"])
            self.engine.set_metadata(workflow_id, "chatgpt_tab_binding", binding)
            # On recovery the dedicated window was recreated → persist the fresh
            # Jarvis-owned window binding so subsequent stages reuse it.
            wb = restore.get("window_binding")
            if restore.get("recovered") and isinstance(wb, dict):
                self.engine.set_metadata(workflow_id, "jarvis_browser_window", wb)
        except Exception:
            logger.debug("persist tab identity failed", exc_info=True)

    # ── Part 5 → Part 6 handoff ─────────────────────────────────────────────

    def _trigger_output_capture(self, workflow_id: str) -> None:
        """Enqueue the Part 6 output capture for a GPT_OUTPUT_READY workflow.

        Best-effort + idempotent: the approval service refuses to run when the
        workflow is not GPT_OUTPUT_READY, when a capture is already active for
        it, or (recovery) when the output was already persisted — so duplicate
        events / reconnects / restarts never cause a second copy. Injectable via
        ``capture_trigger_fn`` for tests; defaults to the real singleton.
        """
        try:
            trigger = getattr(self, "_capture_trigger_fn", None)
            if trigger is None:
                from jarvis_core.workflow_approval_service import (
                    workflow_approval_service,
                )
                trigger = workflow_approval_service.capture_and_request_approval
            trigger(workflow_id)
            logger.info("Part 6 capture enqueued for %s", workflow_id)
        except Exception:
            # Never fail the Part 5 completion because of the handoff; the
            # capture can also be resumed by restart recovery.
            logger.debug("Part 6 capture enqueue failed (will recover)", exc_info=True)

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self, workflow_id: str) -> dict:
        wf = self.engine.get_workflow(workflow_id)
        if wf is None:
            raise CommandError("Workflow not found")
        command_meta = (wf.metadata or {}).get("current_command") or {}
        response_meta = (wf.metadata or {}).get("latest_gpt_response") or {}
        return {
            "state": wf.state,
            "current_user_command": wf.current_user_command,
            "command_meta": command_meta,
            "latest_gpt_response": response_meta,
            "conversation_url": wf.chatgpt_conversation_url,
            "window_hwnd": wf.chatgpt_window_hwnd,
            "iteration_number": wf.iteration_number,
            "active": self.is_active(workflow_id),
        }


# Module-level singleton, following the project convention.
workflow_command_service = WorkflowCommandService()
