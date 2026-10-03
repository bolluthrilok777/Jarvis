"""WorkflowEngine — authoritative service layer for the locked Jarvis
architecture (Part 1).

This is the ONLY sanctioned way to create, mutate, and inspect workflows.
Parts 2-12 must call these methods instead of writing DB rows directly.

Guarantees:
- One authoritative ``transition`` method enforces the transition table; no
  arbitrary state mutation is possible through the public API.
- Every meaningful change is persisted atomically together with an audit event
  (``commit_transition`` uses a single SQLite transaction with optimistic
  concurrency on ``version``).
- Idempotent actions: ``begin_action`` refuses to start an action that is
  already running or completed, and this survives restart (state lives in the
  ``workflow_actions`` table, keyed by ``action_id``).
- Pause/resume and fail/retry survive restart because ``resume_state`` /
  ``previous_state`` are durable columns.
- ``restore_active_workflows`` and ``get_recovery_plan`` describe what should
  happen next WITHOUT running any Chrome/Kiro side effects. Part 1 never
  triggers side effects.

Thread-safety: a per-engine ``RLock`` serialises read-modify-write cycles, and
the store additionally enforces optimistic concurrency so a stale write is
rejected rather than silently clobbering a concurrent change.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from jarvis_core.timezone_utils import jarvis_now
from jarvis_core.workflow_models import (
    ActionStatus,
    EventType,
    RecoveryPlan,
    Workflow,
    WorkflowAction,
    WorkflowEvent,
)
from jarvis_core.workflow_states import (
    RESUMABLE_STATES,
    RETRYABLE_TARGET_STATES,
    WorkflowState,
    can_transition,
    coerce_state,
    is_terminal,
)
from jarvis_core.workflow_store import WorkflowStore

logger = logging.getLogger("jarvis.workflow")


def _now() -> str:
    return jarvis_now().isoformat()


# Fields on Workflow that update_workflow_context is allowed to set. Excludes
# state-machine columns (state/previous_state/resume_state/version) and audit
# pointers that only the engine may change.
_UPDATABLE_CONTEXT_FIELDS = {
    "project_id", "project_name", "project_path",
    "started_at", "completed_at",
    "iteration_number",
    "selected_chrome_profile_key", "selected_chrome_account_email",
    "master_gpt_prompt", "chatgpt_conversation_url", "chatgpt_tab_id",
    "chatgpt_window_hwnd",
    "current_user_command", "latest_gpt_output", "approved_gpt_prompt",
    "kiro_session_id", "kiro_project_path", "latest_kiro_result",
}


class WorkflowError(Exception):
    """Base class for workflow engine errors."""


class WorkflowNotFound(WorkflowError):
    """Raised when a workflow id does not exist."""


class InvalidTransition(WorkflowError):
    """Raised when a requested state transition is not permitted."""


class TerminalWorkflowError(WorkflowError):
    """Raised when attempting to mutate a terminal (COMPLETED/CANCELLED) workflow."""


class ConcurrencyError(WorkflowError):
    """Raised when an optimistic-concurrency guard rejects a stale write."""


class WorkflowEngine:
    """Durable, thread-safe workflow orchestration engine."""

    def __init__(self, store: Optional[WorkflowStore] = None, db_path=None) -> None:
        self._store = store if store is not None else WorkflowStore(db_path)
        self._lock = threading.RLock()

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _require(self, workflow_id: str) -> Workflow:
        wf = self._store.get_workflow(workflow_id)
        if wf is None:
            raise WorkflowNotFound(f"No workflow with id {workflow_id!r}")
        return wf

    def _build_event(
        self,
        wf: Workflow,
        event_type: str,
        *,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        action_id: Optional[str] = None,
        message: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> WorkflowEvent:
        return WorkflowEvent(
            workflow_id=wf.workflow_id,
            event_type=event_type,
            from_state=from_state,
            to_state=to_state,
            iteration_number=wf.iteration_number,
            action_id=action_id,
            message=message,
            metadata=metadata or {},
        )

    def _commit(self, wf: Workflow, event: WorkflowEvent, *, expected_version: int) -> Workflow:
        """Bump version, persist workflow + event atomically, return fresh copy."""
        wf.version = expected_version + 1
        wf.touch()
        ok = self._store.commit_transition(wf, event, expected_version=expected_version)
        if not ok:
            raise ConcurrencyError(
                f"Concurrent modification of workflow {wf.workflow_id!r} "
                f"(expected version {expected_version})"
            )
        return wf

    # ── Creation & lookup ────────────────────────────────────────────────────

    def create_workflow(
        self,
        *,
        project_name: str = "",
        project_id: Optional[str] = None,
        project_path: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Workflow:
        """Create a new workflow in the IDLE state and persist it."""
        wf = Workflow(
            project_name=project_name,
            project_id=project_id,
            project_path=project_path,
            state=WorkflowState.IDLE.value,
            metadata=metadata or {},
            version=0,
        )
        with self._lock:
            self._store.insert_workflow(wf)
            event = self._build_event(
                wf,
                EventType.WORKFLOW_CREATED.value,
                to_state=wf.state,
                message=f"Workflow created for project {project_name!r}",
            )
            self._store.append_event(event)
        logger.info("Workflow %s created (project=%s)", wf.workflow_id, project_name)
        return wf

    def get_workflow(self, workflow_id: str) -> Optional[Workflow]:
        return self._store.get_workflow(workflow_id)

    def list_active_workflows(self) -> list[Workflow]:
        """Return non-terminal workflows (COMPLETED/CANCELLED excluded)."""
        return self._store.list_active_workflows()

    def list_workflows(self) -> list[Workflow]:
        return self._store.list_workflows()

    def get_events(self, workflow_id: str, *, limit: Optional[int] = None) -> list[WorkflowEvent]:
        return self._store.get_events(workflow_id, limit=limit)

    # ── Context updates (non-state fields) ────────────────────────────────────

    def update_workflow_context(self, workflow_id: str, **fields: Any) -> Workflow:
        """Update non-state context fields (e.g. GPT/Kiro identifiers, outputs).

        Rejects attempts to change state-machine columns — those only move via
        ``transition`` and friends. Emits a ``field_updated`` audit event that
        records only the field *names* (never values, which may be large or
        sensitive).
        """
        with self._lock:
            wf = self._require(workflow_id)
            if wf.is_terminal():
                raise TerminalWorkflowError(
                    f"Workflow {workflow_id!r} is terminal ({wf.state}); cannot update context"
                )
            invalid = set(fields) - _UPDATABLE_CONTEXT_FIELDS
            if invalid:
                raise WorkflowError(
                    f"Fields not updatable via update_workflow_context: {sorted(invalid)}"
                )
            expected_version = wf.version
            for name, value in fields.items():
                setattr(wf, name, value)
            event = self._build_event(
                wf,
                EventType.FIELD_UPDATED.value,
                from_state=wf.state,
                to_state=wf.state,
                message="Updated fields: " + ", ".join(sorted(fields)),
                metadata={"fields": sorted(fields)},
            )
            return self._commit(wf, event, expected_version=expected_version)

    # ── Authoritative transition ──────────────────────────────────────────────

    def transition(
        self,
        workflow_id: str,
        target: "WorkflowState | str",
        *,
        message: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> Workflow:
        """Move a workflow to *target*, enforcing the transition table.

        This is the single authoritative transition path. It rejects illegal
        transitions and any transition out of a terminal state.
        """
        target_state = coerce_state(target)
        with self._lock:
            wf = self._require(workflow_id)
            current = coerce_state(wf.state)
            if is_terminal(current):
                raise TerminalWorkflowError(
                    f"Workflow {workflow_id!r} is terminal ({current.value}); "
                    f"cannot transition to {target_state.value}"
                )
            if not can_transition(current, target_state):
                raise InvalidTransition(
                    f"Illegal transition {current.value} -> {target_state.value} "
                    f"for workflow {workflow_id!r}"
                )
            return self._apply_transition(
                wf, target_state, message=message, metadata=metadata
            )

    def _apply_transition(
        self,
        wf: Workflow,
        target_state: WorkflowState,
        *,
        message: str = "",
        metadata: Optional[dict[str, Any]] = None,
        event_type: str = EventType.STATE_TRANSITION.value,
    ) -> Workflow:
        """Apply an already-validated transition (caller holds the lock)."""
        expected_version = wf.version
        from_state = wf.state
        wf.previous_state = from_state
        wf.state = target_state.value

        # Lifecycle timestamp bookkeeping.
        if target_state == WorkflowState.INITIALIZING and not wf.started_at:
            wf.started_at = _now()
        if is_terminal(target_state):
            wf.completed_at = _now()

        event = self._build_event(
            wf,
            event_type,
            from_state=from_state,
            to_state=target_state.value,
            message=message or f"{from_state} -> {target_state.value}",
            metadata=metadata,
        )
        result = self._commit(wf, event, expected_version=expected_version)
        logger.info(
            "Workflow %s transition %s -> %s",
            wf.workflow_id, from_state, target_state.value,
        )
        return result

    # ── Pause / resume ────────────────────────────────────────────────────────

    def pause(self, workflow_id: str, reason: str = "") -> Workflow:
        """Pause a workflow, saving its current state as the resume target.

        The current state must be resumable and pausable. Terminal workflows
        cannot be paused.
        """
        with self._lock:
            wf = self._require(workflow_id)
            current = coerce_state(wf.state)
            if is_terminal(current):
                raise TerminalWorkflowError(
                    f"Cannot pause terminal workflow {workflow_id!r} ({current.value})"
                )
            if current == WorkflowState.PAUSED:
                return wf  # already paused; idempotent
            if current not in RESUMABLE_STATES:
                raise InvalidTransition(
                    f"State {current.value} is not pausable/resumable"
                )
            if not can_transition(current, WorkflowState.PAUSED):
                raise InvalidTransition(
                    f"State {current.value} cannot transition to PAUSED"
                )
            # Record where to resume BEFORE flipping state.
            wf.resume_state = current.value
            wf.pause_reason = reason or None
            return self._apply_transition(
                wf,
                WorkflowState.PAUSED,
                message=f"Paused: {reason}" if reason else "Paused",
                metadata={"resume_state": current.value},
                event_type=EventType.WORKFLOW_PAUSED.value,
            )

    def resume(self, workflow_id: str) -> Workflow:
        """Resume a paused workflow back to its saved ``resume_state``."""
        with self._lock:
            wf = self._require(workflow_id)
            current = coerce_state(wf.state)
            if is_terminal(current):
                raise TerminalWorkflowError(
                    f"Cannot resume terminal workflow {workflow_id!r} ({current.value})"
                )
            if current != WorkflowState.PAUSED:
                raise InvalidTransition(
                    f"Workflow {workflow_id!r} is not paused (state={current.value})"
                )
            if not wf.resume_state:
                raise WorkflowError(
                    f"Workflow {workflow_id!r} has no saved resume_state"
                )
            target = coerce_state(wf.resume_state)
            if target not in RESUMABLE_STATES:
                raise InvalidTransition(
                    f"Saved resume_state {target.value} is not resumable"
                )
            # PAUSED -> target is permitted by the table for all resumable states.
            wf.resume_state = None
            wf.pause_reason = None
            return self._apply_transition(
                wf,
                target,
                message=f"Resumed to {target.value}",
                event_type=EventType.WORKFLOW_RESUMED.value,
            )

    # ── Fail / retry ────────────────────────────────────────────────────────

    def fail(
        self,
        workflow_id: str,
        *,
        error: str = "",
        error_code: Optional[str] = None,
    ) -> Workflow:
        """Mark a workflow FAILED, preserving the state it failed from."""
        with self._lock:
            wf = self._require(workflow_id)
            current = coerce_state(wf.state)
            if is_terminal(current):
                raise TerminalWorkflowError(
                    f"Cannot fail terminal workflow {workflow_id!r} ({current.value})"
                )
            if not can_transition(current, WorkflowState.FAILED):
                raise InvalidTransition(
                    f"State {current.value} cannot transition to FAILED"
                )
            wf.last_error = error or None
            wf.last_error_code = error_code
            # previous_state is set by _apply_transition and is the failed-from
            # state, which drives retry() below.
            return self._apply_transition(
                wf,
                WorkflowState.FAILED,
                message=f"Failed: {error}" if error else "Failed",
                metadata={"error_code": error_code} if error_code else None,
                event_type=EventType.WORKFLOW_FAILED.value,
            )

    def retry(
        self,
        workflow_id: str,
        target: "WorkflowState | str | None" = None,
        *,
        message: str = "",
    ) -> Workflow:
        """Retry a FAILED workflow into a recoverable target state.

        Part 1 never retries automatically; this only provides the reliable
        mechanism. When *target* is omitted, the state the workflow failed from
        (``previous_state``) is used if it is a valid retry target.
        """
        with self._lock:
            wf = self._require(workflow_id)
            current = coerce_state(wf.state)
            if current != WorkflowState.FAILED:
                raise InvalidTransition(
                    f"Workflow {workflow_id!r} is not FAILED (state={current.value}); cannot retry"
                )
            if target is None:
                if not wf.previous_state:
                    raise WorkflowError(
                        f"Workflow {workflow_id!r} has no previous_state to retry into"
                    )
                target_state = coerce_state(wf.previous_state)
            else:
                target_state = coerce_state(target)
            if target_state not in RETRYABLE_TARGET_STATES:
                raise InvalidTransition(
                    f"{target_state.value} is not a valid retry target"
                )
            if not can_transition(WorkflowState.FAILED, target_state):
                raise InvalidTransition(
                    f"FAILED cannot transition to {target_state.value}"
                )
            # Clear error markers on successful retry.
            wf.last_error = None
            wf.last_error_code = None
            return self._apply_transition(
                wf,
                target_state,
                message=message or f"Retried into {target_state.value}",
                event_type=EventType.WORKFLOW_RETRIED.value,
            )

    # ── Cancel / complete ────────────────────────────────────────────────────

    def cancel(self, workflow_id: str, reason: str = "") -> Workflow:
        """Cancel a workflow (terminal). Idempotent if already cancelled."""
        with self._lock:
            wf = self._require(workflow_id)
            current = coerce_state(wf.state)
            if current == WorkflowState.CANCELLED:
                return wf
            if current == WorkflowState.COMPLETED:
                raise TerminalWorkflowError(
                    f"Workflow {workflow_id!r} already COMPLETED; cannot cancel"
                )
            if not can_transition(current, WorkflowState.CANCELLED):
                raise InvalidTransition(
                    f"State {current.value} cannot transition to CANCELLED"
                )
            wf.cancel_reason = reason or None
            return self._apply_transition(
                wf,
                WorkflowState.CANCELLED,
                message=f"Cancelled: {reason}" if reason else "Cancelled",
                event_type=EventType.WORKFLOW_CANCELLED.value,
            )

    def complete(self, workflow_id: str, *, message: str = "") -> Workflow:
        """Mark a workflow COMPLETED (terminal)."""
        with self._lock:
            wf = self._require(workflow_id)
            current = coerce_state(wf.state)
            if current == WorkflowState.COMPLETED:
                return wf
            if not can_transition(current, WorkflowState.COMPLETED):
                raise InvalidTransition(
                    f"State {current.value} cannot transition to COMPLETED"
                )
            return self._apply_transition(
                wf,
                WorkflowState.COMPLETED,
                message=message or "Completed",
                event_type=EventType.WORKFLOW_COMPLETED.value,
            )

    # ── Idempotent actions ────────────────────────────────────────────────────

    def begin_action(
        self,
        workflow_id: str,
        action_id: str,
        *,
        action_type: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> tuple[bool, WorkflowAction]:
        """Attempt to begin a side-effecting action idempotently.

        Returns ``(started, action)``:
        - ``(True, action)``  — this call registered a new RUNNING action; the
          caller should proceed to perform the side effect.
        - ``(False, action)`` — an action with this id already exists (running
          or completed); the caller MUST NOT perform the side effect again.

        The RUNNING record and completion status are durable, so after a crash
        and restart later code can call ``begin_action`` again and be told the
        action already ran, preventing duplicate side effects.
        """
        with self._lock:
            wf = self._require(workflow_id)
            action = WorkflowAction(
                action_id=action_id,
                workflow_id=workflow_id,
                action_type=action_type,
                status=ActionStatus.RUNNING.value,
                metadata=metadata or {},
            )
            started, stored = self._store.try_begin_action(action)
            if started:
                wf.current_action_id = action_id
                event = self._build_event(
                    wf,
                    EventType.ACTION_STARTED.value,
                    from_state=wf.state,
                    to_state=wf.state,
                    action_id=action_id,
                    message=f"Action started: {action_type or action_id}",
                    metadata={"action_type": action_type} if action_type else None,
                )
                self._commit(wf, event, expected_version=wf.version)
            else:
                # Duplicate — record an audit trail but do NOT re-run.
                event = self._build_event(
                    wf,
                    EventType.ACTION_DUPLICATE_BLOCKED.value,
                    from_state=wf.state,
                    to_state=wf.state,
                    action_id=action_id,
                    message=(
                        f"Duplicate action blocked (status={stored.status})"
                    ),
                )
                self._store.append_event(event)
            return started, stored

    def complete_action(
        self,
        workflow_id: str,
        action_id: str,
        *,
        result_ref: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> WorkflowAction:
        """Mark an action COMPLETED and persist it atomically."""
        with self._lock:
            wf = self._require(workflow_id)
            action = self._store.get_action(action_id)
            if action is None:
                raise WorkflowError(f"No action with id {action_id!r}")
            action.status = ActionStatus.COMPLETED.value
            action.result_ref = result_ref
            action.updated_at = _now()
            if metadata:
                action.metadata = {**(action.metadata or {}), **metadata}
            self._store.update_action(action)
            wf.last_completed_action_id = action_id
            if wf.current_action_id == action_id:
                wf.current_action_id = None
            event = self._build_event(
                wf,
                EventType.ACTION_COMPLETED.value,
                from_state=wf.state,
                to_state=wf.state,
                action_id=action_id,
                message=f"Action completed: {action.action_type or action_id}",
            )
            self._commit(wf, event, expected_version=wf.version)
            return action

    def fail_action(
        self,
        workflow_id: str,
        action_id: str,
        *,
        error: str = "",
        error_code: Optional[str] = None,
    ) -> WorkflowAction:
        """Mark an action FAILED and persist it atomically.

        The action record remains so the failure survives restart and can be
        inspected/recovered; a FAILED action may later be retried by clearing
        it at a higher level (Part 1 does not auto-retry).
        """
        with self._lock:
            wf = self._require(workflow_id)
            action = self._store.get_action(action_id)
            if action is None:
                raise WorkflowError(f"No action with id {action_id!r}")
            action.status = ActionStatus.FAILED.value
            action.error = error or None
            action.error_code = error_code
            action.updated_at = _now()
            self._store.update_action(action)
            if wf.current_action_id == action_id:
                wf.current_action_id = None
            event = self._build_event(
                wf,
                EventType.ACTION_FAILED.value,
                from_state=wf.state,
                to_state=wf.state,
                action_id=action_id,
                message=f"Action failed: {error}" if error else "Action failed",
                metadata={"error_code": error_code} if error_code else None,
            )
            self._commit(wf, event, expected_version=wf.version)
            return action

    def get_action(self, action_id: str) -> Optional[WorkflowAction]:
        return self._store.get_action(action_id)

    def list_actions(self, workflow_id: str) -> list[WorkflowAction]:
        return self._store.list_actions(workflow_id)

    # ── Safe metadata + delete (additive; used by the Part 2 API layer) ───────
    # These exist so higher layers never write SQLite directly. They mutate
    # only the free-form ``metadata`` bag (never state-machine columns) and the
    # persistence layer, keeping the "engine methods only" contract intact.

    def set_metadata(self, workflow_id: str, key: str, value: Any) -> Workflow:
        """Set a single key in the workflow's free-form metadata bag.

        Emits a ``field_updated`` audit event recording only the metadata key
        name (never the value). Never touches state-machine columns.
        """
        with self._lock:
            wf = self._require(workflow_id)
            if wf.is_terminal():
                raise TerminalWorkflowError(
                    f"Workflow {workflow_id!r} is terminal ({wf.state}); cannot update metadata"
                )
            expected_version = wf.version
            new_meta = dict(wf.metadata or {})
            new_meta[str(key)] = value
            wf.metadata = new_meta
            event = self._build_event(
                wf,
                EventType.FIELD_UPDATED.value,
                from_state=wf.state,
                to_state=wf.state,
                message=f"Updated metadata: {key}",
                metadata={"metadata_key": str(key)},
            )
            return self._commit(wf, event, expected_version=expected_version)

    def delete_workflow(self, workflow_id: str) -> bool:
        """Delete a workflow and all its events/actions (used for verification
        cleanup). Returns True if a row was removed."""
        with self._lock:
            return self._store.delete_workflow(workflow_id)

    # ── Restart recovery (no side effects) ────────────────────────────────────

    def restore_active_workflows(self) -> list[Workflow]:
        """Return non-terminal workflows durably restored from the store.

        Part 1 explicitly does NOT restart any actions or run Chrome/Kiro. It
        only surfaces the durable state exactly as persisted so later Parts can
        decide what to do. Terminal workflows are excluded.
        """
        active = self._store.list_active_workflows()
        logger.info("Restored %d active workflow(s) from store", len(active))
        return active

    def get_recovery_plan(self, workflow_id: str) -> RecoveryPlan:
        """Return a structured, side-effect-free description of what to do next.

        This inspects durable state and any in-flight action to tell a caller
        whether an action was already started/completed and what the safe next
        step is. It never executes anything.
        """
        wf = self._require(workflow_id)
        current = coerce_state(wf.state)
        notes: list[str] = []

        pending_action = None
        pending_action_type = None
        if wf.current_action_id:
            action = self._store.get_action(wf.current_action_id)
            if action is not None and action.status == ActionStatus.RUNNING.value:
                pending_action = action.action_id
                pending_action_type = action.action_type
                notes.append(
                    "An action was RUNNING when the process stopped; verify "
                    "whether its side effect completed before re-issuing it."
                )

        terminal = is_terminal(current)
        if terminal:
            safe_next = "none (workflow is terminal)"
            notes.append("Terminal workflow; no recovery needed.")
        elif current == WorkflowState.PAUSED:
            safe_next = f"resume to {wf.resume_state}" if wf.resume_state else "resume (no saved state)"
            notes.append("Workflow is paused; call resume() to continue.")
        elif current == WorkflowState.FAILED:
            tgt = wf.previous_state or "(unknown)"
            safe_next = f"retry into {tgt} (manual decision required)"
            notes.append("Workflow failed; retry is a deliberate manual step in Part 1.")
        elif pending_action is not None:
            safe_next = (
                f"resolve pending action {pending_action}; do NOT blindly re-run it"
            )
        else:
            safe_next = f"continue from {current.value}"

        return RecoveryPlan(
            workflow_id=wf.workflow_id,
            current_state=current.value,
            resume_state=wf.resume_state,
            is_terminal=terminal,
            pending_action_id=pending_action,
            pending_action_type=pending_action_type,
            last_completed_action_id=wf.last_completed_action_id,
            safe_next_step=safe_next,
            notes=notes,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        self._store.close()


# Module-level singleton, matching the project's convention (task_manager,
# feature_orchestrator, ...). Uses the default on-disk DB at data/workflows.db.
workflow_engine = WorkflowEngine()
