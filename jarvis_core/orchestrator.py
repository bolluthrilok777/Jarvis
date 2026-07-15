"""
jarvis_core/orchestrator.py — Phase 5A-1 Orchestrator

Interprets user input, classifies intent and risk, manages task lifecycle,
and dispatches to the action engine or AI chat as appropriate.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from jarvis_core.intent_classifier import classify
from jarvis_core.risk_classifier import classify_risk, classify_concurrency, build_interpreted_command
from jarvis_core.task_manager import task_manager
from jarvis_core.task_models import TaskStatus, RiskLevel
from jarvis_core.action_engine import engine as action_engine
from jarvis_core.models import CommandRequest

logger = logging.getLogger("jarvis.orchestrator")

# ── Safety: never-execute patterns ────────────────────────────────────────────
NEVER_EXECUTE_PATTERNS = [
    "subprocess.run",
    "subprocess.Popen",
    "os.system",
    "os.popen",
    "shutil.rmtree",
    "pathlib.Path.unlink",
    "pathlib.Path.rmdir",
    "__import__",
    "eval(",
    "exec(",
    "compile(",
    "pickle.load",
    "pickle.loads",
    "marshal.load",
    "marshal.loads",
    "shelve.open",
    "webbrowser.open",
    "ctypes.",
    "win32api.",
    "pyautogui.",
    "keyboard.",
    "mouse.",
]

# ── Protected action keywords ─────────────────────────────────────────────────
PROTECTED_KEYWORDS = [
    "delete", "remove", "uninstall", "format", "erase",
    "shutdown", "restart", "reboot", "poweroff",
    "send email", "send mail", "payment", "pay", "purchase",
    "submit", "book", "reserve", "order",
    "server change", "production", "deploy",
    "kill", "terminate", "stop process",
    "modify registry", "change system",
]

# ── Critical action keywords ──────────────────────────────────────────────────
CRITICAL_KEYWORDS = [
    "format disk", "format drive", "format c:",
    "delete system", "delete windows",
    "shutdown now", "restart now",
    "factory reset", "wipe",
    "install driver", "install software",
    "change password", "reset password",
    "disable antivirus", "disable firewall",
    "modify boot", "modify uefi", "modify bios",
]


def _contains_any(text: str, keywords: list[str]) -> bool:
    """Check if text contains any of the given keywords."""
    lower = text.lower()
    return any(kw in lower for kw in keywords)


def _has_never_execute_pattern(command: str) -> bool:
    """Check if a command contains patterns that should never be executed."""
    lower = command.lower()
    return any(p in lower for p in NEVER_EXECUTE_PATTERNS)


def _is_safe_command(command: str) -> bool:
    """Check if a command is safe to auto-execute (no protected/critical keywords)."""
    return not _contains_any(command, PROTECTED_KEYWORDS + CRITICAL_KEYWORDS)


def _is_critical_command(command: str) -> bool:
    """Check if a command is critical (blocked or requires strong confirmation)."""
    return _contains_any(command, CRITICAL_KEYWORDS)


def _is_protected_command(command: str) -> bool:
    """Check if a command is protected (requires confirmation)."""
    return _contains_any(command, PROTECTED_KEYWORDS)


def _validate_plan(plan: dict) -> bool:
    """Validate that an AI-generated plan does not contain unsafe operations."""
    if not isinstance(plan, dict):
        return False
    if "steps" not in plan:
        return False
    steps = plan.get("steps", [])
    if not isinstance(steps, list):
        return False
    for step in steps:
        if not isinstance(step, dict):
            return False
        action = step.get("action", "")
        if _has_never_execute_pattern(action):
            logger.warning("Rejected plan step with unsafe pattern: %s", action)
            return False
        if _is_critical_command(action):
            logger.warning("Rejected plan step with critical action: %s", action)
            return False
    return True


class Orchestrator:
    """Orchestrates task lifecycle from submission to completion."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def submit(self, original_input: str) -> dict:
        """
        Submit a single command line.

        Returns a dict with:
          - task_id (str or None)
          - status (str)
          - message (str)
          - action (str): "created", "chat", "blocked", "error"
        """
        if not original_input or not original_input.strip():
            return {"task_id": None, "status": "error", "message": "Empty input", "action": "error"}

        # 1. Classify intent
        intent = classify(original_input)
        logger.info("Intent for '%s': %s", original_input[:60], intent)

        # 2. If conversational, route to AI chat
        if intent == "conversational_query":
            return {
                "task_id": None,
                "status": "chat",
                "message": "Routed to AI chat",
                "action": "chat",
            }

        # 3. It's an executable command — create a task
        task = task_manager.create_task(original_input)
        task_manager.set_status(task.task_id, TaskStatus.interpreting.value)

        # 4. Check for critical commands (block immediately)
        if _is_critical_command(original_input):
            task_manager.set_status(task.task_id, TaskStatus.failed.value)
            task_manager.set_error(task.task_id, "Critical action blocked for safety")
            task_manager.set_risk_level(task.task_id, RiskLevel.CRITICAL.value)
            return {
                "task_id": task.task_id,
                "status": "blocked",
                "message": "This action is blocked for safety reasons.",
                "action": "blocked",
            }

        # 5. Build interpreted command
        interpreted = build_interpreted_command(original_input)
        task_manager.set_interpreted_command(task.task_id, interpreted)

        # 6. Classify risk
        risk = classify_risk(original_input)
        task_manager.set_risk_level(task.task_id, risk)

        # 7. Classify concurrency group
        concurrency_group = classify_concurrency(original_input)
        task_manager.set_concurrency_group(task.task_id, concurrency_group)

        # 8. Determine if auto-execute or needs confirmation
        if risk == RiskLevel.SAFE.value:
            # Auto-execute
            task_manager.set_status(task.task_id, TaskStatus.queued.value)
            return {
                "task_id": task.task_id,
                "status": "queued",
                "message": "Task queued for execution.",
                "action": "created",
            }
        elif risk == RiskLevel.PROTECTED.value:
            # Needs confirmation
            task_manager.set_status(task.task_id, TaskStatus.waiting_for_confirmation.value)
            return {
                "task_id": task.task_id,
                "status": "waiting_for_confirmation",
                "message": f"This action requires confirmation: {interpreted}",
                "action": "created",
            }
        else:
            # Unknown risk — treat as protected
            task_manager.set_status(task.task_id, TaskStatus.waiting_for_confirmation.value)
            return {
                "task_id": task.task_id,
                "status": "waiting_for_confirmation",
                "message": f"Please confirm this action: {interpreted}",
                "action": "created",
            }

    def confirm(self, task_id: str) -> dict:
        """Confirm a task that is waiting for confirmation."""
        task = task_manager.get_task(task_id)
        if task is None:
            return {"status": "error", "message": "Task not found"}
        if task.status != TaskStatus.waiting_for_confirmation.value:
            return {"status": "error", "message": f"Task is in state '{task.status}', not waiting for confirmation"}

        task_manager.set_status(task_id, TaskStatus.queued.value)
        return {"status": "queued", "message": "Task confirmed and queued for execution."}

    def cancel(self, task_id: str) -> dict:
        """Cancel a task."""
        task = task_manager.get_task(task_id)
        if task is None:
            return {"status": "error", "message": "Task not found"}
        if task.status in (
            TaskStatus.completed.value,
            TaskStatus.failed.value,
            TaskStatus.cancelled.value,
        ):
            return {"status": "error", "message": f"Task is already in terminal state '{task.status}'"}

        task_manager.set_status(task_id, TaskStatus.cancelled.value)
        # Release concurrency slot if running
        if task.concurrency_group:
            task_manager.release_slot(task.concurrency_group)
        return {"status": "cancelled", "message": "Task cancelled."}

    def execute_task(self, task_id: str) -> dict:
        """
        Execute a queued task. This is called by the scheduler.
        Returns result dict.
        """
        task = task_manager.get_task(task_id)
        if task is None:
            return {"status": "error", "message": "Task not found"}
        if task.status != TaskStatus.queued.value:
            return {"status": "error", "message": f"Task is in state '{task.status}', not queued"}

        concurrency_group = task.concurrency_group or "background"

        # Try to acquire a concurrency slot
        if not task_manager.acquire_slot(concurrency_group):
            return {"status": "error", "message": "No concurrency slot available"}

        try:
            task_manager.set_status(task_id, TaskStatus.running.value)

            # Use action engine to execute
            cmd_req = CommandRequest(command=task.interpreted_command or task.original_input)
            result = action_engine.run(cmd_req)

            if result.get("status") in ("done", "partial"):
                task_manager.set_status(task_id, TaskStatus.completed.value)
                task_manager.set_result(task_id, result)
                return {"status": "completed", "result": result}
            else:
                task_manager.set_status(task_id, TaskStatus.failed.value)
                task_manager.set_error(task_id, result.get("message", "Execution failed"))
                task_manager.set_result(task_id, result)
                return {"status": "failed", "error": result.get("message", "Execution failed")}
        except Exception as e:
            logger.exception("Error executing task %s", task_id)
            task_manager.set_status(task_id, TaskStatus.failed.value)
            task_manager.set_error(task_id, str(e))
            return {"status": "failed", "error": str(e)}
        finally:
            task_manager.release_slot(concurrency_group)

    def submit_multiple(self, lines: list[str]) -> list[dict]:
        """Submit multiple command lines as separate tasks."""
        results = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            result = self.submit(stripped)
            results.append(result)
        return results


# Module-level singleton
orchestrator = Orchestrator()