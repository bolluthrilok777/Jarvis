"""Channel-neutral semantic Gmail intent and short-lived follow-up context."""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import date

from jarvis_core.gmail_reporting import GmailDateRange, GmailPeriodError, parse_gmail_period


_EXPLICIT_ACTION_RE = re.compile(
    r"^gmail\s+(?:details\b|draft\s+(?:view|keep|send)\b)", re.IGNORECASE
)
_BLOCKED_NON_REPORT_RE = re.compile(
    r"\b(?:send|compose|write|forward|reply\s+to|mail\s+(?:a|this|the)\s+(?:letter|package))\b",
    re.IGNORECASE,
)
_NON_EMAIL_MAIL_RE = re.compile(
    r"\b(?:mailing\s+address|postal\s+mail|snail\s+mail|mail\s+carrier|mailbox\s+post)\b",
    re.IGNORECASE,
)
_STRONG_GMAIL_RE = re.compile(
    r"\b(?:gmail|inbox|e-?mails?|unread\s+(?:mail|messages?)|drafts?|sent\s+mail|gmail\s+monitoring)\b",
    re.IGNORECASE,
)
_MAIL_WORD_RE = re.compile(r"\bmails?\b", re.IGNORECASE)
_MAIL_QUERY_RE = re.compile(
    r"\b(?:check|show|list|find|any|important|status|received|came|happened|today|yesterday|ago|last|from|do\s+i\s+have|what\s+about|what\s+mails?)\b",
    re.IGNORECASE,
)
_RECRUITMENT_RE = re.compile(
    r"\b(?:recruiters?|recruitment|hr|interviews?|jd|job\s+description|job\s+mails?|hiring|company\s+contact(?:ed)?|company\s+contact\s+me)\b",
    re.IGNORECASE,
)
_RECRUITMENT_QUERY_RE = re.compile(
    r"\b(?:any|check|show|did|what|which|contact(?:ed)?|heard|received|came|today|yesterday|ago|last)\b",
    re.IGNORECASE,
)
_FOLLOWUP_RE = re.compile(
    r"\b(?:ones?|those|them|what\s+about|show|list|open|drafts?|important|recruiters?|recruitment|hr|interviews?|jd|review|today|yesterday|days?\s+ago|last\s+\d+\s+days?|last\s+week)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GmailIntentResolution:
    command: str
    period: GmailDateRange
    category: str = ""
    used_context: bool = False


@dataclass
class _GmailContext:
    start: date
    end: date
    category: str
    expires_at: float


def _extract_period_expression(text: str) -> str:
    low = " ".join(str(text or "").lower().split())
    patterns = (
        r"(?:from\s+)?(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{4}|[a-z]+\s+\d{1,2}(?:,?\s+\d{4})?)\s+to\s+(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{4}|[a-z]+\s+\d{1,2}(?:,?\s+\d{4})?)",
        r"from\s+\d+\s+days?\s+ago",
        r"last\s+\d+\s+days?",
        r"\d+\s+days?\s+ago",
        r"last\s+week",
        r"yesterday",
        r"today",
        r"(?:on\s+)?(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{4}|[a-z]+\s+\d{1,2}(?:,?\s+\d{4})?)",
    )
    for pattern in patterns:
        match = re.search(pattern, low, flags=re.IGNORECASE)
        if match:
            expression = match.group(0).strip()
            return re.sub(r"^on\s+", "", expression)
    return ""


def _category(text: str) -> str:
    low = str(text or "").lower()
    if re.search(r"\bdrafts?\b", low):
        return "drafts"
    if re.search(r"\b(?:needs?\s+review|review\s+required|review\s+items?)\b", low):
        return "needs_review"
    if re.search(r"\bimportant\b", low):
        return "important"
    if _RECRUITMENT_RE.search(low):
        return "jd"
    if re.search(r"\b(?:errors?|failed|failure)\b", low):
        return "errors"
    return ""


def _is_gmail_request(text: str) -> bool:
    if _NON_EMAIL_MAIL_RE.search(text) or _BLOCKED_NON_REPORT_RE.search(text):
        return False
    if re.fullmatch(r"\s*(?:open|launch|go\s+to)\s+gmail\s*", text, flags=re.IGNORECASE):
        return False
    strong = _STRONG_GMAIL_RE.search(text)
    if strong and (_MAIL_QUERY_RE.search(text) or "gmail monitoring" in text.lower()):
        return True
    if _MAIL_WORD_RE.search(text) and _MAIL_QUERY_RE.search(text):
        return True
    if _RECRUITMENT_RE.search(text) and _RECRUITMENT_QUERY_RE.search(text):
        return True
    return False


class GmailIntentRouter:
    def __init__(self, context_ttl_seconds: float = 15 * 60) -> None:
        self.context_ttl_seconds = context_ttl_seconds
        self._contexts: dict[str, _GmailContext] = {}
        self._lock = threading.Lock()

    def clear(self, conversation_id: str | None) -> None:
        if not conversation_id:
            return
        with self._lock:
            self._contexts.pop(conversation_id, None)

    def _get_context(self, conversation_id: str | None) -> _GmailContext | None:
        if not conversation_id:
            return None
        now = time.monotonic()
        with self._lock:
            context = self._contexts.get(conversation_id)
            if context and context.expires_at > now:
                return context
            self._contexts.pop(conversation_id, None)
        return None

    def _save_context(self, conversation_id: str | None, period: GmailDateRange, category: str) -> None:
        if not conversation_id:
            return
        with self._lock:
            self._contexts[conversation_id] = _GmailContext(
                start=period.start,
                end=period.end,
                category=category,
                expires_at=time.monotonic() + self.context_ttl_seconds,
            )

    def resolve(self, text: str, *, conversation_id: str | None = None) -> GmailIntentResolution | None:
        raw = " ".join(str(text or "").strip().split())
        if not raw:
            return None
        if _EXPLICIT_ACTION_RE.match(raw):
            return None

        context = self._get_context(conversation_id)
        explicit = _is_gmail_request(raw)
        contextual = bool(context and _FOLLOWUP_RE.search(raw) and not _BLOCKED_NON_REPORT_RE.search(raw))
        if not explicit and not contextual:
            self.clear(conversation_id)
            return None

        period_expression = _extract_period_expression(raw)
        used_context = False
        if period_expression:
            try:
                period = parse_gmail_period(f"gmail status {period_expression}")
            except GmailPeriodError:
                self.clear(conversation_id)
                return None
        elif context and contextual:
            period = GmailDateRange(context.start, context.end, "FOLLOW-UP PERIOD")
            used_context = True
        else:
            period_expression = "today"
            period = parse_gmail_period("gmail status today")

        category = _category(raw)
        if not category and context and contextual:
            category = context.category

        show_details = bool(category and (
            used_context
            or re.search(r"\b(?:show|list|open)\b", raw, flags=re.IGNORECASE)
            or re.fullmatch(r".*\b(?:drafts?|important\s+ones?|jd\s+mails?)\b[?.!]*", raw, flags=re.IGNORECASE)
        ))
        if show_details:
            command = f"gmail details {category} {period.start.isoformat()} to {period.end.isoformat()}"
        else:
            expression = period_expression or f"{period.start.isoformat()} to {period.end.isoformat()}"
            command = f"gmail status {expression}"

        self._save_context(conversation_id, period, category)
        return GmailIntentResolution(command, period, category, used_context)


gmail_intent_router = GmailIntentRouter()
