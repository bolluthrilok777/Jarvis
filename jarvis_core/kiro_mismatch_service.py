"""KiroMismatchService — human Kiro-session-switch confirmation flow.

When Jarvis detects that a human switched the active Kiro session away from the
one a workflow owns, the WorkflowKiroService pauses fail-closed and records a
durable mismatch (see :mod:`jarvis_core.jarvis_kiro_session`). This service
delivers the confirmation to the user (Telegram) and applies their decision:

    ✅ Continue Current Jarvis Workflow  → reselect original session + resume
    ➕ Continue In A New Kiro Session     → new session, rebind, resume
    ⏸ Keep Human Session / Pause Jarvis  → stay paused, human session untouched
    ❌ Cancel Workflow                    → cancel safely

Callbacks are durable + idempotent (resolved once). It mirrors the Part 6
approval-callback pattern: TelegramService only routes/answers; all logic
(auth, resolve-by-token, idempotency) lives here.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger("jarvis.kiromismatch")

# Callback namespace: ``kmm:<decision>:<mismatch_id>``.
CB_PREFIX = "kmm"
_DECISIONS = ("continue", "new", "pause", "cancel")


class KiroMismatchService:
    """Deliver + resolve Kiro session-mismatch confirmations."""

    def __init__(self, *, engine=None, kiro_service=None,
                 tg_send: Optional[Callable] = None,
                 tg_is_authorized: Optional[Callable] = None) -> None:
        self._engine_ref = engine
        self._kiro_service_ref = kiro_service
        self._tg_send = tg_send
        self._tg_is_authorized = tg_is_authorized

    # ── Dependencies ──────────────────────────────────────────────────────────

    @property
    def engine(self):
        if self._engine_ref is not None:
            return self._engine_ref
        from jarvis_core.workflow_engine import workflow_engine
        return workflow_engine

    @property
    def kiro_service(self):
        if self._kiro_service_ref is not None:
            return self._kiro_service_ref
        from jarvis_core.workflow_kiro_service import workflow_kiro_service
        return workflow_kiro_service

    def wire(self, *, tg_send=None, tg_is_authorized=None) -> None:
        if tg_send is not None:
            self._tg_send = tg_send
        if tg_is_authorized is not None:
            self._tg_is_authorized = tg_is_authorized

    # ── Delivery ───────────────────────────────────────────────────────────────

    def _chat_id_for(self, workflow_id: str):
        """Best-effort resolve the workflow's Telegram chat id from approval /
        command metadata (same source Part 6 uses)."""
        try:
            wf = self.engine.get_workflow(workflow_id)
            meta = wf.metadata or {}
            appr = meta.get("approval") or {}
            if appr.get("chat_id"):
                return appr.get("chat_id")
            cur = meta.get("current_command") or {}
            return cur.get("telegram_chat_id")
        except Exception:
            return None

    def notify(self, workflow_id: str, mismatch: dict) -> Optional[int]:
        """Send the Telegram confirmation with the 4 decision buttons. Returns
        the message id (or None). Best-effort; the durable mismatch record is the
        source of truth regardless of delivery."""
        chat_id = self._chat_id_for(workflow_id)
        if chat_id is None or self._tg_send is None:
            logger.info("Kiro mismatch (no chat/send) for %s: %s",
                        workflow_id, (mismatch or {}).get("reason"))
            return None
        mm_id = (mismatch or {}).get("mismatch_id") or ""
        wf = self.engine.get_workflow(workflow_id)
        wf_label = (wf.master_gpt_prompt or workflow_id)[:48] if wf else workflow_id
        substate = (mismatch or {}).get("workflow_substate") or "Kiro stage"
        text = (
            "\u26a0\ufe0f Kiro session changed\n\n"
            f"Workflow: {wf_label}\n"
            f"Jarvis session: {(mismatch or {}).get('expected_session') or '(bound)'}\n"
            f"Active Kiro session: {(mismatch or {}).get('active_session_title') or '(unknown)'}\n"
            f"Jarvis was: {substate}\n\n"
            "What should Jarvis do?"
        )
        markup = {"inline_keyboard": [
            [{"text": "\u2705 Continue Current Workflow",
              "callback_data": f"{CB_PREFIX}:continue:{mm_id}"}],
            [{"text": "\u2795 New Kiro Session",
              "callback_data": f"{CB_PREFIX}:new:{mm_id}"}],
            [{"text": "\u23f8 Pause Jarvis",
              "callback_data": f"{CB_PREFIX}:pause:{mm_id}"}],
            [{"text": "\u274c Cancel Workflow",
              "callback_data": f"{CB_PREFIX}:cancel:{mm_id}"}],
        ]}
        try:
            return self._tg_send(chat_id, text, markup)
        except Exception:
            logger.debug("mismatch telegram send failed", exc_info=True)
            return None

    # ── Resolution ──────────────────────────────────────────────────────────────

    def _find_by_mismatch_id(self, mismatch_id: str):
        """Return (workflow_id, mismatch_record) for a durable, UNRESOLVED
        mismatch, searching all workflows. None when not found."""
        try:
            candidates = self.engine.list_workflows()
        except Exception:
            candidates = self.engine.list_active_workflows()
        for wf in candidates:
            rec = (wf.metadata or {}).get("kiro_session_mismatch") or {}
            if rec.get("mismatch_id") == mismatch_id:
                return wf.workflow_id, rec
        return None, None

    def handle_callback(self, *, user_id, chat_id, decision: str,
                        mismatch_id: str, callback_id=None,
                        message_id=None) -> dict:
        """Authorize + resolve a session-mismatch decision. Idempotent: a
        duplicate callback for an already-resolved mismatch is a no-op."""
        if self._tg_is_authorized is not None and not self._tg_is_authorized(user_id):
            return {"ok": False, "reason": "unauthorized",
                    "message": "You are not authorized."}
        decision = (decision or "").strip().lower()
        if decision not in _DECISIONS:
            return {"ok": False, "reason": "unknown_decision",
                    "message": "Unknown decision."}
        workflow_id, rec = self._find_by_mismatch_id(mismatch_id)
        if workflow_id is None:
            return {"ok": False, "reason": "stale",
                    "message": "This session prompt is no longer valid."}
        if rec.get("resolved"):
            return {"ok": True, "reason": "already_resolved",
                    "workflow_id": workflow_id,
                    "message": f"Already handled ({rec.get('decision')})."}
        try:
            result = self.kiro_service.resume_after_confirmation(workflow_id, decision)
        except Exception as exc:
            logger.exception("resume_after_confirmation failed")
            return {"ok": False, "reason": "error", "message": str(exc)[:120]}
        human = {
            "continue": "\u2705 Continuing in the original Jarvis session.",
            "new": "\u2795 Continuing in a new Kiro session.",
            "pause": "\u23f8 Paused. Your Kiro session is untouched.",
            "cancel": "\u274c Workflow cancelled.",
        }[decision]
        if chat_id is not None and self._tg_send is not None:
            try:
                self._tg_send(chat_id, human, None)
            except Exception:
                pass
        return {"ok": True, "reason": decision, "workflow_id": workflow_id,
                "message": human, "result": result}


# Module-level singleton, project convention.
kiro_mismatch_service = KiroMismatchService()
