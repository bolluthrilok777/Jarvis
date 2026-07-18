"""Gmail API service for reading emails, downloading attachments, and creating drafts.

Uses Gmail API OAuth 2.0. Falls back gracefully if credentials are missing.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import datetime, timezone
from email import message_from_bytes
from pathlib import Path
from typing import Any, Optional

from jarvis_core.gmail_models import GmailMessagePreview

logger = logging.getLogger("jarvis.gmail_service")

# Paths for credentials and token
CONFIG_DIR = Path("C:/Jarvis/config")
CREDENTIALS_FILE = CONFIG_DIR / "gmail_credentials.json"
TOKEN_FILE = CONFIG_DIR / "gmail_token.json"

SCOPES_READ_ONLY = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

SCOPES_WITH_SEND = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

# Global service cache
_gmail_service = None
_credentials_available = False


def credentials_exist() -> bool:
    """Check if Gmail credentials file exists."""
    return CREDENTIALS_FILE.exists()


def token_exists() -> bool:
    """Check if Gmail token file exists."""
    return TOKEN_FILE.exists()


def is_connected() -> bool:
    """Check if Gmail API is connected (token + credentials available)."""
    return credentials_exist() and token_exists()


def _get_credentials() -> Any:
    """Load credentials from file. Returns None if not available."""
    try:
        if not CREDENTIALS_FILE.exists():
            return None
        with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
            creds_data = json.load(f)
        # Return raw dict - we'll use google.oauth2 if available
        return creds_data
    except Exception as e:
        logger.error("Failed to load Gmail credentials: %s", e)
        return None


def _get_token() -> Any:
    """Load token from file. Returns None if not available."""
    try:
        if not TOKEN_FILE.exists():
            return None
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to load Gmail token: %s", e)
        return None


def _build_service() -> Any:
    """Build and return a Gmail API service instance.

    Returns None if credentials/token are missing or if google libraries
    are not installed.
    """
    global _gmail_service, _credentials_available

    if _gmail_service is not None:
        return _gmail_service

    if not is_connected():
        _credentials_available = False
        return None

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        token_data = _get_token()
        if token_data is None:
            return None

        creds = Credentials.from_authorized_user_info(token_data, SCOPES_READ_ONLY)

        # If token is expired, try to refresh
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                # Save refreshed token
                with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
                logger.info("Gmail token refreshed successfully")
            except Exception as e:
                logger.warning("Could not refresh Gmail token: %s", e)
                return None

        if not creds or not creds.valid:
            logger.warning("Gmail credentials are not valid")
            return None

        service = build("gmail", "v1", credentials=creds)
        _gmail_service = service
        _credentials_available = True
        logger.info("Gmail API service built successfully")
        return service

    except ImportError:
        logger.warning(
            "Google API libraries not installed. "
            "Install with: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client"
        )
        return None
    except Exception as e:
        logger.error("Failed to build Gmail service: %s", e)
        return None


def reset_service() -> None:
    """Reset the cached Gmail service (e.g., after token refresh)."""
    global _gmail_service, _credentials_available
    _gmail_service = None
    _credentials_available = False


def list_recent_messages(max_results: int = 20) -> list[GmailMessagePreview]:
    """Fetch recent messages from Gmail inbox.

    Returns a list of GmailMessagePreview objects (privacy-minimized).
    Returns empty list if not connected or on error.
    """
    service = _build_service()
    if service is None:
        logger.warning("Gmail service not available")
        return []

    try:
        # Get list of messages from inbox
        results = (
            service.users()
            .messages()
            .list(userId="me", maxResults=max_results, q="in:inbox")
            .execute()
        )

        messages = results.get("messages", [])
        if not messages:
            return []

        previews = []
        for msg in messages:
            try:
                preview = _get_message_preview(service, msg["id"])
                if preview:
                    previews.append(preview)
            except Exception as e:
                logger.warning("Failed to process message %s: %s", msg["id"], e)
                continue

        return previews

    except Exception as e:
        logger.error("Failed to list Gmail messages: %s", e)
        return []


def list_recent_unread(max_results: int = 20) -> list[GmailMessagePreview]:
    """Fetch recent UNREAD messages from Gmail inbox."""
    service = _build_service()
    if service is None:
        return []

    try:
        results = (
            service.users()
            .messages()
            .list(userId="me", maxResults=max_results, q="in:inbox is:unread")
            .execute()
        )

        messages = results.get("messages", [])
        if not messages:
            return []

        previews = []
        for msg in messages:
            try:
                preview = _get_message_preview(service, msg["id"])
                if preview:
                    previews.append(preview)
            except Exception as e:
                logger.warning("Failed to process message %s: %s", msg["id"], e)
                continue

        return previews

    except Exception as e:
        logger.error("Failed to list unread Gmail messages: %s", e)
        return []


def _get_message_preview(service: Any, message_id: str) -> Optional[GmailMessagePreview]:
    """Get a privacy-minimized preview of a single message."""
    try:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata")
            .execute()
        )

        headers = _parse_headers(msg.get("payload", {}).get("headers", []))
        sender = headers.get("From", "")
        subject = headers.get("Subject", "")
        date = headers.get("Date", "")
        snippet = msg.get("snippet", "")[:200]

        # Get attachment filenames
        attachment_names = _get_attachment_filenames(msg)

        # Get a short body preview (privacy-minimized)
        body_preview = _get_body_preview(msg, max_chars=500)

        return GmailMessagePreview(
            message_id=message_id,
            sender=sender,
            subject=subject,
            snippet=snippet,
            date=date,
            attachment_filenames=attachment_names,
            body_preview=body_preview,
        )

    except Exception as e:
        logger.warning("Error getting message preview for %s: %s", message_id, e)
        return None


def _parse_headers(headers: list[dict]) -> dict[str, str]:
    """Parse email headers into a dict."""
    result = {}
    for h in headers:
        name = h.get("name", "")
        value = h.get("value", "")
        if name:
            result[name] = value
    return result


def _get_attachment_filenames(msg: dict) -> list[str]:
    """Extract attachment filenames from a message."""
    filenames = []
    payload = msg.get("payload", {})

    # Check main payload
    if payload.get("filename"):
        filenames.append(payload["filename"])

    # Check parts
    parts = payload.get("parts", [])
    for part in parts:
        if part.get("filename"):
            filenames.append(part["filename"])
        # Nested parts
        for subpart in part.get("parts", []):
            if subpart.get("filename"):
                filenames.append(subpart["filename"])

    return filenames


def _get_body_preview(msg: dict, max_chars: int = 500) -> str:
    """Get a short body preview from a message (privacy-minimized)."""
    try:
        payload = msg.get("payload", {})
        parts = payload.get("parts", [])

        # Try to get plain text body
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                    # Remove excessive whitespace
                    decoded = re.sub(r"\s+", " ", decoded).strip()
                    return decoded[:max_chars]

        # Fall back to snippet
        return msg.get("snippet", "")[:max_chars]

    except Exception:
        return msg.get("snippet", "")[:max_chars]


def download_attachment(
    message_id: str, attachment_id: str, filename: str, save_dir: Path
) -> Optional[Path]:
    """Download a single attachment from a Gmail message.

    Args:
        message_id: Gmail message ID
        attachment_id: Attachment ID within the message
        filename: Original filename
        save_dir: Directory to save to

    Returns:
        Path to saved file, or None on failure.
    """
    service = _build_service()
    if service is None:
        return None

    try:
        attachment = (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )

        data = attachment.get("data", "")
        if not data:
            logger.warning("No data in attachment %s", attachment_id)
            return None

        file_data = base64.urlsafe_b64decode(data)

        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / _safe_filename(filename)

        with open(save_path, "wb") as f:
            f.write(file_data)

        logger.info("Saved attachment %s to %s", filename, save_path)
        return save_path

    except Exception as e:
        logger.error("Failed to download attachment %s: %s", attachment_id, e)
        return None


def _safe_filename(filename: str) -> str:
    """Sanitize a filename to prevent path traversal."""
    # Remove path separators
    filename = filename.replace("\\", "_").replace("/", "_")
    # Remove null bytes
    filename = filename.replace("\x00", "")
    # Limit length
    if len(filename) > 200:
        name, ext = os.path.splitext(filename)
        filename = name[:190] + ext
    return filename


def get_attachment_ids_and_names(
    message_id: str,
) -> list[dict]:
    """Get attachment IDs and filenames for a message.

    Returns list of dicts with 'id', 'filename', 'mimeType'.
    """
    service = _build_service()
    if service is None:
        return []

    try:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

        attachments = []
        payload = msg.get("payload", {})
        parts = payload.get("parts", [])

        for part in parts:
            if part.get("filename"):
                body = part.get("body", {})
                att_id = body.get("attachmentId")
                if att_id:
                    attachments.append(
                        {
                            "id": att_id,
                            "filename": part["filename"],
                            "mimeType": part.get("mimeType", ""),
                        }
                    )

        return attachments

    except Exception as e:
        logger.warning("Failed to get attachment IDs for %s: %s", message_id, e)
        return []


def create_draft(to: str, subject: str, body: str) -> Optional[str]:
    """Create a Gmail draft reply.

    Args:
        to: Recipient email address
        subject: Email subject
        body: Email body text

    Returns:
        Draft ID if successful, None on failure.
    """
    service = _build_service()
    if service is None:
        logger.warning("Gmail service not available, cannot create draft")
        return None

    try:
        import email.mime.text

        message = email.mime.text.MIMEText(body)
        message["To"] = to
        message["Subject"] = subject

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

        draft = (
            service.users()
            .drafts()
            .create(userId="me", body={"message": {"raw": raw}})
            .execute()
        )

        draft_id = draft.get("id", "")
        logger.info("Created draft for %s: %s", to, draft_id)
        return draft_id

    except Exception as e:
        logger.error("Failed to create draft: %s", e)
        return None


def send_email(to: str, subject: str, body: str) -> bool:
    """Send an email via Gmail API.

    Only call this when auto_send_enabled is explicitly True and all
    safety checks have passed.

    Args:
        to: Recipient email address
        subject: Email subject
        body: Email body text

    Returns:
        True if sent successfully, False otherwise.
    """
    service = _build_service()
    if service is None:
        logger.warning("Gmail service not available, cannot send email")
        return False

    try:
        import email.mime.text

        message = email.mime.text.MIMEText(body)
        message["To"] = to
        message["Subject"] = subject

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

        sent = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )

        logger.info("Sent email to %s: message ID %s", to, sent.get("id", ""))
        return True

    except Exception as e:
        logger.error("Failed to send email: %s", e)
        return False


def mark_as_read(message_id: str) -> bool:
    """Mark a message as read."""
    service = _build_service()
    if service is None:
        return False

    try:
        service.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
        return True
    except Exception as e:
        logger.warning("Failed to mark message %s as read: %s", message_id, e)
        return False