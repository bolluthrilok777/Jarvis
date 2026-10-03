"""Persistent data models for the locked Jarvis workflow engine (Part 1).

These Pydantic v2 models describe the durable orchestration foundation:

- :class:`Workflow`      — one project workflow and its full state.
- :class:`WorkflowEvent` — an append-only audit/event history entry.
- :class:`WorkflowAction`— an idempotency record for a side-effecting action.

Only safe, structured state belongs here. Secrets (tokens, credentials, API
keys, cookies) must never be stored in these models or in event metadata.

Most future integration fields (ChatGPT / Kiro identifiers, outputs) are
nullable: Part 1 creates the schema, Parts 2-12 populate it. Adding a new
optional field with a default is backward compatible with existing rows.
"""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from jarvis_core.timezone_utils import jarvis_now
from jarvis_core.workflow_states import WorkflowState


def _new_id() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return jarvis_now().isoformat()


# Current logical schema version for the Workflow model. Bumped when a
# migration changes the on-disk schema (see workflow_store.SCHEMA_VERSION).
WORKFLOW_MODEL_VERSION = 1


class ActionStatus(str, Enum):
    """Lifecycle of an idempotent, side-effecting action."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class EventType(str, Enum):
    """Well-known workflow event types written to the audit log.

    Callers may also pass arbitrary string event types, but these named ones
    are what Part 1 emits internally.
    """

    WORKFLOW_CREATED = "workflow_created"
    STATE_TRANSITION = "state_transition"
    WORKFLOW_PAUSED = "workflow_paused"
    WORKFLOW_RESUMED = "workflow_resumed"
    WORKFLOW_FAILED = "workflow_failed"
    WORKFLOW_RETRIED = "workflow_retried"
    WORKFLOW_CANCELLED = "workflow_cancelled"
    WORKFLOW_COMPLETED = "workflow_completed"
    FIELD_UPDATED = "field_updated"
    ACTION_STARTED = "action_started"
    ACTION_COMPLETED = "action_completed"
    ACTION_FAILED = "action_failed"
    ACTION_DUPLICATE_BLOCKED = "action_duplicate_blocked"


class Workflow(BaseModel):
    """One project workflow and its complete, durable state.

    This is the single source of truth for a workflow. The engine never mutates
    a workflow except through its authoritative methods, which persist every
    change atomically.
    """

    # ── Identity ────────────────────────────────────────────────────────────
    workflow_id: str = Field(default_factory=_new_id)
    project_id: Optional[str] = None
    project_name: str = ""
    project_path: Optional[str] = None

    # ── State machine ─────────────────────────────────────────────────────
    state: str = WorkflowState.IDLE.value
    previous_state: Optional[str] = None
    resume_state: Optional[str] = None

    # ── Timestamps (ISO strings, Jarvis-local tz) ───────────────────────────
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    # ── Iteration ───────────────────────────────────────────────────────────
    iteration_number: int = 0

    # ── Chrome profile selection (populated by later Parts) ─────────────────
    selected_chrome_profile_key: Optional[str] = None
    selected_chrome_account_email: Optional[str] = None

    # ── ChatGPT context (populated by later Parts) ──────────────────────────
    master_gpt_prompt: Optional[str] = None
    chatgpt_conversation_url: Optional[str] = None
    chatgpt_tab_id: Optional[str] = None
    chatgpt_window_hwnd: Optional[int] = None

    # ── Command / output cycle (populated by later Parts) ───────────────────
    current_user_command: Optional[str] = None
    latest_gpt_output: Optional[str] = None
    approved_gpt_prompt: Optional[str] = None

    # ── Kiro context (populated by later Parts) ─────────────────────────────
    kiro_session_id: Optional[str] = None
    kiro_project_path: Optional[str] = None
    latest_kiro_result: Optional[str] = None

    # ── Error / pause / cancel bookkeeping ──────────────────────────────────
    last_error: Optional[str] = None
    last_error_code: Optional[str] = None
    pause_reason: Optional[str] = None
    cancel_reason: Optional[str] = None

    # ── Action idempotency pointers ─────────────────────────────────────────
    current_action_id: Optional[str] = None
    last_completed_action_id: Optional[str] = None

    # ── Schema / optimistic-concurrency version ─────────────────────────────
    # Bumped on every persisted mutation; used for optimistic concurrency so a
    # stale read-modify-write cannot silently clobber a concurrent change.
    version: int = 0

    # ── Safe metadata bag for future extensions ─────────────────────────────
    metadata: dict[str, Any] = Field(default_factory=dict)

    def touch(self) -> None:
        """Update ``updated_at`` to now."""
        self.updated_at = _now()

    def is_terminal(self) -> bool:
        from jarvis_core.workflow_states import is_terminal as _is_terminal

        return _is_terminal(self.state)

    def public(self) -> dict:
        """Safe public view of the workflow for UI/frontend consumption.

        Excludes potentially large / prompt-bearing fields by default; those
        are available via the full model when a caller explicitly needs them.
        """
        return {
            "workflow_id": self.workflow_id,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "project_path": self.project_path,
            "state": self.state,
            "previous_state": self.previous_state,
            "resume_state": self.resume_state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "iteration_number": self.iteration_number,
            "selected_chrome_profile_key": self.selected_chrome_profile_key,
            "selected_chrome_account_email": self.selected_chrome_account_email,
            "chatgpt_conversation_url": self.chatgpt_conversation_url,
            "chatgpt_tab_id": self.chatgpt_tab_id,
            "kiro_session_id": self.kiro_session_id,
            "kiro_project_path": self.kiro_project_path,
            "last_error": self.last_error,
            "last_error_code": self.last_error_code,
            "pause_reason": self.pause_reason,
            "cancel_reason": self.cancel_reason,
            "current_action_id": self.current_action_id,
            "last_completed_action_id": self.last_completed_action_id,
            "version": self.version,
        }


class WorkflowEvent(BaseModel):
    """One append-only audit entry describing a meaningful workflow change.

    Event metadata must never contain secrets. This history is what the
    frontend will later render when a user inspects an architecture block.
    """

    event_id: str = Field(default_factory=_new_id)
    workflow_id: str
    event_type: str
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    timestamp: str = Field(default_factory=_now)
    iteration_number: int = 0
    action_id: Optional[str] = None
    message: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Monotonic per-DB sequence assigned by the store on insert. Guarantees a
    # deterministic ordering of events even when timestamps collide.
    sequence: Optional[int] = None


class WorkflowAction(BaseModel):
    """An idempotency record for a side-effecting action.

    Later Parts launch real side effects (open Chrome, send a prompt, start
    Kiro). Each such action is registered here with a caller-chosen
    ``action_id`` so that after a crash/restart the system can tell whether an
    action already ran and must not be duplicated.
    """

    action_id: str
    workflow_id: str
    action_type: Optional[str] = None
    status: str = ActionStatus.RUNNING.value
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    result_ref: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecoveryPlan(BaseModel):
    """Structured, side-effect-free description of how to safely resume.

    Returned by ``WorkflowEngine.get_recovery_plan``. Part 1 never *acts* on
    this; it only reports what a later Part should do next.
    """

    workflow_id: str
    current_state: str
    resume_state: Optional[str] = None
    is_terminal: bool = False
    pending_action_id: Optional[str] = None
    pending_action_type: Optional[str] = None
    last_completed_action_id: Optional[str] = None
    safe_next_step: str = ""
    notes: list[str] = Field(default_factory=list)
