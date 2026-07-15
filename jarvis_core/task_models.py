"""
jarvis_core/task_models.py — Phase 5A-1 Task Models

Pydantic models for the multi-task queue.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from jarvis_core.action_models import ActionPlan


class TaskStatus(str, Enum):
    queued = "queued"
    interpreting = "interpreting"
    waiting_for_confirmation = "waiting_for_confirmation"
    running = "running"
    completed = "completed"
    partially_completed = "partially_completed"
    failed = "failed"
    cancelled = "cancelled"


class RiskLevel(str, Enum):
    SAFE = "SAFE"
    PROTECTED = "PROTECTED"
    CRITICAL = "CRITICAL"


class ConcurrencyGroup(str, Enum):
    background = "background"
    browser = "browser"
    desktop_focus = "desktop_focus"


class Task(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    original_input: str
    interpreted_command: str = ""
    action_plan: Optional[ActionPlan] = None
    risk_level: str = RiskLevel.SAFE.value
    concurrency_group: str = ConcurrencyGroup.background.value
    status: str = TaskStatus.queued.value
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    hidden: bool = False   # True when user removes card from view


class TaskPublic(BaseModel):
    """Safe public view of a Task — no raw ActionPlan JSON exposed to UI."""
    task_id: str
    original_input: str
    interpreted_command: str
    risk_level: str
    concurrency_group: str
    status: str
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result_summary: Optional[str] = None
    step_results: Optional[list[dict]] = None
    error: Optional[str] = None
    hidden: bool

    @classmethod
    def from_task(cls, task: Task) -> "TaskPublic":
        result_summary = None
        step_results = None
        if task.result:
            result_summary = task.result.get("summary")
            raw_steps = task.result.get("results", [])
            step_results = [
                {
                    "description": (
                        s.get("step", {}).get("description")
                        or s.get("step", {}).get("skill_id", "")
                    ),
                    "success": s.get("success", False),
                    "output_summary": _summarize_output(s.get("output", {})),
                    "error": s.get("error"),
                }
                for s in raw_steps
            ]
        return cls(
            task_id=task.task_id,
            original_input=task.original_input,
            interpreted_command=task.interpreted_command,
            risk_level=task.risk_level,
            concurrency_group=task.concurrency_group,
            status=task.status,
            created_at=task.created_at.isoformat() if task.created_at else "",
            started_at=task.started_at.isoformat() if task.started_at else None,
            completed_at=task.completed_at.isoformat() if task.completed_at else None,
            result_summary=result_summary,
            step_results=step_results,
            error=task.error,
            hidden=task.hidden,
        )


def _summarize_output(output: dict) -> str:
    """Convert a step output dict into a short human-readable string."""
    if not output:
        return ""
    if "result" in output:
        return str(output["result"])
    if "message" in output:
        return str(output["message"])
    if "reply" in output:
        return str(output["reply"])
    if "files" in output:
        return f"Found {len(output['files'])} file(s)"
    if "url" in output:
        return str(output["url"])
    if "file" in output:
        return str(output["file"])
    return output.get("status", "")
