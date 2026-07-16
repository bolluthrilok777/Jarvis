from pydantic import BaseModel
from typing import Optional


class ChatRequest(BaseModel):
    message: str
    mode: str = "light"


class CommandRequest(BaseModel):
    command: str
    mode: str = "light"


class ConfirmRequest(BaseModel):
    action_id: str
    confirm: bool


# ── Phase 5A-1 Task Models ────────────────────────────────────────────────────


class TaskSubmitRequest(BaseModel):
    """Request to submit one or more command lines."""
    text: str


class TaskConfirmRequest(BaseModel):
    """Request to confirm or cancel a task."""
    confirm: bool


class MonitoringRequest(BaseModel):
    start: str = "09:00"
    end: str = "18:30"
    interval: int = 15
    sound: str = "soft"


class ActivityEntryRequest(BaseModel):
    entry: str
