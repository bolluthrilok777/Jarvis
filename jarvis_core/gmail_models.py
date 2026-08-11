"""Pydantic models for Gmail monitoring."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class GmailConfig(BaseModel):
    """Configuration for Gmail monitoring."""

    enabled: bool = False
    poll_interval_minutes: int = 5
    draft_only: bool = True
    auto_send_enabled: bool = False
    allowed_auto_send_categories: list[str] = Field(default_factory=list)
    allowed_senders: list[str] = Field(default_factory=list)
    scan_period: str = "today"
    save_jd_folder: str = "C:\\Jarvis\\jobs_jd"
    log_folder: str = "C:\\Jarvis\\notes"

    @classmethod
    def load(cls, path: Path) -> GmailConfig:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls(**data)
        return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.model_dump(), f, indent=2)


class GmailEvent(BaseModel):
    """A single Gmail event record for the log."""

    detected_at: str = ""
    email_date: str = ""
    from_: str = Field(default="", alias="from")
    subject: str = ""
    category: str = ""
    importance: str = "low"
    summary: str = ""
    action_required: str = ""
    candidate: str = ""
    company: str = ""
    attachments_saved: list[str] = Field(default_factory=list)
    jd_saved_path: str = ""
    draft_reply_path: str = ""
    gmail_message_id: str = ""
    gmail_thread_id: str = ""
    gmail_draft_id: str = ""

    class Config:
        populate_by_name = True

    def to_log_line(self) -> str:
        return json.dumps(self.model_dump(by_alias=True), default=str)


class ClassificationResult(BaseModel):
    """Output from the AI/deterministic classifier."""

    is_job_related: bool = False
    category: str = "unknown"
    importance: str = "low"
    summary: str = ""
    action_required: str = ""
    should_save_jd: bool = False
    should_create_draft: bool = False
    draft_type: Optional[str] = None  # jd_received|interview|documents|offer|unknown


class DraftContent(BaseModel):
    """Content for a draft reply."""

    to: str = ""
    subject: str = ""
    body: str = ""
    draft_type: str = "unknown"


class GmailMessagePreview(BaseModel):
    """Privacy-minimized email preview for classification."""

    message_id: str = ""
    thread_id: str = ""
    sender: str = ""
    subject: str = ""
    snippet: str = ""
    date: str = ""
    attachment_filenames: list[str] = Field(default_factory=list)
    body_preview: str = ""

    @field_validator("body_preview", mode="before")
    @classmethod
    def truncate_body_preview(cls, v):
        if v and isinstance(v, str) and len(v) > 500:
            return v[:500]
        return v


class GmailMonitorState(BaseModel):
    """Current state of the Gmail monitor."""

    running: bool = False
    connected: bool = False
    last_checked: Optional[str] = None
    important_mails_today: int = 0
    jd_files_saved_today: int = 0
    drafts_created_today: int = 0      # Only real Gmail API drafts
    local_drafts_today: int = 0        # Only local .txt backup drafts
    last_poll_at: Optional[str] = None          # ISO timestamp of last poll attempt
    last_poll_success_at: Optional[str] = None  # ISO timestamp of last successful poll
    error_message: str = ""
    poll_interval_minutes: int = 5
    draft_only: bool = True
    # New fields for Gmail Connect UI
    credentials_configured: bool = False
    token_configured: bool = False
    account_email: Optional[str] = None
    monitoring_active: bool = False
    auto_send_enabled: bool = False
