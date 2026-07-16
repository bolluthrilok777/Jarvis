"""
tests/test_task_manager.py — Phase 5A-1 Task Manager Tests

Tests for task creation, state transitions, concurrency control, and task listing.
All tests use the module directly — no HTTP, no mocks needed for core logic.
"""
from __future__ import annotations

import pytest

from jarvis_core.task_manager import TaskManager
from jarvis_core.task_models import TaskStatus, RiskLevel


def test_calculator_output_summary_reports_gui_success():
    from jarvis_core.task_models import _summarize_output
    assert _summarize_output({"result": 884, "gui_ok": True}) == (
        "884 — Calculator GUI updated"
    )


def test_calculator_output_summary_reports_gui_warning():
    from jarvis_core.task_models import _summarize_output
    summary = _summarize_output({
        "result": 884, "gui_ok": False,
        "warning": "Calculator GUI automation failed: Calculator window did not receive focus",
    })
    assert summary.startswith("884 — Calculator GUI automation failed:")


@pytest.fixture
def mgr():
    """Fresh TaskManager for each test."""
    return TaskManager()


# ── Task Creation ─────────────────────────────────────────────────────────────

def test_create_task(mgr):
    """Creating a task returns a Task with queued status."""
    task = mgr.create_task("system status")
    assert task.task_id is not None
    assert task.original_input == "system status"
    assert task.status == TaskStatus.queued.value


def test_create_task_empty_input(mgr):
    """Empty input raises ValueError."""
    with pytest.raises(ValueError):
        mgr.create_task("")
    with pytest.raises(ValueError):
        mgr.create_task("   ")


def test_create_task_unique_ids(mgr):
    """Each task gets a unique ID."""
    t1 = mgr.create_task("task one")
    t2 = mgr.create_task("task two")
    assert t1.task_id != t2.task_id


# ── State Transitions ─────────────────────────────────────────────────────────

def test_set_status(mgr):
    """Setting status updates the task."""
    task = mgr.create_task("test")
    mgr.set_status(task.task_id, TaskStatus.running.value)
    updated = mgr.get_task(task.task_id)
    assert updated.status == TaskStatus.running.value


def test_set_status_nonexistent(mgr):
    """Setting status on a nonexistent task does not raise."""
    mgr.set_status("nonexistent", TaskStatus.running.value)  # should not raise


def test_set_error(mgr):
    """Setting error stores the error message."""
    task = mgr.create_task("test")
    mgr.set_error(task.task_id, "Something went wrong")
    updated = mgr.get_task(task.task_id)
    assert updated.error == "Something went wrong"


def test_set_result(mgr):
    """Setting result stores the result dict."""
    task = mgr.create_task("test")
    result = {"status": "done", "value": 42}
    mgr.set_result(task.task_id, result)
    updated = mgr.get_task(task.task_id)
    assert updated.result == result


def test_set_risk_level(mgr):
    """Setting risk level stores it."""
    task = mgr.create_task("test")
    mgr.set_risk_level(task.task_id, RiskLevel.PROTECTED.value)
    updated = mgr.get_task(task.task_id)
    assert updated.risk_level == RiskLevel.PROTECTED.value


def test_set_interpreted_command(mgr):
    """Setting interpreted command stores it."""
    task = mgr.create_task("open calculator")
    mgr.set_interpreted_command(task.task_id, "open the calculator application")
    updated = mgr.get_task(task.task_id)
    assert updated.interpreted_command == "open the calculator application"


def test_set_concurrency_group(mgr):
    """Setting concurrency group stores it."""
    task = mgr.create_task("test")
    mgr.set_concurrency_group(task.task_id, "background")
    updated = mgr.get_task(task.task_id)
    assert updated.concurrency_group == "background"


# ── Task State Transitions ────────────────────────────────────────────────────

def test_full_lifecycle(mgr):
    """Task goes through queued → running → completed."""
    task = mgr.create_task("system status")
    assert task.status == TaskStatus.queued.value

    mgr.set_status(task.task_id, TaskStatus.running.value)
    assert mgr.get_task(task.task_id).status == TaskStatus.running.value

    mgr.set_status(task.task_id, TaskStatus.completed.value)
    assert mgr.get_task(task.task_id).status == TaskStatus.completed.value


def test_failed_state(mgr):
    """Task can transition to failed."""
    task = mgr.create_task("test")
    mgr.set_status(task.task_id, TaskStatus.failed.value)
    mgr.set_error(task.task_id, "Execution error")
    updated = mgr.get_task(task.task_id)
    assert updated.status == TaskStatus.failed.value
    assert updated.error == "Execution error"


def test_cancelled_state(mgr):
    """Task can transition to cancelled."""
    task = mgr.create_task("test")
    mgr.set_status(task.task_id, TaskStatus.cancelled.value)
    assert mgr.get_task(task.task_id).status == TaskStatus.cancelled.value


def test_waiting_for_confirmation(mgr):
    """Task can be set to waiting_for_confirmation."""
    task = mgr.create_task("delete file")
    mgr.set_status(task.task_id, TaskStatus.waiting_for_confirmation.value)
    assert mgr.get_task(task.task_id).status == TaskStatus.waiting_for_confirmation.value


# ── Task Listing ──────────────────────────────────────────────────────────────

def test_get_public_tasks(mgr):
    """get_public_tasks returns TaskPublic dicts."""
    mgr.create_task("task one")
    mgr.create_task("task two")
    tasks = mgr.get_public_tasks()
    assert len(tasks) == 2
    for t in tasks:
        assert "task_id" in t
        assert "original_input" in t
        assert "status" in t


def test_get_public_task(mgr):
    """get_public_task returns a single task dict."""
    task = mgr.create_task("test")
    public = mgr.get_public_task(task.task_id)
    assert public is not None
    assert public["task_id"] == task.task_id
    assert public["original_input"] == "test"


def test_get_public_task_nonexistent(mgr):
    """get_public_task returns None for nonexistent task."""
    assert mgr.get_public_task("nonexistent") is None


def test_get_task_nonexistent(mgr):
    """get_task returns None for nonexistent task."""
    assert mgr.get_task("nonexistent") is None


# ── Clear Completed ───────────────────────────────────────────────────────────

def test_clear_completed(mgr):
    """clear_completed hides completed/failed/cancelled tasks."""
    t1 = mgr.create_task("task one")
    t2 = mgr.create_task("task two")
    t3 = mgr.create_task("task three")

    mgr.set_status(t1.task_id, TaskStatus.completed.value)
    mgr.set_status(t2.task_id, TaskStatus.failed.value)
    # t3 stays queued

    count = mgr.clear_completed()
    assert count == 2
    assert len(mgr.get_public_tasks()) == 1


def test_clear_completed_no_tasks(mgr):
    """clear_completed with no completed tasks returns 0."""
    mgr.create_task("task one")
    count = mgr.clear_completed()
    assert count == 0


# ── Concurrency Control ───────────────────────────────────────────────────────

def test_acquire_release_slot(mgr):
    """acquire_slot returns True and release_slot frees it."""
    assert mgr.acquire_slot("background")
    mgr.release_slot("background")


def test_max_background_slots(mgr):
    """Maximum 2 background tasks."""
    assert mgr.acquire_slot("background")
    assert mgr.acquire_slot("background")
    assert not mgr.acquire_slot("background")  # third should fail

    mgr.release_slot("background")
    assert mgr.acquire_slot("background")  # should succeed after release


def test_max_browser_slots(mgr):
    """Maximum 2 browser tasks."""
    assert mgr.acquire_slot("browser")
    assert mgr.acquire_slot("browser")
    assert not mgr.acquire_slot("browser")

    mgr.release_slot("browser")
    assert mgr.acquire_slot("browser")


def test_max_desktop_slots(mgr):
    """Maximum 1 desktop-focus task."""
    assert mgr.acquire_slot("desktop_focus")
    assert not mgr.acquire_slot("desktop_focus")

    mgr.release_slot("desktop_focus")
    assert mgr.acquire_slot("desktop_focus")


def test_release_nonexistent_slot(mgr):
    """Releasing a slot that was never acquired does not raise."""
    mgr.release_slot("background")  # should not raise


def test_different_groups_independent(mgr):
    """Different concurrency groups do not block each other."""
    assert mgr.acquire_slot("background")
    assert mgr.acquire_slot("background")
    assert mgr.acquire_slot("browser")
    assert mgr.acquire_slot("browser")
    assert mgr.acquire_slot("desktop_focus")
    # All groups at max — no more slots
    assert not mgr.acquire_slot("background")
    assert not mgr.acquire_slot("browser")
    assert not mgr.acquire_slot("desktop_focus")


# ── Duplicate Submission ──────────────────────────────────────────────────────

def test_duplicate_submission_blocked(mgr):
    """Submitting the same text twice creates two separate tasks (not blocked at manager level)."""
    t1 = mgr.create_task("system status")
    t2 = mgr.create_task("system status")
    assert t1.task_id != t2.task_id
    # Duplicate blocking is handled at the orchestrator/route level
