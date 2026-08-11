"""Gmail monitor that runs in a background thread.

Polls Gmail at configurable intervals, classifies emails, saves JDs,
creates drafts, and logs events. Designed to survive errors without
crashing Jarvis.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from jarvis_core.draft_writer import generate_draft, save_draft_to_file
from jarvis_core.gmail_classifier import classify_email
from jarvis_core.gmail_models import (
    GmailConfig,
    GmailEvent,
    GmailMonitorState,
    GmailMessagePreview,
)
from jarvis_core.gmail_service import (
    is_connected,
    list_messages_for_period,
    list_recent_unread,
    mark_as_read,
    reset_service,
    token_exists,
)
from jarvis_core.jd_saver import save_jd_attachment, save_jd_link, extract_jd_links_from_body

logger = logging.getLogger("jarvis.gmail_monitor")

# Paths
CONFIG_DIR = Path("C:/Jarvis/config")
CONFIG_FILE = CONFIG_DIR / "gmail_monitor.json"
DATA_DIR = Path("C:/Jarvis/data")
PROCESSED_IDS_FILE = DATA_DIR / "gmail_processed.json"
NOTES_DIR = Path("C:/Jarvis/notes")

# Global monitor state
_monitor_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_state = GmailMonitorState()
_config = GmailConfig()
_lock = threading.Lock()
# Flag to prevent Check Now and background scan from overlapping
_scan_running = threading.Event()

# Persistent state file — survives Jarvis restarts
STATE_FILE = DATA_DIR / "gmail_monitor_state.json"


def _ensure_dirs() -> None:
    """Ensure required directories exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)


def _load_persistent_state() -> dict:
    """Load persistent monitor state from disk (survives restarts)."""
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning("Failed to load persistent state: %s", e)
    return {}


def _save_persistent_state(data: dict) -> None:
    """Save persistent monitor state to disk."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning("Failed to save persistent state: %s", e)


def get_last_scan_timestamp() -> Optional[str]:
    """Return the ISO timestamp of the last completed scan, or None."""
    state = _load_persistent_state()
    return state.get("last_scan_at")


def set_last_scan_timestamp(ts: str) -> None:
    """Persist the ISO timestamp of the last completed scan."""
    state = _load_persistent_state()
    state["last_scan_at"] = ts
    state["full_scan_done"] = True
    _save_persistent_state(state)


def is_full_scan_done() -> bool:
    """Return True if the initial full scan has been completed at least once."""
    state = _load_persistent_state()
    return bool(state.get("full_scan_done", False))


def _load_processed_ids() -> set[str]:
    """Load set of already-processed message IDs."""
    try:
        if PROCESSED_IDS_FILE.exists():
            with open(PROCESSED_IDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return set(data.get("processed_ids", []))
    except Exception as e:
        logger.warning("Failed to load processed IDs: %s", e)
    return set()


def _save_processed_ids(ids: set[str]) -> None:
    """Save set of processed message IDs."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(PROCESSED_IDS_FILE, "w", encoding="utf-8") as f:
            json.dump({"processed_ids": list(ids)}, f)
    except Exception as e:
        logger.warning("Failed to save processed IDs: %s", e)


def _get_log_path() -> Path:
    """Get the log file path for today."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    return NOTES_DIR / f"gmail_monitor_log_{date_str}.jsonl"


def _log_event(event: GmailEvent) -> None:
    """Append an event to the daily log file."""
    try:
        log_path = _get_log_path()
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(event.to_log_line() + "\n")
    except Exception as e:
        logger.error("Failed to log event: %s", e)


def _process_email(preview: GmailMessagePreview) -> None:
    """Process a single email: classify, save JD, create draft, log."""
    global _state

    # Classify
    classification = classify_email(preview)

    if not classification.is_job_related:
        logger.debug("Skipping non-job email: %s", preview.subject[:50])
        return

    logger.info(
        "Job-related email: %s | Category: %s | Importance: %s",
        preview.subject[:50], classification.category, classification.importance,
    )

    # Build event
    event = GmailEvent(
        detected_at=datetime.now().isoformat(),
        email_date=preview.date,
        from_=preview.sender,
        subject=preview.subject,
        category=classification.category,
        importance=classification.importance,
        summary=classification.summary,
        action_required=classification.action_required,
        gmail_message_id=preview.message_id,
        gmail_thread_id=getattr(preview, 'thread_id', ''),
    )

    # Save JD attachments
    if classification.should_save_jd:
        saved = save_jd_attachment(
            message_id=preview.message_id,
            sender=preview.sender,
            subject=preview.subject,
            save_dir=Path(_config.save_jd_folder),
        )
        if saved:
            event.attachments_saved = [s["path"] for s in saved]
            event.jd_saved_path = saved[0]["path"]
            with _lock:
                _state.jd_files_saved_today += len(saved)

        # Check for JD links in body
        jd_links = extract_jd_links_from_body(preview.body_preview)
        for link in jd_links:
            link_path = save_jd_link(
                link=link,
                sender=preview.sender,
                subject=preview.subject,
                save_dir=Path(_config.save_jd_folder),
            )
            if link_path:
                if not event.jd_saved_path:
                    event.jd_saved_path = str(link_path)
                with _lock:
                    _state.jd_files_saved_today += 1

    # Create draft
    if classification.should_create_draft and _config.draft_only:
        draft = generate_draft(
            sender=preview.sender,
            subject=preview.subject,
            classification=classification,
            config=_config,
        )
        if draft:
            # Attempt real Gmail API draft creation first
            gmail_draft_id = None
            try:
                from jarvis_core.gmail_service import create_draft as create_gmail_draft
                gmail_draft_id = create_gmail_draft(
                    to=draft.to,
                    subject=draft.subject,
                    body=draft.body,
                )
                if gmail_draft_id:
                    logger.info("Gmail draft created: id=%s", gmail_draft_id[:8] + "...")
            except Exception as e:
                error_msg = str(e)
                if any(w in error_msg.lower() for w in ("insufficient", "scope", "permission", "forbidden")):
                    logger.warning("Gmail draft permission missing — falling back to local file only")
                    with _lock:
                        _state.error_message = "Gmail draft permission missing. Reconnect Gmail with draft permission."
                else:
                    logger.warning("Gmail API draft creation failed (%s) — falling back to local file", error_msg[:80])

            # Always save local backup file
            draft_path = save_draft_to_file(draft)
            if draft_path:
                event.draft_reply_path = str(draft_path)

            # Verify draft exists before counting — prevent phantom counts
            gmail_draft_verified = False
            if gmail_draft_id:
                try:
                    from jarvis_core.gmail_service import verify_draft_exists
                    gmail_draft_verified = verify_draft_exists(gmail_draft_id)
                    if not gmail_draft_verified:
                        logger.warning("Gmail draft id=%s returned by API but NOT verified — not counting", gmail_draft_id[:8])
                except Exception as ve:
                    logger.warning("Draft verification error: %s", str(ve)[:60])
                    gmail_draft_verified = False

            if gmail_draft_verified:
                # Confirmed real Gmail draft — store ID for deep linking
                event.gmail_draft_id = gmail_draft_id or ""
                with _lock:
                    _state.drafts_created_today += 1
                logger.info("Gmail draft verified and counted")
            else:
                # Only local backup — do NOT increment Gmail drafts metric
                with _lock:
                    _state.local_drafts_today += 1
                if gmail_draft_id:
                    logger.warning("Gmail draft was not created. Local backup saved.")
                else:
                    logger.info("Gmail draft was not created (no API id). Local backup saved.")

    # Log event
    _log_event(event)

    with _lock:
        _state.important_mails_today += 1

    # Mark as read so we don't re-process
    try:
        mark_as_read(preview.message_id)
    except Exception as e:
        logger.warning("Failed to mark message as read: %s", e)


def run_incremental_scan(source: str = "incremental") -> dict:
    """Scan only emails received AFTER the last recorded scan timestamp.

    This is used by the background monitor and manual Rescan button.
    It preserves all existing processed IDs and only processes new mail.
    Returns dict with keys: emails_scanned, important_found, jd_files_saved,
    drafts_created, errors, warnings, new_since.
    """
    global _state

    logger.info("Incremental scan started (source=%s)", source)

    # Get last scan timestamp — scan only emails since then
    last_scan = get_last_scan_timestamp()
    new_since = last_scan or ""

    # Snapshot counters before scan
    with _lock:
        _snap_important = _state.important_mails_today
        _snap_jd = _state.jd_files_saved_today
        _snap_drafts = _state.drafts_created_today + _state.local_drafts_today

    result = {
        "emails_scanned": 0,
        "important_found": 0,
        "jd_files_saved": 0,
        "drafts_created": 0,
        "errors": 0,
        "warnings": [],
        "new_since": new_since,
    }

    processed_ids = _load_processed_ids()

    try:
        # Always fetch recent messages — the processed_ids set prevents re-processing
        # Use "week" window to catch any emails from the past 7 days that slipped through
        messages = list_messages_for_period("week", max_results=500)
        logger.info("Incremental scan: %d messages fetched (source=%s)", len(messages), source)
    except Exception as e:
        err_lower = str(e).lower()
        if "ssl" in err_lower or "wrong_version" in err_lower or "connection" in err_lower:
            raise RuntimeError("Gmail API connection error (SSL/network). Will retry.")
        elif "credentials" in err_lower or "token" in err_lower or "auth" in err_lower:
            raise RuntimeError("Gmail authentication error. May need to reconnect Gmail.")
        elif "quota" in err_lower or "rate" in err_lower:
            raise RuntimeError("Gmail API rate limit. Will retry.")
        else:
            raise RuntimeError("Gmail API error. Will retry.")

    for preview in messages:
        # Skip already-processed emails
        if preview.message_id in processed_ids:
            continue
        result["emails_scanned"] += 1
        try:
            _process_email(preview)
            processed_ids.add(preview.message_id)
        except Exception:
            logger.error("Incremental scan: skipped one email (source=%s)", source)
            result["errors"] += 1
            result["warnings"].append("Skipped one email due to processing error.")
            continue

    _save_processed_ids(processed_ids)

    # Compute delta for this scan
    with _lock:
        result["important_found"] = max(0, _state.important_mails_today - _snap_important)
        result["jd_files_saved"] = max(0, _state.jd_files_saved_today - _snap_jd)
        result["drafts_created"] = max(0, (_state.drafts_created_today + _state.local_drafts_today) - _snap_drafts)

    # Persist scan timestamp
    now_iso = datetime.now().isoformat()
    set_last_scan_timestamp(now_iso)

    logger.info(
        "Incremental scan completed (source=%s): scanned=%d important=%d jd=%d drafts=%d errors=%d",
        source, result["emails_scanned"], result["important_found"],
        result["jd_files_saved"], result["drafts_created"], result["errors"],
    )
    return result


def run_gmail_scan(processed_ids: set, source: str = "unknown") -> dict:
    """Legacy shared scan logic — kept for backward compatibility.
    New code should use run_incremental_scan() instead.
    """
    # Snapshot state counters BEFORE scan to compute delta for THIS scan only.
    with _lock:
        _snap_important = _state.important_mails_today
        _snap_jd = _state.jd_files_saved_today
        _snap_drafts = _state.drafts_created_today + _state.local_drafts_today

    result = {
        "emails_scanned": 0,
        "important_found": 0,
        "jd_files_saved": 0,
        "drafts_created": 0,
        "errors": 0,
        "warnings": [],
    }

    logger.info("Gmail scan started (source=%s)", source)

    try:
        messages = list_messages_for_period(_config.scan_period, max_results=500)
        result["emails_scanned"] = len(messages)
        logger.info("Gmail scan: %d messages fetched (source=%s)", len(messages), source)
    except Exception as e:
        err_lower = str(e).lower()
        if "ssl" in err_lower or "wrong_version" in err_lower or "connection" in err_lower:
            raise RuntimeError("Gmail API connection error (SSL/network). Will retry.")
        elif "credentials" in err_lower or "token" in err_lower or "auth" in err_lower:
            raise RuntimeError("Gmail authentication error. May need to reconnect Gmail.")
        elif "quota" in err_lower or "rate" in err_lower:
            raise RuntimeError("Gmail API rate limit. Will retry.")
        else:
            raise RuntimeError("Gmail API error. Will retry.")

    for preview in messages:
        if preview.message_id in processed_ids:
            continue
        try:
            _process_email(preview)
            processed_ids.add(preview.message_id)
        except Exception:
            logger.error("Scan: skipped one email due to processing error (source=%s)", source)
            result["errors"] += 1
            result["warnings"].append("Skipped one email due to processing error.")
            continue

    _save_processed_ids(processed_ids)
    with _lock:
        result["important_found"] = max(0, _state.important_mails_today - _snap_important)
        result["jd_files_saved"] = max(0, _state.jd_files_saved_today - _snap_jd)
        result["drafts_created"] = max(0, (_state.drafts_created_today + _state.local_drafts_today) - _snap_drafts)
    logger.info(
        "Gmail scan completed (source=%s): scanned=%d important=%d jd=%d drafts=%d errors=%d",
        source, result["emails_scanned"], result["important_found"],
        result["jd_files_saved"], result["drafts_created"], result["errors"],
    )
    return result


def run_full_scan(source: str = "full_scan") -> dict:
    """Run a complete full scan of ALL emails from the last 30 days.
    
    This is the initial scan that:
    1. Clears all processed IDs
    2. Fetches ALL emails from the last 30 days (up to 1500)
    3. Classifies each one with Ollama
    4. Saves JDs and creates drafts
    5. Returns comprehensive results with counts
    
    Returns dict with keys: emails_scanned, important_found, jd_files_saved,
    drafts_created, errors, warnings, total_emails_in_period.
    """
    global _state
    
    logger.info("Full scan started (source=%s)", source)
    
    # Snapshot state counters BEFORE scan
    with _lock:
        _snap_important = _state.important_mails_today
        _snap_jd = _state.jd_files_saved_today
        _snap_drafts = _state.drafts_created_today + _state.local_drafts_today
        # Reset state for fresh scan
        _state.important_mails_today = 0
        _state.jd_files_saved_today = 0
        _state.drafts_created_today = 0
        _state.local_drafts_today = 0
    
    result = {
        "emails_scanned": 0,
        "important_found": 0,
        "jd_files_saved": 0,
        "drafts_created": 0,
        "errors": 0,
        "warnings": [],
        "total_emails_in_period": 0,
    }
    
    # Clear processed IDs so we re-process everything
    processed_ids = set()
    _save_processed_ids(processed_ids)
    
    try:
        # Fetch ALL messages from last 30 days (up to 1500)
        messages = list_messages_for_period("month", max_results=1500)
        result["total_emails_in_period"] = len(messages)
        logger.info("Full scan: %d messages fetched from last 30 days", len(messages))
    except Exception as e:
        err_lower = str(e).lower()
        if "ssl" in err_lower or "wrong_version" in err_lower or "connection" in err_lower:
            raise RuntimeError("Gmail API connection error (SSL/network). Will retry.")
        elif "credentials" in err_lower or "token" in err_lower or "auth" in err_lower:
            raise RuntimeError("Gmail authentication error. May need to reconnect Gmail.")
        elif "quota" in err_lower or "rate" in err_lower:
            raise RuntimeError("Gmail API rate limit. Will retry.")
        else:
            raise RuntimeError("Gmail API error. Will retry.")
    
    # Process each email
    for preview in messages:
        if preview.message_id in processed_ids:
            continue
        try:
            _process_email(preview)
            processed_ids.add(preview.message_id)
            result["emails_scanned"] += 1
        except Exception:
            logger.error("Full scan: skipped one email due to processing error")
            result["errors"] += 1
            result["warnings"].append("Skipped one email due to processing error.")
            continue
    
    _save_processed_ids(processed_ids)
    
    # Compute delta for THIS scan
    with _lock:
        result["important_found"] = max(0, _state.important_mails_today - 0)  # Reset to 0 above
        result["jd_files_saved"] = max(0, _state.jd_files_saved_today - 0)
        result["drafts_created"] = max(0, (_state.drafts_created_today + _state.local_drafts_today) - 0)
    
    # Update last checked timestamp and persist state
    now_iso = datetime.now().isoformat()
    with _lock:
        _state.last_checked = now_iso
        _state.last_poll_success_at = now_iso

    # Mark full scan as done and set last scan timestamp so incremental scans start from here
    set_last_scan_timestamp(now_iso)
    
    logger.info(
        "Full scan completed: scanned=%d important=%d jd=%d drafts=%d errors=%d total_in_period=%d",
        result["emails_scanned"],
        result["important_found"],
        result["jd_files_saved"],
        result["drafts_created"],
        result["errors"],
        result["total_emails_in_period"],
    )
    return result


def _monitor_loop() -> None:
    """Main monitoring loop running in background thread."""
    global _state

    logger.info("Gmail monitor thread started")

    processed_ids = _load_processed_ids()

    _consecutive_failures = 0
    _MAX_CONSECUTIVE_FAILURES = 3

    while not _stop_event.is_set():
        try:
            if not is_connected():
                with _lock:
                    _state.connected = False
                    _state.error_message = "Gmail not connected. Add gmail_credentials.json and complete OAuth setup."
                _stop_event.wait(60)
                continue

            with _lock:
                _state.connected = True

            # Skip if Check Now is running to prevent overlap
            if _scan_running.is_set():
                logger.info("Background scan skipped because Check Now is running")
                _stop_event.wait(30)
                continue

            # Use incremental scan — only processes new emails since last scan
            logger.info("Background scan started")
            # Reset service before each cycle to prevent stale httplib2 transport
            # causing [SSL: WRONG_VERSION_NUMBER] in long-running background threads.
            reset_service()
            _scan_running.set()
            try:
                run_incremental_scan(source="monitor")
                _consecutive_failures = 0
                now_iso = datetime.now().isoformat()
                with _lock:
                    if _state.error_message.startswith("Monitoring poll failed"):
                        _state.error_message = ""
                    _state.last_poll_at = now_iso
                    _state.last_poll_success_at = now_iso
                    _state.last_checked = now_iso
                logger.info("Background scan completed")
            except RuntimeError as poll_err:
                _consecutive_failures += 1
                safe_reason = str(poll_err)
                logger.warning("Background scan failed (attempt %d): %s", _consecutive_failures, safe_reason)
                with _lock:
                    _state.error_message = f"Monitoring poll failed: {safe_reason}"
                    _state.last_poll_at = datetime.now().isoformat()
                logger.info("Background scan failed safely")
                if _consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    logger.warning(
                        "Gmail monitor: %d consecutive poll failures. Polling degraded.",
                        _consecutive_failures,
                    )
                _stop_event.wait(60)
                continue
            finally:
                _scan_running.clear()

            interval = _config.poll_interval_minutes * 60
            _stop_event.wait(interval)

        except Exception as e:
            err_str = str(e)
            if "ssl" in err_str.lower() or "wrong_version" in err_str.lower():
                safe_msg = "SSL connection error in monitor loop. Resetting service."
            else:
                safe_msg = err_str[:100]
            logger.error("Gmail monitor loop error: %s", safe_msg)
            with _lock:
                _state.error_message = safe_msg
            reset_service()
            _stop_event.wait(60)

    logger.info("Gmail monitor thread stopped")


def start_monitor() -> dict:
    """Start the Gmail monitor in a background thread.

    Validates Gmail connection health before starting.
    Returns status dict with clear error if not ready.
    """
    global _monitor_thread, _stop_event, _state

    # Pre-check: must be connected
    if not is_connected():
        return {
            "status": "error",
            "error_code": "not_connected",
            "message": "Gmail is not connected. Please connect Gmail first.",
        }

    # Pre-check: token must be usable (valid or refreshable)
    from jarvis_core.gmail_service import validate_token_health
    health = validate_token_health()
    if not health["valid"] and not health["has_refresh_token"]:
        return {
            "status": "error",
            "error_code": "token_invalid",
            "message": "Gmail token is expired and cannot be refreshed. Please reconnect Gmail.",
        }

    with _lock:
        if _state.running:
            return {"status": "already_running"}

        _ensure_dirs()
        _state.running = True
        _state.error_message = ""
        _stop_event.clear()

        _monitor_thread = threading.Thread(target=_monitor_loop, daemon=True)
        _monitor_thread.start()

        logger.info("Gmail monitor started")
        return {"status": "started"}


def stop_monitor() -> dict:
    """Stop the Gmail monitor.

    Returns status dict.
    """
    global _monitor_thread, _state

    with _lock:
        if not _state.running:
            return {"status": "not_running"}

        _stop_event.set()
        _state.running = False

        if _monitor_thread and _monitor_thread.is_alive():
            _monitor_thread.join(timeout=5)

        _monitor_thread = None
        logger.info("Gmail monitor stopped")
        return {"status": "stopped"}


def check_now() -> dict:
    """Trigger an immediate check (runs in current thread).

    Returns structured status dict with detailed summary and events.
    
    Success response:
    {
        "status": "ok",
        "message": "Check completed",
        "checked_at": "...",
        "summary": {
            "emails_scanned": 0,
            "important_found": 0,
            "jd_files_saved": 0,
            "drafts_created": 0,
            "errors": 0
        },
        "events": [...]
    }
    
    Error response:
    {
        "status": "error",
        "message": "Safe human-readable reason",
        "error_code": "gmail_not_connected / token_invalid / gmail_api_error / network_error"
    }
    """
    global _state

    logger.info("Check Now: started")
    
    # Check if Gmail is connected
    if not is_connected():
        with _lock:
            _state.connected = False
            _state.error_message = "Gmail not connected."
        
        logger.warning("Check Now: failed - Gmail not connected")
        
        # Check if token exists but is invalid
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
    
    logger.info("Check Now: Gmail connected - yes")
    
    # Initialize counters
    emails_scanned = 0
    important_found = 0
    jd_files_saved = 0
    drafts_created = 0
    errors = 0
    warnings = []
    events_processed = []
    
    try:
        # Use incremental scan — only new emails since last scan
        _scan_running.set()
        try:
            scan_result = run_incremental_scan(source="check_now")
            emails_scanned = scan_result["emails_scanned"]
            important_found = scan_result.get("important_found", 0)
            jd_files_saved = scan_result.get("jd_files_saved", 0)
            drafts_created = scan_result.get("drafts_created", 0)
            errors = scan_result["errors"]
            warnings = scan_result.get("warnings", [])
            logger.info("Check Now: emails scanned - %d", emails_scanned)
        except RuntimeError as e:
            logger.error("Check Now: Gmail scan error - %s", str(e))
            return {
                "status": "error",
                "message": "Failed to fetch emails from Gmail. Please check your connection.",
                "error_code": "gmail_api_error"
            }
        finally:
            _scan_running.clear()
        
        # Update state
        checked_at = datetime.now().isoformat()
        with _lock:
            _state.last_checked = checked_at
        
        logger.info("Check Now: important mails found - %d", important_found)
        logger.info("Check Now: JD files saved - %d", jd_files_saved)
        logger.info("Check Now: drafts created - %d", drafts_created)
        logger.info("Check Now: errors - %d", errors)
        logger.info("Check Now: completed successfully")
        
        # Return success response
        response = {
            "status": "ok" if errors == 0 else "partial",
            "message": "Check completed" if errors == 0 else "Check completed with warnings",
            "checked_at": checked_at,
            "summary": {
                "emails_scanned": emails_scanned,
                "important_found": important_found,
                "jd_files_saved": jd_files_saved,
                "drafts_created": drafts_created,
                "errors": errors
            },
            "events": [e.model_dump() if hasattr(e, 'model_dump') else e for e in events_processed]
        }
        
        if warnings:
            response["warnings"] = warnings
            
        return response
        
    except Exception as e:
        logger.error("Check Now: unexpected error - %s", str(e))
        return {
            "status": "error",
            "message": f"An unexpected error occurred: {str(e)[:100]}",
            "error_code": "unexpected_error"
        }


def get_state() -> GmailMonitorState:
    """Get current monitor state."""
    with _lock:
        return _state.model_copy()


def get_config() -> GmailConfig:
    """Get current monitor config."""
    with _lock:
        return _config.model_copy()


def update_config(new_config: GmailConfig) -> dict:
    """Update monitor config.

    Args:
        new_config: New configuration values

    Returns status dict.
    """
    global _config

    with _lock:
        _config = new_config
        _config.save(CONFIG_FILE)
        _state.poll_interval_minutes = _config.poll_interval_minutes
        _state.draft_only = _config.draft_only

    logger.info("Gmail monitor config updated")
    return {"status": "ok"}


def load_config() -> GmailConfig:
    """Load config from file."""
    global _config

    _ensure_dirs()
    _config = GmailConfig.load(CONFIG_FILE)
    with _lock:
        _state.poll_interval_minutes = _config.poll_interval_minutes
        _state.draft_only = _config.draft_only

    return _config


def get_events_today() -> list[dict]:
    """Get today's logged events.

    Returns list of event dicts.
    """
    log_path = _get_log_path()
    if not log_path.exists():
        return []

    events = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        logger.error("Failed to read events: %s", e)

    return events


def clear_processed_ids() -> dict:
    """Clear processed message IDs so all unread emails are re-scanned next cycle.

    Use this when user wants to force a full re-scan of existing emails.
    Never exposes tokens or credentials.
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(PROCESSED_IDS_FILE, "w", encoding="utf-8") as f:
            json.dump({"processed_ids": []}, f)
        logger.info("Processed IDs cleared -- next scan will re-process all unread emails")
        return {"status": "ok", "message": "Processed email IDs cleared. Next scan will check all unread emails."}
    except Exception as e:
        logger.error("Failed to clear processed IDs: %s", e)
        return {"status": "error", "message": "Failed to clear processed IDs."}