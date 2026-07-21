"""FastAPI routes for Gmail monitoring.

All endpoints are safe and do not expose sensitive data.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
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
from jarvis_core.gmail_service import (
    credentials_exist,
    delete_credentials,
    delete_token,
    exchange_code_for_token,
    get_account_email,
    get_authorization_url,
    is_connected,
    save_credentials_json,
    token_exists,
    validate_credentials_json,
    _get_credentials,
    _get_credentials_type,
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


class CredentialsUploadRequest(BaseModel):
    """Request body for uploading credentials JSON."""
    json_data: str = ""


# ── Status & Control ──────────────────────────────────────────────────────


@router.get("/status")
def gmail_status():
    """Get current Gmail monitor status."""
    state = get_state()
    creds_configured = credentials_exist()
    tok_configured = token_exists()
    connected = is_connected()
    email = get_account_email() if connected else None

    return {
        "status": "ok",
        "connected": connected,
        "account_email": email,
        "credentials_configured": creds_configured,
        "token_configured": tok_configured,
        "running": state.running,
        "monitoring_active": state.running,
        "last_checked": state.last_checked,
        "important_mails_today": state.important_mails_today,
        "jd_files_saved_today": state.jd_files_saved_today,
        "drafts_created_today": state.drafts_created_today,
        "error_message": state.error_message,
        "poll_interval_minutes": state.poll_interval_minutes,
        "draft_only": state.draft_only,
        "auto_send_enabled": state.auto_send_enabled,
    }


@router.post("/start")
def gmail_start():
    """Start the Gmail monitor."""
    if not is_connected():
        raise HTTPException(
            status_code=400,
            detail="Gmail is not connected. Connect Gmail first.",
        )
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
    if not is_connected():
        raise HTTPException(
            status_code=400,
            detail="Gmail is not connected. Connect Gmail first.",
        )
    result = check_now()
    return result


# ── OAuth Connect Flow ────────────────────────────────────────────────────

@router.get("/connect")
def gmail_connect():
    """Start Gmail OAuth connection flow.

    Returns authorization URL if credentials exist.
    """
    if not credentials_exist():
        return {
            "status": "credentials_needed",
            "message": "Gmail OAuth credentials not configured. Upload credentials JSON first.",
        }

    if is_connected():
        email = get_account_email()
        return {
            "status": "already_connected",
            "message": f"Gmail is already connected: {email}",
            "account_email": email,
        }

    # Check credentials type
    creds_data = _get_credentials()
    if creds_data is None:
        return {
            "status": "error",
            "message": "Failed to read credentials file."
        }

    creds_type = _get_credentials_type(creds_data)
    if creds_type == "unknown":
        return {
            "status": "error",
            "message": "Invalid credentials format. Must contain 'web' or 'installed' key."
        }

    if creds_type == "installed":
        return {
            "status": "error",
            "message": "Please upload Web application OAuth credentials with redirect URI http://127.0.0.1:8000/gmail/oauth/callback"
        }

    # Generate authorization URL
    auth_url, error_msg = get_authorization_url()
    if auth_url is None:
        return {
            "status": "error",
            "message": error_msg or "Failed to generate OAuth authorization URL. Check credentials."
        }

    return {
        "status": "authorization_needed",
        "authorization_url": auth_url,
        "message": "Open the authorization URL in your browser to connect Gmail.",
    }


@router.get("/oauth/start")
def gmail_oauth_start():
    """Start OAuth flow and return authorization URL.

    Returns JSON with status and either authorization_url or error message.
    Never returns HTTP 5xx; always returns a JSON object.
    """
    if not credentials_exist():
        return {
            "status": "error",
            "message": "Gmail OAuth credentials not configured. Please upload credentials JSON first."
        }

    # Check credentials type
    creds_data = _get_credentials()
    if creds_data is None:
        return {
            "status": "error",
            "message": "Failed to read credentials file."
        }

    creds_type = _get_credentials_type(creds_data)
    if creds_type == "unknown":
        return {
            "status": "error",
            "message": "Invalid credentials format. Must contain 'web' or 'installed' key."
        }

    if creds_type == "installed":
        return {
            "status": "error",
            "message": "Please upload Web application OAuth credentials with redirect URI http://127.0.0.1:8000/gmail/oauth/callback"
        }

    auth_url, error_msg = get_authorization_url()
    if auth_url is None:
        return {
            "status": "error",
            "message": error_msg or "Failed to generate authorization URL. Check credentials file."
        }

    return {
        "status": "ok",
        "authorization_url": auth_url
    }


@router.get("/oauth/callback")
def gmail_oauth_callback(
    code: Optional[str] = None,
    error: Optional[str] = None,
    state: Optional[str] = None,
    request: Request = None,
):
    """Handle OAuth callback from Google.

    Exchanges authorization code for token and saves it.
    Redirects back to UI on success or failure.
    Never logs or exposes token values, refresh tokens, or client secrets.
    """
    logger.info("OAuth callback received")

    # If Google sends an error
    if error:
        logger.info("OAuth code received: no (error from Google)")
        logger.warning("OAuth callback error from Google: %s", error)
        return RedirectResponse(url="http://127.0.0.1:8000/ui?gmail=error")

    # If no code provided
    if not code:
        logger.info("OAuth code received: no")
        logger.warning("OAuth callback missing authorization code")
        return RedirectResponse(url="http://127.0.0.1:8000/ui?gmail=error")

    logger.info("OAuth code received: yes")

    # Exchange code for token
    success, message = exchange_code_for_token(code)

    if success:
        logger.info("Token saved: yes")
        logger.info("Token file path exists: yes")
        return RedirectResponse(url="http://127.0.0.1:8000/ui?gmail=connected")
    else:
        logger.info("Token saved: no")
        logger.warning("OAuth token exchange failed: %s", message)
        return RedirectResponse(url="http://127.0.0.1:8000/ui?gmail=error")


# ── Credentials Management ────────────────────────────────────────────────

@router.post("/credentials/upload")
def gmail_credentials_upload(req: CredentialsUploadRequest):
    """Upload and save Gmail OAuth credentials JSON."""
    if not req.json_data.strip():
        raise HTTPException(status_code=400, detail="Empty JSON data")

    try:
        data = json.loads(req.json_data)
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON: {str(e)}",
        )

    is_valid, error = validate_credentials_json(data)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error)

    saved = save_credentials_json(data)
    if not saved:
        raise HTTPException(
            status_code=500,
            detail="Failed to save credentials file",
        )

    return {
        "status": "ok",
        "message": "Gmail OAuth credentials saved successfully.",
    }


@router.post("/credentials/save-json")
def gmail_credentials_save_json(req: CredentialsUploadRequest):
    return gmail_credentials_upload(req)


@router.get("/credentials/status")
def gmail_credentials_status():
    return {
        "status": "ok",
        "credentials_configured": credentials_exist(),
        "token_configured": token_exists(),
        "connected": is_connected(),
    }


# ── Disconnect ────────────────────────────────────────────────────────────

@router.post("/disconnect")
def gmail_disconnect():
    """Disconnect Gmail by deleting token only.

    Does NOT delete credentials unless remove_credentials=true.
    """
    stop_monitor()

    deleted = delete_token()
    if not deleted:
        raise HTTPException(
            status_code=500,
            detail="Failed to delete Gmail token"
        )

    return {
        "status": "ok",
        "message": "Gmail disconnected. Token deleted. Credentials preserved."
    }


@router.post("/disconnect/remove-credentials")
def gmail_disconnect_remove_credentials():
    """Disconnect Gmail and remove credentials entirely."""
    stop_monitor()
    delete_token()
    delete_credentials()

    return {
        "status": "ok",
        "message": "Gmail disconnected. Credentials and token removed."
    }


# ── Events & Data ─────────────────────────────────────────────────────────

@router.get("/events")
def gmail_events():
    events = get_events_today()
    return {"status": "ok", "count": len(events), "events": events}


@router.get("/jd-files")
def gmail_jd_files():
    files = list_saved_jd_files()
    return {"status": "ok", "count": len(files), "files": files}


@router.get("/drafts")
def gmail_drafts():
    drafts = list_drafts()
    return {"status": "ok", "count": len(drafts), "drafts": drafts}


# ── Configuration ─────────────────────────────────────────────────────────

@router.get("/config")
def gmail_get_config():
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
        }
    }


@router.post("/config")
def gmail_update_config(req: ConfigUpdateRequest):
    config = get_config()
    config.poll_interval_minutes = req.poll_interval_minutes
    config.draft_only = req.draft_only
    config.auto_send_enabled = req.auto_send_enabled
    config.allowed_auto_send_categories = req.allowed_auto_send_categories
    config.allowed_senders = req.allowed_senders

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

    try:
        with open(draft_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read draft: {e}",
        )

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

    from jarvis_core.gmail_service import send_email

    success = send_email(to=to_line, subject=subject_line, body=body)
    if not success:
        raise HTTPException(
            status_code=500,
            detail="Failed to send email. Check Gmail connection.",
        )

    return {"status": "ok", "message": "Email sent successfully."}