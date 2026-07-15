"""
jarvis_core/task_manager.py — Phase 5A-1 Task Manager

Manages the multi-task queue with concurrency control, state transitions,
and CRUD operations for tasks.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from jarvis_core.task_models import Task, TaskStatus, TaskPublic

logger = logging.getLogger("jarvis.task_manager")

# ── Concurrency limits ────────────────────────────────────────────────────────
MAX_BACKGROUND_TASKS = 2
MAX_BROWSER_TASKS = 2
MAX_DESKTOP_FOCUS_TASKS = 1


class TaskManager:
    """Thread-safe task queue with concurrency-aware scheduling."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        self._running_counts: dict[str, int] = {
            "background": 0,
            "browser": 0,
            "desktop_focus": 0,
        }

    # ── Task CRUD ──────────────────────────────────────────────────────────────

    def create_task(self, original_input: str) -> Task:
        """Create a new task in 'queued' state."""
        if not original_input or not original_input.strip():
            raise ValueError("original_input must be a non-empty string")
        task = Task(original_input=original_input.strip())
        with self._lock:
            self._tasks[task.task_id] = task
        logger.info("Task %s created: %s", task.task_id, original_input[:60])
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def get_all_tasks(self, include_hidden: bool = False) -> list[Task]:
        with self._lock:
            tasks = list(self._tasks.values())
        if not include_hidden:
            tasks = [t for t in tasks if not t.hidden]
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def get_public_tasks(self, include_hidden: bool = False) -> list[dict]:
        return [
            TaskPublic.from_task(t).model_dump()
            for t in self.get_all_tasks(include_hidden=include_hidden)
        ]

    def get_public_task(self, task_id: str) -> Optional[dict]:
        task = self.get_task(task_id)
        if task is None:
            return None
        return TaskPublic.from_task(task).model_dump()

    def hide_task(self, task_id: str) -> bool:
        """Mark a task as hidden (removed from default view)."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task.hidden = True
        return True

    def clear_completed(self) -> int:
        """Hide all completed/failed/cancelled tasks. Returns count hidden."""
        count = 0
        with self._lock:
            for task in self._tasks.values():
                if task.status in (
                    TaskStatus.completed.value,
                    TaskStatus.failed.value,
                    TaskStatus.cancelled.value,
                    TaskStatus.partially_completed.value,
                ):
                    task.hidden = True
                    count += 1
        return count

    # ── State transitions ──────────────────────────────────────────────────────

    def set_status(self, task_id: str, status: str) -> bool:
        """Set task status. Returns False if task not found."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task.status = status
            if status == TaskStatus.running.value:
                task.started_at = datetime.now(timezone.utc)
            elif status in (
                TaskStatus.completed.value,
                TaskStatus.failed.value,
                TaskStatus.cancelled.value,
                TaskStatus.partially_completed.value,
            ):
                task.completed_at = datetime.now(timezone.utc)
        return True

    def set_interpreted_command(self, task_id: str, command: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task.interpreted_command = command
        return True

    def set_risk_level(self, task_id: str, risk_level: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task.risk_level = risk_level
        return True

    def set_concurrency_group(self, task_id: str, group: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task.concurrency_group = group
        return True

    def set_result(self, task_id: str, result: dict) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task.result = result
        return True

    def set_error(self, task_id: str, error: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task.error = error
        return True

    # ── Concurrency control ────────────────────────────────────────────────────

    def can_run(self, concurrency_group: str) -> bool:
        """Check if a task in the given group can run now."""
        with self._lock:
            if concurrency_group == "desktop_focus":
                return self._running_counts["desktop_focus"] < MAX_DESKTOP_FOCUS_TASKS
            if concurrency_group == "browser":
                return self._running_counts["browser"] < MAX_BROWSER_TASKS
            return self._running_counts["background"] < MAX_BACKGROUND_TASKS

    def acquire_slot(self, concurrency_group: str) -> bool:
        """Try to acquire a concurrency slot. Returns True if acquired."""
        with self._lock:
            if concurrency_group == "desktop_focus":
                if self._running_counts["desktop_focus"] >= MAX_DESKTOP_FOCUS_TASKS:
                    return False
                self._running_counts["desktop_focus"] += 1
            elif concurrency_group == "browser":
                if self._running_counts["browser"] >= MAX_BROWSER_TASKS:
                    return False
                self._running_counts["browser"] += 1
            else:
                if self._running_counts["background"] >= MAX_BACKGROUND_TASKS:
                    return False
                self._running_counts["background"] += 1
            return True

    def release_slot(self, concurrency_group: str) -> None:
        """Release a concurrency slot."""
        with self._lock:
            if concurrency_group == "desktop_focus":
                self._running_counts["desktop_focus"] = max(0, self._running_counts["desktop_focus"] - 1)
            elif concurrency_group == "browser":
                self._running_counts["browser"] = max(0, self._running_counts["browser"] - 1)
            else:
                self._running_counts["background"] = max(0, self._running_counts["background"] - 1)

    def get_running_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._running_counts)

    def get_queued_tasks(self) -> list[Task]:
        """Get all tasks in 'queued' state, in FIFO order."""
        with self._lock:
            return [
                t for t in sorted(self._tasks.values(), key=lambda x: x.created_at)
                if t.status == TaskStatus.queued.value
            ]

    def get_waiting_tasks(self) -> list[Task]:
        """Get all tasks in 'waiting_for_confirmation' state."""
        with self._lock:
            return [
                t for t in self._tasks.values()
                if t.status == TaskStatus.waiting_for_confirmation.value
            ]


# Module-level singleton
task_manager = TaskManager()