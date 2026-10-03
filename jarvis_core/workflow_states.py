"""Authoritative workflow state model for the locked Jarvis architecture (Part 1).

This module defines the *only* public workflow states and the *only* legal
transitions between them. Everything else in Part 1 (models, store, engine)
depends on these definitions, and Parts 2-12 must transition workflows through
this table rather than mutating state directly.

Design rules:
- Strict enum of public states. No silently invented extra public states.
- One authoritative transition table (``ALLOWED_TRANSITIONS``).
- ``COMPLETED`` and ``CANCELLED`` are terminal — no outgoing transitions.
- ``PAUSED`` / ``FAILED`` transitions are intentionally broad because the
  concrete resume/retry target is validated dynamically by the engine against
  the stored ``resume_state`` / recovery target rather than hard-coded here.
"""
from __future__ import annotations

from enum import Enum


class WorkflowState(str, Enum):
    """The authoritative set of public workflow states.

    Values are stored verbatim in the database, so renaming a value is a
    schema-affecting change. Add new states deliberately and update
    ``ALLOWED_TRANSITIONS`` in the same change.
    """

    IDLE = "IDLE"
    INITIALIZING = "INITIALIZING"
    GPT_BOOTSTRAPPING = "GPT_BOOTSTRAPPING"
    WAITING_FOR_COMMAND = "WAITING_FOR_COMMAND"
    GPT_RUNNING = "GPT_RUNNING"
    GPT_OUTPUT_READY = "GPT_OUTPUT_READY"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    KIRO_STARTING = "KIRO_STARTING"
    KIRO_RUNNING = "KIRO_RUNNING"
    KIRO_WAITING_PERMISSION = "KIRO_WAITING_PERMISSION"
    KIRO_RESULT_READY = "KIRO_RESULT_READY"
    GPT_REVIEWING = "GPT_REVIEWING"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# Terminal states never have outgoing transitions.
TERMINAL_STATES: frozenset[WorkflowState] = frozenset(
    {WorkflowState.COMPLETED, WorkflowState.CANCELLED}
)

# States that a workflow may be *resumed* into after a PAUSE. Terminal states
# and PAUSED/FAILED itself are never valid resume targets.
RESUMABLE_STATES: frozenset[WorkflowState] = frozenset(
    {
        WorkflowState.INITIALIZING,
        WorkflowState.GPT_BOOTSTRAPPING,
        WorkflowState.WAITING_FOR_COMMAND,
        WorkflowState.GPT_RUNNING,
        WorkflowState.GPT_OUTPUT_READY,
        WorkflowState.WAITING_FOR_APPROVAL,
        WorkflowState.KIRO_STARTING,
        WorkflowState.KIRO_RUNNING,
        WorkflowState.KIRO_WAITING_PERMISSION,
        WorkflowState.KIRO_RESULT_READY,
        WorkflowState.GPT_REVIEWING,
    }
)

# States a FAILED workflow may be retried back into. Kept conservative: retry
# targets are the recoverable operational states. The engine additionally
# validates the concrete target, and Part 1 never retries automatically.
RETRYABLE_TARGET_STATES: frozenset[WorkflowState] = frozenset(
    {
        WorkflowState.INITIALIZING,
        WorkflowState.GPT_BOOTSTRAPPING,
        WorkflowState.WAITING_FOR_COMMAND,
        WorkflowState.GPT_RUNNING,
        WorkflowState.WAITING_FOR_APPROVAL,
        WorkflowState.KIRO_STARTING,
        WorkflowState.KIRO_RUNNING,
        WorkflowState.KIRO_WAITING_PERMISSION,
        WorkflowState.GPT_REVIEWING,
    }
)


# ── Authoritative transition table ──────────────────────────────────────────
# Maps each state to the complete set of states it may transition INTO.
# This is the single source of truth for legal transitions. The engine's
# ``transition()`` method rejects anything not present here.
ALLOWED_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.IDLE: frozenset(
        {WorkflowState.INITIALIZING}
    ),
    WorkflowState.INITIALIZING: frozenset(
        {
            WorkflowState.GPT_BOOTSTRAPPING,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.GPT_BOOTSTRAPPING: frozenset(
        {
            WorkflowState.WAITING_FOR_COMMAND,
            WorkflowState.FAILED,
            WorkflowState.PAUSED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.WAITING_FOR_COMMAND: frozenset(
        {
            WorkflowState.GPT_RUNNING,
            WorkflowState.PAUSED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.GPT_RUNNING: frozenset(
        {
            WorkflowState.GPT_OUTPUT_READY,
            WorkflowState.FAILED,
            WorkflowState.PAUSED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.GPT_OUTPUT_READY: frozenset(
        {WorkflowState.WAITING_FOR_APPROVAL}
    ),
    WorkflowState.WAITING_FOR_APPROVAL: frozenset(
        {
            WorkflowState.KIRO_STARTING,
            WorkflowState.GPT_RUNNING,
            WorkflowState.COMPLETED,
            WorkflowState.CANCELLED,
            WorkflowState.PAUSED,
        }
    ),
    WorkflowState.KIRO_STARTING: frozenset(
        {
            WorkflowState.KIRO_RUNNING,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
            WorkflowState.PAUSED,
        }
    ),
    WorkflowState.KIRO_RUNNING: frozenset(
        {
            WorkflowState.KIRO_WAITING_PERMISSION,
            WorkflowState.KIRO_RESULT_READY,
            WorkflowState.FAILED,
            WorkflowState.PAUSED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.KIRO_WAITING_PERMISSION: frozenset(
        {
            WorkflowState.KIRO_RUNNING,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.KIRO_RESULT_READY: frozenset(
        {WorkflowState.GPT_REVIEWING}
    ),
    WorkflowState.GPT_REVIEWING: frozenset(
        {
            WorkflowState.WAITING_FOR_APPROVAL,
            WorkflowState.COMPLETED,
            WorkflowState.FAILED,
            WorkflowState.PAUSED,
            WorkflowState.CANCELLED,
        }
    ),
    # PAUSED resumes into a previously-saved resumable state, or is cancelled.
    # The concrete resume target is validated by the engine against the stored
    # ``resume_state``; here we allow the full resumable set plus CANCELLED.
    WorkflowState.PAUSED: RESUMABLE_STATES | frozenset({WorkflowState.CANCELLED}),
    # FAILED retries into a recoverable target, or is cancelled. The concrete
    # retry target is validated by the engine; Part 1 never retries on its own.
    WorkflowState.FAILED: RETRYABLE_TARGET_STATES | frozenset({WorkflowState.CANCELLED}),
    # Terminal — no outgoing transitions.
    WorkflowState.COMPLETED: frozenset(),
    WorkflowState.CANCELLED: frozenset(),
}


def coerce_state(value: "WorkflowState | str") -> WorkflowState:
    """Return a :class:`WorkflowState`, accepting either the enum or its value."""
    if isinstance(value, WorkflowState):
        return value
    return WorkflowState(str(value))


def is_terminal(state: "WorkflowState | str") -> bool:
    """True when *state* is a terminal state (COMPLETED or CANCELLED)."""
    return coerce_state(state) in TERMINAL_STATES


def is_resumable(state: "WorkflowState | str") -> bool:
    """True when *state* is a valid target to resume a paused workflow into."""
    return coerce_state(state) in RESUMABLE_STATES


def can_transition(current: "WorkflowState | str", target: "WorkflowState | str") -> bool:
    """Return True when moving from *current* to *target* is permitted.

    This consults the single authoritative :data:`ALLOWED_TRANSITIONS` table.
    Terminal states can never transition. Unknown states return False.
    """
    try:
        cur = coerce_state(current)
        tgt = coerce_state(target)
    except ValueError:
        return False
    return tgt in ALLOWED_TRANSITIONS.get(cur, frozenset())


def allowed_targets(current: "WorkflowState | str") -> frozenset[WorkflowState]:
    """Return the set of states *current* may legally transition into."""
    try:
        cur = coerce_state(current)
    except ValueError:
        return frozenset()
    return ALLOWED_TRANSITIONS.get(cur, frozenset())
