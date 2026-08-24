"""Authoritative Gmail status, date parsing, reports, and draft actions.

This module is channel-neutral.  The Web UI, Jarvis command pipeline, and
Telegram adapter all consume the same monitor state, Gmail API service, event
logs, and existing Gmail draft identifiers through these functions.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from jarvis_core.timezone_utils import jarvis_now, jarvis_timezone, to_jarvis_datetime


class GmailPeriodError(ValueError):
    """Raised when a Gmail date expression is missing or unsafe to infer."""


@dataclass(frozen=True)
class GmailDateRange:
    start: date
    end: date
    label: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def public(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "label": self.label,
            "display": format_date_range(self.start, self.end),
            "days": self.days,
        }


_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def local_today(now: Optional[datetime] = None) -> date:
    return to_jarvis_datetime(now).date() if now is not None else jarvis_now().date()


def format_date_range(start: date, end: date) -> str:
    if start == end:
        return start.strftime("%b %d, %Y").replace(" 0", " ")
    if start.year == end.year and start.month == end.month:
        return (
            f"{start.strftime('%b')} {start.day}–{end.day}, {end.year}"
        )
    if start.year == end.year:
        return f"{start.strftime('%b')} {start.day}–{end.strftime('%b')} {end.day}, {end.year}"
    return (
        f"{start.strftime('%b')} {start.day}, {start.year}–"
        f"{end.strftime('%b')} {end.day}, {end.year}"
    )


def _parse_date_token(value: str, today: date) -> date:
    token = value.strip().strip(".,")
    for pattern, order in (
        (r"(\d{4})-(\d{1,2})-(\d{1,2})", "ymd"),
        (r"(\d{1,2})/(\d{1,2})/(\d{4})", "dmy"),
    ):
        match = re.fullmatch(pattern, token)
        if match:
            first, second, third = (int(part) for part in match.groups())
            if order == "ymd":
                return date(first, second, third)
            return date(third, second, first)

    match = re.fullmatch(
        r"([A-Za-z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?",
        token,
        flags=re.IGNORECASE,
    )
    if match:
        month = _MONTHS.get(match.group(1).lower())
        if month is None:
            raise GmailPeriodError(f"Unknown month in '{value.strip()}'.")
        return date(int(match.group(3) or today.year), month, int(match.group(2)))
    raise GmailPeriodError(
        "Use dates like Aug 19 to Aug 22, 19/08/2026 to 22/08/2026, or 2026-08-19 to 2026-08-22."
    )


def parse_gmail_period(request: str, *, today: Optional[date] = None) -> GmailDateRange:
    """Interpret one supported Gmail calendar expression exactly."""
    current = today or local_today()
    text = re.sub(r"\s+", " ", str(request or "").strip().lower())
    text = re.sub(r"^gmail\s+(?:status|report|refresh|rescan)\b", "", text).strip(" :,-")
    if not text:
        raise GmailPeriodError("Choose Today, Last 2 Days, Last 3 Days, or provide a custom range.")

    if text == "today":
        return GmailDateRange(current, current, "TODAY")
    if text == "yesterday":
        target = current - timedelta(days=1)
        return GmailDateRange(target, target, "YESTERDAY")
    if text == "last week":
        return GmailDateRange(current - timedelta(days=6), current, "LAST WEEK")

    match = re.fullmatch(r"(\d+)\s+days?\s+ago", text)
    if match:
        offset = int(match.group(1))
        target = current - timedelta(days=offset)
        return GmailDateRange(target, target, f"{offset} DAYS AGO")

    match = re.fullmatch(r"last\s+(\d+)\s+days?", text)
    if match:
        days = int(match.group(1))
        if days < 1 or days > 31:
            raise GmailPeriodError("Gmail ranges must be between 1 and 31 calendar days.")
        return GmailDateRange(current - timedelta(days=days - 1), current, f"LAST {days} DAYS")

    match = re.fullmatch(r"from\s+(\d+)\s+days?\s+ago", text)
    if match:
        offset = int(match.group(1))
        if offset > 30:
            raise GmailPeriodError("Gmail ranges are limited to 31 calendar days.")
        return GmailDateRange(current - timedelta(days=offset), current, f"FROM {offset} DAYS AGO")

    range_text = re.sub(r"^from\s+", "", text).strip()
    match = re.fullmatch(r"(.+?)\s+to\s+(.+)", range_text)
    if match:
        start = _parse_date_token(match.group(1), current)
        end = _parse_date_token(match.group(2), current)
        if end < start:
            raise GmailPeriodError("The Gmail end date cannot be before the start date.")
        if (end - start).days + 1 > 31:
            raise GmailPeriodError("Gmail ranges are limited to 31 calendar days.")
        return GmailDateRange(start, end, "CUSTOM RANGE")

    raise GmailPeriodError(
        "I could not read that Gmail period. Try 'yesterday', '2 days ago', 'last 3 days', or 'Aug 19 to Aug 22'."
    )


def period_from_key(key: str, *, today: Optional[date] = None) -> GmailDateRange:
    mapping = {"today": "today", "last2": "last 2 days", "last3": "last 3 days"}
    if key not in mapping:
        raise GmailPeriodError("Unknown Gmail period selection.")
    return parse_gmail_period(mapping[key], today=today)


def _safe_reason(value: Any, fallback: str = "Gmail status is unavailable.") -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return fallback
    text = re.sub(
        r"(?i)(\"?(?:token|secret|password|client[_-]?secret|access[_-]?token|refresh[_-]?token)\"?\s*[:=]\s*)(?:\"[^\"]*\"|\S+)",
        r"\1[redacted]",
        text,
    )
    return text[:240]


def _parse_iso(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return to_jarvis_datetime(parsed)
    except (TypeError, ValueError):
        return None


def build_authoritative_gmail_status(
    *,
    state,
    connected: bool,
    credentials_configured: bool,
    token_configured: bool,
    credentials_error: str = "",
    token_health: Optional[dict] = None,
    token_revoked: bool = False,
    last_scan_at: str = "",
    now: Optional[datetime] = None,
) -> dict:
    """Build one truthful status from facts already used by the Gmail UI."""
    state_data = state.model_dump() if hasattr(state, "model_dump") else dict(state or {})
    token_health = token_health or {}
    current = to_jarvis_datetime(now) if now is not None else jarvis_now()
    running = bool(state_data.get("running"))
    scan_phase = str(state_data.get("scan_phase", ""))
    scan_status = _safe_reason(state_data.get("scan_status", ""), "")
    monitor_error = _safe_reason(state_data.get("error_message", ""), "")
    last_success = str(
        state_data.get("last_poll_success_at")
        or state_data.get("last_checked")
        or last_scan_at
        or ""
    )

    issue = ""
    operational_status = "ok"
    if not credentials_configured:
        operational_status, issue = "error", _safe_reason(credentials_error, "Gmail credentials are not configured.")
    elif not token_configured:
        operational_status, issue = "error", "Gmail authentication is not configured. Reconnect Gmail."
    elif token_revoked:
        operational_status, issue = "error", "Gmail authentication was revoked. Reconnect Gmail."
    elif not connected:
        operational_status, issue = "error", _safe_reason(credentials_error or monitor_error, "Gmail is not connected.")
    elif token_health and not token_health.get("valid", False) and not token_health.get("has_refresh_token", False):
        operational_status, issue = "error", "Gmail authentication is expired and cannot be refreshed."
    elif scan_phase == "failed":
        operational_status, issue = "error", scan_status or "The latest Gmail scan failed."
    elif monitor_error:
        operational_status, issue = "error", monitor_error
    elif not running:
        operational_status, issue = "paused", "Gmail monitoring is paused."
    elif not last_success:
        operational_status, issue = "unavailable", "No successful Gmail scan is available yet."
    else:
        last_dt = _parse_iso(last_success)
        interval = max(1, int(state_data.get("poll_interval_minutes") or 5))
        if last_dt is not None:
            if last_dt.tzinfo is None:
                last_dt = last_dt.astimezone()
            age = current - last_dt.astimezone(current.tzinfo)
            if age > timedelta(minutes=max(15, interval * 2 + 2)):
                operational_status = "stale"
                issue = f"Gmail data is stale; last successful scan was {last_success}."

    return {
        "operational_status": operational_status,
        "healthy": operational_status == "ok",
        "connected": bool(connected),
        "monitoring_active": running,
        "monitoring_paused": bool(connected and not running),
        "message": issue or "Gmail monitoring and the latest scan are current.",
        "safe_error": issue if operational_status in {"error", "unavailable", "stale"} else "",
        "latest_scan_phase": scan_phase,
        "latest_scan_status": scan_status,
        "latest_scan_summary": state_data.get("scan_summary") or {},
        "scan_in_progress": bool(state_data.get("scan_in_progress")),
        "last_checked": str(state_data.get("last_checked") or ""),
        "last_poll_at": str(state_data.get("last_poll_at") or ""),
        "last_successful_scan": last_success,
    }


def get_authoritative_gmail_status() -> dict:
    from jarvis_core.gmail_monitor import get_last_scan_timestamp, get_state
    from jarvis_core.gmail_service import (
        credentials_exist,
        get_credentials_error,
        is_connected,
        is_token_revoked,
        token_exists,
        validate_token_health,
    )

    return build_authoritative_gmail_status(
        state=get_state(),
        connected=is_connected(),
        credentials_configured=credentials_exist(),
        token_configured=token_exists(),
        credentials_error=get_credentials_error() or "",
        token_health=validate_token_health(),
        token_revoked=is_token_revoked(),
        last_scan_at=get_last_scan_timestamp() or "",
    )


def _event_datetime(event: dict) -> Optional[datetime]:
    raw = str(event.get("email_date") or "").strip()
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
            return to_jarvis_datetime(parsed)
        except (TypeError, ValueError, OverflowError):
            parsed = _parse_iso(raw)
            if parsed:
                return parsed
    return _parse_iso(event.get("detected_at"))


def list_events_for_range(period: GmailDateRange) -> list[dict]:
    """Read safe stored Gmail events and filter by the email's received date."""
    from jarvis_core.gmail_monitor import NOTES_DIR

    safe_fields = {
        "detected_at", "email_date", "from", "subject", "category", "importance",
        "summary", "action_required", "attachments_saved", "jd_saved_path",
        "draft_reply_path", "gmail_message_id", "gmail_thread_id", "gmail_draft_id",
    }
    by_message: dict[str, dict] = {}
    for path in sorted(NOTES_DIR.glob("gmail_monitor_log_*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                raw = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            received = _event_datetime(raw)
            if received is None or not (period.start <= to_jarvis_datetime(received).date() <= period.end):
                continue
            event = {key: raw.get(key) for key in safe_fields if key in raw}
            identity = str(event.get("gmail_message_id") or f"{event.get('from')}|{event.get('subject')}|{event.get('email_date')}")
            by_message[identity] = event
    return sorted(by_message.values(), key=lambda item: _event_datetime(item) or datetime.min.replace(tzinfo=jarvis_timezone()))


def _is_jd(event: dict) -> bool:
    category = str(event.get("category", "")).lower()
    return bool(
        "jd" in category
        or "job_description" in category
        or category in {
            "recruiter", "recruitment", "interview_invite",
            "interview_confirmation", "client_interview", "shortlisted",
            "selection", "offer_letter", "salary_discussion",
        }
        or event.get("jd_saved_path")
        or event.get("attachments_saved")
    )


def _is_important(event: dict) -> bool:
    category = str(event.get("category", "")).lower()
    importance = str(event.get("importance", "")).lower()
    return importance in {"high", "medium", "important"} or category not in {"", "unknown", "non_job"}


def _needs_review(event: dict) -> bool:
    action = str(event.get("action_required", "")).strip().lower()
    category = str(event.get("category", "")).lower()
    return bool(action) or category in {"", "unknown"}


def _drafts_from_events(events: list[dict]) -> list[dict]:
    from jarvis_core.gmail_service import get_gmail_draft

    drafts = []
    seen = set()
    for event in events:
        draft_id = str(event.get("gmail_draft_id") or "").strip()
        if not draft_id or draft_id in seen:
            continue
        seen.add(draft_id)
        draft = get_gmail_draft(draft_id) or {
            "id": draft_id,
            "to": str(event.get("from") or ""),
            "subject": str(event.get("subject") or ""),
            "body": "",
        }
        draft.update({
            "source_message_id": str(event.get("gmail_message_id") or ""),
            "source_thread_id": str(event.get("gmail_thread_id") or ""),
            "source_sender": str(event.get("from") or ""),
            "source_subject": str(event.get("subject") or ""),
            "received": str(event.get("email_date") or ""),
            "received_at_display": str(event.get("received_at_display") or ""),
        })
        drafts.append(draft)
    return drafts


def _gmail_drafts_for_period(period: GmailDateRange) -> list[dict]:
    """Return real drafts created in the period, using Jarvis timezone."""
    from jarvis_core.gmail_service import list_gmail_drafts

    drafts = []
    for draft in list_gmail_drafts(max_results=500):
        try:
            created_at = datetime.fromtimestamp(
                int(draft.get("internal_date", "")) / 1000,
                tz=timezone.utc,
            ).astimezone(jarvis_timezone())
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        if period.start <= created_at.date() <= period.end:
            drafts.append({
                **draft,
                "created_at": created_at.isoformat(),
                "created_at_display": created_at.strftime("%b %d, %Y %I:%M:%S %p %z").replace(" 0", " "),
            })
    return drafts


def _format_time(value: str) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        return value or "Unavailable"
    return to_jarvis_datetime(parsed).strftime("%b %d, %Y %I:%M:%S %p %z").replace(" 0", " ")


def _event_with_jarvis_received_time(event: dict) -> dict:
    received = _event_datetime(event)
    if received is None:
        return {**event, "received_at_jarvis": "", "received_at_display": "Unknown"}
    local_received = to_jarvis_datetime(received)
    return {
        **event,
        "received_at_jarvis": local_received.isoformat(),
        "received_at_display": local_received.strftime("%b %d, %Y %I:%M:%S %p %z").replace(" 0", " "),
    }


def format_gmail_report(report: dict) -> str:
    period = report["period"]
    status = report.get("report_status", "completed")
    status_text = "✅ Completed" if status == "completed" else "⚠️ Completed with errors"
    gmail_status = report.get("gmail_status", {})
    connected = bool(gmail_status.get("connected"))
    lines = [
        f"📧 GMAIL — {period['label']}",
        "",
        f"Status: {'✅ Connected' if connected else '❌ Disconnected'}",
        f"Scan: {status_text}",
        f"Period: {period['display']}",
        "Email counts use received date (Jarvis timezone).",
        "Draft counts use draft created date (Jarvis timezone).",
        "",
        f"📥 Emails checked: {report.get('emails_checked', 0)}",
        "",
        f"⭐ Important: {report.get('important', 0)}",
        f"💼 Recruiter/JD: {report.get('recruiter_jd', report.get('jd_emails', 0))}",
        f"📎 JD files saved: {report.get('jd_files_saved', 0)}",
        f"📝 Drafts: {report.get('drafts_created', 0)}",
        f"⚠️ Needs review: {report.get('needs_review', 0)}",
        f"❌ Errors: {report.get('errors', 0)}",
        "",
        f"Last checked: {_format_time(report.get('checked_at', ''))}",
    ]
    monitor = gmail_status
    if monitor.get("monitoring_paused"):
        lines.append("Monitoring: ⏸ Paused")
    elif monitor.get("monitoring_active"):
        lines.append("Monitoring: ✅ Active")
    elif monitor.get("safe_error"):
        lines.append(f"Gmail status: ⚠️ {monitor['safe_error']}")
    return "\n".join(lines)


def _format_failure(period: GmailDateRange, reason: str, status: dict) -> str:
    lines = [
        "❌ GMAIL SCAN FAILED",
        f"Period: {format_date_range(period.start, period.end)}",
        f"Reason: {_safe_reason(reason)}",
    ]
    if status.get("last_successful_scan"):
        lines.append(f"Last successful scan: {_format_time(status['last_successful_scan'])}")
    return "\n".join(lines)


def run_gmail_report(request: str) -> dict:
    """Scan/query a requested period and return a channel-neutral report."""
    try:
        period = parse_gmail_period(request)
    except GmailPeriodError as exc:
        return {"status": "error", "kind": "gmail_report", "message": str(exc), "summary": str(exc)}

    before = get_authoritative_gmail_status()
    if not before.get("connected"):
        summary = _format_failure(period, before.get("message", "Gmail is not connected."), before)
        return {
            "status": "error", "kind": "gmail_report", "message": before.get("message"),
            "summary": summary, "period": period.public(), "gmail_status": before,
        }

    from jarvis_core.gmail_monitor import run_date_range_scan

    try:
        scan = run_date_range_scan(period.start, period.end, source="jarvis_command")
    except Exception as exc:
        current = get_authoritative_gmail_status()
        reason = current.get("safe_error") or _safe_reason(exc, "The requested Gmail range could not be read.")
        summary = _format_failure(period, reason, current)
        return {
            "status": "error", "kind": "gmail_report", "message": reason,
            "summary": summary, "period": period.public(), "gmail_status": current,
        }

    events = list_events_for_range(period)
    drafts = _gmail_drafts_for_period(period)
    current = get_authoritative_gmail_status()
    report = {
        "period": period.public(),
        "report_status": "completed" if int(scan.get("errors", 0)) == 0 else "completed_with_errors",
        "emails_checked": int(scan.get("emails_scanned", 0)),
        "important": sum(1 for event in events if _is_important(event)),
        "jd_emails": sum(1 for event in events if _is_jd(event)),
        "recruiter_jd": sum(1 for event in events if _is_jd(event)),
        "jd_files_saved": sum(
            len(event.get("attachments_saved") or []) or int(bool(event.get("jd_saved_path")))
            for event in events
        ),
        "drafts_created": len(drafts),
        "email_date_basis": "received_date_jarvis_timezone",
        "draft_date_basis": "gmail_internal_date_jarvis_timezone",
        "needs_review": sum(1 for event in events if _needs_review(event)),
        "errors": int(scan.get("errors", 0)),
        "checked_at": str(scan.get("checked_at") or current.get("last_checked") or ""),
        "gmail_status": current,
    }
    summary = format_gmail_report(report)
    return {
        "status": "done",
        "kind": "gmail_report",
        "message": "Gmail report completed.",
        "summary": summary,
        "report": report,
        "period": period.public(),
        "events": events,
        "drafts": drafts,
    }


def _period_from_iso(start: str, end: str) -> GmailDateRange:
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError as exc:
        raise GmailPeriodError("Invalid Gmail report range.") from exc
    if end_date < start_date or (end_date - start_date).days + 1 > 31:
        raise GmailPeriodError("Invalid Gmail report range.")
    return GmailDateRange(start_date, end_date, "SELECTED RANGE")


def get_gmail_details(category: str, start: str, end: str) -> dict:
    """Return concise details from the same stored events used by reports/UI."""
    try:
        period = _period_from_iso(start, end)
    except GmailPeriodError as exc:
        return {"status": "error", "message": str(exc)}
    events = list_events_for_range(period)
    drafts: list[dict] = []
    wanted = str(category or "").lower()
    if wanted == "important":
        events = [_event_with_jarvis_received_time(event) for event in events if _is_important(event)]
        title = "⭐ IMPORTANT MAILS"
    elif wanted == "jd":
        events = [_event_with_jarvis_received_time(event) for event in events if _is_jd(event)]
        title = "💼 JD / RECRUITER MAILS"
    elif wanted == "needs_review":
        events = [_event_with_jarvis_received_time(event) for event in events if _needs_review(event)]
        title = "⚠️ NEEDS REVIEW"
    elif wanted == "drafts":
        # Draft selection uses Gmail's internalDate, not the source email's
        # received date. These can fall on different Jarvis calendar days.
        events = []
        drafts = _gmail_drafts_for_period(period)
        title = "📝 GMAIL DRAFTS"
    elif wanted == "errors":
        status = get_authoritative_gmail_status()
        message = status.get("safe_error") or status.get("latest_scan_status") or "No Gmail errors recorded for the latest scan."
        return {"status": "done", "kind": "gmail_details", "category": wanted, "summary": f"❌ GMAIL ERRORS\n{message}", "events": []}
    else:
        return {"status": "error", "message": "Unknown Gmail detail category."}

    lines = [title, f"Period: {format_date_range(period.start, period.end)}", ""]
    if wanted == "drafts":
        lines.append("Date basis: draft created date (Jarvis timezone).")
        lines.append("")
        if not drafts:
            lines.append("No Gmail drafts were created in this period.")
        for index, draft in enumerate(drafts[:10], start=1):
            lines.extend([
                f"{index}. {str(draft.get('subject') or 'No subject')[:120]}",
                f"To: {str(draft.get('to') or 'Unknown recipient')[:120]}",
                f"Draft created (Jarvis time): {draft.get('created_at_display') or _format_time(str(draft.get('created_at') or ''))}",
                "",
            ])
        if len(drafts) > 10:
            lines.append(f"Showing 10 of {len(drafts)} Gmail drafts.")
    elif not events:
        lines.append("No matching emails were stored for this period.")
    for index, event in enumerate(events[:10] if wanted != "drafts" else [], start=1):
        lines.extend([
            f"{index}. {str(event.get('subject') or 'No subject')[:120]}",
            f"From: {str(event.get('from') or 'Unknown sender')[:120]}",
            f"Received (Jarvis time): {event.get('received_at_display') or 'Unknown'}",
            f"Category: {str(event.get('category') or 'unknown')}",
            f"JD file: {'Yes' if event.get('jd_saved_path') or event.get('attachments_saved') else 'No'}",
            f"Reply draft: {'Yes' if event.get('gmail_draft_id') or event.get('draft_reply_path') else 'No'}",
            f"Review required: {'Yes' if _needs_review(event) else 'No'}",
            "",
        ])
    if len(events) > 10:
        lines.append(f"Showing 10 of {len(events)} matching emails.")
    if wanted != "drafts":
        drafts = _drafts_from_events(events)
    return {
        "status": "done", "kind": "gmail_details", "category": wanted,
        "summary": "\n".join(lines).strip(), "events": events,
        "period": period.public(), "drafts": drafts,
    }


def view_gmail_draft(draft_id: str) -> dict:
    from jarvis_core.gmail_service import get_gmail_draft

    draft = get_gmail_draft(draft_id)
    if not draft:
        return {"status": "error", "message": "The Gmail draft could not be found."}
    body = str(draft.get("body") or "").strip() or "(Draft body is empty.)"
    summary = (
        "📝 GMAIL DRAFT\n"
        f"To: {draft.get('to') or 'Unknown'}\n"
        f"Subject: {draft.get('subject') or '(No subject)'}\n"
        f"Draft ID: {draft.get('id')}\n\n"
        f"{body}"
    )
    return {"status": "done", "kind": "gmail_draft", "summary": summary, "draft": draft}


def keep_gmail_draft(draft_id: str) -> dict:
    from jarvis_core.gmail_service import get_gmail_draft

    draft = get_gmail_draft(draft_id)
    if not draft:
        return {"status": "error", "message": "The Gmail draft could not be found."}
    return {
        "status": "done", "kind": "gmail_draft_kept", "draft": draft,
        "summary": f"✏️ Draft kept in Gmail.\nTo: {draft.get('to') or 'Unknown'}\nSubject: {draft.get('subject') or '(No subject)'}",
    }


def send_existing_gmail_draft(draft_id: str) -> dict:
    from jarvis_core.gmail_service import send_gmail_draft

    result = send_gmail_draft(draft_id)
    if result.get("status") != "done":
        return result
    return {
        **result,
        "kind": "gmail_draft_sent",
        "summary": "✅ Gmail draft sent successfully.",
    }


def list_new_draft_events(since: str) -> list[dict]:
    """Return newly logged real Gmail drafts for channel notification adapters."""
    period = GmailDateRange(local_today(), local_today(), "TODAY")
    since_dt = _parse_iso(since) or datetime.now().astimezone()
    events = []
    for event in list_events_for_range(period):
        detected = _parse_iso(event.get("detected_at"))
        if detected and detected > since_dt and event.get("gmail_draft_id"):
            events.append(event)
    return events
