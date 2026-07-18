"""FastAPI routes for Gmail monitoring.

All endpoints are safe and do not expose sensitive data.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jarvis_core.gmail_models import GmailConfig
from jarvis_core.gmail_monitor import (
    check_now,
    get_config,
    get_events_today,
    get_state,
    load_config,
    start_monitor,
    stop_monitor,
    update_config,
)
from jarvis_core.jd_saver import list_saved_jd_files
from jarvis_core.draft_writer import list_drafts

logger = logging.getLogger("jarvis.gmail_routes")

router = APIRouter(prefix="/gmail", tags=["gmail"])


class ConfigUpdateRequest(BaseModel):
    """Request body for updating Gmail config."""
    poll_interval_minutes: int = 5
    draft_only: bool = True
    auto_send_enabled: bool = False
    allowed_auto_send_categories: list[str] = []
    allowed_senders: list[str] = []


class SendDraftRequest(BaseModel):
    """Request body for sending a draft."""
    draft_path: str = ""
    confirm_send: bool = False


# ── Status & Control ─────────────────────────────────────────────────────


@router.get("/status")
def gmail_status():
    """Get current Gmail monitor status."""
    state = get_state()
    return {
        "status": "ok",
        "running": state.running,
        "connected": state.connected,
        "last_checked": state.last_checked,
        "important_mails_today": state.important_mails_today,
        "jd_files_saved_today": state.jd_files_saved_today,
        "drafts_created_today": state.drafts_created_today,
        "error_message": state.error_message,
        "poll_interval_minutes": state.poll_interval_minutes,
        "draft_only": state.draft_only,
    }


@router.post("/start")
def gmail_start():
    """Start the Gmail monitor."""
    result = start_monitor()
    return result


@router.post("/stop")
def gmail_stop():
    """Stop the Gmail monitor."""
    result = stop_monitor()
    return result


@router.post("/check-now")
def gmail_check_now():
    """Trigger an immediate email check."""
    result = check_now()
    return result


# ── Events & Data ────────────────────────────────────────────────────────


@router.get("/events")
def gmail_events():
    """Get today's logged events."""
    events = get_events_today()
    return {"status": "ok", "count": len(events), "events": events}


@router.get("/jd-files")
def gmail_jd_files():
    """List saved JD files."""
    files = list_saved_jd_files()
    return {"status": "ok", "count": len(files), "files": files}


@router.get("/drafts")
def gmail_drafts():
    """List saved draft files."""
    drafts = list_drafts()
    return {"status": "ok", "count": len(drafts), "drafts": drafts}


# ── Configuration ────────────────────────────────────────────────────────


@router.get("/config")
def gmail_get_config():
    """Get current Gmail monitor configuration."""
    config = get_config()
    return {
        "status": "ok",
        "config": {
            "enabled": config.enabled,
            "poll_interval_minutes": config.poll_interval_minutes,
            "draft_only": config.draft_only,
            "auto_send_enabled": config.auto_send_enabled,
            "allowed_auto_send_categories": config.allowed_auto_send_categories,
            "allowed_senders": config.allowed_senders,
            "save_jd_folder": config.save_jd_folder,
            "log_folder": config.log_folder,
        },
    }


@router.post("/config")
def gmail_update_config(req: ConfigUpdateRequest):
    """Update Gmail monitor configuration."""
    config = get_config()
    config.poll_interval_minutes = req.poll_interval_minutes
    config.draft_only = req.draft_only
    config.auto_send_enabled = req.auto_send_enabled
    config.allowed_auto_send_categories = req.allowed_auto_send_categories
    config.allowed_senders = req.allowed_senders

    # Validate poll interval
    valid_intervals = [1, 5, 10, 15, 30]
    if config.poll_interval_minutes not in valid_intervals:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid poll interval. Must be one of: {valid_intervals}",
        )

    result = update_config(config)
    return result


# ── Protected: Send Draft ────────────────────────────────────────────────


@router.post("/send-draft")
def gmail_send_draft(req: SendDraftRequest):
    """Send a draft email.

    This is a protected endpoint. Requires confirm_send=true.
    Does not send if confirm_send is missing or false.
    """
    if not req.confirm_send:
        raise HTTPException(
            status_code=400,
            detail="Confirmation required. Set confirm_send=true to send.",
        )

    if not req.draft_path:
        raise HTTPException(
            status_code=400,
            detail="draft_path is required.",
        )

    draft_path = Path(req.draft_path)
    if not draft_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Draft file not found.",
        )

    # Read the draft file
    try:
        with open(draft_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read draft: {e}",
        )

    # Parse the draft
    to_line = ""
    subject_line = ""
    body_lines = []
    in_body = False
    for line in content.split("\n"):
        if line.startswith("To: "):
            to_line = line[4:]
        elif line.startswith("Subject: "):
            subject_line = line[9:]
        elif line.startswith("=" * 60):
            if not in_body:
                in_body = True
                continue
            else:
                break
        elif in_body:
            body_lines.append(line)

    if not to_line or not subject_line:
        raise HTTPException(
            status_code=400,
            detail="Invalid draft format.",
        )

    body = "\n".join(body_lines).strip()

    # Send via Gmail API
    from jarvis_core.gmail_service import send_email

    success = send_email(to=to_line, subject=subject_line, body=body)
    if not success:
        raise HTTPException(
            status_code=500,
            detail="Failed to send email. Check Gmail connection.",
        )

    return {"status": "ok", "message": "Email sent successfully."}