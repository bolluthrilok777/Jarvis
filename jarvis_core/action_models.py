"""
jarvis_core/action_models.py — Phase 4.5 Action Engine Pydantic Models
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, field_validator


class ActionStep(BaseModel):
    skill_id: str
    parameters: dict = {}
    description: str = ""
    requires_confirmation: bool = False


class ActionPlan(BaseModel):
    steps: list[ActionStep]
    raw_command: str
    normalized_command: str
    confidence: float
    clarification_needed: bool = False
    clarification_prompt: Optional[str] = None

    @field_validator("confidence")
    @classmethod
    def _check_confidence(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"confidence must be between 0.0 and 1.0, got {v}")
        return v


class ActionResult(BaseModel):
    step: ActionStep
    success: bool
    output: dict = {}
    error: Optional[str] = None
    verified: bool = False


class ExecutionReport(BaseModel):
    plan: ActionPlan
    results: list[ActionResult]
    summary: str
    all_succeeded: bool
