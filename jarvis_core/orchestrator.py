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
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._active_lock = threading.Lock()
        self._active_tasks: set[str] = set()

    def submit(
        self,
        original_input: str,
        *,
        source: str = "Jarvis",
        track_conversation: bool = False,
        conversation_id: str | None = None,
    ) -> dict:
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

        # 1. Normalize first so classification, display, and execution all use
        # the same conservatively reframed command while preserving the original.
        from jarvis_core.normalizer import normalize
        normalized = normalize(original_input)
        interpreted = normalized if isinstance(normalized, str) and normalized else original_input.strip()
        interpreted = build_interpreted_command(interpreted)
        # Semantic Gmail understanding only selects a deterministic canonical
        # Gmail command. Real data still comes exclusively from Gmail skills.
        from jarvis_core.gmail_intent import gmail_intent_router

        gmail_resolution = gmail_intent_router.resolve(
            original_input,
            conversation_id=conversation_id,
        )
        if gmail_resolution is not None:
            interpreted = gmail_resolution.command
        intent = classify(interpreted, original_input)
        logger.info("Intent for '%s': %s", original_input[:60], intent)

        # 2. Conversational HTTP/UI requests keep their established /chat flow.
        # Remote integrations may opt into a tracked conversation task so the
        # same request is visible in LIVE TASKS and completes through the worker.
        if intent == "conversational_query":
            if track_conversation:
                task = task_manager.create_task(
                    original_input,
                    source=source,
                    task_type="conversation",
                )
                task_manager.set_status(task.task_id, TaskStatus.interpreting.value)
                task_manager.set_interpreted_command(task.task_id, interpreted)
                from jarvis_core.model_router import router

                decision = router.decide(interpreted, purpose="chat")
                task_manager.set_routing(task.task_id, decision.public())
                task_manager.add_event(
                    task.task_id,
                    "routed",
                    decision.label,
                    reason=decision.reason,
                )
                task_manager.set_status(task.task_id, TaskStatus.queued.value)
                self.wake_worker()
                return {
                    "task_id": task.task_id,
                    "status": "queued",
                    "message": "Conversation queued for Jarvis.",
                    "action": "created",
                }
            return {
                "task_id": None,
                "status": "chat",
                "message": "Routed to AI chat",
                "action": "chat",
            }

        # 3. It's an executable command — create a task
        task = task_manager.create_task(original_input, source=source)
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
        task_manager.set_interpreted_command(task.task_id, interpreted)

        # Publish the same routing decision that execution will use.  A rule
        # match is a real direct-tool path, so no Ollama model is claimed.
        from jarvis_core.rule_matcher import matcher
        from jarvis_core.model_router import router
        if matcher.match(interpreted, original_input) is not None:
            decision = router.record_direct("task", "Matched a verified Jarvis tool rule.")
        else:
            decision = router.decide(interpreted, purpose="planner")
        task_manager.set_routing(task.task_id, decision.public())
        task_manager.add_event(task.task_id, "routed", decision.label, reason=decision.reason)

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
            # Wake the worker to process this task
            self.wake_worker()
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

        task_manager.set_confirmed(task_id, True)
        task_manager.add_event(task_id, "confirmed", "Action confirmed")
        task_manager.set_status(task_id, TaskStatus.queued.value)
        # Wake the worker to process this confirmed task
        self.wake_worker()
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
            task_manager.add_event(task_id, "executing", "Executing task")

            # Use the established Jarvis conversational or action execution flow.
            if task.task_type == "conversation":
                from jarvis_core.conversation_service import run_conversation

                result = run_conversation(task.original_input)
            else:
                cmd_req = CommandRequest(command=task.interpreted_command or task.original_input)
                if task.confirmed:
                    result = action_engine.run(cmd_req, confirmed=True)
                else:
                    result = action_engine.run(cmd_req)
            if result.get("routing"):
                task_manager.set_routing(task_id, result["routing"])
                task_manager.add_event(task_id, "routing", result["routing"].get("label", "Route selected"), reason=result["routing"].get("reason", ""))

            if result.get("status") == "confirmation_required":
                # The action engine can discover a protected skill after the
                # initial risk pass. Keep that task in the same task-level
                # confirmation flow rather than treating the safety gate as a
                # failure or executing it automatically.
                action_id = result.get("action_id")
                if action_id:
                    from jarvis_core.command_dispatcher import pending_actions

                    pending_actions.pop(action_id, None)
                task_manager.set_result(task_id, result)
                task_manager.set_status(
                    task_id,
                    TaskStatus.waiting_for_confirmation.value,
                )
                task_manager.add_event(
                    task_id,
                    "confirmation",
                    "Waiting for confirmation",
                )
                return {
                    "status": TaskStatus.waiting_for_confirmation.value,
                    "message": result.get("message") or "Confirmation required.",
                }

            if result.get("status") in ("done", "partial"):
                plan_data = result.get("plan")
                if isinstance(plan_data, dict):
                    try:
                        from jarvis_core.action_models import ActionPlan
                        reframed = build_interpreted_command(ActionPlan.model_validate(plan_data))
                        task_manager.set_interpreted_command(task_id, reframed)
                    except Exception:
                        logger.warning("Could not build display reframe for task %s", task_id)
                # Verify calculator results actually contain numeric value before marking completed
                if self._has_calculator_step(result) and not self._has_calculator_result(result):
                    task_manager.set_status(task_id, TaskStatus.failed.value)
                    error_msg = "Calculator did not produce a numeric result"
                    task_manager.set_error(task_id, error_msg)
                    task_manager.set_result(task_id, result)
                    return {"status": "failed", "error": error_msg}
                final_status = (
                    TaskStatus.partially_completed.value
                    if result.get("status") == "partial"
                    else TaskStatus.completed.value
                )
                task_manager.set_status(task_id, final_status)
                task_manager.set_result(task_id, result)
                task_manager.add_event(task_id, "completed", "Task execution completed")
                return {"status": final_status, "result": result}
            else:
                task_manager.set_status(task_id, TaskStatus.failed.value)
                failure_message = self._failure_message(result)
                task_manager.set_error(task_id, failure_message)
                task_manager.set_result(task_id, result)
                task_manager.add_event(task_id, "failed", failure_message)
                return {"status": "failed", "error": failure_message}
        except Exception as e:
            logger.exception("Error executing task %s", task_id)
            task_manager.set_status(task_id, TaskStatus.failed.value)
            task_manager.set_error(task_id, str(e))
            return {"status": "failed", "error": str(e)}
        finally:
            task_manager.release_slot(concurrency_group)

    def _has_calculator_step(self, result: dict) -> bool:
        """Check if the result contains a calculator.calculate step."""
        for step_result in result.get("results", []):
            step = step_result.get("step", {})
            if isinstance(step, dict) and step.get("skill_id") == "calculator.calculate":
                return True
            if isinstance(step, str) and "calculator" in step.lower():
                return True
        return False

    def _has_calculator_result(self, result: dict) -> bool:
        """Check if the calculator step produced a numeric result."""
        import math
        for step_result in result.get("results", []):
            step = step_result.get("step", {})
            if isinstance(step, dict) and step.get("skill_id") == "calculator.calculate":
                output = step_result.get("output", {})
                if isinstance(output, dict):
                    val = output.get("result") or output.get("value")
                    if val is not None:
                        try:
                            return math.isfinite(float(val))
                        except (TypeError, ValueError):
                            return False
                return False
        return False

    @staticmethod
    def _failure_message(result: dict) -> str:
        """Prefer the concrete failed-step message over a generic status."""
        if result.get("message"):
            return str(result["message"])
        for step_result in result.get("results", []):
            if step_result.get("success"):
                continue
            if step_result.get("error"):
                return str(step_result["error"])
            output = step_result.get("output", {})
            if isinstance(output, dict) and output.get("message"):
                return str(output["message"])
        return "Execution failed"

    def submit_multiple(
        self,
        lines: list[str],
        *,
        source: str = "Jarvis",
        track_conversation: bool = False,
        conversation_id: str | None = None,
    ) -> list[dict]:
        """Submit multiple command lines as separate tasks."""
        results = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            result = self.submit(
                stripped,
                source=source,
                track_conversation=track_conversation,
                conversation_id=conversation_id,
            )
            results.append(result)
        return results

    # ── Background Worker ──────────────────────────────────────────────────────

    def start_worker(self) -> None:
        """Start the background task-processing worker thread."""
        if self._worker_thread is not None and self._worker_thread.is_alive():
            logger.warning("Worker thread is already running")
            return
        self._stop_event.clear()
        self._wake_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="task-worker",
            daemon=True,
        )
        self._worker_thread.start()
        logger.info("Task worker thread started")

    def stop_worker(self, timeout: float = 5.0) -> None:
        """Signal the worker to stop and wait for it to finish."""
        self._stop_event.set()
        self._wake_event.set()  # Wake up so it can exit
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)
            if self._worker_thread.is_alive():
                logger.warning("Worker thread did not stop within %s seconds", timeout)
            else:
                logger.info("Task worker thread stopped")
        self._worker_thread = None

    def _execute_in_worker(self, task_id: str) -> None:
        try:
            self.execute_task(task_id)
        finally:
            with self._active_lock:
                self._active_tasks.discard(task_id)
            self._wake_event.set()

    def wake_worker(self) -> None:
        """Wake the worker thread to check for new tasks."""
        self._wake_event.set()

    def _worker_loop(self) -> None:
        """Main worker loop: poll for queued tasks and execute them."""
        logger.info("Task worker loop started")
        while not self._stop_event.is_set():
            try:
                # Get all queued tasks
                queued = task_manager.get_queued_tasks()
                processed_any = False

                for task in queued:
                    if self._stop_event.is_set():
                        break
                    # Re-check status under lock to avoid race conditions
                    current = task_manager.get_task(task.task_id)
                    if current is None or current.status != TaskStatus.queued.value:
                        continue
                    with self._active_lock:
                        if task.task_id in self._active_tasks:
                            continue
                        if not task_manager.can_run(task.concurrency_group or "background"):
                            continue
                        self._active_tasks.add(task.task_id)
                    logger.info("Worker dispatching task %s: %s", task.task_id, task.original_input[:60])
                    threading.Thread(
                        target=self._execute_in_worker,
                        args=(task.task_id,),
                        name=f"task-{task.task_id[:8]}",
                        daemon=True,
                    ).start()
                    processed_any = True

                if not processed_any:
                    # No tasks to process — wait for wake signal or timeout
                    self._wake_event.wait(timeout=2.0)
                    self._wake_event.clear()
            except Exception:
                logger.exception("Unhandled error in task worker loop")
                # Sleep briefly to avoid tight loop on persistent errors
                self._wake_event.wait(timeout=1.0)
                self._wake_event.clear()
        logger.info("Task worker loop exited")


# Module-level singleton
orchestrator = Orchestrator()
