"""
jarvis_core/normalizer.py — Phase 4.5 Command Normalizer

Stateless text normalization pipeline:
  1. Strip filler phrases (please, hey jarvis, can you, etc.)
  2. Strip leading articles (the, a, an)
  3. Convert spoken math operators (plus → +, times → *, etc.)
  4. Convert spoken numbers (forty-five → 45)

Public API:
    normalize(command: str) -> str | dict
    strip_fillers(text: str) -> str
    normalize_operators(text: str) -> str
    normalize_numbers(text: str) -> str | dict
"""
from __future__ import annotations

import re
from typing import Union

# ── 1. Filler removal ─────────────────────────────────────────────────────────

# Ordered longest-first so "hey jarvis" is removed before "jarvis"
FILLER_PHRASES: list[str] = [
    "hey jarvis",
    "jarvis",
    "can you",
    "could you",
    "would you",
    "for me",
    "please",
]

# Leading articles to strip after filler removal
LEADING_ARTICLES: list[str] = ["the", "a", "an"]


def strip_fillers(text: str) -> str:
    """Remove filler phrases and leading articles, return cleaned text."""
    result = text.strip()
    # Iteratively remove fillers until stable (handles "please please open")
    changed = True
    while changed:
        changed = False
        for phrase in FILLER_PHRASES:
            # Whole-word, case-insensitive removal anywhere in string
            pattern = r"(?i)\b" + re.escape(phrase) + r"\b[,\s]*"
            new = re.sub(pattern, " ", result).strip()
            if new != result:
                result = new
                changed = True
    # Strip leading articles once
    for article in LEADING_ARTICLES:
        pattern = r"(?i)^" + re.escape(article) + r"\s+"
        result = re.sub(pattern, "", result).strip()
    return result


# ── 2. Spoken math operator conversion ───────────────────────────────────────

# Ordered longest-first so "to the power of" matches before "power"
OPERATOR_MAP: list[tuple[str, str]] = [
    ("to the power of", "**"),
    ("multiplied by",   "*"),
    ("divided by",      "/"),
    ("squared",         "**2"),
    ("square",          "**2"),
    ("times",           "*"),
    ("plus",            "+"),
    ("minus",           "-"),
    ("percentage",      "%"),
    ("power",           "**"),
]


def normalize_operators(text: str) -> str:
    """Replace spoken math operators with symbolic equivalents."""
    result = text
    for spoken, symbol in OPERATOR_MAP:
        # Whole-word match, case-insensitive
        pattern = r"(?i)\b" + re.escape(spoken) + r"\b"
        result = re.sub(pattern, symbol, result)
    # Collapse multiple spaces
    result = re.sub(r"  +", " ", result).strip()
    return result


# ── 3. Spoken parentheses conversion ─────────────────────────────────────────

def _normalize_brackets(text: str) -> str:
    """Convert 'open brackets ... close brackets' to '( ... )'."""
    # Handle "open brackets/parentheses ... close brackets/parentheses"
    pattern = (
        r"(?i)\b(?:open)\s+(?:brackets?|parenthes[ei]s)\b"
        r"(.*?)"
        r"\b(?:close)\s+(?:brackets?|parenthes[ei]s)\b"
    )
    result = re.sub(pattern, r"(\1)", text, flags=re.IGNORECASE | re.DOTALL)
    return result


# ── 4. Spoken number conversion ───────────────────────────────────────────────

def normalize_numbers(text: str) -> Union[str, dict]:
    """
    Scan text for English number phrases and replace with digit strings.

    Returns a str on success, or a clarification dict if word2number raises
    an unrecoverable error (ambiguous or unparseable phrase).
    """
    try:
        from word2number import w2n
    except ImportError:
        # word2number not installed — return text unchanged
        return text

    # Tokenize on whitespace, try progressively longer windows
    tokens = text.split()
    result_tokens: list[str] = []
    i = 0
    while i < len(tokens):
        # Try windows from longest to shortest (max 9 words per spec)
        converted = False
        for window in range(min(9, len(tokens) - i), 0, -1):
            phrase = " ".join(tokens[i : i + window])
            # Only attempt conversion if the phrase looks like it could be numeric
            # (contains a digit word — avoid false positives on regular words)
            if not _looks_numeric(phrase):
                continue
            try:
                num = w2n.word_to_num(phrase)
                result_tokens.append(str(num))
                i += window
                converted = True
                break
            except ValueError:
                continue
        if not converted:
            result_tokens.append(tokens[i])
            i += 1

    return " ".join(result_tokens)


# Words that are numeric indicators — used to decide whether to attempt w2n
_NUMERIC_WORDS = frozenset({
    "zero","one","two","three","four","five","six","seven","eight","nine","ten",
    "eleven","twelve","thirteen","fourteen","fifteen","sixteen","seventeen",
    "eighteen","nineteen","twenty","thirty","forty","fifty","sixty","seventy",
    "eighty","ninety","hundred","thousand","million","billion",
    "forty-five","forty-six","forty-seven","forty-eight","forty-nine",
    "twenty-one","twenty-two","twenty-three","twenty-four","twenty-five",
    "twenty-six","twenty-seven","twenty-eight","twenty-nine",
    "thirty-one","thirty-two","thirty-three","thirty-four","thirty-five",
})


def _looks_numeric(phrase: str) -> bool:
    """Return True if the phrase contains at least one numeric word."""
    words = re.split(r"[\s\-]+", phrase.lower())
    return any(w in _NUMERIC_WORDS for w in words)


# ── Public entry point ────────────────────────────────────────────────────────

def normalize(command: str) -> Union[str, dict]:
    """
    Full normalization pipeline:
      strip_fillers → normalize_operators → _normalize_brackets → normalize_numbers

    Returns the normalized string, or a clarification dict if spoken-number
    parsing encounters an ambiguous phrase.
    """
    if not command:
        return ""
    step1 = strip_fillers(command)
    step2 = normalize_operators(step1)
    step3 = _normalize_brackets(step2)
    result = normalize_numbers(step3)
    return result
