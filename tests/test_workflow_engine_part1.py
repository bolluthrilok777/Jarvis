"""Focused Part 1 tests for the locked Jarvis workflow engine.

These cover the 25 required behaviours: persistence across restart, transition
enforcement, terminal safety, pause/resume, fail/retry, cancel, event ordering,
iteration + future-field persistence, action idempotency and recovery, a
concurrency-safety check, active-workflow restore, recovery plans, schema
migration/versioning, and a no-secret-logging check.

All tests use a temporary SQLite DB (never the live data/workflows.db). A
"restart" is simulated by closing the engine's store and constructing a NEW
WorkflowEngine over the SAME db_path.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from jarvis_core.workflow_engine import (
    ConcurrencyError,
    InvalidTransition,
    TerminalWorkflowError,
    WorkflowEngine,
    WorkflowNotFound,
)
from jarvis_core.workflow_models import ActionStatus, EventType
from jarvis_core.workflow_states import WorkflowState
from jarvis_core.workflow_store import SCHEMA_VERSION, WorkflowStore


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_workflows.db"


@pytest.fixture()
def engine(db_path):
    eng = WorkflowEngine(db_path=db_path)
    try:
        yield eng
    finally:
        eng.close()


def _restart(engine, db_path) -> WorkflowEngine:
    """Simulate a Jarvis restart: close and reopen a fresh engine on same DB."""
    engine.close()
    return WorkflowEngine(db_path=db_path)


def _advance_to(engine, wf_id, target: WorkflowState):
    """Drive a fresh IDLE workflow forward along the happy path to *target*."""
    path = [
        WorkflowState.INITIALIZING,
        WorkflowState.GPT_BOOTSTRAPPING,
        WorkflowState.WAITING_FOR_COMMAND,
        WorkflowState.GPT_RUNNING,
        WorkflowState.GPT_OUTPUT_READY,
        WorkflowState.WAITING_FOR_APPROVAL,
        WorkflowState.KIRO_STARTING,
        WorkflowState.KIRO_RUNNING,
        WorkflowState.KIRO_RESULT_READY,
        WorkflowState.GPT_REVIEWING,
    ]
    for state in path:
        engine.transition(wf_id, state)
        if state == target:
            return
    return


# 1. create workflow
def test_create_workflow(engine):
    wf = engine.create_workflow(project_name="Alpha")
    assert wf.workflow_id
    assert wf.state == WorkflowState.IDLE.value
    assert wf.project_name == "Alpha"
    assert wf.version == 0


# 2. workflow persists to DB
def test_workflow_persists_to_db(engine, db_path):
    wf = engine.create_workflow(project_name="Alpha")
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT workflow_id, state FROM workflows WHERE workflow_id=?",
            (wf.workflow_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == wf.workflow_id
    assert row[1] == WorkflowState.IDLE.value


# 3. reload workflow after new service instance
def test_reload_after_new_service_instance(engine, db_path):
    wf = engine.create_workflow(project_name="Alpha")
    engine.transition(wf.workflow_id, WorkflowState.INITIALIZING)
    eng2 = _restart(engine, db_path)
    try:
        reloaded = eng2.get_workflow(wf.workflow_id)
        assert reloaded is not None
        assert reloaded.state == WorkflowState.INITIALIZING.value
    finally:
        eng2.close()


# 4. valid state transition succeeds
def test_valid_transition_succeeds(engine):
    wf = engine.create_workflow(project_name="Alpha")
    result = engine.transition(wf.workflow_id, WorkflowState.INITIALIZING)
    assert result.state == WorkflowState.INITIALIZING.value
    assert result.previous_state == WorkflowState.IDLE.value
    assert result.started_at is not None


# 5. invalid state transition rejected
def test_invalid_transition_rejected(engine):
    wf = engine.create_workflow(project_name="Alpha")
    with pytest.raises(InvalidTransition):
        engine.transition(wf.workflow_id, WorkflowState.KIRO_RUNNING)
    # State unchanged and persisted.
    assert engine.get_workflow(wf.workflow_id).state == WorkflowState.IDLE.value


# 6. terminal COMPLETED cannot transition
def test_completed_cannot_transition(engine):
    wf = engine.create_workflow(project_name="Alpha")
    _advance_to(engine, wf.workflow_id, WorkflowState.WAITING_FOR_APPROVAL)
    engine.complete(wf.workflow_id)
    with pytest.raises(TerminalWorkflowError):
        engine.transition(wf.workflow_id, WorkflowState.GPT_RUNNING)


# 7. terminal CANCELLED cannot transition
def test_cancelled_cannot_transition(engine):
    wf = engine.create_workflow(project_name="Alpha")
    engine.transition(wf.workflow_id, WorkflowState.INITIALIZING)
    engine.cancel(wf.workflow_id, reason="user abort")
    with pytest.raises(TerminalWorkflowError):
        engine.transition(wf.workflow_id, WorkflowState.GPT_BOOTSTRAPPING)


# 8. pause stores resume state
def test_pause_stores_resume_state(engine):
    wf = engine.create_workflow(project_name="Alpha")
    _advance_to(engine, wf.workflow_id, WorkflowState.WAITING_FOR_COMMAND)
    paused = engine.pause(wf.workflow_id, reason="waiting on user")
    assert paused.state == WorkflowState.PAUSED.value
    assert paused.resume_state == WorkflowState.WAITING_FOR_COMMAND.value
    assert paused.pause_reason == "waiting on user"


# 9. resume returns to exact state
def test_resume_returns_to_exact_state(engine):
    wf = engine.create_workflow(project_name="Alpha")
    _advance_to(engine, wf.workflow_id, WorkflowState.GPT_RUNNING)
    engine.pause(wf.workflow_id)
    resumed = engine.resume(wf.workflow_id)
    assert resumed.state == WorkflowState.GPT_RUNNING.value
    assert resumed.resume_state is None
    assert resumed.pause_reason is None


# 10. failed workflow stores error
def test_failed_workflow_stores_error(engine):
    wf = engine.create_workflow(project_name="Alpha")
    _advance_to(engine, wf.workflow_id, WorkflowState.GPT_RUNNING)
    failed = engine.fail(wf.workflow_id, error="boom", error_code="E123")
    assert failed.state == WorkflowState.FAILED.value
    assert failed.last_error == "boom"
    assert failed.last_error_code == "E123"
    assert failed.previous_state == WorkflowState.GPT_RUNNING.value


# 11. retry only allowed appropriately
def test_retry_only_allowed_appropriately(engine):
    wf = engine.create_workflow(project_name="Alpha")
    # Cannot retry a non-failed workflow.
    with pytest.raises(InvalidTransition):
        engine.retry(wf.workflow_id)
    _advance_to(engine, wf.workflow_id, WorkflowState.GPT_RUNNING)
    engine.fail(wf.workflow_id, error="boom")
    # Retry defaults to the failed-from state (GPT_RUNNING is retryable).
    retried = engine.retry(wf.workflow_id)
    assert retried.state == WorkflowState.GPT_RUNNING.value
    assert retried.last_error is None
    # An invalid retry target is rejected.
    engine.fail(wf.workflow_id, error="again")
    with pytest.raises(InvalidTransition):
        engine.retry(wf.workflow_id, WorkflowState.COMPLETED)


# 12. cancel persists
def test_cancel_persists(engine, db_path):
    wf = engine.create_workflow(project_name="Alpha")
    engine.transition(wf.workflow_id, WorkflowState.INITIALIZING)
    engine.cancel(wf.workflow_id, reason="stop")
    eng2 = _restart(engine, db_path)
    try:
        reloaded = eng2.get_workflow(wf.workflow_id)
        assert reloaded.state == WorkflowState.CANCELLED.value
        assert reloaded.cancel_reason == "stop"
        assert reloaded.completed_at is not None
    finally:
        eng2.close()


# 13. events written in correct order
def test_events_written_in_correct_order(engine):
    wf = engine.create_workflow(project_name="Alpha")
    engine.transition(wf.workflow_id, WorkflowState.INITIALIZING)
    engine.transition(wf.workflow_id, WorkflowState.GPT_BOOTSTRAPPING)
    events = engine.get_events(wf.workflow_id)
    types = [e.event_type for e in events]
    assert types[0] == EventType.WORKFLOW_CREATED.value
    assert types[1] == EventType.STATE_TRANSITION.value
    assert types[2] == EventType.STATE_TRANSITION.value
    seqs = [e.sequence for e in events]
    assert seqs == sorted(seqs)
    assert events[1].from_state == WorkflowState.IDLE.value
    assert events[1].to_state == WorkflowState.INITIALIZING.value


# 14. iteration persists
def test_iteration_persists(engine, db_path):
    wf = engine.create_workflow(project_name="Alpha")
    engine.update_workflow_context(wf.workflow_id, iteration_number=7)
    eng2 = _restart(engine, db_path)
    try:
        assert eng2.get_workflow(wf.workflow_id).iteration_number == 7
    finally:
        eng2.close()


# 15. GPT future fields persist
def test_gpt_future_fields_persist(engine, db_path):
    wf = engine.create_workflow(project_name="Alpha")
    engine.update_workflow_context(
        wf.workflow_id,
        master_gpt_prompt="do the thing",
        chatgpt_conversation_url="https://chatgpt.com/c/abc",
        chatgpt_tab_id="tab-42",
        chatgpt_window_hwnd=99887,
        latest_gpt_output="result text",
    )
    eng2 = _restart(engine, db_path)
    try:
        r = eng2.get_workflow(wf.workflow_id)
        assert r.master_gpt_prompt == "do the thing"
        assert r.chatgpt_conversation_url == "https://chatgpt.com/c/abc"
        assert r.chatgpt_tab_id == "tab-42"
        assert r.chatgpt_window_hwnd == 99887
        assert r.latest_gpt_output == "result text"
    finally:
        eng2.close()


# 16. Kiro future fields persist
def test_kiro_future_fields_persist(engine, db_path):
    wf = engine.create_workflow(project_name="Alpha")
    engine.update_workflow_context(
        wf.workflow_id,
        kiro_session_id="sess-1",
        kiro_project_path="C:/some/proj",
        latest_kiro_result="applied 3 files",
    )
    eng2 = _restart(engine, db_path)
    try:
        r = eng2.get_workflow(wf.workflow_id)
        assert r.kiro_session_id == "sess-1"
        assert r.kiro_project_path == "C:/some/proj"
        assert r.latest_kiro_result == "applied 3 files"
    finally:
        eng2.close()


# 17. action begin persists
def test_action_begin_persists(engine, db_path):
    wf = engine.create_workflow(project_name="Alpha")
    started, action = engine.begin_action(
        wf.workflow_id, "act-1", action_type="send_prompt"
    )
    assert started is True
    assert action.status == ActionStatus.RUNNING.value
    eng2 = _restart(engine, db_path)
    try:
        stored = eng2.get_action("act-1")
        assert stored is not None
        assert stored.status == ActionStatus.RUNNING.value
        assert eng2.get_workflow(wf.workflow_id).current_action_id == "act-1"
    finally:
        eng2.close()


# 18. duplicate begin blocked
def test_duplicate_begin_blocked(engine):
    wf = engine.create_workflow(project_name="Alpha")
    started1, _ = engine.begin_action(wf.workflow_id, "act-1")
    started2, existing = engine.begin_action(wf.workflow_id, "act-1")
    assert started1 is True
    assert started2 is False
    assert existing.action_id == "act-1"


# 19. completed action remains completed after restart
def test_completed_action_remains_completed_after_restart(engine, db_path):
    wf = engine.create_workflow(project_name="Alpha")
    engine.begin_action(wf.workflow_id, "act-1", action_type="send_prompt")
    engine.complete_action(wf.workflow_id, "act-1", result_ref="ref-xyz")
    eng2 = _restart(engine, db_path)
    try:
        stored = eng2.get_action("act-1")
        assert stored.status == ActionStatus.COMPLETED.value
        assert stored.result_ref == "ref-xyz"
        # A fresh begin after restart must be refused (no duplicate side effect).
        started, existing = eng2.begin_action(wf.workflow_id, "act-1")
        assert started is False
        assert existing.status == ActionStatus.COMPLETED.value
        assert eng2.get_workflow(wf.workflow_id).last_completed_action_id == "act-1"
    finally:
        eng2.close()


# 20. failed action recoverable
def test_failed_action_recoverable(engine, db_path):
    wf = engine.create_workflow(project_name="Alpha")
    engine.begin_action(wf.workflow_id, "act-1", action_type="send_prompt")
    engine.fail_action(wf.workflow_id, "act-1", error="net down", error_code="NET")
    eng2 = _restart(engine, db_path)
    try:
        stored = eng2.get_action("act-1")
        assert stored.status == ActionStatus.FAILED.value
        assert stored.error == "net down"
        assert stored.error_code == "NET"
        # current_action_id cleared so recovery is not blocked.
        assert eng2.get_workflow(wf.workflow_id).current_action_id is None
    finally:
        eng2.close()


# 21. two concurrent transition attempts cannot corrupt state
def test_concurrent_transitions_cannot_corrupt(engine, db_path):
    wf = engine.create_workflow(project_name="Alpha")
    engine.transition(wf.workflow_id, WorkflowState.INITIALIZING)

    # Two independent engines over the SAME DB read the SAME version, then both
    # try to transition. Optimistic concurrency must let exactly one win.
    eng_a = WorkflowEngine(db_path=db_path)
    eng_b = WorkflowEngine(db_path=db_path)
    results = {}
    barrier = threading.Barrier(2)

    def attempt(name, eng, target):
        barrier.wait()
        try:
            eng.transition(wf.workflow_id, target)
            results[name] = "ok"
        except (ConcurrencyError, InvalidTransition) as exc:
            results[name] = type(exc).__name__

    # Both attempt the same legal next state from INITIALIZING.
    ta = threading.Thread(
        target=attempt, args=("a", eng_a, WorkflowState.GPT_BOOTSTRAPPING)
    )
    tb = threading.Thread(
        target=attempt, args=("b", eng_b, WorkflowState.GPT_BOOTSTRAPPING)
    )
    ta.start(); tb.start(); ta.join(); tb.join()
    eng_a.close(); eng_b.close()

    # State must be exactly GPT_BOOTSTRAPPING with a single clean transition,
    # never corrupted. At least one attempt must have been rejected OR the DB
    # serialized them; either way the final state is consistent and version
    # advanced by exactly one transition.
    eng_c = WorkflowEngine(db_path=db_path)
    try:
        final = eng_c.get_workflow(wf.workflow_id)
        assert final.state == WorkflowState.GPT_BOOTSTRAPPING.value
        # version: create(0) -> INITIALIZING(1) -> GPT_BOOTSTRAPPING(2).
        assert final.version == 2
        events = eng_c.get_events(wf.workflow_id)
        transitions_to_boot = [
            e for e in events
            if e.to_state == WorkflowState.GPT_BOOTSTRAPPING.value
        ]
        assert len(transitions_to_boot) == 1
    finally:
        eng_c.close()


# 22. restore_active_workflows excludes terminal workflows
def test_restore_active_excludes_terminal(engine, db_path):
    active = engine.create_workflow(project_name="Active")
    engine.transition(active.workflow_id, WorkflowState.INITIALIZING)

    done = engine.create_workflow(project_name="Done")
    _advance_to(engine, done.workflow_id, WorkflowState.WAITING_FOR_APPROVAL)
    engine.complete(done.workflow_id)

    killed = engine.create_workflow(project_name="Killed")
    engine.transition(killed.workflow_id, WorkflowState.INITIALIZING)
    engine.cancel(killed.workflow_id)

    eng2 = _restart(engine, db_path)
    try:
        restored = eng2.restore_active_workflows()
        ids = {w.workflow_id for w in restored}
        assert active.workflow_id in ids
        assert done.workflow_id not in ids
        assert killed.workflow_id not in ids
    finally:
        eng2.close()


# 23. recovery plan correct
def test_recovery_plan_correct(engine):
    # Paused workflow → plan says resume.
    wf = engine.create_workflow(project_name="Alpha")
    _advance_to(engine, wf.workflow_id, WorkflowState.WAITING_FOR_COMMAND)
    engine.pause(wf.workflow_id, reason="hold")
    plan = engine.get_recovery_plan(wf.workflow_id)
    assert plan.current_state == WorkflowState.PAUSED.value
    assert plan.resume_state == WorkflowState.WAITING_FOR_COMMAND.value
    assert not plan.is_terminal
    assert "resume" in plan.safe_next_step.lower()

    # Workflow with a RUNNING action → plan flags the pending action.
    wf2 = engine.create_workflow(project_name="Beta")
    _advance_to(engine, wf2.workflow_id, WorkflowState.GPT_RUNNING)
    engine.begin_action(wf2.workflow_id, "act-9", action_type="send_prompt")
    plan2 = engine.get_recovery_plan(wf2.workflow_id)
    assert plan2.pending_action_id == "act-9"
    assert plan2.pending_action_type == "send_prompt"
    assert "do not blindly re-run" in plan2.safe_next_step.lower()

    # Terminal → plan says terminal, no next step.
    wf3 = engine.create_workflow(project_name="Gamma")
    engine.transition(wf3.workflow_id, WorkflowState.INITIALIZING)
    engine.cancel(wf3.workflow_id)
    plan3 = engine.get_recovery_plan(wf3.workflow_id)
    assert plan3.is_terminal
    assert "terminal" in plan3.safe_next_step.lower()


# 24. SQLite/schema migration/version initializes safely
def test_schema_migration_version(db_path):
    store = WorkflowStore(db_path=db_path)
    try:
        assert store._user_version() == SCHEMA_VERSION
        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert {"workflows", "workflow_events", "workflow_actions",
                    "workflow_metadata", "schema_migrations"} <= tables
            mig = conn.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
            assert (SCHEMA_VERSION,) in mig
        finally:
            conn.close()
    finally:
        store.close()
    # Re-opening the same DB must not re-run migrations or corrupt anything.
    store2 = WorkflowStore(db_path=db_path)
    try:
        assert store2._user_version() == SCHEMA_VERSION
    finally:
        store2.close()


# 25. no secret data accidentally logged/persisted by normal operations
def test_no_secret_metadata_persisted(engine, db_path):
    wf = engine.create_workflow(project_name="Alpha")
    # Try to sneak secrets into event metadata via an action.
    engine.begin_action(
        wf.workflow_id,
        "act-1",
        action_type="send_prompt",
        metadata={"api_key": "SECRET123", "password": "hunter2", "safe": "ok"},
    )
    action = engine.get_action("act-1")
    assert "api_key" not in action.metadata
    assert "password" not in action.metadata
    assert action.metadata.get("safe") == "ok"

    # Verify no secret string leaked into the raw events/actions tables.
    conn = sqlite3.connect(str(db_path))
    try:
        blobs = conn.execute(
            "SELECT metadata FROM workflow_actions"
        ).fetchall()
        blobs += conn.execute("SELECT metadata FROM workflow_events").fetchall()
        joined = " ".join(str(b[0]) for b in blobs)
        assert "SECRET123" not in joined
        assert "hunter2" not in joined
    finally:
        conn.close()


# Extra: workflow-not-found is a clean error, not a crash.
def test_unknown_workflow_raises(engine):
    with pytest.raises(WorkflowNotFound):
        engine.transition("does-not-exist", WorkflowState.INITIALIZING)
