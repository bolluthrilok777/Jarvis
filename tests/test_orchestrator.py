"""
tests/test_orchestrator.py — Phase 5A-1 Orchestrator Tests

Tests for orchestrator submission, intent routing, risk classification,
confirmation flow, cancellation, and safety validation.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from jarvis_core.orchestrator import Orchestrator, _validate_plan, _is_safe_command, _is_critical_command, _is_protected_command
from jarvis_core.task_models import TaskStatus


@pytest.fixture
def orch():
    """Fresh Orchestrator for each test."""
    return Orchestrator()


# ── Submit ────────────────────────────────────────────────────────────────────

def test_submit_empty_input(orch):
    """Empty input returns error."""
    result = orch.submit("")
    assert result["action"] == "error"
    assert result["status"] == "error"

    result = orch.submit("   ")
    assert result["action"] == "error"


def test_submit_conversational(orch):
    """Conversational queries route to AI chat."""
    with patch("jarvis_core.orchestrator.classify", return_value="conversational_query"):
        result = orch.submit("Who are you?")
    assert result["action"] == "chat"
    assert result["status"] == "chat"


def test_submit_safe_command(orch):
    """Safe commands create a queued task."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        with patch("jarvis_core.orchestrator.classify_risk", return_value="SAFE"):
            with patch("jarvis_core.orchestrator.classify_concurrency", return_value="background"):
                with patch("jarvis_core.orchestrator.build_interpreted_command", return_value="get system status"):
                    result = orch.submit("system status")
    assert result["action"] == "created"
    assert result["status"] == "queued"
    assert result["task_id"] is not None


def test_submit_protected_command(orch):
    """Protected commands wait for confirmation."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        with patch("jarvis_core.orchestrator.classify_risk", return_value="PROTECTED"):
            with patch("jarvis_core.orchestrator.classify_concurrency", return_value="background"):
                with patch("jarvis_core.orchestrator.build_interpreted_command", return_value="delete file"):
                    result = orch.submit("delete file")
    assert result["action"] == "created"
    assert result["status"] == "waiting_for_confirmation"


def test_submit_critical_command(orch):
    """Critical commands are blocked immediately."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        result = orch.submit("format disk")
    assert result["action"] == "blocked"
    assert result["status"] == "blocked"


def test_submit_creates_task_with_interpreted(orch):
    """Submitted command gets interpreted command stored."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        with patch("jarvis_core.orchestrator.classify_risk", return_value="SAFE"):
            with patch("jarvis_core.orchestrator.classify_concurrency", return_value="background"):
                with patch("jarvis_core.orchestrator.build_interpreted_command", return_value="open the calculator"):
                    result = orch.submit("open calculator")
    assert result["task_id"] is not None
    from jarvis_core.task_manager import task_manager
    task = task_manager.get_task(result["task_id"])
    assert task is not None
    assert task.interpreted_command == "open the calculator"


# ── Confirm ───────────────────────────────────────────────────────────────────

def test_confirm_nonexistent(orch):
    """Confirming a nonexistent task returns error."""
    result = orch.confirm("nonexistent")
    assert result["status"] == "error"


def test_confirm_not_waiting(orch):
    """Confirming a task not in waiting state returns error."""
    from jarvis_core.task_manager import task_manager
    task = task_manager.create_task("test")
    task_manager.set_status(task.task_id, TaskStatus.queued.value)
    result = orch.confirm(task.task_id)
    assert result["status"] == "error"


def test_confirm_success(orch):
    """Confirming a waiting task queues it."""
    from jarvis_core.task_manager import task_manager
    task = task_manager.create_task("delete file")
    task_manager.set_status(task.task_id, TaskStatus.waiting_for_confirmation.value)
    result = orch.confirm(task.task_id)
    assert result["status"] == "queued"
    updated = task_manager.get_task(task.task_id)
    assert updated.status == TaskStatus.queued.value


# ── Cancel ────────────────────────────────────────────────────────────────────

def test_cancel_nonexistent(orch):
    """Cancelling a nonexistent task returns error."""
    result = orch.cancel("nonexistent")
    assert result["status"] == "error"


def test_cancel_queued(orch):
    """Cancelling a queued task works."""
    from jarvis_core.task_manager import task_manager
    task = task_manager.create_task("test")
    result = orch.cancel(task.task_id)
    assert result["status"] == "cancelled"
    updated = task_manager.get_task(task.task_id)
    assert updated.status == TaskStatus.cancelled.value


def test_cancel_completed(orch):
    """Cancelling a completed task returns error."""
    from jarvis_core.task_manager import task_manager
    task = task_manager.create_task("test")
    task_manager.set_status(task.task_id, TaskStatus.completed.value)
    result = orch.cancel(task.task_id)
    assert result["status"] == "error"


# ── Submit Multiple ───────────────────────────────────────────────────────────

def test_submit_multiple(orch):
    """Multiple lines create separate tasks."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        with patch("jarvis_core.orchestrator.classify_risk", return_value="SAFE"):
            with patch("jarvis_core.orchestrator.classify_concurrency", return_value="background"):
                with patch("jarvis_core.orchestrator.build_interpreted_command", side_effect=lambda x: x):
                    results = orch.submit_multiple(["system status", "open calculator", ""])
    assert len(results) == 2  # blank line skipped
    assert results[0]["action"] == "created"
    assert results[1]["action"] == "created"
    assert results[0]["task_id"] != results[1]["task_id"]


def test_submit_multiple_all_blank(orch):
    """All blank lines returns empty list."""
    results = orch.submit_multiple(["", "  ", ""])
    assert results == []


# ── Safety Helpers ────────────────────────────────────────────────────────────

def test_is_safe_command():
    """Safe commands return True."""
    assert _is_safe_command("system status")
    assert _is_safe_command("open calculator")
    assert not _is_safe_command("delete file")
    assert not _is_safe_command("format disk")


def test_is_critical_command():
    """Critical commands are identified."""
    assert _is_critical_command("format disk")
    assert _is_critical_command("shutdown now")
    assert not _is_critical_command("system status")


def test_is_protected_command():
    """Protected commands are identified."""
    assert _is_protected_command("delete file")
    assert _is_protected_command("shutdown")
    assert not _is_protected_command("system status")


def test_validate_plan_valid():
    """Valid plan passes validation."""
    plan = {"steps": [{"action": "system status"}, {"action": "open calculator"}]}
    assert _validate_plan(plan)


def test_validate_plan_invalid_pattern():
    """Plan with unsafe pattern is rejected."""
    plan = {"steps": [{"action": "subprocess.run('rm -rf /')"}]}
    assert not _validate_plan(plan)


def test_validate_plan_critical():
    """Plan with critical action is rejected."""
    plan = {"steps": [{"action": "format disk"}]}
    assert not _validate_plan(plan)


def test_validate_plan_not_dict():
    """Non-dict plan is rejected."""
    assert not _validate_plan("not a dict")
    assert not _validate_plan(None)
    assert not _validate_plan(42)


def test_validate_plan_no_steps():
    """Plan without steps is rejected."""
    assert not _validate_plan({"not_steps": []})
    assert not _validate_plan({})


def test_validate_plan_bad_step():
    """Plan with non-dict step is rejected."""
    assert not _validate_plan({"steps": ["not a dict"]})


# ── Duplicate Submission ──────────────────────────────────────────────────────

def test_duplicate_submission_not_blocked_at_orchestrator(orch):
    """Orchestrator does not block duplicate text (handled at route level)."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        with patch("jarvis_core.orchestrator.classify_risk", return_value="SAFE"):
            with patch("jarvis_core.orchestrator.classify_concurrency", return_value="background"):
                with patch("jarvis_core.orchestrator.build_interpreted_command", return_value="system status"):
                    r1 = orch.submit("system status")
                    r2 = orch.submit("system status")
    assert r1["task_id"] != r2["task_id"]