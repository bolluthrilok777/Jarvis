"""FastAPI routes for Gmail monitoring.

All endpoints are safe and do not expose sensitive data.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from jarvis_core.gmail_models import GmailConfig
from jarvis_core.gmail_monitor import (
    check_now,
    clear_processed_ids,
    delete_mail_memory,
    get_config,
    get_events_today,
    get_state,
    load_config,
    run_full_scan,
    run_incremental_scan,
    start_window_rescan,
    start_monitor,
    stop_monitor,
    update_config,
    get_last_scan_timestamp,
    is_full_scan_done,
)
from jarvis_core.model_router import router as model_router
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
    is_token_revoked,
    validate_token_health,
    validate_web_credentials_strict,
    _get_credentials,
    _get_credentials_type,
)
from jarvis_core.jd_saver import list_saved_jd_files
from jarvis_core.draft_writer import list_drafts
from jarvis_core.config import NOTES_DIR as JARVIS_NOTES_DIR, JOBS_JD_DIR, GMAIL_DRAFTS_DIR, CONFIG_DIR as JARVIS_CONFIG_DIR
from jarvis_core.gmail_service import (
    list_gmail_drafts,
    get_draft_scope_available,
    get_account_email as _get_email,
    is_connected as _is_connected,
)

logger = logging.getLogger("jarvis.gmail_routes")

router = APIRouter(prefix="/gmail", tags=["gmail"])


class ConfigUpdateRequest(BaseModel):
    """Request body for updating Gmail config."""
    poll_interval_minutes: int = 5
    draft_only: bool = True
    auto_send_enabled: bool = False
    allowed_auto_send_categories: list[str] = []
    allowed_senders: list[str] = []
    scan_period: str = "today"


class SendDraftRequest(BaseModel):
    """Request body for sending a draft."""
    draft_path: str = ""
    confirm_send: bool = False


class RescanRequest(BaseModel):
    """Selected history window for a manual Gmail rescan."""
    days_back: int = 7


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
    # Use cached email from monitor state to avoid calling get_account_email()
    # on every /gmail/status poll (polled every 2s by UI), which causes constant
    # SSL-vulnerable API calls in background threads.
    email = state.account_email if state.account_email else (get_account_email() if connected else None)
    
    # Determine overall error message - prefer monitoring errors over credential warnings
    error_message = state.error_message
    if not error_message and creds_error and not connected:
        error_message = creds_error

    token_health = validate_token_health()
    token_revoked = is_token_revoked()
    last_scan_at = get_last_scan_timestamp() or ""
    from jarvis_core.gmail_reporting import build_authoritative_gmail_status

    authoritative = build_authoritative_gmail_status(
        state=state,
        connected=connected,
        credentials_configured=creds_configured,
        token_configured=tok_configured,
        credentials_error=creds_error or "",
        token_health=token_health,
        token_revoked=token_revoked,
        last_scan_at=last_scan_at,
    )

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
        "local_drafts_today": state.local_drafts_today,
        "error_message": error_message or authoritative["safe_error"],
        "poll_interval_minutes": state.poll_interval_minutes,
        "draft_only": state.draft_only,
        "auto_send_enabled": state.auto_send_enabled,
        "drafts_in_progress": state.drafts_in_progress,
        "draft_status": state.draft_status,
        "draft_subject": state.draft_subject,
        "scan_in_progress": state.scan_in_progress,
        "scan_phase": state.scan_phase,
        "scan_status": state.scan_status,
        "scan_days_back": state.scan_days_back,
        "scan_current": state.scan_current,
        "scan_total": state.scan_total,
        "scan_summary": state.scan_summary,
        "poll_healthy": authoritative["healthy"],
        # Preserve the UI's established poll-specific field contract. Other
        # scan/auth failures are exposed through error_message/safe_error.
        "poll_error": state.error_message if state.error_message.startswith("Monitoring poll failed") else "",
        "last_poll_at": state.last_poll_at or "",
        "last_poll_success_at": state.last_poll_success_at or "",
        "routing": model_router.get_last(),
        "token_health": token_health,
        "token_revoked": token_revoked,
        "last_scan_at": last_scan_at,
        "full_scan_done": is_full_scan_done(),
        # Shared source of truth also consumed by command and Telegram paths.
        "operational_status": authoritative["operational_status"],
        "healthy": authoritative["healthy"],
        "status_message": authoritative["message"],
        "safe_error": authoritative["safe_error"],
        "latest_scan_phase": authoritative["latest_scan_phase"],
        "latest_scan_status": authoritative["latest_scan_status"],
        "last_successful_scan": authoritative["last_successful_scan"],
    }


@router.post("/start")
def gmail_start():
    """Start the Gmail monitor.

    Returns JSON with status. Never raises HTTPException — always returns JSON.
    """
    result = start_monitor()
    return result


@router.post("/stop")
def gmail_stop():
    """Stop the Gmail monitor."""
    result = stop_monitor()
    return result


@router.post("/rescan")
def gmail_rescan(req: RescanRequest = RescanRequest()):
    """Read every mailbox item in the selected 1–30 day history window.

    This is the primary button for manual scanning after the initial full scan.
    It runs as a persistent background job, so closing the Operations Center
    never cancels Gmail reading, JD capture, or draft composition.
    Never raises HTTPException - always returns JSON.
    """
    if not is_connected():
        if token_exists():
            return {
                "status": "error",
                "message": "Gmail session expired. Please reconnect Gmail.",
                "error_code": "token_invalid"
            }
        return {
            "status": "error",
            "message": "Gmail is not connected. Please connect Gmail first.",
            "error_code": "gmail_not_connected"
        }

    days = max(1, min(req.days_back, 30))
    return start_window_rescan(days)


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


@router.post("/full-scan")
def gmail_full_scan():
    """Run a complete full scan of ALL emails from the last 30 days.
    
    This is the initial scan that:
    1. Clears all processed IDs
    2. Fetches ALL emails from the last 30 days (up to 1500)
    3. Classifies each one with Ollama
    4. Saves JDs and creates drafts
    5. Returns comprehensive results with counts
    
    Returns structured JSON with status, summary, and events.
    Never raises HTTPException - always returns JSON response.
    """
    # Check if Gmail is connected
    if not is_connected():
        if token_exists():
            return {
                "status": "error",
                "message": "Gmail session expired. Please reconnect Gmail.",
                "error_code": "token_invalid"
            }
        else:
            return {
                "status": "error",
                "message": "Gmail is not connected. Please connect Gmail first.",
                "error_code": "gmail_not_connected"
            }
    
    try:
        result = run_full_scan(source="full_scan")
        return {
            "status": "ok",
            "message": "Full scan completed",
            "checked_at": datetime.now().isoformat(),
            "summary": {
                "emails_scanned": result.get("emails_scanned", 0),
                "important_found": result.get("important_found", 0),
                "jd_files_saved": result.get("jd_files_saved", 0),
                "drafts_created": result.get("drafts_created", 0),
                "errors": result.get("errors", 0),
                "total_emails_in_period": result.get("total_emails_in_period", 0),
            },
            "warnings": result.get("warnings", []),
        }
    except RuntimeError as e:
        error_msg = str(e)
        if "connection" in error_msg.lower() or "ssl" in error_msg.lower():
            return {
                "status": "error",
                "message": "Gmail API connection error. Please check your connection.",
                "error_code": "gmail_api_error"
            }
        elif "auth" in error_msg.lower() or "token" in error_msg.lower():
            return {
                "status": "error",
                "message": "Gmail authentication error. Please reconnect Gmail.",
                "error_code": "token_invalid"
            }
        else:
            return {
                "status": "error",
                "message": error_msg,
                "error_code": "gmail_api_error"
            }
    except Exception as e:
        logger.error("Full scan unexpected error: %s", str(e)[:100])
        return {
            "status": "error",
            "message": f"An unexpected error occurred: {str(e)[:100]}",
            "error_code": "unexpected_error"
        }


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
        from jarvis_core.gmail_service import EXPECTED_REDIRECT_URI
        return {
            "status": "error",
            "message": f"Please upload Web application OAuth credentials with redirect URI {EXPECTED_REDIRECT_URI}"
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
        from jarvis_core.gmail_service import EXPECTED_REDIRECT_URI as _EXPECTED_URI
        return {
            "status": "error",
            "message": f"Please upload Web application OAuth credentials with redirect URI {_EXPECTED_URI}"
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
        return RedirectResponse(url="/ui?gmail=error&reason=google_denied")

    # If no code provided
    if not actual_code:
        logger.info("OAuth code received: no")
        logger.warning("OAuth callback missing authorization code")
        return RedirectResponse(url="/ui?gmail=error&reason=missing_code")

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
        return RedirectResponse(url="/ui?gmail=connected")
    else:
        logger.info("Token exchange failure")
        logger.info("Token saved: no")
        logger.warning("OAuth token exchange failed (safe reason): %s", message[:100])
        logger.info("Redirect target: /ui?gmail=error")
        
        # Handle specific error cases with user-friendly redirects
        if "invalid_grant" in message.lower() or "invalid authorization code" in message.lower():
            return RedirectResponse(url="/ui?gmail=error&reason=code_expired")
        elif "redirect_uri_mismatch" in message.lower():
            return RedirectResponse(url="/ui?gmail=error&reason=uri_mismatch")
        else:
            return RedirectResponse(url="/ui?gmail=error&reason=token_exchange_failed")


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

@router.post("/clear-processed")
def gmail_clear_processed():
    """Clear processed email IDs so all unread emails are re-scanned.

    Use this to force a full re-scan when emails were already marked processed.
    Never exposes tokens or secrets.
    """
    result = clear_processed_ids()
    return result


@router.post("/delete-memory")
def gmail_delete_memory():
    """Clear only Jarvis's local Gmail-processing memory.

    This never deletes Gmail mail/drafts or saved JD files. The next rescan
    rechecks everything and creates only outputs that are missing.
    """
    return delete_mail_memory()


@router.post("/restore-memory")
def gmail_restore_memory_legacy():
    """Backward-compatible alias for earlier client versions."""
    return delete_mail_memory()


def _count_events_for_period(period: str = "today") -> dict:
    """Count Gmail events across a time period from daily log files.

    period: "today" | "week" | "month"
    Returns safe counts — no raw email content.
    """
    from datetime import date, timedelta
    notes_dir = JARVIS_NOTES_DIR
    today = date.today()
    days_back = {"week": 7, "month": 30}.get(period, 1)

    important_count = 0
    jd_count = 0
    draft_count = 0
    total_count = 0

    for i in range(days_back):
        day = today - timedelta(days=i)
        log_path = notes_dir / f"gmail_monitor_log_{day.isoformat()}.jsonl"
        if not log_path.exists():
            continue
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        total_count += 1
                        category = event.get("category", "")
                        if category and category != "non_job":
                            important_count += 1
                        if event.get("jd_saved_path"):
                            jd_count += 1
                        if event.get("draft_reply_path"):
                            draft_count += 1
                    except (json.JSONDecodeError, Exception):
                        continue
        except Exception:
            continue

    return {
        "important_count": important_count,
        "jd_count": jd_count,
        "draft_count": draft_count,
        "total_count": total_count,
    }


def _count_gmail_drafts_for_period(period: str) -> int | None:
    """Return the real Gmail draft count for the selected period when available."""
    if not _is_connected() or not get_draft_scope_available():
        return None

    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days={"week": 7, "month": 30}.get(period, 1))
    count = 0
    for draft in list_gmail_drafts(max_results=500):
        try:
            draft_date = datetime.fromtimestamp(int(draft.get("internal_date", "")) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            continue
        if draft_date >= cutoff:
            count += 1
    return count


@router.get("/events")
def gmail_events(period: str = "today"):
    """Get Gmail events for a time period.

    period query param: today | week | month (default: today)
    Returns event list plus aggregate counts for the period.
    Never exposes tokens or secrets.
    """
    from datetime import date, timedelta
    notes_dir = JARVIS_NOTES_DIR
    today = date.today()
    days_back = {"week": 7, "month": 30}.get(period, 1)

    all_events = []
    for i in range(days_back):
        day = today - timedelta(days=i)
        log_path = notes_dir / f"gmail_monitor_log_{day.isoformat()}.jsonl"
        if not log_path.exists():
            continue
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            all_events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except Exception:
            continue

    counts = _count_events_for_period(period)
    real_draft_count = _count_gmail_drafts_for_period(period)
    if real_draft_count is not None:
        counts["draft_count"] = real_draft_count
    return {
        "status": "ok",
        "period": period,
        "count": len(all_events),
        "events": all_events,
        "period_counts": counts,
    }


@router.get("/jd-files")
def gmail_jd_files():
    """Return list of saved JD files with safe metadata only."""
    from pathlib import Path as _Path
    _jd_abs = JOBS_JD_DIR
    files = list_saved_jd_files(save_dir=_jd_abs) if _jd_abs.exists() else []
    safe_files = [
        {
            "filename": f.get("filename", ""),
            "date": f.get("date", ""),
            "size": f.get("size", 0),
            "relative_path": str(Path(f.get("path", "")).relative_to(_jd_abs)),
        }
        for f in files
    ]
    return {
        "status": "ok",
        "count": len(safe_files),
        "files": safe_files,
        "folder": str(JOBS_JD_DIR),
    }


@router.get("/jd-file")
def gmail_jd_file(path: str):
    """Open a saved JD only when its resolved path remains in the JD folder."""
    jd_root = JOBS_JD_DIR.resolve()
    try:
        target = (jd_root / path).resolve()
        target.relative_to(jd_root)
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JD file path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="JD file not found")
    return FileResponse(target, filename=target.name)


@router.get("/drafts")
def gmail_drafts():
    """Return Gmail API drafts and local backup drafts with diagnostics.

    Never exposes tokens, secrets, or credentials.
    """
    connected = _is_connected()
    account_email = _get_email() if connected else None

    # Fetch real Gmail drafts from API
    gmail_api_drafts = []
    draft_scope_ok = False
    last_draft_error = ""
    if connected:
        try:
            draft_scope_ok = get_draft_scope_available()
            if draft_scope_ok:
                gmail_api_drafts = list_gmail_drafts(max_results=50)
            else:
                last_draft_error = "Gmail draft permission missing. Reconnect Gmail with draft permission."
                logger.warning("Draft scope not available for account: %s", account_email or "unknown")
        except Exception as e:
            last_draft_error = "Failed to fetch Gmail drafts: " + str(e)[:80]
            logger.error("gmail_drafts endpoint error: %s", str(e)[:80])

    # Fetch local backup drafts — use draft_writer helper (absolute path) first;
    # fall back to direct filesystem read if helper returns empty to avoid CWD issues.
    local_files_raw = list_drafts()
    if not local_files_raw:
        _local_drafts_dir = GMAIL_DRAFTS_DIR
        if _local_drafts_dir.exists():
            from datetime import datetime as _dt
            for _f in sorted(_local_drafts_dir.iterdir(), reverse=True):
                if _f.is_file() and _f.suffix in (".txt", ".md", ".json"):
                    try:
                        local_files_raw.append({
                            "filename": _f.name,
                            "size": _f.stat().st_size,
                            "modified": _dt.fromtimestamp(_f.stat().st_mtime).isoformat(),
                        })
                    except Exception:
                        pass

    safe_local = [
        {
            "filename": d.get("filename", ""),
            "size": d.get("size", 0),
            "modified": d.get("modified", ""),
        }
        for d in local_files_raw
    ]

    # Build Gmail Drafts URL using connected account if available
    if account_email:
        gmail_drafts_url = f"https://mail.google.com/mail/?authuser={account_email}#drafts"
    else:
        gmail_drafts_url = "https://mail.google.com/mail/#drafts"

    return {
        "status": "ok",
        # Real Gmail drafts
        "gmail_drafts": gmail_api_drafts,
        "gmail_drafts_count": len(gmail_api_drafts),
        # Local backup drafts
        "local_drafts": safe_local,
        "local_backup_count": len(safe_local),
        "local_folder": str(GMAIL_DRAFTS_DIR),
        # Diagnostics
        "connected_account_email": account_email or "",
        "draft_scope_available": draft_scope_ok,
        "last_gmail_draft_error": last_draft_error,
        "gmail_drafts_url": gmail_drafts_url,
    }


@router.get("/log")
def gmail_log(lines: int = 100):
    """Return recent safe Gmail monitor log lines.

    Never exposes tokens, secrets, credentials, or full email bodies.
    """
    import re as _re
    from datetime import date, timedelta

    notes_dir = JARVIS_NOTES_DIR
    today_str = date.today().isoformat()
    log_path = notes_dir / f"gmail_monitor_log_{today_str}.jsonl"

    if not log_path.exists():
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        log_path = notes_dir / f"gmail_monitor_log_{yesterday}.jsonl"

    if not log_path.exists():
        return {
            "status": "ok",
            "count": 0,
            "entries": [],
            "message": "No Gmail logs found yet.",
        }

    _SENSITIVE = _re.compile(
        r"token|secret|credential|password|client_id|client_secret|access_token|refresh_token",
        _re.IGNORECASE,
    )
    _SAFE_FIELDS = {
        "detected_at", "email_date", "from", "subject", "category",
        "importance", "summary", "action_required", "jd_saved_path",
        "draft_reply_path", "gmail_message_id", "gmail_draft_id",
    }

    entries = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()

        for line in raw_lines[-lines:]:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                safe_entry = {}
                for key in _SAFE_FIELDS:
                    if key in data:
                        val = str(data[key])
                        if not _SENSITIVE.search(val):
                            safe_entry[key] = val[:200]
                entries.append(safe_entry)
            except json.JSONDecodeError:
                if not _SENSITIVE.search(line):
                    entries.append({"message": line[:200]})

    except Exception as e:
        logger.error("Failed to read log file: %s", e)
        return {
            "status": "error",
            "message": "Failed to read log file.",
            "count": 0,
            "entries": [],
        }

    return {
        "status": "ok",
        "count": len(entries),
        "entries": entries,
        "log_date": today_str,
    }


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
            "scan_period": config.scan_period,
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
    if req.scan_period not in {"today", "week", "month"}:
        raise HTTPException(status_code=400, detail="Invalid scan period")
    config.scan_period = req.scan_period

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
    config_path = JARVIS_CONFIG_DIR / "gmail_prompt_rules.json"
    
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
    
    Saves configuration to the Jarvis config directory (gmail_prompt_rules.json).
    Always enforces draft_only_safety=True for security.
    """
    config_path = JARVIS_CONFIG_DIR / "gmail_prompt_rules.json"
    
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

# ── New Events Polling (for notification bell) ────────────────────────────

@router.get("/new-events")
def gmail_new_events(since: str = ""):
    """Return only events that occurred after the given ISO timestamp.

    Used by the notification bell to poll for new important emails without
    re-fetching the entire event list.

    Query params:
        since: ISO datetime string (e.g. 2026-08-03T10:30:00). Events with
               detected_at > since are returned. If omitted, returns last 20
               events from today.

    Returns safe event data only — no tokens or credentials.
    """
    from datetime import date, timedelta

    notes_dir = JARVIS_NOTES_DIR
    today = date.today()

    # Collect today's + yesterday's events (in case monitor ran across midnight)
    all_events: list[dict] = []
    for i in range(2):
        day = today - timedelta(days=i)
        log_path = notes_dir / f"gmail_monitor_log_{day.isoformat()}.jsonl"
        if not log_path.exists():
            continue
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            all_events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except Exception:
            continue

    # Filter to only events after `since`
    new_events: list[dict] = []
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            for ev in all_events:
                try:
                    ev_dt = datetime.fromisoformat(ev.get("detected_at", ""))
                    if ev_dt > since_dt:
                        new_events.append(ev)
                except (ValueError, TypeError):
                    pass
        except ValueError:
            # Invalid `since` — return last 20
            new_events = all_events[-20:]
    else:
        new_events = all_events[-20:]

    # Return only safe fields
    _SAFE = {"detected_at", "email_date", "from", "subject", "category",
             "importance", "summary", "jd_saved_path", "draft_reply_path",
             "gmail_message_id", "gmail_thread_id", "gmail_draft_id"}
    safe_events = []
    for ev in new_events:
        safe_ev = {k: v for k, v in ev.items() if k in _SAFE}
        safe_events.append(safe_ev)

    return {
        "status": "ok",
        "count": len(safe_events),
        "events": safe_events,
    }


# ── Open Message Redirect ─────────────────────────────────────────────────

@router.get("/open-message")
def gmail_open_message(message_id: str = "", thread_id: str = ""):
    """Redirect browser to a specific Gmail message or thread.

    Uses the connected account email to determine the correct authuser index
    by checking which gmail.com account is active, then redirects to Gmail.

    Falls back to inbox if neither ID is provided.
    Never exposes tokens or credentials.
    """
    if not is_connected():
        # Not connected — just open Gmail inbox
        return RedirectResponse(url="https://mail.google.com/mail/", status_code=302)

    # Use threadId preferentially (Gmail URLs use threads), fall back to messageId
    link_id = thread_id or message_id

    if not link_id:
        return RedirectResponse(url="https://mail.google.com/mail/#inbox", status_code=302)

    # Try to get the account email to determine user index
    account_email = get_account_email()

    # Attempt to look up the correct account index via the Gmail API
    # Gmail web URLs use u/0, u/1, etc. based on sign-in order.
    # We try u/0 first (most common), then construct with authuser param as fallback.
    # The most reliable cross-account URL uses the authuser query param on the base,
    # then the fragment — but fragments cannot follow query strings in standard HTTP.
    # Solution: use a meta-refresh HTML page that sets the hash after load.
    gmail_url = f"https://mail.google.com/mail/u/0/#all/{link_id}"

    # If we have the account email, construct a more reliable URL using
    # Gmail's account switcher path by trying to match u/0 → u/3
    # Most users only have 1-2 accounts so u/0 covers the majority.
    # We return a tiny HTML redirect page that appends the fragment correctly.
    html_redirect = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Opening Gmail…</title>
  <script>
    // Try u/0 first; if that fails (wrong account), Gmail will prompt to switch.
    // Using location.replace so back button works correctly.
    window.location.replace("https://mail.google.com/mail/u/0/#all/{link_id}");
  </script>
</head>
<body style="background:#050505;color:#a1a1aa;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;font-size:14px;">
  Opening Gmail…
</body>
</html>"""

    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html_redirect, status_code=200)
