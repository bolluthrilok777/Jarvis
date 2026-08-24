"""Email classifier for job-related Gmail messages.

Uses AI (Ollama) for classification with deterministic keyword pre-filter
to reduce cost. Falls back to deterministic-only if AI is unavailable.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from jarvis_core.gmail_models import ClassificationResult, GmailMessagePreview
from jarvis_core.ollama_service import ask_ollama

logger = logging.getLogger("jarvis.gmail_classifier")

# ── Job-related keywords (deterministic pre-filter) ──────────────────────

JOB_KEYWORDS = [
    "job description", "jd attached", "interview invite", "interview invitation", "interview confirmation",
    "selection mail", "shortlisted", "offer letter", "document request",
    "salary discussion", "joining formalities", "hr follow-up", "recruiter",
    "client interview", "feedback mail", "hiring", "application",
    "your application", "we are pleased", "congratulations", "offer",
    "interview schedule", "technical interview", "hr round", "onsite interview",
    "job offer", "appointment letter", "employment", "onboarding",
    "background verification", "notice period", "relieving letter",
    "experience letter", "pay slip", "compensation", "ctc",
    "walk-in drive", "campus placement", "referral", "job opportunity",
    "we would like to invite", "next steps", "recruitment process",
    "assessment", "coding challenge", "assignment",
]

NON_JOB_KEYWORDS = [
    "promotion", "spam", "newsletter", "bank offer", "shopping",
    "social media", "otp", "verification code", "cashback", "discount",
    "sale", "offer ends", "subscribe", "unsubscribe", "you won",
    "lottery", "prize", "click here", "limited time",
    "amazon", "flipkart", "myntra", "ajio", "zomato", "swiggy",
    "instagram", "facebook", "twitter",
    "youtube", "netflix", "spotify",
]

JOB_CATEGORIES = {
    "job_description": ["job description", "jd attached", "jd for", "role description"],
    "interview_invite": ["interview invite", "interview invitation", "interview schedule", "interview call",
                         "technical interview", "hr interview", "onsite interview"],
    "interview_confirmation": ["interview confirmation", "confirmed your interview",
                               "interview scheduled"],
    "selection": ["selection mail", "selected", "we are pleased to inform",
                  "congratulations", "you have been selected"],
    "shortlisted": ["shortlisted", "short list", "shortlist"],
    "offer_letter": ["offer letter", "appointment letter", "job offer",
                     "offer of employment"],
    "document_request": ["document request", "documents required", "upload documents",
                         "submit documents", "background verification"],
    "salary_discussion": ["salary discussion", "compensation", "ctc", "salary negotiation",
                          "pay package"],
    "joining_formalities": ["joining formalities", "onboarding", "joining date",
                            "date of joining", "onboarding process"],
    "hr_followup": ["hr follow-up", "hr follow up", "next steps", "recruitment process"],
    "recruiter": ["recruiter", "recruitment", "talent acquisition"],
    "client_interview": ["client interview", "client round", "client discussion"],
    "feedback": ["feedback mail", "interview feedback", "assessment feedback"],
}

# Dangerous attachment extensions
DANGEROUS_EXTENSIONS = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".msi"}
ALLOWED_ATTACHMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt", ".html", ".htm"}


def is_dangerous_attachment(filename: str) -> bool:
    """Check if a filename has a dangerous extension."""
    ext = Path(filename).suffix.lower()
    return ext in DANGEROUS_EXTENSIONS


def is_allowed_attachment(filename: str) -> bool:
    """Check if a filename has an allowed extension for JD saving."""
    ext = Path(filename).suffix.lower()
    return ext in ALLOWED_ATTACHMENT_EXTENSIONS


def deterministic_pre_filter(preview: GmailMessagePreview) -> Optional[bool]:
    """Quick deterministic check if email is job-related.

    Returns:
        True if likely job-related
        False if likely non-job
        None if uncertain (needs AI)
    """
    subject_lower = preview.subject.lower()
    sender_lower = preview.sender.lower()
    snippet_lower = preview.snippet.lower()
    body_lower = preview.body_preview.lower()
    combined = f"{subject_lower} {snippet_lower} {body_lower}"

    # Step 1: Check sender domains first — recruiter platforms always pass
    recruiter_domains = [
        "naukri.com", "linkedin.com", "indeed.com", "monster.com",
        "shine.com", "timesjobs.com", "hirect.in", "apna.co",
        "foundit.in", "hirist.com", "cutshort.com",
    ]
    for domain in recruiter_domains:
        if domain in sender_lower:
            return True

    # Step 2: Check sender patterns — recruiter senders always pass
    job_sender_patterns = [
        r"@recruit", r"@hiring", r"@talent", r"@career",
        r"@jobs", r"@placement", r"@hr\.",
        r"noreply.*recruit", r"career", r"hiring",
    ]
    for pattern in job_sender_patterns:
        if re.search(pattern, sender_lower):
            return True

    # Step 3: Check job keywords before non-job filter
    for kw in JOB_KEYWORDS:
        if kw in combined or kw in subject_lower:
            return True

    # Step 4: Check non-job keywords (only fires after job checks above)
    for kw in NON_JOB_KEYWORDS:
        if kw in combined or kw in subject_lower:
            return False

    return None  # Uncertain


def classify_with_ai(preview: GmailMessagePreview, full_context: str = "") -> Optional[ClassificationResult]:
    """Use Ollama AI to classify an email.

    Falls back to None if AI is unavailable.
    """
    context = full_context or preview.thread_context or preview.full_body or preview.body_preview
    prompt = f"""Classify this job-related email using the complete local conversation context.

Decide the current state from the latest message, not from an earlier invitation.
If a specific interview date/time or meeting link is already confirmed and the email
does not ask the candidate to reply, set should_create_draft to false. Never create
an availability-request reply for an already scheduled interview.

Sender: {preview.sender[:100]}
Subject: {preview.subject[:200]}
Attachments: {', '.join(preview.attachment_filenames[:5]) or 'none'}
Complete conversation (local only):
{context[:24000]}

Respond with ONLY valid JSON (no markdown, no explanation):
{{
  "is_job_related": true/false,
  "category": "job_description|interview_invite|interview_confirmation|selection|shortlisted|offer_letter|document_request|salary_discussion|joining_formalities|hr_followup|recruiter|client_interview|feedback|unknown",
  "importance": "low|medium|high|critical",
  "summary": "one-line summary",
  "action_required": "what action is needed or none",
  "should_save_jd": true/false,
  "should_create_draft": true/false,
  "draft_type": "jd_received|interview|documents|offer|unknown|null"
}}"""

    try:
        reply, model, error = ask_ollama(prompt, mode="auto", purpose="gmail_classification")
        if error:
            logger.warning("AI classification failed: %s", error)
            return None

        # Extract JSON from reply
        json_match = re.search(r"\{.*\}", reply, re.DOTALL)
        if not json_match:
            logger.warning("No JSON found in AI reply")
            return None

        data = json.loads(json_match.group())
        return ClassificationResult(**data)

    except (json.JSONDecodeError, Exception) as e:
        logger.warning("AI classification parse error: %s", e)
        return None


def deterministic_classify(preview: GmailMessagePreview, full_context: str = "") -> ClassificationResult:
    """Fallback deterministic classification when AI is unavailable."""
    subject_lower = preview.subject.lower()
    combined = f"{preview.subject.lower()} {preview.snippet.lower()} {preview.body_preview.lower()} {full_context.lower()}"

    result = ClassificationResult(is_job_related=False)

    # Check if job-related
    for kw in JOB_KEYWORDS:
        if kw in combined or kw in subject_lower:
            result.is_job_related = True
            break

    # A concrete scheduled-interview notice is job-related even when it uses
    # natural wording such as "your final interview is scheduled for..."
    # rather than one of the shorter keyword phrases above. This must happen
    # before the early return so the no-draft confirmation guard can apply.
    if "interview" in combined and any(
        marker in combined
        for marker in ("interview is scheduled", "interview has been scheduled", "scheduled for")
    ):
        result.is_job_related = True

    if not result.is_job_related:
        return result

    # Determine category
    for category, keywords in JOB_CATEGORIES.items():
        for kw in keywords:
            if kw in combined or kw in subject_lower:
                result.category = category
                break
        if result.category != "unknown":
            break

    if result.category == "unknown":
        result.category = "recruiter"  # Generic job-related

    # Determine importance
    critical_keywords = ["offer letter", "offer of employment", "appointment letter",
                         "joining date", "onboarding"]
    high_keywords = ["interview", "shortlisted", "selected", "salary",
                     "document request", "background verification"]
    medium_keywords = ["job description", "jd", "recruiter", "hr follow-up",
                       "next steps", "feedback"]

    for kw in critical_keywords:
        if kw in combined:
            result.importance = "critical"
            break
    else:
        for kw in high_keywords:
            if kw in combined:
                result.importance = "high"
                break
        else:
            for kw in medium_keywords:
                if kw in combined:
                    result.importance = "medium"
                    break

    # Summary
    result.summary = f"Job-related email: {result.category}"

    # Action required
    if result.category in ("offer_letter", "selection", "shortlisted"):
        result.action_required = "Review and respond"
    elif result.category in ("interview_invite", "interview_confirmation", "client_interview"):
        result.action_required = "Confirm availability"
    elif result.category == "document_request":
        result.action_required = "Submit requested documents"
    elif result.category == "salary_discussion":
        result.action_required = "Review and negotiate"
    elif result.category == "joining_formalities":
        result.action_required = "Complete joining formalities"
    else:
        result.action_required = "Review"

    # JD saving
    if result.category == "job_description":
        result.should_save_jd = True
    for fn in preview.attachment_filenames:
        if is_allowed_attachment(fn):
            result.should_save_jd = True

    # Draft creation
    if result.category in ("job_description", "interview_invite", "interview_confirmation",
                           "document_request", "offer_letter", "selection", "shortlisted"):
        result.should_create_draft = True
        draft_map = {
            "job_description": "jd_received",
            "interview_invite": "interview",
            "interview_confirmation": "interview",
            "client_interview": "interview",
            "document_request": "documents",
            "offer_letter": "offer",
            "selection": "offer",
            "shortlisted": "offer",
        }
        result.draft_type = draft_map.get(result.category, "unknown")

    # A calendar link or explicit interview time is information, not an
    # unanswered availability request. Default to no draft unless the mail
    # explicitly asks the candidate to confirm, select, or reschedule.
    scheduled_markers = (
        "interview has been scheduled", "interview is scheduled", "scheduled for",
        "your interview", "meeting link", "calendar invite", "join the meeting",
    )
    response_markers = ("please confirm", "confirm your availability", "select a slot", "reschedule")
    has_time = bool(re.search(r"\b(?:\d{1,2}:\d{2}|\d{1,2}\s*(?:am|pm))\b", combined))
    if any(marker in combined for marker in scheduled_markers) and has_time and not any(
        marker in combined for marker in response_markers
    ):
        result.category = "interview_confirmation"
        result.summary = "Interview schedule is already confirmed."
        result.action_required = "Review the scheduled interview details"
        result.should_create_draft = False
        result.draft_type = None

    return result


def _extract_company_from_sender(sender: str) -> str:
    """Extract company name from sender email address."""
    import re
    # Try to extract from email domain
    match = re.search(r'@([\w.-]+)', sender)
    if match:
        domain = match.group(1)
        # Remove common TLDs and email prefixes
        company = domain.split('.')[0]
        # Capitalize and clean
        company = company.replace('-', ' ').replace('_', ' ').title()
        return company
    return ""


def classify_email(preview: GmailMessagePreview, full_context: str = "") -> ClassificationResult:
    """Classify an email using AI first, then fall back to deterministic.

    Args:
        preview: Privacy-minimized email preview

    Returns:
        ClassificationResult with classification details
    """
    # Step 1: Deterministic pre-filter
    pre_filter = deterministic_pre_filter(preview)
    if pre_filter is False:
        # Clearly non-job
        return ClassificationResult(is_job_related=False, category="non_job")

    # Step 2: Try AI classification
    ai_result = classify_with_ai(preview, full_context)
    if ai_result is not None:
        result = ai_result
    else:
        # Step 3: Fall back to deterministic
        logger.info("AI classification unavailable, using deterministic fallback")
        result = deterministic_classify(preview, full_context)

    # Apply the confirmed-schedule guard even when Ollama returns an otherwise
    # plausible interview draft. This prevents the exact stale-slot error that
    # occurs when a thread contains both an invitation and the later schedule.
    combined = f"{preview.subject} {preview.snippet} {preview.body_preview} {full_context}".lower()
    scheduled_markers = ("interview has been scheduled", "interview is scheduled", "scheduled for",
                         "meeting link", "calendar invite", "join the meeting")
    response_markers = ("please confirm", "confirm your availability", "select a slot", "reschedule")
    has_time = bool(re.search(r"\b(?:\d{1,2}:\d{2}|\d{1,2}\s*(?:am|pm))\b", combined))
    if any(marker in combined for marker in scheduled_markers) and has_time and not any(
        marker in combined for marker in response_markers
    ):
        result.category = "interview_confirmation"
        result.summary = "Interview schedule is already confirmed."
        result.action_required = "Review the scheduled interview details"
        result.should_create_draft = False
        result.draft_type = None

    # Step 4: Override rules for specific senders — applied after any classification
    sender_lower = preview.sender.lower()
    # Naukri alert emails: low priority, no draft needed (they are automated job alerts, not recruiter contact)
    if "naukri.com" in sender_lower or "naukrialerts" in sender_lower:
        result.importance = "low"
        result.should_create_draft = False
        result.draft_type = None
        result.action_required = "Review job alert (automated)"
        logger.debug("Naukri sender override applied: low priority, no draft")

    return result
