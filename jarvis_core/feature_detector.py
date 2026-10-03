"""Rule-based detector that distinguishes feature-development requests from
ordinary Jarvis commands.

Mirrors the style used by jarvis_core.gmail_intent: deterministic heuristics,
evaluated BEFORE any Ollama call, so normal commands are never intercepted.
"""
from __future__ import annotations

import re

# Verbs that signal "build something new" rather than "do a normal action".
_FEATURE_VERB_RE = re.compile(
    r"^(?:add|implement|build|develop|create|integrate|improve|enhance|upgrade|"
    r"modernize|introduce|design|write|construct|set\s+up|make)\b",
    re.IGNORECASE,
)

# Fallback for a typo'd/fused verb where the user dropped the space between the
# feature verb and the next word (e.g. "adda note feature" -> "add a note
# feature"). The trailing \b above rejects it, which would let a genuine
# feature request fall through to the normal-command queue and silently bypass
# the Telegram clarification gate. This looser prefix match is only accepted when
# a feature noun is also present (see detect_feature_request), so ordinary words
# like "addition"/"address" stay normal commands.
_FEATURE_VERB_FUSED_RE = re.compile(
    r"^(?:add|implement|build|develop|create|integrate|improve|enhance|upgrade|"
    r"modernize|introduce|design|write|construct|make)",
    re.IGNORECASE,
)

# Nouns that strongly indicate a feature / capability being requested.
_IMPL_NOUN_RE = re.compile(
    r"\b(?:integration|system|module|support|feature|monitoring|automation|"
    r"classification|memory|calendar|browser automation|workflow|pipeline|"
    r"api|interface|notifier|scraper|dashboard|panel|widget|tracker|reminder|"
    r"scheduler|search|summarization|sync|export|import|notification|alerts?|"
    r"analytics|reporting|filter|rules|templates?|versioning|backups?|"
    r"automation tool|connector|plugin|extension|engine|indexer|agent|"
    r"conversation history|memory system|knowledge base|knowledge graph)\b",
    re.IGNORECASE,
)

# Normal commands that must never be treated as feature requests.
_BLOCKLIST_PREFIXES = (
    "open ", "close ", "launch ", "start ", "stop ", "kill ", "shutdown ",
    "restart ", "reboot ", "system status", "task manager", "pc status",
    "gmail status", "gmail details", "gmail draft", "gmail refresh",
    "gmail sent", "gmail report", "calculate", "compute", "screenshot ",
    "play ", "pause ", "resume ", "show system status", "what is system status",
    "track ", "share ", "book ", "schedule a meeting ",
)

_BLOCKLIST_EXACT = {
    "system status", "pc status", "laptop status", "task manager",
    "help", "status", "hello", "hi", "hey", "memory",
}


def _normalize(raw: str) -> str:
    text = re.sub(r"^(?:hey\s+)?(?:jarvis|jarvis\s*,?)\s*", "", str(raw or "").strip(),
                  flags=re.IGNORECASE)
    text = re.sub(r"^(?:please\s*)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[\s!?.,;:]+$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def detect_feature_request(text: str) -> dict:
    """Return {is_feature, objective, confidence, reason} for a command."""
    normalized = _normalize(text)
    if not normalized:
        return {"is_feature": False, "objective": "", "reason": "empty"}

    lower = normalized.lower()
    words = lower.split()

    # Questions are conversational, not feature orders.
    if "?" in normalized or any(w in lower for w in ("what is", "what are", "how do", "how does")):
        return {"is_feature": False, "objective": "", "reason": "question"}

    # Exact / prefix blocklist (normal commands).
    if lower in _BLOCKLIST_EXACT:
        return {"is_feature": False, "objective": "", "reason": "blocklist-exact"}
    if any(lower.startswith(p) for p in _BLOCKLIST_PREFIXES):
        return {"is_feature": False, "objective": "", "reason": "blocklist-prefix"}

    verb_match = _FEATURE_VERB_RE.match(lower)
    if not verb_match:
        # Tolerate a typo'd/fused verb where the user dropped the space between the
        # feature verb and the next word (e.g. "adda note feature"). Only accept
        # the looser prefix match when a feature noun is also present, so ordinary
        # words like "addition"/"address" stay normal commands.
        if _IMPL_NOUN_RE.search(normalized) and _FEATURE_VERB_FUSED_RE.match(lower):
            verb_match = _FEATURE_VERB_FUSED_RE.match(lower)
        if not verb_match:
            return {"is_feature": False, "objective": "", "reason": "no-feature-verb"}

    # A feature request should describe a domain/capability, not a one-liner
    # like "add calculator".
    has_noun = bool(_IMPL_NOUN_RE.search(normalized))
    if not has_noun and len(words) < 4:
        return {"is_feature": False, "objective": "", "reason": "too-short"}

    objective = _objective_of(normalized, verb_match.group(0))
    if not objective:
        return {"is_feature": False, "objective": "", "reason": "no-objective"}

    return {"is_feature": True, "objective": objective, "reason": "feature-detect"}


def _objective_of(normalized: str, verb: str) -> str:
    """Extract a human feature title from the request text."""
    remainder = normalized[len(verb):].strip()
    remainder = re.sub(r"^for\s+this\s+workflow\s*$", "", remainder, flags=re.IGNORECASE)
    remainder = re.sub(r"(?i)\b(so that|so i can|to (?:let|help|enable|allow)|because)\b.*$", "", remainder)
    remainder = re.sub(r"\s+", " ", remainder).strip(" -–")
    remainder = re.sub(r"^a\s+|^an\s+|^the\s+", "", remainder, flags=re.IGNORECASE)
    remainder = re.sub(r"\s+browser automation$", " browser automation", remainder, flags=re.IGNORECASE)
    if not remainder:
        return ""
    # Title-case for a clean objective without mutating acronyms.
    keep = {"api", "os", "ui", "ai", "jdk", "sql", "url", "gmail", "naukri",
            "ios", "android", "vs", "id", "cl", "cli"}
    title_words = []
    for w in remainder.split():
        title_words.append(w if w.lower() in keep or w.isupper() else w.capitalize())
    return " ".join(title_words)[:120]


def is_feature_request(text: str) -> bool:
    return bool(detect_feature_request(text).get("is_feature"))