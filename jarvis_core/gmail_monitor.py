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
from datetime import datetime, timezone
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


def _ensure_dirs() -> None:
    """Ensure required directories exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)


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
            draft_path = save_draft_to_file(draft)
            if draft_path:
                event.draft_reply_path = str(draft_path)
                with _lock:
                    _state.drafts_created_today += 1

    # Log event
    _log_event(event)

    with _lock:
        _state.important_mails_today += 1

    # Mark as read so we don't re-process
    try:
        mark_as_read(preview.message_id)
    except Exception as e:
        logger.warning("Failed to mark message as read: %s", e)


def _monitor_loop() -> None:
    """Main monitoring loop running in background thread."""
    global _state

    logger.info("Gmail monitor thread started")

    processed_ids = _load_processed_ids()

    while not _stop_event.is_set():
        try:
            if not is_connected():
                with _lock:
                    _state.connected = False
                    _state.error_message = "Gmail not connected. Add gmail_credentials.json and complete OAuth setup."
                _stop_event.wait(60)  # Wait a minute before retrying
                continue

            with _lock:
                _state.connected = True
                _state.error_message = ""

            # Fetch unread messages
            messages = list_recent_unread(max_results=10)

            for preview in messages:
                if _stop_event.is_set():
                    break

                # Skip already processed
                if preview.message_id in processed_ids:
                    continue

                try:
                    _process_email(preview)
                    processed_ids.add(preview.message_id)
                except Exception as e:
                    logger.error(
                        "Error processing email %s: %s", preview.message_id, e
                    )
                    # Continue with next email — don't crash the monitor
                    continue

            # Save processed IDs periodically
            _save_processed_ids(processed_ids)

            with _lock:
                _state.last_checked = datetime.now().isoformat()

            # Wait for next poll interval
            interval = _config.poll_interval_minutes * 60
            _stop_event.wait(interval)

        except Exception as e:
            logger.error("Gmail monitor loop error: %s", e)
            with _lock:
                _state.error_message = str(e)[:200]
            _stop_event.wait(60)  # Wait before retrying

    logger.info("Gmail monitor thread stopped")


def start_monitor() -> dict:
    """Start the Gmail monitor in a background thread.

    Returns status dict.
    """
    global _monitor_thread, _stop_event, _state

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
        processed_ids = _load_processed_ids()
        
        # Fetch recent unread messages
        try:
            messages = list_recent_unread(max_results=10)
            emails_scanned = len(messages)
            logger.info("Check Now: emails scanned - %d", emails_scanned)
        except Exception as e:
            logger.error("Check Now: Gmail API error - %s", str(e))
            return {
                "status": "error",
                "message": "Failed to fetch emails from Gmail. Please check your connection.",
                "error_code": "gmail_api_error"
            }
        
        # Process each message
        for preview in messages:
            if preview.message_id in processed_ids:
                continue
                
            try:
                # Process the email
                event_data = _process_email(preview)
                processed_ids.add(preview.message_id)
                
                # Track statistics
                if event_data:
                    events_processed.append(event_data)
                    
                    # Check if it's important
                    if hasattr(event_data, 'importance') and event_data.importance in ('high', 'critical'):
                        important_found += 1
                    
                    # Check if JD was saved (look for attachments or links in action)
                    if hasattr(event_data, 'action_required'):
                        action = event_data.action_required or ""
                        if "JD" in action or "attachment" in action or "link" in action:
                            jd_files_saved += 1
                    
                    # Check if draft was created
                    if hasattr(event_data, 'action_required'):
                        action = event_data.action_required or ""
                        if "draft" in action.lower():
                            drafts_created += 1
                            
            except Exception as e:
                logger.error("Check Now: error processing email %s - %s", preview.message_id, str(e))
                errors += 1
                warnings.append(f"Failed to process email from {preview.sender}: {str(e)[:50]}")
                continue
        
        # Save processed IDs
        _save_processed_ids(processed_ids)
        
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