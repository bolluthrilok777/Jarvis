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


# ── Rule table ────────────────────────────────────────────────────────────────
# Each rule is (compiled_pattern, match_handler).
# match_handler(m, raw, norm) -> ActionPlan
# Rules are tried in order; first match wins.

def _build_rules():
    rules = []

    # ── 1. Open calculator AND calculate (must come before bare "open calc") ──
    rules.append((
        re.compile(
            r"^open\s+calc(?:ulator)?\s+and\s+(?:calculate|compute|evaluate)\s+(.+)$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("app.open", {"app_name": "calculator"}, "Open calculator"),
            _step("calculator.calculate", {"expression": m.group(1).strip()}, "Calculate"),
        ], raw, norm),
    ))

    # ── 2. Open notepad AND type ───────────────────────────────────────────────
    rules.append((
        re.compile(
            r"^open\s+notepad\s+and\s+type\s+(.+)$",
            re.IGNORECASE,
        ),
        lambda m, raw, norm: _plan([
            _step("notepad.open", {}, "Open Notepad"),
            _step("notepad.type", {"text": m.group(1).strip()}, "Type text"),
        ], raw, norm),
    ))

    # ── 3. Take screenshot AND open folder ────────────────────────────────────
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

    # ── 4. Open folder AND find files ─────────────────────────────────────────
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

    # ── 5. Open YouTube and search ────────────────────────────────────────────
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
        lambda m, raw, norm: _plan([
            _step("calculator.calculate", {"expression": m.group(1).strip()}, "Calculate"),
        ], raw, norm),
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
        re.compile(r"^create\s+note\s+(.+)$", re.IGNORECASE),
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
