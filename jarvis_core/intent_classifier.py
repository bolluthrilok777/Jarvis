"""
jarvis_core/intent_classifier.py — Phase 5A-1 Intent Classifier

Lightweight heuristic classifier that decides whether normalized input is:
  - conversational_query  → route to AIChat (no task created)
  - executable_command    → route to OllamaPlanner (task created)

Evaluated BEFORE any Ollama call to avoid wasting inference on questions.
"""
from __future__ import annotations

import re

# ── Conversational trigger patterns ──────────────────────────────────────────
# Patterns that strongly indicate a question/conversation, not an action command.
# Match against the normalized (filler-stripped) text.

_CONV_PREFIXES: tuple[str, ...] = (
    "who ", "who?",
    "what ", "what?",
    "why ", "why?",
    "when ", "when?",
    "where ", "where?",
    "how ", "how?",
    "tell me ",
    "explain ",
    "describe ",
    "are you ",
    "do you ",
    "is it ",
    "can you tell",
    "could you tell",
    "what is ",
    "what are ",
    "what does ",
    "what do ",
    "what can ",
    "how does ",
    "how do ",
    "how can ",
)

# "can you" followed by a non-action verb is conversational.
# "can you open" / "can you search" are action requests — handled by normalizer stripping
# "can you" filler, so by the time we see it here the command is already normalized.
# We only treat "can you" as conversational when followed by non-action words.
_ACTION_VERBS: frozenset[str] = frozenset({
    "open", "search", "find", "launch", "start", "create", "take", "show",
    "list", "read", "get", "calculate", "compute", "close", "focus",
    "type", "save", "screenshot", "calculate", "check",
})

# Phrases that look conversational but are actually executable commands
_EXECUTABLE_EXACT: frozenset[str] = frozenset({
    "system status", "pc status", "laptop status", "computer status",
    "task manager", "resource usage", "performance stats",
    "cpu status", "ram status", "gpu status", "memory status",
    "check cpu", "check ram", "check gpu", "check memory",
    "check disk", "check temperature", "check temp",
    "what is system status", "what is the system status",
    "how is my pc", "how is my laptop", "how is my computer",
    "how is the pc doing", "how is my pc doing",
    "system info", "system information", "system health",
    "system monitor", "system metrics", "system performance",
    "system resources",
})

_CONV_EXACT: frozenset[str] = frozenset({
    "who are you",
    "what can you do",
    "what are you",
    "what is jarvis",
    "hello",
    "hi",
    "hey",
    "help",
    "help me",
})


def classify(normalized: str) -> str:
    """
    Classify normalized text as 'conversational_query' or 'executable_command'.

    Returns:
        'conversational_query'  — route to AIChat
        'executable_command'    — route to OllamaPlanner
    """
    text = normalized.strip().lower()

    if not text:
        return "executable_command"

    # Hard-coded executable overrides — checked FIRST before any conversational
    # patterns so phrases like "what is system status" are never mis-routed.
    if text in _EXECUTABLE_EXACT:
        return "executable_command"

    # Check for math expressions first — these are always executable commands
    # even if they start with "what is" or other conversational prefixes.
    _MATH_WORDS = frozenset({
        "plus", "minus", "times", "multiplied", "divided", "into",
        "+", "-", "*", "/", "x", "percent", "percentage", "%",
    })
    words = text.split()
    if any(w in _MATH_WORDS for w in words):
        return "executable_command"

    # Exact matches
    if text in _CONV_EXACT:
        return "conversational_query"

    # Prefix matches
    for prefix in _CONV_PREFIXES:
        if text.startswith(prefix):
            return "conversational_query"

    # Single-word question
    if text.endswith("?") and " " not in text.rstrip("?"):
        return "conversational_query"

    # Multi-word ending with ? and no action verb at the start
    if text.endswith("?"):
        first_word = text.split()[0]
        if first_word not in _ACTION_VERBS:
            return "conversational_query"

    return "executable_command"
