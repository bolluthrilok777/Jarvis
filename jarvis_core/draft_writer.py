"""Draft reply writer for Gmail monitoring.

Generates draft replies for job-related emails but never sends them
unless explicitly configured and confirmed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from jarvis_core.gmail_models import ClassificationResult, DraftContent, GmailConfig

logger = logging.getLogger("jarvis.draft_writer")

# Default user name for drafts
DEFAULT_USER_NAME = "Jarvis User"

# Drafts save directory
DRAFTS_DIR = Path("C:/Jarvis/notes/gmail_drafts")


def _ensure_drafts_dir() -> Path:
    """Ensure the drafts directory exists."""
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    return DRAFTS_DIR


def _extract_name_from_sender(sender: str) -> str:
    """Extract recipient name from sender email."""
    match = re.match(r'^"?([^"<]+)"?\s*<', sender)
    if match:
        return match.group(1).strip()
    # Fall back to email local part
    local_match = re.match(r"([^@]+)@", sender)
    if local_match:
        name = local_match.group(1).replace(".", " ").replace("_", " ").title()
        return name
    return "Hiring Team"


def _extract_email(sender: str) -> str:
    """Extract email address from sender string."""
    match = re.search(r"<([^>]+)>", sender)
    if match:
        return match.group(1)
    # If no angle brackets, it might already be just an email
    if "@" in sender:
        return sender.strip()
    return sender


def generate_draft(
    sender: str,
    subject: str,
    classification: ClassificationResult,
    config: GmailConfig,
    user_name: str = DEFAULT_USER_NAME,
) -> Optional[DraftContent]:
    """Generate a draft reply based on email classification.

    Args:
        sender: Sender email address/string
        subject: Email subject
        classification: Classification result
        config: Gmail monitor config
        user_name: User's name for signature

    Returns:
        DraftContent if draft should be created, None otherwise.
    """
    if not classification.should_create_draft:
        return None

    if not classification.draft_type:
        return None

    recipient_name = _extract_name_from_sender(sender)
    recipient_email = _extract_email(sender)
    draft_type = classification.draft_type

    body = _generate_body(draft_type, recipient_name, subject, user_name)
    draft_subject = _generate_subject(draft_type, subject)

    return DraftContent(
        to=recipient_email,
        subject=draft_subject,
        body=body,
        draft_type=draft_type,
    )


def _generate_body(
    draft_type: str, recipient_name: str, original_subject: str, user_name: str
) -> str:
    """Generate the body text for a draft reply."""
    if draft_type == "jd_received":
        return (
            f"Hi {recipient_name},\n\n"
            f"Thank you for sharing the job description. I have received it and will review the details.\n\n"
            f"Regards,\n{user_name}"
        )
    elif draft_type == "interview":
        return (
            f"Hi {recipient_name},\n\n"
            f"Thank you for the interview invitation. I would like to confirm my availability. "
            f"Please let me know the available slots and I will confirm accordingly.\n\n"
            f"Regards,\n{user_name}"
        )
    elif draft_type == "documents":
        return (
            f"Hi {recipient_name},\n\n"
            f"Thank you for your request. I will share the requested documents shortly.\n\n"
            f"Regards,\n{user_name}"
        )
    elif draft_type == "offer":
        return (
            f"Hi {recipient_name},\n\n"
            f"Thank you for this communication. I have received the information and will review it carefully.\n\n"
            f"Regards,\n{user_name}"
        )
    else:
        return (
            f"Hi {recipient_name},\n\n"
            f"Thank you for your email regarding '{original_subject}'. "
            f"I have received it and will get back to you shortly.\n\n"
            f"Regards,\n{user_name}"
        )


def _generate_subject(draft_type: str, original_subject: str) -> str:
    """Generate the subject line for a draft reply."""
    if not original_subject.lower().startswith("re:"):
        return f"Re: {original_subject}"
    return original_subject


def save_draft_to_file(draft: DraftContent) -> Optional[Path]:
    """Save a draft to a local file for review.

    Args:
        draft: Draft content to save

    Returns:
        Path to saved draft file, or None on failure.
    """
    try:
        drafts_dir = _ensure_drafts_dir()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_to = re.sub(r"[^a-zA-Z0-9]", "_", draft.to)[:30]
        filename = f"draft_{timestamp}_{safe_to}_{draft.draft_type}.txt"
        filepath = drafts_dir / filename

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"To: {draft.to}\n")
            f.write(f"Subject: {draft.subject}\n")
            f.write(f"Type: {draft.draft_type}\n")
            f.write(f"Created: {datetime.now().isoformat()}\n")
            f.write(f"{'='*60}\n")
            f.write(draft.body)
            f.write(f"\n{'='*60}\n")
            f.write("DRAFT ONLY — Not sent. Review before approving.\n")

        logger.info("Saved draft to %s", filepath)
        return filepath

    except Exception as e:
        logger.error("Failed to save draft: %s", e)
        return None


def list_drafts() -> list[dict]:
    """List saved draft files.

    Returns list of draft file info dicts.
    """
    drafts_dir = _ensure_drafts_dir()
    if not drafts_dir.exists():
        return []

    files = []
    for f in sorted(drafts_dir.iterdir(), reverse=True):
        if f.is_file() and f.suffix in (".txt", ".md", ".json"):
            files.append({
                "path": str(f),
                "filename": f.name,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })

    return files


def can_auto_send(
    draft: DraftContent,
    config: GmailConfig,
    sender_email: str,
) -> tuple[bool, str]:
    """Check if a draft can be auto-sent based on config.

    Returns (can_send, reason) tuple.
    """
    if not config.auto_send_enabled:
        return False, "Auto-send is disabled in config"

    if not config.allowed_senders:
        return False, "No allowed senders configured"

    # Check if sender is allowlisted
    sender_ok = False
    for allowed in config.allowed_senders:
        if allowed.lower() in sender_email.lower():
            sender_ok = True
            break

    if not sender_ok:
        return False, f"Sender {sender_email} is not in allowed_senders"

    # Check if category is allowlisted
    if draft.draft_type not in config.allowed_auto_send_categories:
        return False, f"Draft type '{draft.draft_type}' not in allowed_auto_send_categories"

    # Never auto-send sensitive categories
    sensitive_types = {"salary_negotiation", "resignation", "acceptance", "rejection"}
    if draft.draft_type in sensitive_types:
        return False, f"Draft type '{draft.draft_type}' requires manual approval"

    return True, "Auto-send checks passed"