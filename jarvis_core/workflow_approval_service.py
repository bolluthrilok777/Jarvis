"""WorkflowApprovalService — Part 6 coordinator.

Owns the "GPT copy capture + Telegram approval" sequence that begins once a
workflow is at ``GPT_OUTPUT_READY`` (left there by Part 5):

    GPT_OUTPUT_READY
      → restore + verify the SAME selected Chrome profile & saved conversation
      → locate the LATEST assistant response and its Copy control (proven newest)
      → click Copy exactly once (idempotent action chatgpt-copy-output:<wf>:<it>)
      → capture the EXACT clipboard text via a sentinel (positive proof)
      → persist exact output + hash + length + captured_at (never rewritten)
      → GPT_OUTPUT_READY → WAITING_FOR_APPROVAL
      → send the EXACT output to the EXISTING Telegram bot (chunk if long)
      → present Proceed / Edit / Cancel inline buttons
      → handle the approval callback safely (idempotent, stale-rejecting)

What Part 6 deliberately does NOT do (Part 7 owns Kiro):
- It NEVER opens Kiro, pastes into Kiro, auto-Allows, monitors Kiro, or sends
  a Kiro result to GPT.
- Proceed only persists ``approved_gpt_prompt`` + approval metadata and leaves
  the workflow safely at WAITING_FOR_APPROVAL for Part 7 to consume.

Coordinates the locked lower layers WITHOUT reimplementing them:
- ``WorkflowEngine`` (Part 1)          — persistence, transitions, idempotent actions.
- ``ChatGPTBrowserController`` (Part 4/5/6) — restore/verify + latest-response
                                         Copy + sentinel clipboard capture.
- ``workflow_event_bus`` (Part 2)      — realtime progress.

The blocking browser/UIA work runs on a single dedicated daemon worker thread
(mirroring the Part 4/5 services) so Telegram polling is never blocked.

Idempotency & recovery: the one real side effect (clicking Copy) is guarded by
a Part 1 action keyed by the workflow iteration:

    chatgpt-copy-output:<workflow_id>:<iteration>

If the output was already durably persisted, we DO NOT re-copy — we resume
Telegram delivery / approval. Approval callbacks are idempotent and reject
stale buttons (mismatched workflow/iteration/output hash).
"""
from __future__ import annotations

import hashlib
import logging
import queue
import threading
from typing import Callable, Optional

from jarvis_core.timezone_utils import jarvis_now

logger = logging.getLogger("jarvis.approval")

# Realtime event types (published via the Part 2 bus).
EV_CAPTURE_STARTED = "gpt_output_capture_started"
EV_LATEST_LOCATED = "gpt_latest_response_located"
EV_COPY_CONTROL_LOCATED = "gpt_copy_control_located"
EV_COPY_CLICKED = "gpt_copy_clicked"
EV_CLIPBOARD_CAPTURED = "gpt_clipboard_captured"
EV_OUTPUT_PERSISTED = "gpt_output_persisted"
EV_TELEGRAM_OUTPUT_SENT = "telegram_gpt_output_sent"
EV_APPROVAL_REQUESTED = "telegram_approval_requested"
EV_APPROVAL_PROCEED = "approval_proceed"
EV_APPROVAL_EDIT_REQUESTED = "approval_edit_requested"
EV_APPROVAL_EDIT_RECEIVED = "approval_edit_received"
EV_APPROVAL_CANCELLED = "approval_cancelled"
EV_APPROVAL_FAILED = "approval_failed"

ACTION_COPY_OUTPUT = "chatgpt-copy-output"

# Telegram text-message safe limit (chunk longer output without changing it).
TELEGRAM_CHUNK_LIMIT = 3500


def _now_iso() -> str:
    return jarvis_now().isoformat()


def _short_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


class ApprovalError(Exception):
    """Base error for the approval service (mapped to user replies)."""


class CaptureError(ApprovalError):
    """Copy capture could not be completed safely."""


class WorkflowApprovalService:
    """Captures the GPT output and runs the Telegram approval for a workflow."""

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
        self._run_inline = run_inline

        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._start_lock = threading.RLock()
        self._active: set[str] = set()
        self._active_lock = threading.Lock()

        # Telegram channels (reused existing bot; NEVER a second poller).
        self._tg_send: Optional[Callable] = None          # (chat_id, text, markup=None) -> message_id|None
        self._tg_edit: Optional[Callable] = None          # (chat_id, msg_id, text, markup=None) -> bool
        self._tg_is_authorized: Optional[Callable] = None  # (user_id) -> bool
        # In-memory edit-mode registry: (user_id, chat_id) -> {workflow_id, iteration}
        self._edit_pending: dict = {}
        self._edit_lock = threading.Lock()

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
        self._chatgpt_ref = ChatGPTBrowserController(profile_controller=self._profile_ref)
        return self._chatgpt_ref

    # ── Telegram wiring ───────────────────────────────────────────────────────

    def set_telegram_channels(
        self,
        *,
        send: Optional[Callable] = None,
        edit: Optional[Callable] = None,
        is_authorized: Optional[Callable] = None,
    ) -> None:
        """Give the service safe Telegram send/edit/auth hooks (reuses the bot)."""
        if send is not None:
            self._tg_send = send
        if edit is not None:
            self._tg_edit = edit
        if is_authorized is not None:
            self._tg_is_authorized = is_authorized

    # ── Realtime publish ─────────────────────────────────────────────────────

    def _publish(self, workflow_id: str, event_type: str, payload: Optional[dict] = None) -> None:
        if self._publish_fn is not None:
            try:
                self._publish_fn(workflow_id, event_type, payload or {})
            except Exception:
                logger.debug("approval publish failed", exc_info=True)
            return
        try:
            from jarvis_core.workflow_events_bus import workflow_event_bus
            workflow_event_bus.publish(
                workflow_id,
                {"type": event_type, "workflow_id": workflow_id, "approval": payload or {}},
            )
        except Exception:
            logger.debug("approval publish failed", exc_info=True)

    # ── Worker lifecycle ──────────────────────────────────────────────────────

    def start_worker(self) -> None:
        if self._run_inline:
            return
        with self._start_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._worker_loop, name="workflow-approval", daemon=True
            )
            self._worker.start()
            logger.info("Workflow approval worker started")

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
            wid = job.get("workflow_id")
            try:
                self._run_capture(wid)
            except Exception:
                logger.exception("Capture run failed for %s", wid)
            finally:
                if wid:
                    with self._active_lock:
                        self._active.discard(wid)

    # ── Public API: schedule capture ──────────────────────────────────────────

    def capture_and_request_approval(self, workflow_id: str) -> dict:
        """Validate + schedule the capture/approval flow for a workflow.

        Only valid from GPT_OUTPUT_READY. Returns immediately (worker path) or
        runs inline (tests). Raises :class:`ApprovalError` for validation.
        """
        wf = self.engine.get_workflow(workflow_id)
        if wf is None:
            raise ApprovalError("Workflow not found")
        if wf.state != "GPT_OUTPUT_READY":
            raise ApprovalError(f"CAPTURE_ONLY_FROM_GPT_OUTPUT_READY (state={wf.state})")
        if not wf.selected_chrome_profile_key:
            raise ApprovalError("NO_CHROME_PROFILE_SELECTED")
        if not wf.chatgpt_conversation_url:
            raise ApprovalError("NO_CONVERSATION_SAVED")

        with self._active_lock:
            if workflow_id in self._active:
                raise ApprovalError("CAPTURE_ALREADY_RUNNING")
            self._active.add(workflow_id)

        if self._run_inline:
            try:
                self._run_capture(workflow_id)
            finally:
                with self._active_lock:
                    self._active.discard(workflow_id)
        else:
            self.start_worker()
            self._queue.put({"workflow_id": workflow_id})
        return {"scheduled": True, "workflow_id": workflow_id}

    def is_active(self, workflow_id: str) -> bool:
        with self._active_lock:
            return workflow_id in self._active

    def recover_pending_captures(self) -> list[str]:
        """Restart recovery: enqueue capture for persisted GPT_OUTPUT_READY
        workflows that have not yet been captured/delivered.

        Called at startup AFTER the engine restores active workflows. Safe +
        idempotent: skips workflows whose output is already persisted (recovery
        C handles the rest inside the capture body) and workflows already active.
        Returns the list of workflow ids it scheduled.
        """
        scheduled: list[str] = []
        try:
            active = self.engine.list_active_workflows()
        except Exception:
            logger.debug("recover_pending_captures: list failed", exc_info=True)
            return scheduled
        for wf in active:
            if wf.state != "GPT_OUTPUT_READY":
                continue
            # Already captured (output persisted) → nothing to do; the workflow
            # is simply awaiting the (already-sent) approval, or Part 6 will
            # resume delivery on demand. We still (re)enqueue only when output
            # is NOT yet persisted, to avoid a needless recopy attempt.
            if (wf.latest_gpt_output or "") and (wf.metadata or {}).get("latest_gpt_output_hash"):
                continue
            try:
                self.capture_and_request_approval(wf.workflow_id)
                scheduled.append(wf.workflow_id)
                logger.info("Recovery enqueued Part 6 capture for %s", wf.workflow_id)
            except ApprovalError as exc:
                logger.info("Recovery skip %s: %s", wf.workflow_id, exc)
            except Exception:
                logger.debug("recover capture failed for %s", wf.workflow_id, exc_info=True)
        return scheduled

    # ── Owned-tab-aware restore ──────────────────────────────────────────────

    @staticmethod
    def _restore_owned(chatgpt, profile_key, conversation_url, *, workflow_id,
                       identity, expected_email):
        """Legacy owned/plain restore — kept only for non-workflow controllers /
        older test doubles. The workflow path uses
        :meth:`_restore_workflow_binding`."""
        fn = getattr(chatgpt, "restore_owned_conversation", None)
        if callable(fn):
            return fn(profile_key, conversation_url, workflow_id=workflow_id,
                      identity=identity, expected_email=expected_email)
        return chatgpt.restore_conversation(
            profile_key, conversation_url, expected_email=expected_email)

    @staticmethod
    def _restore_workflow_binding(chatgpt, *, workflow_id, conversation_url,
                                  window_binding, tab_identity, profile_key,
                                  expected_email):
        """Restore via the PERSISTED Jarvis-owned window binding (launch
        provenance). Never legacy profile discovery on the workflow path."""
        fn = getattr(chatgpt, "restore_workflow_owned_conversation", None)
        if callable(fn):
            return fn(workflow_id=workflow_id, conversation_url=conversation_url,
                      window_binding=window_binding or {}, tab_identity=tab_identity,
                      selected_profile_key=profile_key,
                      selected_account_email=expected_email)
        legacy = getattr(chatgpt, "restore_owned_conversation", None)
        if callable(legacy):
            return legacy(profile_key, conversation_url, workflow_id=workflow_id,
                          identity=tab_identity, expected_email=expected_email)
        return chatgpt.restore_conversation(
            profile_key, conversation_url, expected_email=expected_email)

    def _persist_tab_identity(self, workflow_id: str, restore: dict) -> None:
        """Persist the (possibly refreshed) owned-tab identity + window binding.
        Never raises."""
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
            wb = restore.get("window_binding")
            if restore.get("recovered") and isinstance(wb, dict):
                self.engine.set_metadata(workflow_id, "jarvis_browser_window", wb)
        except Exception:
            logger.debug("persist tab identity failed", exc_info=True)

    # ── The capture sequence ──────────────────────────────────────────────────

    def _run_capture(self, workflow_id: str) -> None:
        engine = self.engine
        wf = engine.get_workflow(workflow_id)
        if wf is None:
            return
        profile_key = wf.selected_chrome_profile_key
        conversation_url = wf.chatgpt_conversation_url
        expected_email = wf.selected_chrome_account_email
        iteration = wf.iteration_number or 0

        try:
            self._publish(workflow_id, EV_CAPTURE_STARTED, {"iteration": iteration})

            # RECOVERY C: output already durably persisted → do NOT recopy.
            if (wf.latest_gpt_output or "") and (wf.metadata or {}).get("latest_gpt_output_hash"):
                logger.info("Output already persisted for %s; skipping recopy", workflow_id)
                text = wf.latest_gpt_output
            else:
                chatgpt = self.chatgpt
                # Step 1 — restore the SAME conversation using the PERSISTED
                # Jarvis-owned window binding (launch provenance): live binding →
                # reuse exact HWND/tab; dead binding → hardened token recovery.
                # NEVER legacy profile discovery on the workflow path.
                tab_binding = (wf.metadata or {}).get("chatgpt_tab_binding") or {}
                tab_identity = tab_binding.get("tab_identity")
                window_binding = (wf.metadata or {}).get("jarvis_browser_window") or {}
                restore = self._restore_workflow_binding(
                    chatgpt, workflow_id=workflow_id, conversation_url=conversation_url,
                    window_binding=window_binding, tab_identity=tab_identity,
                    profile_key=profile_key, expected_email=expected_email)
                hwnd = restore.get("hwnd")
                if not restore.get("same"):
                    raise CaptureError(
                        "CONVERSATION_MISMATCH: restored a different conversation "
                        f"(requested {conversation_url!r}, observed {restore.get('observed_url')!r})"
                    )
                self._persist_tab_identity(workflow_id, restore)

                # Step 2 — locate the LATEST assistant response + its Copy control.
                latest = chatgpt.find_latest_assistant_response(hwnd)
                assistant_index = int(latest.get("index", 0))
                self._publish(workflow_id, EV_LATEST_LOCATED, {
                    "index": latest.get("index"), "count": latest.get("count")})
                self._publish(workflow_id, EV_COPY_CONTROL_LOCATED, {
                    "index": latest.get("index")})

                # Step 3 — Copy exactly once, keyed by a REAL GPT RESPONSE
                # IDENTITY (conversation id + assistant message index +
                # normalized content hash). This distinguishes R1 from a
                # regenerated R2 in the SAME iteration, and recognises the SAME
                # response observed repeatedly. We try to derive the identity
                # from a cheap pre-copy content read; if that exact response was
                # already copied, we NEVER click Copy again (dup trigger / WS
                # reconnect / recovery / restart). Otherwise we copy once and
                # FINALISE the identity from the actual captured content.
                pre_text = None
                try:
                    pre_text = chatgpt.read_latest_response_text(hwnd)
                except Exception:
                    pre_text = None
                copied_set = set((engine.get_workflow(workflow_id).metadata or {})
                                 .get("copied_response_identities") or [])
                if pre_text is not None:
                    pre_identity = chatgpt.compute_response_identity(
                        conversation_url, assistant_index=assistant_index,
                        content=pre_text)
                    if pre_identity in copied_set:
                        existing = engine.get_workflow(workflow_id).latest_gpt_output or ""
                        logger.info("Response %s already copied for %s; NOT "
                                    "clicking Copy again", pre_identity, workflow_id)
                        return self._deliver_and_wait(workflow_id, existing, iteration)

                copy_action = f"{ACTION_COPY_OUTPUT}:{workflow_id}:{iteration}"
                started, action = engine.begin_action(
                    workflow_id, copy_action, action_type="chatgpt_copy_output")
                if not started:
                    # RECOVERY B: copy action already ran in a prior attempt. If
                    # output is persisted, use it; otherwise re-attempt safely.
                    if (engine.get_workflow(workflow_id).latest_gpt_output or ""):
                        logger.info("Copy action done + output present for %s", workflow_id)
                        text = engine.get_workflow(workflow_id).latest_gpt_output
                        return self._deliver_and_wait(workflow_id, text, iteration)
                    # else fall through and capture (do not double-count click)

                # Step 4 — sentinel-proven clipboard capture (positive proof).
                # This is the ONE Copy-button click for this response identity.
                cap = chatgpt.capture_clipboard_via_sentinel(hwnd)
                self._publish(workflow_id, EV_COPY_CLICKED, {"method": cap.get("method")})
                self._publish(workflow_id, EV_CLIPBOARD_CAPTURED, {"length": cap.get("length")})
                text = cap["text"]
                # FINAL response identity from the ACTUAL captured content.
                response_identity = chatgpt.compute_response_identity(
                    conversation_url, assistant_index=assistant_index, content=text)
                try:
                    engine.complete_action(workflow_id, copy_action,
                                           result_ref=f"len={cap['length']}")
                except Exception:
                    logger.debug("complete_action copy failed", exc_info=True)

                # Step 5 — persist EXACT output + metadata + the copied
                # response-identity (so this exact response is never re-copied).
                self._persist_output(workflow_id, text, iteration, cap["sha256"],
                                     cap["length"], response_identity=response_identity)

            # Step 6 — GPT_OUTPUT_READY → WAITING_FOR_APPROVAL (idempotent-ish).
            wf = engine.get_workflow(workflow_id)
            if wf.state == "GPT_OUTPUT_READY":
                engine.transition(workflow_id, "WAITING_FOR_APPROVAL",
                                  message="GPT output captured; awaiting approval")

            # Step 7/8 — deliver exact output to Telegram + approval buttons.
            self._deliver_and_wait(workflow_id, text, iteration)
            logger.info("Capture + approval request complete for %s", workflow_id)
        except Exception as exc:
            code = type(exc).__name__
            msg = str(exc)
            logger.warning("Capture error for %s: %s (%s)", workflow_id, msg, code)
            # A copy/capture failure does NOT fail the workflow: GPT_OUTPUT_READY
            # only transitions to WAITING_FOR_APPROVAL, so the workflow simply
            # stays at GPT_OUTPUT_READY and remains safely RETRYABLE (Retry
            # Capture). We record the error and publish, but never force an
            # illegal transition to FAILED. If the workflow had already advanced
            # to WAITING_FOR_APPROVAL (delivery-only failure), it likewise stays
            # there for the approval to be retried/resent.
            try:
                self.engine.set_metadata(workflow_id, "last_capture_error",
                                         {"error": msg, "code": code, "at": _now_iso()})
            except Exception:
                logger.debug("set last_capture_error failed", exc_info=True)
            self._publish(workflow_id, EV_APPROVAL_FAILED, {"error": msg, "code": code})

    def _persist_output(self, workflow_id: str, text: str, iteration: int,
                        sha256: str, length: int,
                        *, response_identity: Optional[str] = None) -> None:
        engine = self.engine
        engine.update_workflow_context(workflow_id, latest_gpt_output=text)
        captured_at = _now_iso()
        engine.set_metadata(workflow_id, "latest_gpt_output_hash", sha256)
        engine.set_metadata(workflow_id, "latest_gpt_output_len", length)
        engine.set_metadata(workflow_id, "latest_gpt_output_captured_at", captured_at)
        engine.set_metadata(workflow_id, "latest_gpt_output_iteration", iteration)
        # Real GPT response-identity copied latch (browser-level Copy-once).
        # A DURABLE SET of copied response identities recognises the same
        # response observed again (no second Copy) while allowing a genuinely
        # new/regenerated response (new identity) exactly one Copy.
        if response_identity is not None:
            engine.set_metadata(workflow_id, "latest_gpt_response_identity",
                                response_identity)
            wf = engine.get_workflow(workflow_id)
            copied = list((wf.metadata or {}).get("copied_response_identities") or [])
            if response_identity not in copied:
                copied.append(response_identity)
            engine.set_metadata(workflow_id, "copied_response_identities", copied)
        engine.set_metadata(workflow_id, "latest_gpt_output_copied_at", captured_at)
        self._publish(workflow_id, EV_OUTPUT_PERSISTED, {
            "length": length, "sha256": sha256, "captured_at": captured_at,
            "iteration": iteration})

    # ── Telegram delivery + approval buttons ──────────────────────────────────

    @staticmethod
    def _chunk(text: str, limit: int = TELEGRAM_CHUNK_LIMIT) -> list[str]:
        """Split EXACT text into ordered chunks without changing content."""
        if not text:
            return [""]
        return [text[i:i + limit] for i in range(0, len(text), limit)]

    def _approval_token(self, workflow_id: str, iteration: int, output_hash: str) -> str:
        """Compact approval token binding wf+iteration+output hash.

        Telegram callback_data is limited to 64 bytes, so we use a short digest
        of the full identity and keep the durable mapping in workflow metadata.
        """
        raw = f"{workflow_id}:{iteration}:{output_hash}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _deliver_and_wait(self, workflow_id: str, text: str, iteration: int) -> None:
        wf = self.engine.get_workflow(workflow_id)
        meta = wf.metadata or {}
        chat_id = (meta.get("current_command") or {}).get("telegram_chat_id")
        output_hash = meta.get("latest_gpt_output_hash") or _short_hash(text)
        token = self._approval_token(workflow_id, iteration, output_hash)

        # FIX C — response-identity delivery latch. Delivery is keyed by the
        # deterministic token (workflow_id + iteration + output_hash). If we
        # already delivered THIS exact response, do not resend the GPT output to
        # Telegram (guards against Part5→6 handoff + restart recovery + WS
        # reconnect all trying to deliver the same captured response). The
        # existing approval buttons/token remain valid.
        existing_appr = meta.get("approval") or {}
        if (existing_appr.get("token") == token
                and existing_appr.get("delivered") is True):
            logger.info("Response %s already delivered for %s (iter %s); "
                        "not resending", token, workflow_id, iteration)
            return

        # Persist the approval token → durable identity mapping (stale-button guard).
        self.engine.set_metadata(workflow_id, "approval", {
            "token": token,
            "iteration": iteration,
            "output_hash": output_hash,
            "status": "pending",
            "requested_at": _now_iso(),
            "chat_id": chat_id,
        })

        if chat_id is None or self._tg_send is None:
            # No Telegram channel (e.g. inline tests) — approval can still be
            # driven via handle_callback with the token.
            self._publish(workflow_id, EV_APPROVAL_REQUESTED, {"token": token,
                          "iteration": iteration, "delivered": False})
            return

        # Send the EXACT output (chunked if long) — never summarised.
        chunks = self._chunk(text)
        n = len(chunks)
        for idx, chunk in enumerate(chunks, start=1):
            header = "\U0001f9e0 GPT RESPONSE" if n == 1 else f"\U0001f9e0 GPT RESPONSE \u2014 {idx}/{n}"
            try:
                self._tg_send(chat_id, f"{header}\n\n{chunk}")
            except Exception:
                logger.debug("telegram output chunk send failed", exc_info=True)
        self._publish(workflow_id, EV_TELEGRAM_OUTPUT_SENT, {"chunks": n, "length": len(text)})

        # Approval buttons (separate message). callback_data carries the token.
        markup = {"inline_keyboard": [[
            {"text": "\u2705 Proceed", "callback_data": f"appr:proceed:{token}"},
            {"text": "\u270f\ufe0f Edit", "callback_data": f"appr:edit:{token}"},
            {"text": "\u274c Cancel", "callback_data": f"appr:cancel:{token}"},
        ]]}
        msg_id = None
        try:
            msg_id = self._tg_send(chat_id, "What should Jarvis do?", markup)
        except Exception:
            logger.debug("telegram approval buttons send failed", exc_info=True)
        # Record the approval message id + mark this exact response as delivered
        # (response-identity latch, see guard at the top of this method).
        appr = dict((self.engine.get_workflow(workflow_id).metadata or {}).get("approval") or {})
        appr["message_id"] = msg_id
        appr["delivered"] = True
        self.engine.set_metadata(workflow_id, "approval", appr)
        self._publish(workflow_id, EV_APPROVAL_REQUESTED, {"token": token,
                      "iteration": iteration, "delivered": True, "message_id": msg_id})

    # ── Approval callback handling (delegated from TelegramService) ───────────

    def _find_by_token(self, token: str):
        """Find the workflow whose approval token matches (durable).

        Searches ALL workflows (including terminal CANCELLED/COMPLETED) so that
        duplicate/stale callbacks on an already-resolved workflow are answered
        idempotently rather than mistaken for an unknown/stale token.
        """
        try:
            candidates = self.engine.list_workflows()
        except Exception:
            candidates = self.engine.list_active_workflows()
        for wf in candidates:
            appr = (wf.metadata or {}).get("approval") or {}
            if appr.get("token") == token:
                return wf
        return None

    def handle_callback(self, *, user_id, chat_id, action: str, token: str,
                        callback_id=None, message_id=None) -> dict:
        """Handle a Proceed/Edit/Cancel approval callback.

        Authorization, idempotency and stale-button rejection are enforced here.
        Returns a dict describing the outcome for the caller to answer/notify.
        """
        # Authorization (reuse existing allowlist).
        if self._tg_is_authorized is not None and not self._tg_is_authorized(user_id):
            return {"ok": False, "reason": "unauthorized",
                    "message": "You are not authorized to approve this."}

        wf = self._find_by_token(token)
        if wf is None:
            return {"ok": False, "reason": "stale",
                    "message": "This approval is no longer valid (stale button)."}
        workflow_id = wf.workflow_id
        appr = (wf.metadata or {}).get("approval") or {}

        # Stale/duplicate guards by durable status.
        status = appr.get("status")
        if action == "proceed":
            return self._do_proceed(workflow_id, appr, chat_id, message_id)
        if action == "edit":
            return self._do_edit_request(workflow_id, appr, user_id, chat_id, message_id)
        if action == "cancel":
            return self._do_cancel(workflow_id, appr, chat_id, message_id)
        return {"ok": False, "reason": "unknown_action", "message": "Unknown action."}

    def _do_proceed(self, workflow_id, appr, chat_id, message_id) -> dict:
        engine = self.engine
        wf = engine.get_workflow(workflow_id)
        # Idempotent: already approved → do nothing (no duplicate approval).
        if appr.get("status") == "approved" or wf.approved_gpt_prompt:
            return {"ok": True, "reason": "already_approved", "workflow_id": workflow_id,
                    "message": "Already approved. Ready for Kiro."}
        if appr.get("status") == "cancelled" or wf.state == "CANCELLED":
            return {"ok": False, "reason": "cancelled",
                    "message": "This workflow was cancelled."}
        if wf.state != "WAITING_FOR_APPROVAL":
            return {"ok": False, "reason": "wrong_state",
                    "message": f"Not awaiting approval (state={wf.state})."}
        # Verify iteration + output hash still match the current output.
        cur_hash = (wf.metadata or {}).get("latest_gpt_output_hash")
        if appr.get("output_hash") and cur_hash and appr["output_hash"] != cur_hash:
            return {"ok": False, "reason": "stale",
                    "message": "A newer response exists; this button is stale."}
        # Persist approval. Part 6 STOPS here — NO Kiro.
        exact_output = wf.latest_gpt_output or ""
        engine.update_workflow_context(workflow_id, approved_gpt_prompt=exact_output)
        new_appr = dict(appr)
        new_appr.update({"status": "approved", "approved_at": _now_iso(),
                         "approved_prompt_hash": _short_hash(exact_output),
                         "message_id": message_id or appr.get("message_id")})
        engine.set_metadata(workflow_id, "approval", new_appr)
        self._publish(workflow_id, EV_APPROVAL_PROCEED, {"status": "approved"})
        if chat_id is not None and self._tg_send is not None:
            try:
                self._tg_send(chat_id, "\u2705 Approved. Ready for Kiro.", None)
            except Exception:
                pass
        return {"ok": True, "reason": "approved", "workflow_id": workflow_id,
                "message": "Approved. Ready for Kiro."}

    def _do_edit_request(self, workflow_id, appr, user_id, chat_id, message_id) -> dict:
        engine = self.engine
        wf = engine.get_workflow(workflow_id)
        if wf.state != "WAITING_FOR_APPROVAL":
            return {"ok": False, "reason": "wrong_state",
                    "message": f"Not awaiting approval (state={wf.state})."}
        # Enter edit mode: the NEXT authorized text from this user is the edited
        # prompt for THIS workflow — NOT routed to Part 5 as a new GPT command.
        with self._edit_lock:
            self._edit_pending[(user_id, chat_id)] = {
                "workflow_id": workflow_id, "iteration": appr.get("iteration"),
                "token": appr.get("token")}
        new_appr = dict(appr)
        new_appr["status"] = "edit_requested"
        engine.set_metadata(workflow_id, "approval", new_appr)
        self._publish(workflow_id, EV_APPROVAL_EDIT_REQUESTED, {})
        if chat_id is not None and self._tg_send is not None:
            try:
                self._tg_send(chat_id, "\u270f\ufe0f Send the edited prompt now.", None)
            except Exception:
                pass
        return {"ok": True, "reason": "edit_mode", "workflow_id": workflow_id,
                "message": "Edit mode: send the edited prompt now."}

    def is_edit_pending(self, user_id, chat_id) -> bool:
        with self._edit_lock:
            return (user_id, chat_id) in self._edit_pending

    def handle_edit_text(self, *, user_id, chat_id, text: str) -> dict:
        """Consume the NEXT authorized text as the edited prompt (not via Part 5).

        Persists ``edited_gpt_prompt`` + status ``edited_pending_confirmation``
        and sends a preview with Proceed-with-Edit / Edit-Again / Cancel.
        Returns a dict; ``handled=False`` when no edit is pending for this user.
        """
        with self._edit_lock:
            pending = self._edit_pending.get((user_id, chat_id))
        if not pending:
            return {"handled": False}
        if self._tg_is_authorized is not None and not self._tg_is_authorized(user_id):
            return {"handled": True, "ok": False, "message": "Unauthorized."}
        workflow_id = pending["workflow_id"]
        engine = self.engine
        wf = engine.get_workflow(workflow_id)
        if wf is None or wf.state != "WAITING_FOR_APPROVAL":
            with self._edit_lock:
                self._edit_pending.pop((user_id, chat_id), None)
            return {"handled": True, "ok": False, "message": "Workflow no longer awaiting approval."}
        edited = text  # EXACT edited text; never routed to GPT in Part 6.
        engine.set_metadata(workflow_id, "edited_gpt_prompt", edited)
        appr = dict((wf.metadata or {}).get("approval") or {})
        appr["status"] = "edited_pending_confirmation"
        appr["edited_hash"] = _short_hash(edited)
        engine.set_metadata(workflow_id, "approval", appr)
        self._publish(workflow_id, EV_APPROVAL_EDIT_RECEIVED, {"length": len(edited)})
        # Clear the edit-capture latch (a confirm button drives the rest).
        with self._edit_lock:
            self._edit_pending.pop((user_id, chat_id), None)
        token = appr.get("token")
        if chat_id is not None and self._tg_send is not None:
            try:
                for chunk in self._chunk(edited):
                    self._tg_send(chat_id, f"\u270f\ufe0f EDITED PROMPT\n\n{chunk}")
                markup = {"inline_keyboard": [[
                    {"text": "\u2705 Proceed with Edit", "callback_data": f"appr:eproceed:{token}"},
                    {"text": "\u21a9\ufe0f Edit Again", "callback_data": f"appr:edit:{token}"},
                    {"text": "\u274c Cancel", "callback_data": f"appr:cancel:{token}"},
                ]]}
                self._tg_send(chat_id, "Confirm the edited prompt?", markup)
            except Exception:
                logger.debug("edit preview send failed", exc_info=True)
        return {"handled": True, "ok": True, "workflow_id": workflow_id,
                "message": "Edited prompt received; confirm to approve."}

    def handle_edit_proceed(self, *, user_id, chat_id, token: str,
                            message_id=None) -> dict:
        """Confirm an edited prompt → approved_gpt_prompt = exact edited text."""
        if self._tg_is_authorized is not None and not self._tg_is_authorized(user_id):
            return {"ok": False, "reason": "unauthorized", "message": "Unauthorized."}
        wf = self._find_by_token(token)
        if wf is None:
            return {"ok": False, "reason": "stale", "message": "Stale approval."}
        workflow_id = wf.workflow_id
        appr = (wf.metadata or {}).get("approval") or {}
        engine = self.engine
        if appr.get("status") == "approved" or wf.approved_gpt_prompt:
            return {"ok": True, "reason": "already_approved",
                    "message": "Already approved. Ready for Kiro."}
        if wf.state != "WAITING_FOR_APPROVAL":
            return {"ok": False, "reason": "wrong_state",
                    "message": f"Not awaiting approval (state={wf.state})."}
        edited = (wf.metadata or {}).get("edited_gpt_prompt")
        if not edited:
            return {"ok": False, "reason": "no_edit", "message": "No edited prompt to confirm."}
        engine.update_workflow_context(workflow_id, approved_gpt_prompt=edited)
        new_appr = dict(appr)
        new_appr.update({"status": "edited", "approved_at": _now_iso(),
                         "approved_prompt_hash": _short_hash(edited),
                         "message_id": message_id or appr.get("message_id")})
        engine.set_metadata(workflow_id, "approval", new_appr)
        self._publish(workflow_id, EV_APPROVAL_PROCEED, {"status": "edited"})
        if chat_id is not None and self._tg_send is not None:
            try:
                self._tg_send(chat_id, "\u2705 Edited prompt approved. Ready for Kiro.", None)
            except Exception:
                pass
        return {"ok": True, "reason": "edited_approved", "workflow_id": workflow_id,
                "message": "Edited prompt approved. Ready for Kiro."}

    def _do_cancel(self, workflow_id, appr, chat_id, message_id) -> dict:
        engine = self.engine
        wf = engine.get_workflow(workflow_id)
        # Idempotent: already cancelled → nothing.
        if appr.get("status") == "cancelled" or wf.state == "CANCELLED":
            return {"ok": True, "reason": "already_cancelled",
                    "message": "Already cancelled."}
        # A stale Cancel must NOT alter an already-approved workflow.
        if appr.get("status") in ("approved", "edited") or wf.approved_gpt_prompt:
            return {"ok": False, "reason": "already_approved",
                    "message": "Already approved; cannot cancel now."}
        if wf.state != "WAITING_FOR_APPROVAL":
            return {"ok": False, "reason": "wrong_state",
                    "message": f"Not awaiting approval (state={wf.state})."}
        # Record the cancelled approval metadata BEFORE cancelling — a CANCELLED
        # workflow is terminal and set_metadata would then be rejected.
        new_appr = dict(appr)
        new_appr.update({"status": "cancelled", "cancelled_at": _now_iso()})
        engine.set_metadata(workflow_id, "approval", new_appr)
        engine.cancel(workflow_id, reason="User cancelled at approval")
        self._publish(workflow_id, EV_APPROVAL_CANCELLED, {})
        if chat_id is not None and self._tg_send is not None:
            try:
                self._tg_send(chat_id, "\u274c Cancelled.", None)
            except Exception:
                pass
        return {"ok": True, "reason": "cancelled", "workflow_id": workflow_id,
                "message": "Cancelled."}

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self, workflow_id: str) -> dict:
        wf = self.engine.get_workflow(workflow_id)
        if wf is None:
            raise ApprovalError("Workflow not found")
        meta = wf.metadata or {}
        return {
            "state": wf.state,
            "latest_gpt_output_len": meta.get("latest_gpt_output_len"),
            "latest_gpt_output_hash": meta.get("latest_gpt_output_hash"),
            "latest_gpt_output_captured_at": meta.get("latest_gpt_output_captured_at"),
            "approval": meta.get("approval") or {},
            "approved_gpt_prompt_present": bool(wf.approved_gpt_prompt),
            "active": self.is_active(workflow_id),
        }


# Module-level singleton, following the project convention.
workflow_approval_service = WorkflowApprovalService()
