"""
jarvis_core/rule_matcher.py — Phase 4.5 Deterministic Rule Matcher

Maps normalized command strings to ActionPlans using compiled regex rules.
All matched plans have confidence=1.0 (deterministic, no AI needed).
"""
from __future__ import annotations

import re
from typing import Optional

from jarvis_core.action_models import ActionStep, ActionPlan

# ── Helpers ───────────────────────────────────────────────────────────────────

def _plan(steps: list[ActionStep], raw: str, norm: str) -> ActionPlan:
    return ActionPlan(
        steps=steps,
        raw_command=raw,
        normalized_command=norm,
        confidence=1.0,
    )


def _step(skill_id: str, params: dict, desc: str = "", confirm: bool = False) -> ActionStep:
    return ActionStep(
        skill_id=skill_id,
        parameters=params,
        description=desc,
        requires_confirmation=confirm,
    )


# ── App/folder name normalisation helpers ─────────────────────────────────────

# Map spoken aliases to ALLOWED_APPS canonical keys
_APP_ALIASES: dict[str, str] = {
    "calc":             "calculator",
    "calculator":       "calculator",
    "notepad":          "notepad",
    "note pad":         "notepad",
    "chrome":           "chrome",
    "edge":             "edge",
    "paint":            "paint",
    "cmd":              "cmd",
    "command prompt":   "cmd",
    "powershell":       "powershell",
    "task manager":     "task manager",
    "explorer":         "explorer",
}

_FOLDER_ALIASES: dict[str, str] = {
    "downloads":    "downloads",
    "download":     "downloads",
    "desktop":      "desktop",
    "documents":    "documents",
    "document":     "documents",
    "pictures":     "pictures",
    "picture":      "pictures",
    "jarvis":       "jarvis",
    "notes":        "notes",
    "note":         "notes",
    "screenshots":  "screenshots",
    "screenshot":   "screenshots",
}


def _normalize_app_name(raw: str) -> str:
    """Return the canonical ALLOWED_APPS key for a spoken app name."""
    return _APP_ALIASES.get(raw.lower().strip(), raw.lower().strip())


def _normalize_folder_name(raw: str) -> str:
    """Return the canonical ALLOWED_FOLDERS key for a spoken folder name."""
    return _FOLDER_ALIASES.get(raw.lower().strip(), raw.lower().strip())


def _number_text(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def _percentage_breakdown_plan(match, raw: str, norm: str) -> ActionPlan:
    primary_percent, base, first_share, second_share = map(float, match.groups())
    primary_value = base * primary_percent / 100
    primary_expr = f"{_number_text(base)}*{_number_text(primary_percent)}/100"
    first_expr = f"{_number_text(primary_value)}*{_number_text(first_share)}/100"
    second_expr = f"{_number_text(primary_value)}*{_number_text(second_share)}/100"
    return _plan([
        _step("app.open", {"app_name": "calculator"}, "Open Windows Calculator"),
        _step("calculator.calculate", {"expression": primary_expr, "use_gui": False},
              f"{_number_text(primary_percent)}% of {_number_text(base)}"),
        _step("calculator.calculate", {"expression": first_expr, "use_gui": False},
              f"{_number_text(first_share)}% of {_number_text(primary_value)}"),
        _step("calculator.calculate", {"expression": second_expr, "use_gui": True},
              f"{_number_text(second_share)}% of {_number_text(primary_value)}"),
    ], raw, norm)


def _extract_math_expression(text: str) -> Optional[str]:
    """Extract a safe-looking numeric expression from conversational filler."""
    number = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
    pattern = rf"({number}(?:\s*(?:\*\*|[+\-*/%])\s*{number})+)"
    matches = re.findall(pattern, text)
    if not matches:
        return None
    return matches[-1].strip()


def _combined_calculator_plan(match, raw: str, norm: str) -> Optional[ActionPlan]:
    expression = _extract_math_expression(match.group(1))
    if not expression:
        return None
    return _plan([
        _step("app.open", {"app_name": "calculator"}, "Open calculator"),
        _step("calculator.calculate", {"expression": expression, "use_gui": True},
              f"Calculate {expression} in Windows Calculator"),
    ], raw, norm)


def _internal_calculator_plan(match, raw: str, norm: str) -> Optional[ActionPlan]:
    expression = _extract_math_expression(match.group(1))
    if not expression:
        return None
    return _plan([
        _step("calculator.calculate", {"expression": expression}, f"Calculate {expression}"),
    ], raw, norm)


# ── Rule table ────────────────────────────────────────────────────────────────
# Each rule is (compiled_pattern, match_handler).
# match_handler(m, raw, norm) -> ActionPlan
# Rules are tried in order; first match wins.

def _build_rules():
    rules = []

    rules.append((
        re.compile(
            r"^open\s+youtube,?\s*(?:and\s+)?search\s+(.+?),?\s*(?:and\s+)?play\s+(?:the\s+)?best\s+video,?\s*(?:and\s+)?note\s+important\s+points$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("browser.youtube_research", {"query": m.group(1).strip()},
                  "Research YouTube and save verified notes"),
        ], raw, norm),
    ))

    rules.append((
        re.compile(
            r"^(?:open\s+youtube,?\s*(?:and\s+)?)?"
            r"(?:search(?:\s+for)?|find)\s+(.+?),?\s+(?:and|then)\s+play\s+"
            r"(?:the\s+)?(?:latest|newest|most\s+recent)(?:\s+uploaded)?\s+"
            r"(?:one|1|video|result)$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("browser.youtube_play_latest", {"query": m.group(1).strip()},
                  "Search YouTube and play the latest result"),
        ], raw, norm),
    ))

    # Natural-language percentage breakdown.
    rules.append((
        re.compile(
            r"^what\s+(?:is|it)\s+(?:the\s+)?(\d+(?:\.\d+)?)\s*(?:percent|percentage|%)\s+of\s+"
            r"(\d+(?:\.\d+)?)\s+and\s+from\s+that\s+(?:\d+(?:\.\d+)?\s*(?:percent|percentage|%)\s+)?"
            r"what\s+is\s+(?:the\s+)?(\d+(?:\.\d+)?)\s*(?:percent|percentage|%)\s+amount\s+and\s+"
            r"(\d+(?:\.\d+)?)\s*(?:percent|percentage|%)\s+amount\??$",
            re.IGNORECASE,
        ),
        _percentage_breakdown_plan,
    ))

    # ── 1. Open calculator AND calculate (must come before bare "open calc") ──
    rules.append((
        re.compile(
            r"^open\s+(?:the\s+)?(?:super\s+)?calc(?:ulator)?\s+and\s+(?:calculate|compute|evaluate|execute|do)(?:\s+the\s+(?:adding|subtraction|math)\s+of)?\s+(.+)$",
            re.IGNORECASE,
        ),
        _combined_calculator_plan,
    ))

    # ── 2. Open notepad AND type ───────────────────────────────────────────────
    rules.append((
        re.compile(
            r"^open\s+(?:notepad|note\s+pad)\s+and\s+(?:type|write)\s+(.+)$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("notepad.open", {}, "Open Notepad"),
            _step("notepad.type", {"text": m.group(1).strip()}, "Type text"),
        ], raw, norm),
    ))

    # ── 3. Open YouTube and search (must come before generic "open X and search") ──
    rules.append((
        re.compile(
            r"^(?:open\s+youtube\s+and\s+search|search\s+youtube\s+for)\s+(.+)$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("browser.open", {"url": "https://www.youtube.com"}, "Open YouTube"),
            _step("browser.youtube_search", {"query": m.group(1).strip()}, "Search YouTube"),
        ], raw, norm),
    ))

    # ── 4. Take screenshot AND open folder ────────────────────────────────────
    rules.append((
        re.compile(
            r"^take\s+(?:a\s+)?screenshot\s+and\s+open\s+(?:the\s+)?(\w+)(?:\s+folder)?$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("system.screenshot", {}, "Take screenshot"),
            _step("folder.open", {"folder_name": _normalize_folder_name(m.group(1))}, "Open folder"),
        ], raw, norm),
    ))

    # ── 5. Open folder AND find files ─────────────────────────────────────────
    rules.append((
        re.compile(
            r"^open\s+(\w+)(?:\s+folder)?\s+and\s+(?:find|search(?:\s+for)?)\s+(?:the\s+)?(.+)$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("folder.open", {"folder_name": _normalize_folder_name(m.group(1))}, "Open folder"),
            _step("file.search", {
                "folder": _normalize_folder_name(m.group(1)),
                "pattern": m.group(2).strip(),
            }, "Search files"),
        ], raw, norm),
    ))

    # ── 6. Open <site with dot> and search ───────────────────────────────────
    rules.append((
        re.compile(
            r"^open\s+([\w\-]+\.[\w.\-]+)\s+and\s+search(?:\s+for)?\s+(.+)$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("browser.open", {"url": m.group(1).strip()}, "Open website"),
            _step("browser.website_search", {
                "site": m.group(1).strip(),
                "query": m.group(2).strip(),
            }, "Search website"),
        ], raw, norm),
    ))

    # ── 7. Named app open ─────────────────────────────────────────────────────
    rules.append((
        re.compile(
            r"^(?:open|show|find)\s+(.+?)\s+(?:in|with)\s+"
            r"(?:the\s+)?(?:file\s+manager|file\s+explorer|explorer)$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("file.open_containing", {"query": m.group(1).strip()},
                  f"Find and open {m.group(1).strip()} in File Explorer"),
        ], raw, norm),
    ))

    _apps_re = "|".join(re.escape(k) for k in _APP_ALIASES)
    rules.append((
        re.compile(
            rf"^(?:open|start)\s+({_apps_re})$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("app.open", {"app_name": _normalize_app_name(m.group(1))}, f"Open {m.group(1)}"),
        ], raw, norm),
    ))

    # ── 8. Open app <name> (generic prefix) ───────────────────────────────────
    rules.append((
        re.compile(r"^open\s+app\s+(\w[\w\s]*)$", re.IGNORECASE),
        lambda m, raw, norm: _plan([
            _step("app.open", {"app_name": m.group(1).strip().lower()}, f"Open {m.group(1)}"),
        ], raw, norm),
    ))

    # ── 9. Named folder open ──────────────────────────────────────────────────
    _folders_re = "|".join(re.escape(k) for k in _FOLDER_ALIASES)
    rules.append((
        re.compile(
            rf"^open\s+({_folders_re})(?:\s+folder)?$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("folder.open", {"folder_name": _normalize_folder_name(m.group(1))}, f"Open {m.group(1)} folder"),
        ], raw, norm),
    ))

    # ── 10. Open folder <name> (generic prefix) ───────────────────────────────
    rules.append((
        re.compile(r"^open\s+folder\s+(\w+)$", re.IGNORECASE),
        lambda m, raw, norm: _plan([
            _step("folder.open", {"folder_name": m.group(1).strip().lower()}, f"Open {m.group(1)}"),
        ], raw, norm),
    ))

    # ── 11. Calculate / compute / evaluate / what is ──────────────────────────
    rules.append((
        re.compile(
            r"^(?:calculate|compute|evaluate|what\s+is|what's)\s+(.+)$",
            re.IGNORECASE,
        ),
        _internal_calculator_plan,
    ))

    # ── 12. Google search (two prefixes) ──────────────────────────────────────
    rules.append((
        re.compile(
            r"^(?:search\s+google\s+for|google)\s+(.+)$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("browser.google_search", {"query": m.group(1).strip()}, "Google search"),
        ], raw, norm),
    ))

    # ── 13. Open website <url> ────────────────────────────────────────────────
    rules.append((
        re.compile(r"^open\s+website\s+(.+)$", re.IGNORECASE),
        lambda m, raw, norm: _plan([
            _step("browser.open", {"url": m.group(1).strip()}, "Open website"),
        ], raw, norm),
    ))

    # ── 14. Read webpage ──────────────────────────────────────────────────────
    rules.append((
        re.compile(r"^read\s+webpage\s+(.+)$", re.IGNORECASE),
        lambda m, raw, norm: _plan([
            _step("browser.read_page", {"url": m.group(1).strip()}, "Read webpage"),
        ], raw, norm),
    ))

    # ── 15. Website screenshot ────────────────────────────────────────────────
    rules.append((
        re.compile(
            r"^(?:take\s+)?website\s+screenshot\s+(.+)$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("browser.website_screenshot", {"url": m.group(1).strip()}, "Website screenshot"),
        ], raw, norm),
    ))

    # ── 16. Take screenshot ───────────────────────────────────────────────────
    rules.append((
        re.compile(
            r"^(?:take\s+(?:a\s+)?screenshot|screenshot|capture\s+screen)$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("system.screenshot", {}, "Take screenshot"),
        ], raw, norm),
    ))

    # ── 17. System status ─────────────────────────────────────────────────────
    rules.append((
        re.compile(
            r"^(?:system\s+status|status|pc\s+status|laptop\s+status)$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("system.status", {}, "System status"),
        ], raw, norm),
    ))

    # ── 18. Create note ───────────────────────────────────────────────────────
    rules.append((
        re.compile(r"^(?:create\s+(?:a\s+)?note|note\s+down|remember)\s+(.+)$", re.IGNORECASE),
        lambda m, raw, norm: _plan([
            _step("note.create", {"content": m.group(1).strip()}, "Create note"),
        ], raw, norm),
    ))

    # ── 19. List notes ────────────────────────────────────────────────────────
    rules.append((
        re.compile(r"^(?:list|show)\s+notes?$", re.IGNORECASE),
        lambda m, raw, norm: _plan([
            _step("note.create", {"content": "__list__"}, "List notes"),
        ], raw, norm),
    ))

    # ── 20. "do <math>" prefix (e.g. "do 55 + 5") ─────────────────────────────
    rules.append((
        re.compile(
            r"^do\s+(\d+(?:\.\d+)?\s*[+\-*/]\s*\d+(?:\s*[+\-*/]\s*\d+(?:\.\d+)?)*)$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("calculator.calculate", {"expression": m.group(1).strip()}, "Calculate"),
        ], raw, norm),
    ))

    # ── 21. Bare math expression (e.g. "79 + 8", "51 + 79") ───────────────────
    # Must come after all other rules that might contain math-like patterns.
    # Matches expressions that start with a number and contain math operators.
    rules.append((
        re.compile(
            r"^(\d+(?:\.\d+)?\s*[+\-*/]\s*\d+(?:\s*[+\-*/]\s*\d+(?:\.\d+)?)*)$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("calculator.calculate", {"expression": m.group(1).strip()}, "Calculate"),
        ], raw, norm),
    ))

    return rules


RULES = _build_rules()


# ── RuleMatcher class ─────────────────────────────────────────────────────────

class RuleMatcher:
    """Stateless deterministic rule-based command matcher."""

    def match(self, normalized: str, raw: str) -> Optional[ActionPlan]:
        """
        Try each rule in order. Return the first matching ActionPlan,
        or None if no rule matches.
        """
        norm = normalized.strip()
        for pattern, handler in RULES:
            m = pattern.match(norm)
            if m:
                return handler(m, raw, norm)
        return None


# Module-level singleton
matcher = RuleMatcher()
