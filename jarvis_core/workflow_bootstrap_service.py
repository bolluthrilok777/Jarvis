"""WorkflowBootstrapService — Part 4 coordinator.

Owns the ``Start Workflow`` sequence that bootstraps a persistent ChatGPT
conversation for a workflow:

    IDLE → INITIALIZING → GPT_BOOTSTRAPPING → WAITING_FOR_COMMAND
                                                     (FAILED on error)

Coordinates the locked lower layers WITHOUT reimplementing them:
- ``WorkflowEngine`` (Part 1)          — persistence, transitions, idempotent actions.
- ``ChromeProfileController`` (Part 3) — exact-profile focus + ownership proof.
- ``ChatGPTBrowserController`` (Part 4)— the actual browser automation.
- ``workflow_event_bus`` (Part 2)      — realtime progress.

The long-running browser work runs on a single dedicated daemon worker thread
(mirroring FeatureOrchestrator). ``POST /start`` validates + schedules and
returns immediately; progress is streamed over the WebSocket. Only ONE bootstrap
may run per workflow at a time.

Idempotency: the two real side effects are guarded by Part 1 actions:
- ``chatgpt-bootstrap-open:<workflow_id>``  (open + new chat)
- ``chatgpt-master-prompt:<workflow_id>``   (submit the Master Requirement)
After a crash/restart the Master Requirement is NEVER blindly resent — the
completed action record prevents duplicate submission.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Optional

logger = logging.getLogger("jarvis.bootstrap")

# Realtime event types (published via the Part 2 bus).
EV_START_REQUESTED = "workflow_start_requested"
EV_PROFILE_RESOLVING = "chrome_profile_resolving"
EV_PROFILE_VERIFIED = "chrome_profile_verified"
EV_CHATGPT_OPENING = "chatgpt_opening"
EV_CHATGPT_READY = "chatgpt_ready"
EV_CHATGPT_NEW_CHAT = "chatgpt_new_chat"
EV_PROMPT_SUBMITTING = "chatgpt_master_prompt_submitting"
EV_PROMPT_SUBMITTED = "chatgpt_master_prompt_submitted"
EV_GENERATING = "chatgpt_generating"
EV_GENERATION_COMPLETE = "chatgpt_generation_complete"
EV_CONVERSATION_SAVED = "chatgpt_conversation_saved"
EV_BOOTSTRAP_COMPLETE = "chatgpt_bootstrap_complete"
EV_BOOTSTRAP_FAILED = "chatgpt_bootstrap_failed"
EV_AWAITING_COMMAND_NOTIFIED = "telegram_awaiting_command_notified"

ACTION_AWAITING_COMMAND = "telegram-awaiting-command"

# The exact readiness prompt sent to the authorized user once a workflow is
# durably WAITING_FOR_COMMAND (Part 5 command-entry stage).
AWAITING_COMMAND_TEXT = (
    "\U0001f9e0 GPT is ready.\nSend your next requirement to Jarvis."
)

ACTION_OPEN = "chatgpt-bootstrap-open"
ACTION_MASTER_PROMPT = "chatgpt-master-prompt"


class BootstrapError(Exception):
    """Raised for start-request validation problems (mapped to 4xx by routes)."""


class WorkflowBootstrapService:
    """Schedules and runs ChatGPT bootstrap for workflows."""

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
        # run_inline runs the bootstrap synchronously in the calling thread
        # (used by tests). Production uses the background worker.
        self._run_inline = run_inline

        self._queue: "queue.Queue[str]" = queue.Queue()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._start_lock = threading.RLock()
        self._active: set[str] = set()
        self._active_lock = threading.Lock()
        # Telegram notify hook (reuse the existing bot; no second poller).
        # (text) -> message_id|None. Wired by telegram_service.start().
        self._tg_notify: Optional[Callable] = None

    def set_telegram_channels(self, *, notify: Optional[Callable] = None) -> None:
        """Give the bootstrap service a Telegram notify hook for the
        WAITING_FOR_COMMAND readiness message (reuses the existing bot)."""
        if notify is not None:
            self._tg_notify = notify

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
        # Build one bound to the (possibly injected) profile controller.
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

    def _publish(self, workflow_id: str, event_type: str, payload: Optional[dict] = None) -> None:
        if self._publish_fn is not None:
            try:
                self._publish_fn(workflow_id, event_type, payload or {})
            except Exception:
                logger.debug("bootstrap publish failed", exc_info=True)
            return
        try:
            from jarvis_core.workflow_events_bus import workflow_event_bus
            workflow_event_bus.publish(
                workflow_id,
                {"type": event_type, "workflow_id": workflow_id, "bootstrap": payload or {}},
            )
        except Exception:
            logger.debug("bootstrap publish failed", exc_info=True)

    # ── Worker lifecycle ──────────────────────────────────────────────────────

    def start_worker(self) -> None:
        if self._run_inline:
            return
        with self._start_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._worker_loop, name="workflow-bootstrap", daemon=True
            )
            self._worker.start()
            logger.info("Workflow bootstrap worker started")

    def stop_worker(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=timeout)
        self._worker = None

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                workflow_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._run_bootstrap(workflow_id)
            except Exception:
                logger.exception("Bootstrap failed for %s", workflow_id)
            finally:
                with self._active_lock:
                    self._active.discard(workflow_id)

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, workflow_id: str) -> dict:
        """Validate the request and schedule the bootstrap.

        Returns the (validated) workflow snapshot immediately. Raises
        :class:`BootstrapError` for validation failures. Prevents duplicate
        concurrent bootstraps for the same workflow.
        """
        wf = self.engine.get_workflow(workflow_id)
        if wf is None:
            raise BootstrapError("Workflow not found")
        if not wf.selected_chrome_profile_key:
            raise BootstrapError("NO_CHROME_PROFILE_SELECTED")
        if not (wf.master_gpt_prompt or "").strip():
            raise BootstrapError("MASTER_REQUIREMENT_MISSING")
        # Only allow starting from IDLE (or re-starting a FAILED workflow).
        if wf.state not in ("IDLE", "FAILED"):
            raise BootstrapError(f"Workflow not startable from state {wf.state}")

        with self._active_lock:
            if workflow_id in self._active:
                raise BootstrapError("BOOTSTRAP_ALREADY_RUNNING")
            self._active.add(workflow_id)

        self._publish(workflow_id, EV_START_REQUESTED, {"state": wf.state})

        if self._run_inline:
            try:
                self._run_bootstrap(workflow_id)
            finally:
                with self._active_lock:
                    self._active.discard(workflow_id)
        else:
            self.start_worker()
            self._queue.put(workflow_id)
        return {"scheduled": True, "workflow_id": workflow_id}

    def is_active(self, workflow_id: str) -> bool:
        with self._active_lock:
            return workflow_id in self._active

    # ── The bootstrap sequence ────────────────────────────────────────────────

    def _run_bootstrap(self, workflow_id: str) -> None:
        engine = self.engine
        wf = engine.get_workflow(workflow_id)
        if wf is None:
            return
        profile_key = wf.selected_chrome_profile_key
        expected_email = wf.selected_chrome_account_email
        prompt = (wf.master_gpt_prompt or "").strip()
        try:
            # Drive to GPT_BOOTSTRAPPING, tolerant of recovery re-entry:
            #  - IDLE      → INITIALIZING → GPT_BOOTSTRAPPING (fresh start)
            #  - FAILED    → retry INITIALIZING → GPT_BOOTSTRAPPING (recover)
            #  - already GPT_BOOTSTRAPPING → resume in place (crash recovery),
            #    do NOT re-transition (that would be illegal).
            if wf.state == "IDLE":
                engine.transition(workflow_id, "INITIALIZING",
                                  message="Bootstrap: initializing")
                self._publish(workflow_id, EV_PROFILE_RESOLVING, {"profile_key": profile_key})
                engine.transition(workflow_id, "GPT_BOOTSTRAPPING",
                                  message="Bootstrap: opening ChatGPT")
            elif wf.state == "FAILED":
                engine.retry(workflow_id, "INITIALIZING")
                self._publish(workflow_id, EV_PROFILE_RESOLVING, {"profile_key": profile_key})
                engine.transition(workflow_id, "GPT_BOOTSTRAPPING",
                                  message="Bootstrap: retrying")
            elif wf.state == "GPT_BOOTSTRAPPING":
                # Resume an interrupted bootstrap; idempotent actions below
                # prevent duplicate side effects.
                self._publish(workflow_id, EV_PROFILE_RESOLVING, {"profile_key": profile_key})
            else:
                raise BootstrapError(f"Cannot bootstrap from state {wf.state}")

            chatgpt = self.chatgpt

            # Step 1 — open ChatGPT + new chat, guarded by an idempotent action.
            open_action = f"{ACTION_OPEN}:{workflow_id}"
            started, action = engine.begin_action(
                workflow_id, open_action, action_type="chatgpt_open",
            )
            hwnd = None
            tab_identity = None
            if started:
                self._publish(workflow_id, EV_CHATGPT_OPENING,
                              {"profile_key": profile_key, "expected_email": expected_email})
                # AUTHORITATIVE Part-7 browser ownership: launch a DEDICATED
                # Jarvis-owned Chrome window for the selected profile directory
                # and prove it via chrome://version Profile Path (+ dir→email),
                # then reuse that window's Jarvis tab for a fresh ChatGPT. Jarvis
                # never adopts an arbitrary existing window and never touches the
                # frontend. Falls back to the new-tab path only for controllers/
                # fakes that don't implement the dedicated-window open.
                window_binding = None
                dedicated = getattr(chatgpt, "open_chatgpt_in_dedicated_window", None)
                if callable(dedicated):
                    from jarvis_core.chrome_profile_controller import profile_dir_from_key
                    opened = dedicated(
                        profile_key, workflow_id=workflow_id,
                        selected_profile_directory=profile_dir_from_key(profile_key),
                        expected_email=expected_email)
                    window_binding = opened.get("window_binding")
                else:
                    opened = chatgpt.open_chatgpt_in_new_tab(
                        profile_key, workflow_id=workflow_id,
                        expected_email=expected_email)
                hwnd = opened.get("hwnd")
                tab_identity = opened.get("identity")
                if window_binding is not None:
                    engine.set_metadata(workflow_id, "jarvis_browser_window", window_binding)
                self._publish(workflow_id, EV_PROFILE_VERIFIED,
                              {"profile_key": profile_key, "hwnd": hwnd})
                # Confirm the new tab is a fresh ChatGPT (ready, not login).
                chatgpt.ensure_chatgpt_ready(hwnd)
                self._publish(workflow_id, EV_CHATGPT_READY, {"hwnd": hwnd})
                self._publish(workflow_id, EV_CHATGPT_NEW_CHAT, {"hwnd": hwnd})
                engine.complete_action(workflow_id, open_action,
                                       result_ref=f"hwnd={hwnd}")
            else:
                # Already opened in a previous (crashed) run — re-verify the
                # exact profile (directory + email) before any further action.
                hwnd = chatgpt.ensure_selected_profile(profile_key, expected_email=expected_email)

            # Step 2 — submit the Master Requirement, guarded by idempotency.
            prompt_action = f"{ACTION_MASTER_PROMPT}:{workflow_id}"
            prompt_started, prompt_rec = engine.begin_action(
                workflow_id, prompt_action, action_type="chatgpt_master_prompt",
            )
            if prompt_started:
                self._publish(workflow_id, EV_PROMPT_SUBMITTING, {})
                chatgpt.submit_prompt(hwnd, prompt)
                self._publish(workflow_id, EV_PROMPT_SUBMITTED, {})
                # Positive generation detection (start then completion).
                self._publish(workflow_id, EV_GENERATING, {})
                chatgpt.detect_generation_started(hwnd)
                chatgpt.wait_for_generation_complete(hwnd)
                self._publish(workflow_id, EV_GENERATION_COMPLETE, {})
                engine.complete_action(workflow_id, prompt_action,
                                       result_ref="submitted+generated")
            else:
                # CRITICAL: do NOT resend. The prompt was already submitted in a
                # prior run; only proceed to capture/verify the conversation.
                logger.info("Master prompt already submitted for %s; not resending",
                            workflow_id)

            # Step 3 — capture + persist the conversation URL.
            url = chatgpt.capture_conversation_url(hwnd)
            wf = engine.update_workflow_context(
                workflow_id,
                chatgpt_conversation_url=url,
                chatgpt_window_hwnd=int(hwnd) if hwnd else None,
            )
            self._publish(workflow_id, EV_CONVERSATION_SAVED, {"url": url, "hwnd": hwnd})

            # Establish EXACTLY ONE Jarvis-owned ChatGPT tab and persist its
            # REAL, workflow-scoped tab identity (Fix B). The identity comes from
            # the existing tab-control layer: CDP ``target_id`` when Chrome
            # exposes a remote-debugging endpoint, otherwise UIA
            # (host_hwnd + runtime_id + index). The durable cross-restart anchor
            # is always the saved conversation URL. If >1 tab already sits on
            # THIS workflow's conversation, the canonical one is kept and only
            # the proven Jarvis duplicates (same conversation URL) are closed —
            # the user's unrelated ChatGPT tabs are never touched.
            # FAIL-CLOSED (B1): establish the owned tab and obtain a REAL tab
            # identity BEFORE reaching WAITING_FOR_COMMAND. If no controllable
            # identity can be obtained, TabIdentityUnavailableError propagates to
            # the outer handler and the workflow FAILS — Jarvis never downgrades
            # to window/URL-only ownership. (A controller/fake that does not
            # implement establish_owned_tab is tolerated for unit tests only.)
            tab_result = {}
            establish = getattr(chatgpt, "establish_owned_tab", None)
            if callable(establish):
                tab_result = establish(
                    hwnd, workflow_id=workflow_id, conversation_url=url) or {}
            try:
                import time as _time
                now_iso = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
                # Prefer the conversation-bound identity from establish; fall back
                # to the identity of the tab Jarvis just CREATED (open step) so we
                # never lose the real owned-tab identity.
                identity = tab_result.get("identity") or tab_identity
                binding = {
                    "owner": "jarvis",
                    "workflow_id": workflow_id,
                    "profile_key": profile_key,
                    "expected_email": expected_email,
                    "window_hwnd": int(hwnd) if hwnd else None,
                    "conversation_url": url,
                    "tab_identity": identity,          # real tab identity or None
                    "created_at": now_iso,
                    "verified_at": now_iso,
                }
                engine.set_metadata(workflow_id, "chatgpt_tab_binding", binding)
                # Persist a compact durable tab-id string for quick display /
                # status (CDP target_id or uia:<host>:<runtime>), or None when
                # the tab layer was not controllable at bootstrap time.
                tab_id = self._compact_tab_id(identity)
                engine.update_workflow_context(workflow_id, chatgpt_tab_id=tab_id)
                if tab_result.get("closed"):
                    self._publish(workflow_id, EV_CONVERSATION_SAVED,
                                  {"url": url, "hwnd": hwnd,
                                   "duplicate_tabs_closed": tab_result["closed"]})
            except Exception:
                logger.debug("Persisting chatgpt_tab_binding failed", exc_info=True)

            # GPT_BOOTSTRAPPING → WAITING_FOR_COMMAND
            engine.transition(workflow_id, "WAITING_FOR_COMMAND",
                              message="ChatGPT bootstrap complete")
            self._publish(workflow_id, EV_BOOTSTRAP_COMPLETE,
                          {"url": url, "state": "WAITING_FOR_COMMAND"})
            logger.info("Bootstrap complete for %s → WAITING_FOR_COMMAND", workflow_id)

            # Part 5 command-entry: notify the authorized Telegram user, ONCE,
            # AFTER the state is durably WAITING_FOR_COMMAND. Idempotent + never
            # fails the bootstrap.
            self.notify_awaiting_command(workflow_id)
        except Exception as exc:
            code = type(exc).__name__
            msg = str(exc)
            logger.warning("Bootstrap error for %s: %s (%s)", workflow_id, msg, code)
            try:
                engine.fail(workflow_id, error=msg, error_code=code)
            except Exception:
                logger.debug("fail() transition failed", exc_info=True)
            self._publish(workflow_id, EV_BOOTSTRAP_FAILED, {"error": msg, "code": code})

    # ── Part 5 readiness notification (WAITING_FOR_COMMAND) ───────────────────

    def notify_awaiting_command(self, workflow_id: str) -> bool:
        """Send the 'GPT is ready — send your next requirement' Telegram message
        ONCE for a WAITING_FOR_COMMAND workflow. Idempotent + recovery-safe.

        Guards:
          - only when the workflow is durably WAITING_FOR_COMMAND;
          - an idempotent Part 1 action ``telegram-awaiting-command:<wf>:<iter>``
            prevents duplicate sends across event/reconnect/restart;
          - persists ``telegram_waiting_notification_sent/message_id/sent_at``.
        Returns True when a notification was sent by THIS call. Never raises.
        """
        engine = self.engine
        try:
            wf = engine.get_workflow(workflow_id)
            if wf is None or wf.state != "WAITING_FOR_COMMAND":
                return False
            iteration = wf.iteration_number or 0
            # Already sent? (durable metadata) → do not resend.
            meta = wf.metadata or {}
            if (meta.get("telegram_waiting_notification") or {}).get("sent"):
                return False
            # Idempotent action guard (survives restart/duplicate events). The
            # DURABLE source of truth is the telegram_waiting_notification.sent
            # flag checked above; the action prevents concurrent double-send. If
            # a prior attempt FAILED (sent flag still false), we are allowed to
            # retry: begin a fresh attempt action id so the retry is not blocked
            # by the earlier FAILED record.
            action_id = f"{ACTION_AWAITING_COMMAND}:{workflow_id}:{iteration}"
            started, rec = engine.begin_action(
                workflow_id, action_id, action_type="telegram_awaiting_command")
            if not started:
                # Existing record. If it COMPLETED, a notification was sent →
                # do not resend. If it FAILED (or is stale RUNNING) and the
                # durable sent flag is false, retry under a unique attempt id.
                if getattr(rec, "status", None) == "COMPLETED":
                    return False
                import uuid as _uuid
                action_id = f"{ACTION_AWAITING_COMMAND}:{workflow_id}:{iteration}:{_uuid.uuid4().hex[:8]}"
                started, rec = engine.begin_action(
                    workflow_id, action_id, action_type="telegram_awaiting_command")
                if not started:
                    return False
            message_id = None
            ok = False
            try:
                if self._tg_notify is not None:
                    message_id = self._tg_notify(AWAITING_COMMAND_TEXT)
                    ok = message_id is not None
                else:
                    # No channel wired (e.g. inline tests) — record attempt but
                    # mark not sent so recovery can retry once a channel exists.
                    ok = False
            except Exception:
                logger.debug("awaiting-command notify send failed", exc_info=True)
                ok = False
            from jarvis_core.timezone_utils import jarvis_now
            record = {
                "sent": bool(ok),
                "message_id": message_id,
                "sent_at": jarvis_now().isoformat() if ok else None,
                "iteration": iteration,
            }
            engine.set_metadata(workflow_id, "telegram_waiting_notification", record)
            if ok:
                engine.complete_action(workflow_id, action_id,
                                       result_ref=f"msg={message_id}")
                self._publish(workflow_id, EV_AWAITING_COMMAND_NOTIFIED,
                              {"message_id": message_id})
            else:
                # Failed/unavailable: mark the action failed so a later recovery
                # attempt can re-begin and retry the send.
                try:
                    engine.fail_action(workflow_id, action_id,
                                       error="notify unavailable/failed")
                except Exception:
                    logger.debug("fail_action awaiting notify failed", exc_info=True)
            return bool(ok)
        except Exception:
            logger.debug("notify_awaiting_command error", exc_info=True)
            return False

    def recover_awaiting_command_notifications(self) -> list[str]:
        """Restart recovery: for every workflow durably WAITING_FOR_COMMAND whose
        readiness notification was never sent, send it once. Returns ids sent."""
        sent: list[str] = []
        try:
            for wf in self.engine.list_active_workflows():
                if wf.state != "WAITING_FOR_COMMAND":
                    continue
                if (wf.metadata or {}).get("telegram_waiting_notification", {}).get("sent"):
                    continue
                if self.notify_awaiting_command(wf.workflow_id):
                    sent.append(wf.workflow_id)
        except Exception:
            logger.debug("recover awaiting notifications failed", exc_info=True)
        return sent

    @staticmethod
    def _compact_tab_id(identity) -> Optional[str]:
        """A compact durable tab-id string for the persisted chatgpt_tab_id
        column: the CDP target_id, or ``uia:<host_hwnd>:<runtime_id>`` for UIA,
        or None when no real tab identity was obtained."""
        if not identity:
            return None
        mode = str(identity.get("control_mode") or "").lower()
        if mode == "cdp" and identity.get("target_id"):
            return str(identity["target_id"])
        if mode == "uia" and identity.get("host_hwnd") is not None:
            return f"uia:{identity.get('host_hwnd')}:{identity.get('runtime_id') or ''}"
        return None

    # ── Open an existing conversation (no new chat) ──────────────────────────

    def open_conversation(self, workflow_id: str) -> dict:
        """Focus the selected profile and open the SAVED conversation URL."""
        wf = self.engine.get_workflow(workflow_id)
        if wf is None:
            raise BootstrapError("Workflow not found")
        if not wf.selected_chrome_profile_key:
            raise BootstrapError("NO_CHROME_PROFILE_SELECTED")
        if not wf.chatgpt_conversation_url:
            raise BootstrapError("NO_CONVERSATION_SAVED")
        result = self.chatgpt.restore_conversation(
            wf.selected_chrome_profile_key, wf.chatgpt_conversation_url,
            expected_email=wf.selected_chrome_account_email,
        )
        return result

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self, workflow_id: str) -> dict:
        wf = self.engine.get_workflow(workflow_id)
        if wf is None:
            raise BootstrapError("Workflow not found")
        # Report the most recent bootstrap action outcomes.
        open_status = None
        prompt_status = None
        last_error = None
        for act in self.engine.list_actions(workflow_id):
            if act.action_type == "chatgpt_open":
                open_status = act.status
            elif act.action_type == "chatgpt_master_prompt":
                prompt_status = act.status
            if act.status == "FAILED" and act.error:
                last_error = act.error
        status_label = "not_initialized"
        if wf.state == "GPT_BOOTSTRAPPING":
            status_label = "bootstrapping"
        elif wf.chatgpt_conversation_url:
            status_label = "saved"
        elif wf.state == "FAILED":
            status_label = "failed"
        return {
            "state": wf.state,
            "status": status_label,
            "selected_chrome_profile_key": wf.selected_chrome_profile_key,
            "selected_account_email": wf.selected_chrome_account_email,
            "conversation_url": wf.chatgpt_conversation_url,
            "window_hwnd": wf.chatgpt_window_hwnd,
            "active": self.is_active(workflow_id),
            "open_action": open_status,
            "prompt_action": prompt_status,
            "last_error": last_error or wf.last_error,
        }


# Module-level singleton, following the project convention.
workflow_bootstrap_service = WorkflowBootstrapService()
