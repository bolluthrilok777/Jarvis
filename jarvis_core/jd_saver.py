"""JD (Job Description) saver module.

Handles downloading JD attachments and saving JD links from emails.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from jarvis_core.gmail_classifier import is_allowed_attachment, is_dangerous_attachment
from jarvis_core.gmail_service import download_attachment, get_attachment_ids_and_names
from jarvis_core.config import JOBS_JD_DIR

logger = logging.getLogger("jarvis.jd_saver")

# Default save folder (derived from BASE_DIR — cross-platform safe)
DEFAULT_JD_FOLDER = JOBS_JD_DIR


def _get_date_folder(base_dir: Path) -> Path:
    """Get the date-based subfolder for saving JDs."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    folder = base_dir / date_str
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _sanitize_filename_part(text: str, max_len: int = 40) -> str:
    """Sanitize a string for use in a filename."""
    # Remove or replace problematic characters
    sanitized = re.sub(r'[<>:"/\\|?*]', "_", text)
    sanitized = re.sub(r"\s+", "_", sanitized)
    sanitized = sanitized.strip("._")
    if len(sanitized) > max_len:
        sanitized = sanitized[:max_len]
    return sanitized if sanitized else "unknown"


def _build_filename(
    company: str, subject: str, original_filename: str
) -> str:
    """Build a standardized filename for a JD file.

    Format: YYYY-MM-DD_HH-mm_FROM_COMPANY_SUBJECT_originalfilename
    """
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d_%H-%M")
    company_part = _sanitize_filename_part(company, 30)
    subject_part = _sanitize_filename_part(subject, 50)
    # Keep original filename safe
    orig = _sanitize_filename_part(original_filename, 60)

    return f"{timestamp}_{company_part}_{subject_part}_{orig}"


def _attachment_already_saved(save_dir: Path, subject: str, original_filename: str) -> bool:
    """Check local JD storage before downloading a duplicate attachment."""
    if not save_dir.exists():
        return False
    subject_part = _sanitize_filename_part(subject, 50).lower()
    original_part = _sanitize_filename_part(original_filename, 60).lower()
    try:
        return any(
            path.is_file() and subject_part in path.name.lower() and original_part in path.name.lower()
            for path in save_dir.rglob("*")
        )
    except OSError as exc:
        logger.warning("Could not check existing JD files: %s", str(exc)[:80])
        return False


def _jd_link_already_saved(save_dir: Path, link: str) -> bool:
    """Avoid duplicate local JD-link records after restoring memory."""
    if not save_dir.exists() or not link:
        return False
    try:
        for path in save_dir.rglob("*_jd_link.txt"):
            if path.is_file() and link in path.read_text(encoding="utf-8", errors="ignore"):
                return True
    except OSError as exc:
        logger.warning("Could not check existing JD links: %s", str(exc)[:80])
    return False


def save_jd_attachment(
    message_id: str,
    sender: str,
    subject: str,
    save_dir: Optional[Path] = None,
) -> list[dict]:
    """Download and save JD attachments from a Gmail message.

    Args:
        message_id: Gmail message ID
        sender: Sender email address
        subject: Email subject
        save_dir: Base directory for saving (default: DEFAULT_JD_FOLDER)

    Returns:
        List of dicts with 'path' and 'filename' for each saved file.
    """
    if save_dir is None:
        save_dir = DEFAULT_JD_FOLDER

    attachments = get_attachment_ids_and_names(message_id)
    saved_files = []

    if not attachments:
        return saved_files

    date_folder = _get_date_folder(save_dir)

    # Extract company from sender
    company = _extract_company_from_sender(sender)

    for att in attachments:
        filename = att.get("filename", "")
        att_id = att.get("id", "")

        if not filename or not att_id:
            continue

        # Skip dangerous attachments
        if is_dangerous_attachment(filename):
            logger.info("Skipping dangerous attachment: %s", filename)
            continue

        # Only save allowed attachment types
        if not is_allowed_attachment(filename):
            logger.info("Skipping unsupported attachment type: %s", filename)
            continue

        if _attachment_already_saved(save_dir, subject, filename):
            logger.info("JD attachment already saved, skipping duplicate: %s", filename)
            continue

        # Build the save filename
        save_name = _build_filename(company, subject, filename)
        save_path = date_folder / save_name

        # Download and save
        result = download_attachment(message_id, att_id, save_name, date_folder)
        if result:
            saved_files.append({
                "path": str(result),
                "filename": filename,
                "original_filename": filename,
            })
            logger.info("Saved JD attachment: %s -> %s", filename, result)

    return saved_files


def save_jd_link(
    link: str,
    sender: str,
    subject: str,
    save_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Save a JD link to a .txt file.

    If the link points to a safely downloadable file, attempt to download it.
    Does not bypass login pages or scrape aggressively.

    Args:
        link: URL to the JD
        sender: Sender email address
        subject: Email subject
        save_dir: Base directory for saving

    Returns:
        Path to saved file, or None on failure.
    """
    if save_dir is None:
        save_dir = DEFAULT_JD_FOLDER

    if _jd_link_already_saved(save_dir, link):
        logger.info("JD link already saved, skipping duplicate")
        return None

    date_folder = _get_date_folder(save_dir)
    company = _extract_company_from_sender(sender)

    # Save link to a .txt file
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d_%H-%M")
    company_part = _sanitize_filename_part(company, 30)
    subject_part = _sanitize_filename_part(subject, 50)
    link_filename = f"{timestamp}_{company_part}_{subject_part}_jd_link.txt"
    link_path = date_folder / link_filename

    try:
        with open(link_path, "w", encoding="utf-8") as f:
            f.write(f"JD Link: {link}\n")
            f.write(f"From: {sender}\n")
            f.write(f"Subject: {subject}\n")
            f.write(f"Saved: {now.isoformat()}\n")
        logger.info("Saved JD link to %s", link_path)
        return link_path
    except Exception as e:
        logger.error("Failed to save JD link: %s", e)
        return None


def _extract_company_from_sender(sender: str) -> str:
    """Extract company name from sender email address.

    Extracts from email domain, not display name.
    Example: 'HR <hr@company.com>' -> 'Company'
    """
    # Extract email address from angle brackets if present
    email_match = re.search(r'<([^>]+)>', sender)
    if email_match:
        email = email_match.group(1)
    else:
        email = sender
    # Try to extract from email domain
    domain_match = re.search(r"@([^.]+)", email)
    if domain_match:
        return domain_match.group(1).capitalize()

    return "Unknown"


def extract_jd_links_from_body(body: str) -> list[str]:
    """Extract potential JD links from email body.

    Returns list of URLs that look like job description links.
    """
    # Common JD link patterns
    url_pattern = r"https?://[^\s<>\"']+"
    urls = re.findall(url_pattern, body)

    # Filter for likely JD links
    jd_keywords = [
        "job", "jd", "career", "position", "role", "apply",
        "description", "opening", "vacancy",
    ]

    jd_links = []
    for url in urls:
        url_lower = url.lower()
        if any(kw in url_lower for kw in jd_keywords):
            jd_links.append(url)

    return jd_links


def list_saved_jd_files(save_dir: Optional[Path] = None) -> list[dict]:
    """List all saved JD files.

    Returns list of dicts with 'path', 'filename', 'date', 'size'.
    """
    if save_dir is None:
        save_dir = DEFAULT_JD_FOLDER

    if not save_dir.exists():
        return []

    files = []
    for date_folder in sorted(save_dir.iterdir(), reverse=True):
        if not date_folder.is_dir():
            continue
        for f in sorted(date_folder.iterdir(), reverse=True):
            if f.is_file():
                files.append({
                    "path": str(f),
                    "filename": f.name,
                    "date": date_folder.name,
                    "size": f.stat().st_size,
                })

    return files
