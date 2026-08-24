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

from jarvis_core.draft_writer import generate_contextual_draft, save_draft_to_file
from jarvis_core.gmail_classifier import classify_email
from jarvis_core.gmail_models import (
    GmailConfig,
    GmailEvent,
    GmailMonitorState,
    GmailMessagePreview,
)
from jarvis_core.gmail_service import (
    is_connected,
    list_messages_for_date_range,
    list_messages_for_period,
    list_messages_for_days,
    list_recent_unread,
    mark_as_read,
    reset_service,
    token_exists,
    get_thread_context,
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
_window_scan_thread: Optional[threading.Thread] = None

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


def _save_window_scan_state() -> None:
    """Persist the visible manual-scan job state for restart-safe recovery."""
    with _lock:
        job = {
            "in_progress": _state.scan_in_progress,
            "phase": _state.scan_phase,
            "status": _state.scan_status,
            "days_back": _state.scan_days_back,
            "current": _state.scan_current,
            "total": _state.scan_total,
            "summary": _state.scan_summary,
        }
    persisted = _load_persistent_state()
    persisted["window_scan"] = job
    _save_persistent_state(persisted)


def _update_window_scan(**values) -> None:
    """Update and persist a manual rescan progress snapshot."""
    with _lock:
        for key, value in values.items():
            setattr(_state, key, value)
    _save_window_scan_state()


def _set_window_scan_phase(phase: str, status: str) -> None:
    """Report a detailed phase only while a user-initiated scan is active."""
    with _lock:
        active = _state.scan_in_progress
    if active:
        _update_window_scan(scan_phase=phase, scan_status=status)


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


def _process_email(
    preview: GmailMessagePreview,
    full_context: Optional[str] = None,
    classification=None,
) -> None:
    """Process a single email: classify, save JD, create draft, log."""
    global _state

    # Classify
    # Fetch the complete thread just before deciding or composing a reply. The
    # UI/log continues to use previews, so full mail content remains local.
    full_context = full_context if full_context is not None else (
        get_thread_context(preview.thread_id) or preview.full_body
    )
    classification = classification or classify_email(preview, full_context)

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
        _set_window_scan_phase("capturing_jd", f"Capturing JD files: {preview.subject[:80]}")
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
        _set_window_scan_phase("composing_draft", f"Composing a draft reply: {preview.subject[:80]}")
        with _lock:
            _state.drafts_in_progress += 1
            _state.draft_subject = preview.subject
            _state.draft_status = "Reading the full conversation and composing a draft…"
        try:
            draft = generate_contextual_draft(
                sender=preview.sender,
                subject=preview.subject,
                full_context=full_context,
                classification=classification,
                config=_config,
            )
        finally:
            with _lock:
                _state.drafts_in_progress = max(0, _state.drafts_in_progress - 1)
                _state.draft_status = ""
                _state.draft_subject = ""
        if draft:
            # Attempt real Gmail API draft creation first
            gmail_draft_id = None
            existing_gmail_draft = False
            try:
                from jarvis_core.gmail_service import (
                    create_draft as create_gmail_draft,
                    gmail_draft_exists,
                )
                existing_gmail_draft = gmail_draft_exists(draft.to, draft.subject)
                if not existing_gmail_draft:
                    gmail_draft_id = create_gmail_draft(
                        to=draft.to,
                        subject=draft.subject,
                        body=draft.body,
                    )
                    if gmail_draft_id:
                        logger.info("Gmail draft created: id=%s", gmail_draft_id[:8] + "...")
                else:
                    logger.info("Matching Gmail draft already exists; skipping duplicate")
            except Exception as e:
                error_msg = str(e)
                if any(w in error_msg.lower() for w in ("insufficient", "scope", "permission", "forbidden")):
                    logger.warning("Gmail draft permission missing — falling back to local file only")
                    with _lock:
                        _state.error_message = "Gmail draft permission missing. Reconnect Gmail with draft permission."
                else:
                    logger.warning("Gmail API draft creation failed (%s) — falling back to local file", error_msg[:80])

            # A Gmail draft already available is the desired result. Only make
            # a local backup when a matching Gmail draft does not exist yet.
            if not existing_gmail_draft:
                draft_path = save_draft_to_file(draft)
                if draft_path:
                    event.draft_reply_path = str(draft_path)

            # Verify draft exists before counting — prevent phantom counts
            gmail_draft_verified = False
            if gmail_draft_id:
                try:
                    _set_window_scan_phase("verifying_draft", "Verifying the Gmail draft was saved…")
                    from jarvis_core.gmail_service import verify_draft_exists
                    gmail_draft_verified = verify_draft_exists(gmail_draft_id)
                    if not gmail_draft_verified:
                        logger.warning("Gmail draft id=%s returned by API but NOT verified — not counting", gmail_draft_id[:8])
                except Exception as ve:
                    logger.warning("Draft verification error: %s", str(ve)[:60])
                    gmail_draft_verified = False

            if existing_gmail_draft:
                logger.info("Existing Gmail draft retained; no duplicate created")
            elif gmail_draft_verified:
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
            # Persist each completed item so a restart resumes from the next
            # unprocessed message rather than replaying an entire scan.
            _save_processed_ids(processed_ids)
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


def run_window_rescan(days_back: int, source: str = "manual_rescan") -> dict:
    """Read every inbox mail in a selected 1–30 day window.

    Every selected message is read and classified with its complete thread.
    Previously completed messages are deliberately not drafted or saved again,
    but are still reviewed so the result genuinely reflects the selected range.
    New messages are written to the processed ledger immediately, which makes
    a restart resume safely without duplicate side effects.
    """
    days = max(1, min(int(days_back), 30))
    with _lock:
        important_before = _state.important_mails_today
        jd_before = _state.jd_files_saved_today
        drafts_before = _state.drafts_created_today + _state.local_drafts_today

    result = {
        "days_back": days, "emails_scanned": 0, "already_reviewed": 0,
        "important_found": 0, "jd_files_saved": 0, "drafts_created": 0,
        "errors": 0, "warnings": [],
    }
    _update_window_scan(
        scan_phase="fetching",
        scan_status=f"Fetching all inbox mail from the last {days} day(s)…",
        scan_days_back=days,
        scan_current=0,
        scan_total=0,
        scan_summary=result.copy(),
    )
    try:
        messages = list_messages_for_days(days, max_results=1500)
    except Exception as exc:
        raise RuntimeError(f"Gmail API error while reading the {days}-day window.") from exc

    logger.info("Window rescan started: %d messages from last %d days", len(messages), days)
    _update_window_scan(
        scan_phase="reading",
        scan_status=f"Reading message 0 of {len(messages)} in the selected history…",
        scan_total=len(messages),
        scan_summary=result.copy(),
    )
    return _review_selected_messages(
        messages,
        result,
        important_before=important_before,
        jd_before=jd_before,
        drafts_before=drafts_before,
    )


def _review_selected_messages(
    messages: list[GmailMessagePreview],
    result: dict,
    *,
    important_before: int,
    jd_before: int,
    drafts_before: int,
) -> dict:
    """Run selected messages through the established classifier/JD/draft flow."""
    processed_ids = _load_processed_ids()
    for index, preview in enumerate(messages, start=1):
        result["emails_scanned"] += 1
        _update_window_scan(
            scan_phase="reading",
            scan_status=f"Reading {index} of {len(messages)}: {preview.subject[:80] or 'Untitled email'}",
            scan_current=index,
            scan_total=len(messages),
            scan_summary=result.copy(),
        )
        try:
            # Reading the entire conversation and classifying it is required
            # for every item, including already-actioned messages.  The ledger
            # controls only side effects (drafts, files, mark-as-read).
            full_context = get_thread_context(preview.thread_id) or preview.full_body
            classification = classify_email(preview, full_context)
            if preview.message_id in processed_ids:
                result["already_reviewed"] += 1
            else:
                _process_email(preview, full_context=full_context, classification=classification)
                processed_ids.add(preview.message_id)
                _save_processed_ids(processed_ids)
        except Exception:
            logger.exception("Window rescan: could not process one message")
            result["errors"] += 1
            result["warnings"].append("One email could not be processed and will be retried later.")
        finally:
            _update_window_scan(
                scan_current=index,
                scan_total=len(messages),
                scan_summary=result.copy(),
            )

    with _lock:
        result["important_found"] = max(0, _state.important_mails_today - important_before)
        result["jd_files_saved"] = max(0, _state.jd_files_saved_today - jd_before)
        result["drafts_created"] = max(
            0, (_state.drafts_created_today + _state.local_drafts_today) - drafts_before
        )
    return result


def run_date_range_scan(start_date, end_date, source: str = "date_range") -> dict:
    """Synchronously scan one exact inclusive local-calendar date range.

    This is the shared command-service entry point used by Jarvis regardless of
    whether the command originated in the Web UI or Telegram.
    """
    from datetime import date as _date

    if not isinstance(start_date, _date) or not isinstance(end_date, _date):
        raise ValueError("A valid Gmail start and end date are required.")
    if end_date < start_date:
        raise ValueError("The Gmail end date cannot be before the start date.")
    inclusive_days = (end_date - start_date).days + 1
    if inclusive_days > 31:
        raise ValueError("Gmail date ranges are limited to 31 calendar days.")
    if _scan_running.is_set():
        raise RuntimeError("Another Gmail scan is already running. Please try again shortly.")

    with _lock:
        important_before = _state.important_mails_today
        jd_before = _state.jd_files_saved_today
        drafts_before = _state.drafts_created_today + _state.local_drafts_today

    result = {
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "days_back": inclusive_days,
        "emails_scanned": 0,
        "already_reviewed": 0,
        "important_found": 0,
        "jd_files_saved": 0,
        "drafts_created": 0,
        "errors": 0,
        "warnings": [],
    }
    _scan_running.set()
    _update_window_scan(
        scan_in_progress=True,
        scan_phase="fetching",
        scan_status=f"Fetching Gmail from {start_date.isoformat()} through {end_date.isoformat()}…",
        scan_days_back=inclusive_days,
        scan_current=0,
        scan_total=0,
        scan_summary=result.copy(),
    )
    try:
        messages = list_messages_for_date_range(start_date, end_date, max_results=1500)
        _update_window_scan(
            scan_phase="reading",
            scan_status=f"Reading message 0 of {len(messages)} in the requested range…",
            scan_total=len(messages),
            scan_summary=result.copy(),
        )
        result = _review_selected_messages(
            messages,
            result,
            important_before=important_before,
            jd_before=jd_before,
            drafts_before=drafts_before,
        )
        checked_at = datetime.now().isoformat()
        result["checked_at"] = checked_at
        _update_window_scan(
            scan_in_progress=False,
            scan_phase="completed",
            scan_status=(
                f"Completed: read {result['emails_scanned']} email(s) for the requested range."
            ),
            scan_summary=result.copy(),
        )
        with _lock:
            _state.last_checked = checked_at
            _state.last_poll_success_at = checked_at
            if _state.error_message.startswith("Gmail range scan failed"):
                _state.error_message = ""
        return result
    except Exception as exc:
        safe_reason = str(exc)[:180] or "The requested Gmail range could not be read."
        logger.error("Exact Gmail date-range scan failed: %s", safe_reason)
        _update_window_scan(
            scan_in_progress=False,
            scan_phase="failed",
            scan_status=safe_reason,
            scan_summary={**result, "errors": max(1, result.get("errors", 0)), "warnings": [safe_reason]},
        )
        with _lock:
            _state.error_message = f"Gmail range scan failed: {safe_reason}"
            _state.last_poll_at = datetime.now().isoformat()
        raise RuntimeError(safe_reason) from exc
    finally:
        _scan_running.clear()


def _window_rescan_worker(days_back: int) -> None:
    """Run one manual scan without tying its lifetime to the web panel."""
    acquired_scan_slot = False
    try:
        # An automatic poll/check may already be using Gmail. Queue this job
        # rather than rejecting the user's request, then claim the shared slot.
        for _ in range(30):
            if not _scan_running.is_set():
                _scan_running.set()
                acquired_scan_slot = True
                break
            _update_window_scan(
                scan_phase="queued",
                scan_status="Waiting for the current Gmail check to finish…",
            )
            time.sleep(2)
        if not acquired_scan_slot:
            raise RuntimeError("The Gmail scan queue is busy. Please try again in a moment.")
        result = run_window_rescan(days_back, source="manual_rescan")
        completed_at = datetime.now().isoformat()
        result["checked_at"] = completed_at
        _update_window_scan(
            scan_in_progress=False,
            scan_phase="completed",
            scan_status=(
                f"Completed: read {result['emails_scanned']} email(s), saved "
                f"{result['jd_files_saved']} JD file(s), and created "
                f"{result['drafts_created']} draft(s)."
            ),
            scan_summary=result,
        )
        with _lock:
            _state.last_checked = completed_at
            _state.last_poll_success_at = completed_at
            if _state.error_message.startswith("Gmail scan failed"):
                _state.error_message = ""
    except Exception as exc:
        logger.exception("Window rescan failed")
        safe_reason = str(exc)[:180] or "The selected Gmail window could not be read."
        _update_window_scan(
            scan_in_progress=False,
            scan_phase="failed",
            scan_status=safe_reason,
            scan_summary={"days_back": days_back, "errors": 1, "warnings": [safe_reason]},
        )
        with _lock:
            _state.error_message = f"Gmail scan failed: {safe_reason}"
            _state.last_poll_at = datetime.now().isoformat()
    finally:
        if acquired_scan_slot:
            _scan_running.clear()


def start_window_rescan(days_back: int) -> dict:
    """Start a persistent manual history scan and return immediately."""
    global _window_scan_thread
    days = max(1, min(int(days_back), 30))
    with _lock:
        if _state.scan_in_progress:
            return {
                "status": "already_running",
                "message": _state.scan_status or "A Gmail scan is already running.",
                "days_back": _state.scan_days_back,
            }
        _state.scan_in_progress = True
        _state.scan_phase = "queued"
        _state.scan_status = f"Preparing a detailed scan of the last {days} day(s)…"
        _state.scan_days_back = days
        _state.scan_current = 0
        _state.scan_total = 0
        _state.scan_summary = {"days_back": days, "emails_scanned": 0, "errors": 0}

    _save_window_scan_state()
    _window_scan_thread = threading.Thread(
        target=_window_rescan_worker,
        args=(days,),
        daemon=True,
        name="gmail-window-rescan",
    )
    _window_scan_thread.start()
    return {
        "status": "started",
        "message": f"Detailed {days}-day Gmail scan started in the background.",
        "days_back": days,
    }


def _resume_window_rescan(days_back: int) -> None:
    """Resume a persisted manual scan after a Jarvis restart."""
    result = start_window_rescan(days_back)
    if result.get("status") not in {"started", "already_running"}:
        logger.warning("Could not resume Gmail history scan: %s", result.get("message", "unknown error"))


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
            _save_processed_ids(processed_ids)
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
        _config.enabled = True
        _config.save(CONFIG_FILE)

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
            # Keep restart behavior aligned with the user's explicit stop even
            # if the worker has already exited after an error.
            _config.enabled = False
            _config.save(CONFIG_FILE)
            return {"status": "not_running"}

        _stop_event.set()
        _state.running = False
        _config.enabled = False
        _config.save(CONFIG_FILE)

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
            safe_message = "Failed to fetch emails from Gmail. Please check your connection."
            failed_at = datetime.now().isoformat()
            with _lock:
                _state.error_message = f"Gmail scan failed: {safe_message}"
                _state.last_poll_at = failed_at
                _state.scan_phase = "failed"
                _state.scan_status = safe_message
                _state.scan_summary = {"errors": 1, "warnings": [safe_message]}
            return {
                "status": "error",
                "message": safe_message,
                "error_code": "gmail_api_error"
            }
        finally:
            _scan_running.clear()
        
        # Update state
        checked_at = datetime.now().isoformat()
        with _lock:
            _state.last_checked = checked_at
            _state.last_poll_success_at = checked_at
            if _state.error_message.startswith("Gmail scan failed"):
                _state.error_message = ""
            _state.scan_phase = "completed"
            _state.scan_status = "Check completed" if errors == 0 else "Check completed with warnings"
            _state.scan_summary = {
                "emails_scanned": emails_scanned,
                "important_found": important_found,
                "jd_files_saved": jd_files_saved,
                "drafts_created": drafts_created,
                "errors": errors,
            }
        
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
        safe_message = f"An unexpected error occurred: {str(e)[:100]}"
        with _lock:
            _state.error_message = f"Gmail scan failed: {safe_message}"
            _state.last_poll_at = datetime.now().isoformat()
            _state.scan_phase = "failed"
            _state.scan_status = safe_message
            _state.scan_summary = {"errors": 1, "warnings": [safe_message]}
        return {
            "status": "error",
            "message": safe_message,
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

    # A monitor explicitly running before a restart is resumed in the
    # background. Closing the Operations Center does not affect this flag.
    if _config.enabled:
        threading.Thread(target=start_monitor, daemon=True).start()

    # A user-initiated scan is also durable. If Jarvis was restarted while it
    # was reading mail, resume the same selected range in the background. The
    # processed ledger prevents duplicate files, drafts, and read operations.
    pending_job = _load_persistent_state().get("window_scan", {})
    if pending_job.get("in_progress"):
        days = max(1, min(int(pending_job.get("days_back", 7)), 30))
        threading.Thread(target=_resume_window_rescan, args=(days,), daemon=True).start()
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


def delete_mail_memory() -> dict:
    """Delete Jarvis's local Gmail processing memory without deleting user data.

    Gmail messages, Gmail drafts, and the saved JD files are deliberately left
    intact. A following history rescan re-evaluates all mail and restores only
    outputs that are currently missing.
    """
    with _lock:
        if _state.scan_in_progress:
            return {
                "status": "error",
                "message": "A Gmail scan is still running. Wait for it to finish before deleting memory.",
            }

    try:
        _ensure_dirs()
        _save_processed_ids(set())

        cleared_logs = 0
        for log_path in NOTES_DIR.glob("gmail_monitor_log_*.jsonl"):
            try:
                log_path.unlink()
                cleared_logs += 1
            except OSError as exc:
                logger.warning("Could not clear Gmail activity log %s: %s", log_path.name, exc)

        with _lock:
            _state.important_mails_today = 0
            _state.jd_files_saved_today = 0
            _state.drafts_created_today = 0
            _state.local_drafts_today = 0
            _state.last_checked = None
            _state.last_poll_at = None
            _state.last_poll_success_at = None
            _state.error_message = ""
            _state.drafts_in_progress = 0
            _state.draft_status = ""
            _state.draft_subject = ""
            _state.scan_in_progress = False
            _state.scan_phase = ""
            _state.scan_status = ""
            _state.scan_days_back = 0
            _state.scan_current = 0
            _state.scan_total = 0
            _state.scan_summary = {}

        persisted = _load_persistent_state()
        # Keep full_scan_done so the user can immediately choose a range and
        # click Rescan; only the remembered mail processing state is removed.
        persisted.pop("last_scan_at", None)
        persisted["window_scan"] = {"in_progress": False}
        _save_persistent_state(persisted)

        logger.info("Gmail processing memory deleted; %d local activity logs cleared", cleared_logs)
        return {
            "status": "ok",
            "message": "Jarvis Gmail memory was deleted. Gmail drafts and JD files were kept.",
            "activity_logs_cleared": cleared_logs,
        }
    except Exception as exc:
        logger.exception("Could not delete Gmail processing memory")
        return {"status": "error", "message": "Could not delete Gmail memory safely."}
