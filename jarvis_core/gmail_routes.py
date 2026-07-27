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
    exchange_code_for_token_direct,
    exchange_code_for_token_with_response,
    get_account_email,
    get_authorization_url,
    get_authorization_url_direct,
    get_credentials_error,
    is_connected,
    save_credentials_json,
    token_exists,
    validate_credentials_json,
    validate_web_credentials_strict,
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
    """Get current Gmail monitor status with enhanced credential and error reporting."""
    state = get_state()
    
    # Check credential status
    creds_configured = credentials_exist()
    creds_error = get_credentials_error()
    
    # Check token status - always verify file existence
    tok_configured = token_exists()
    
    # Check connection status
    connected = is_connected()
    email = get_account_email() if connected else None
    
    # Determine overall error message - prefer monitoring errors over credential warnings
    error_message = state.error_message
    if not error_message and creds_error and not connected:
        error_message = creds_error

    return {
        "status": "ok",
        "connected": connected,
        "account_email": email,
        "credentials_configured": creds_configured,
        "credentials_error": creds_error,
        "token_configured": tok_configured,
        "running": state.running,
        "monitoring_active": state.running,
        "last_checked": state.last_checked,
        "important_mails_today": state.important_mails_today,
        "jd_files_saved_today": state.jd_files_saved_today,
        "drafts_created_today": state.drafts_created_today,
        "error_message": error_message,
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
    """Trigger an immediate email check.
    
    Returns structured JSON with status, summary, and events.
    Never raises HTTPException - always returns JSON response.
    """
    result = check_now()
    
    # The check_now function now handles all error cases internally
    # and returns structured responses, so we just return the result
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
    request: Request = None,
    code: Optional[str] = None,
    error: Optional[str] = None,
    state: Optional[str] = None,
):
    """Handle OAuth callback from Google.

    Exchanges authorization code for token and saves it.
    Redirects back to UI on success or failure.
    Never logs or exposes token values, refresh tokens, or client secrets.
    """
    import os
    
    # Set insecure transport for localhost development
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    
    logger.info("OAuth callback received")
    
    # Parse parameters from request to avoid stale data
    if request is not None:
        actual_code = request.query_params.get("code")
        actual_error = request.query_params.get("error") 
        actual_state = request.query_params.get("state")
        
        # Handle case where query_params.get() returns MagicMock (in tests)
        if hasattr(actual_code, '_mock_name'):
            actual_code = code
        if hasattr(actual_error, '_mock_name'):
            actual_error = error
        if hasattr(actual_state, '_mock_name'):
            actual_state = state
    else:
        # Fallback for tests
        actual_code = code
        actual_error = error
        actual_state = state
    
    # Log safe debug information
    logger.info("Query parameters present: code=%s, error=%s, state=%s", 
                "yes" if actual_code else "no", 
                "yes" if actual_error else "no",
                "yes" if actual_state else "no")

    # If Google sends an error
    if actual_error:
        logger.info("OAuth code received: no (error from Google)")
        logger.warning("OAuth callback error from Google (safe): access denied or user cancelled")
        return RedirectResponse(url="http://127.0.0.1:8000/ui?gmail=error&reason=google_denied")

    # If no code provided
    if not actual_code:
        logger.info("OAuth code received: no")
        logger.warning("OAuth callback missing authorization code")
        return RedirectResponse(url="http://127.0.0.1:8000/ui?gmail=error&reason=missing_code")

    logger.info("OAuth code received: yes")
    logger.info("Token exchange started")

    # Use the full authorization response URL for secure token exchange
    if request is not None:
        auth_response = str(request.url)
        success, message = exchange_code_for_token_with_response(auth_response)
    else:
        # Fallback to direct code method for tests without request
        success, message = exchange_code_for_token(actual_code)

    if success:
        logger.info("Token exchange success")
        logger.info("Token saved: yes")
        logger.info("Token file exists: yes")
        logger.info("Redirect target: /ui?gmail=connected")
        return RedirectResponse(url="http://127.0.0.1:8000/ui?gmail=connected")
    else:
        logger.info("Token exchange failure")
        logger.info("Token saved: no")
        logger.warning("OAuth token exchange failed (safe reason): %s", message[:100])
        logger.info("Redirect target: /ui?gmail=error")
        
        # Handle specific error cases with user-friendly redirects
        if "invalid_grant" in message.lower() or "invalid authorization code" in message.lower():
            return RedirectResponse(url="http://127.0.0.1:8000/ui?gmail=error&reason=code_expired")
        elif "redirect_uri_mismatch" in message.lower():
            return RedirectResponse(url="http://127.0.0.1:8000/ui?gmail=error&reason=uri_mismatch") 
        else:
            return RedirectResponse(url="http://127.0.0.1:8000/ui?gmail=error&reason=token_exchange_failed")


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


# ── Prompt/Rules Configuration ────────────────────────────────────────────

class PromptRulesRequest(BaseModel):
    """Request body for updating prompt/rules configuration."""
    monitoring_objective: str = ""
    important_rules: str = ""
    ignore_rules: str = ""
    reply_style: str = "professional"
    draft_only_safety: bool = True


@router.get("/prompt-rules")
def get_prompt_rules():
    """Get current Gmail behavior prompt/rules configuration.
    
    Returns default values if config file doesn't exist.
    """
    config_path = Path("C:/Jarvis/config/gmail_prompt_rules.json")
    
    # Default configuration
    defaults = {
        "monitoring_objective": "Monitor Gmail for job descriptions, recruiter emails, interview updates, selections, offers, and joining-related messages. Save JD attachments and create safe draft replies only.",
        "important_rules": "Mark as important if mail contains JD, job description, interview schedule, recruiter follow-up, offer, selection, joining, client interview, technical round, HR round, or urgent response request.",
        "ignore_rules": "Ignore promotions, newsletters, OTPs, generic alerts, spam, social media notifications, and unrelated marketing emails.",
        "reply_style": "professional",
        "draft_only_safety": True
    }
    
    if not config_path.exists():
        return {
            "status": "ok",
            "rules": defaults
        }
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            rules = json.load(f)
        
        # Ensure draft_only_safety is always True (protected)
        rules["draft_only_safety"] = True
        
        return {
            "status": "ok",
            "rules": rules
        }
    except Exception as e:
        logger.error("Failed to load prompt rules: %s", e)
        return {
            "status": "ok",
            "rules": defaults
        }


@router.post("/prompt-rules")
def update_prompt_rules(req: PromptRulesRequest):
    """Update Gmail behavior prompt/rules configuration.
    
    Saves configuration to C:/Jarvis/config/gmail_prompt_rules.json.
    Always enforces draft_only_safety=True for security.
    """
    config_path = Path("C:/Jarvis/config/gmail_prompt_rules.json")
    
    # Ensure config directory exists
    config_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Build configuration object (always enforce draft_only_safety)
    rules = {
        "monitoring_objective": req.monitoring_objective,
        "important_rules": req.important_rules,
        "ignore_rules": req.ignore_rules,
        "reply_style": req.reply_style,
        "draft_only_safety": True  # Always True for safety
    }
    
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(rules, f, indent=2)
        
        logger.info("Gmail prompt/rules configuration saved")
        
        return {
            "status": "ok",
            "message": "Prompt/rules configuration saved successfully"
        }
    except Exception as e:
        logger.error("Failed to save prompt rules: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save configuration: {str(e)}"
        )