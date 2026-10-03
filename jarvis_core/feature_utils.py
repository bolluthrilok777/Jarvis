"""Safe-text and risk helpers for the Feature Orchestrator.

Includes secret redaction, prompt-injection sanitisation for untrusted web
content, and the high-risk keyword classifier used by the approval gate.
"""
from __future__ import annotations

import re

# ── Secret patterns ──────────────────────────────────────────────────────────
# Matches are replaced with a neutral placeholder. These are conservative:
# they only mask values that look like credentials.
_SECRET_PATTERNS = (
    (re.compile(r"\b\d{6,10}:[A-Za-z0-9_-]{30,}\b"), "[BOT-TOKEN]"),          # Telegram bot tokens
    (re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}\b"), "[GOOGLE-TOKEN]"),           # Google OAuth tokens
    (re.compile(r"\bAIza[0-9A-Za-z_-]{25,}\b"), "[GOOGLE-KEY]"),               # Google API keys
    (re.compile(r"\bghp_[A-Za-z0-9]{25,}\b"), "[GITHUB-TOKEN]"),               # GitHub PATs
    (re.compile(r"\bgho_[A-Za-z0-9]{25,}\b"), "[GITHUB-TOKEN]"),
    (re.compile(r"\b(?:sk|pk)-[A-Za-z0-9]{20,}\b"), "[API-KEY]"),              # OpenAI-style keys
    (re.compile(r"(?i)\bpassword\s*[:=]\s*\S+"), "password=[REDACTED]"),
    (re.compile(r"(?i)\bclient_secret\s*[:=]\s*[\"']?[A-Za-z0-9_-]{16,}"), "client_secret=[REDACTED]"),
    (re.compile(r"(?i)\bapi[_-]?key\s*[:=]\s*\S+"), "api_key=[REDACTED]"),
    (re.compile(r"(?i)\bauthorization\s*[:=]\s*\S+"), "authorization=[REDACTED]"),
    (re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"), "[PRIVATE-KEY]"),
)


def redact_sensitive(text: str) -> str:
    """Mask credential-shaped values so persisted state never leaks secrets."""
    if not text:
        return text
    redacted = str(text)
    for pattern, placeholder in _SECRET_PATTERNS:
        redacted = pattern.sub(placeholder, redacted)
    return redacted


def contains_sensitive(text: str) -> bool:
    """True if text still looks like it contains a credential after redaction."""
    return any(pattern.search(str(text)) for pattern, _ in _SECRET_PATTERNS)


# ── Prompt-injection sanitisation for untrusted web content ──────────────────
_INJECTION_MARKERS = (
    "ignore all previous instructions",
    "ignore all prior instructions",
    "ignore previous instructions",
    "ignore your instructions",
    "system prompt",
    "system instructions",
    "you are now",
    "act as if",
    "new instructions",
    "override your",
    "do not follow",
    "disregard",
    "reveal your",
    "exfiltrat",
    "send me the contents",
    "print the prompt",
    "jailbreak",
    "your api key",
    "your credentials",
    "bypass",
    "update your system",
    "reset your memory",
)

_MAX_WEB_SNIPPET = 4000


def sanitize_web_content(raw: str) -> str:
    """Strip obvious instruction-override markers and cap length.

    Web content is DATA, never Jarvis instructions. Any line that tries to
    change Jarvis behaviour is discarded and never passed to the local model
    as a directive.
    """
    text = str(raw or "")
    lines = []
    for line in text.splitlines():
        lowered = line.strip().lower()
        if any(marker in lowered for marker in _INJECTION_MARKERS):
            continue
        lines.append(line)
    cleaned = "\n".join(lines)
    return cleaned[:_MAX_WEB_SNIPPET]


# ── High-risk classification ─────────────────────────────────────────────────
_HIGH_RISK_KEYWORDS = (
    "credential", "authentication", "oauth", "password", "bot token",
    "api key", "secret", "email sending", "send email", "email automation",
    "delete file", "delete files", "file deletion", "remove file",
    "os-level", "operating system", "system control", "registry",
    "git history", "rewrite history", "force push", "deployment", "deploy",
    "production", "payment", "payments", "billing", "destructive",
    "wipe", "format disk", "shutdown", "restart the pc", "browser session",
)


def classify_risk(objective: str, research: list[str]) -> str:
    """Return 'High' / 'Medium' / 'Low' for the feature."""
    combined = " ".join([str(objective or ""), " ".join(research or [])]).lower()
    matches = [kw for kw in _HIGH_RISK_KEYWORDS if kw in combined]
    if matches:
        return "High"
    if len(str(objective or "").split()) >= 3 or len(research or []) >= 2:
        return "Medium"
    return "Low"


def sanitize_review_text(text: str) -> str:
    """Normalise and truncate a user-supplied ChatGPT review."""
    return redact_sensitive(str(text or "").strip()[:8000])


# ── Change-request classification ─────────────────────────────────────────────
# Phrases indicating the user wants Jarvis to re-inspect the real repository
# before rebuilding the plan (as opposed to a plan-only tweak).
_RESEARCH_REQUIRED_MARKERS = (
    "inspect", "re-inspect", "reinspect", "scan the repository", "scan repository",
    "identify actual file", "identify the real", "find the actual", "find where",
    "verify service exist", "verify the service exist", "confirm existing component",
    "confirm component", "research dependenc", "research the dependenc",
    "validate storage", "where notes", "how notes", "how the feature",
    "which existing", "real file path", "real path", "repository-grounded",
    "grounded", "look at the actual", "examine the repository", "search the repository",
    "re-run research", "rerun research", "more research",
)

# Phrases indicating a plan-ONLY revision (rebuild wording from existing evidence).
_PLAN_ONLY_MARKERS = (
    "reword", "reword the plan", "reformat", "summarize differently",
    "make clearer", "simpler", "shorter plan", "add a note", "remove s",
    "reorder", "change the wording", "just the wording",
)


def detect_revision_kind(instructions: str) -> str:
    """Classify a change request as requiring re-research or plan-only.

    Returns one of:
      * "research_required" — Jarvis must re-inspect the repository and refresh
        verified evidence before rebuilding the plan.
      * "plan_only"         — rebuild the plan from existing evidence.
    """
    lowered = str(instructions or "").lower()
    if any(marker in lowered for marker in _RESEARCH_REQUIRED_MARKERS):
        return "research_required"
    if any(marker in lowered for marker in _PLAN_ONLY_MARKERS):
        return "plan_only"
    return "plan_only"


# Keyword → proposed (not-yet-existing) module name for common features.
_PROPOSED_MODULE_HINTS = {
    "notes": "notes_export_service.py",
    "calendar": "calendar_integration_service.py",
    "memory": "memory_system_service.py",
    "monitor": "monitoring_extension.py",
    "automation": "automation_service.py",
    "classification": "classification_tuner.py",
    "counter": "test_counter_service.py",
    "export": "export_service.py",
}


def propose_new_files(objective: str) -> list[str]:
    """Proposed (NOT yet existing) paths clearly separated from verified ones.

    These are proposals only — they must never be presented as verified paths.
    """
    words = set(str(objective or "").lower().split())
    slugs = []
    for word, filename in _PROPOSED_MODULE_HINTS.items():
        # matches whole words or the word as a token boundary
        if word in words or any(word in w for w in words):
            slugs.append(f"jarvis_core/{filename} (proposed new module)")
    if not slugs:
        # A generic, honest proposal rather than an invented concrete filename.
        slugs.append(
            "A focused service module inside jarvis_core/ "
            "(proposed; exact name at implementation time)"
        )
    return slugs[:4]
