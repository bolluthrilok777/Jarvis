"""
jarvis_core/risk_classifier.py — Phase 5A-1 Risk Level Classifier

Determines whether an ActionPlan is SAFE, PROTECTED, or CRITICAL
by inspecting all skill_ids against two allowlists.

SAFE     — auto-execute, no confirmation needed
PROTECTED — requires user preview + Confirm click
CRITICAL  — blocked by default, requires strong confirmation
"""
from __future__ import annotations

from jarvis_core.action_models import ActionPlan
from jarvis_core.task_models import RiskLevel, ConcurrencyGroup

# Skills that require user confirmation before execution.
# Any plan containing one of these (but none in CRITICAL_SKILLS) is PROTECTED.
PROTECTED_SKILLS: frozenset[str] = frozenset({
    "app.close",
    "notepad.save",
})

# Skills that are dangerous/irreversible — plan is CRITICAL.
# Currently empty in Phase 5A-1 because the skill registry has no destructive skills.
# Future entries: "file.delete", "registry.write", "message.send", etc.
CRITICAL_SKILLS: frozenset[str] = frozenset()

# Skills assigned to the desktop_focus concurrency group.
DESKTOP_FOCUS_SKILLS: frozenset[str] = frozenset({
    "notepad.type",
    "notepad.save",
    "notepad.open",
    "app.focus",
    "app.close",
})

# Skills assigned to the browser concurrency group.
BROWSER_SKILLS: frozenset[str] = frozenset({
    "browser.open",
    "browser.google_search",
    "browser.youtube_search",
    "browser.youtube_play_latest",
    "browser.youtube_research",
    "browser.website_search",
    "browser.read_page",
    "browser.website_screenshot",
})


def classify_risk(plan: ActionPlan | str) -> RiskLevel:
    """
    Inspect every skill_id in the plan and return the highest risk level.
    CRITICAL > PROTECTED > SAFE

    If a raw string is passed (no ActionPlan available), return SAFE.
    """
    if isinstance(plan, str):
        text = plan.lower()
        critical = ("format drive", "format disk", "wipe", "disable antivirus",
                    "disable firewall", "registry", "factory reset", "payment", "pay ")
        protected = ("send email", "send mail", "whatsapp", "submit form",
                     "book slot", "save over", "close unsaved", "delete file")
        if any(item in text for item in critical):
            return RiskLevel.CRITICAL
        if any(item in text for item in protected):
            return RiskLevel.PROTECTED
        return RiskLevel.SAFE

    skill_ids = {step.skill_id for step in plan.steps}

    if skill_ids & CRITICAL_SKILLS:
        return RiskLevel.CRITICAL
    if skill_ids & PROTECTED_SKILLS:
        return RiskLevel.PROTECTED
    return RiskLevel.SAFE


def classify_concurrency(plan: ActionPlan | str) -> ConcurrencyGroup:
    """
    Assign the plan to the highest-priority concurrency group.
    desktop_focus > browser > background

    If a raw string is passed (no ActionPlan available), return background.
    """
    if isinstance(plan, str):
        text = plan.lower()
        if any(item in text for item in ("notepad", "note pad", "calculator", "paint", "type ", "write ")):
            return ConcurrencyGroup.desktop_focus
        if any(item in text for item in ("youtube", "google", "browser", "website", "http://", "https://")):
            return ConcurrencyGroup.browser
        return ConcurrencyGroup.background

    skill_ids = {step.skill_id for step in plan.steps}

    if skill_ids & DESKTOP_FOCUS_SKILLS:
        return ConcurrencyGroup.desktop_focus
    if skill_ids & BROWSER_SKILLS:
        return ConcurrencyGroup.browser
    return ConcurrencyGroup.background


def build_interpreted_command(plan: ActionPlan | str) -> str:
    """
    Build a human-readable single-line description of what the plan will do.
    Uses the step descriptions if present, otherwise generates from skill_id + params.

    If a raw string is passed (no ActionPlan available), return the string as-is.
    """
    if isinstance(plan, str):
        return plan

    parts = []
    for step in plan.steps:
        if step.description:
            parts.append(step.description)
        else:
            parts.append(_describe_step(step.skill_id, step.parameters))
    return " → ".join(parts) if parts else plan.normalized_command


def _describe_step(skill_id: str, params: dict) -> str:
    """Generate a plain-English description from a skill_id and its parameters."""
    if skill_id == "app.open":
        return f"Open {params.get('app_name', 'application')}"
    if skill_id == "app.close":
        return f"Close {params.get('app_name', 'application')}"
    if skill_id == "app.focus":
        return f"Focus {params.get('app_name', 'application')}"
    if skill_id == "calculator.calculate":
        return f"Calculate {params.get('expression', '')}"
    if skill_id == "browser.open":
        return f"Open {params.get('url', 'website')}"
    if skill_id == "browser.google_search":
        return f"Search Google for '{params.get('query', '')}'"
    if skill_id == "browser.youtube_search":
        return f"Search YouTube for '{params.get('query', '')}'"
    if skill_id == "browser.youtube_play_latest":
        return f"Play latest YouTube result for '{params.get('query', '')}'"
    if skill_id == "browser.youtube_research":
        return f"Research YouTube for '{params.get('query', '')}' and save notes"
    if skill_id == "browser.website_search":
        return f"Search {params.get('site', 'website')} for '{params.get('query', '')}'"
    if skill_id == "browser.read_page":
        return f"Read webpage {params.get('url', '')}"
    if skill_id == "browser.website_screenshot":
        return f"Screenshot {params.get('url', '')}"
    if skill_id == "notepad.open":
        return "Open new Notepad"
    if skill_id == "notepad.type":
        text = params.get('text', '')
        preview = text[:40] + "..." if len(text) > 40 else text
        return f"Type \"{preview}\""
    if skill_id == "notepad.save":
        return "Save document"
    if skill_id == "folder.open":
        return f"Open {params.get('folder_name', 'folder')} folder"
    if skill_id == "file.search":
        return f"Search {params.get('folder', 'folder')} for {params.get('pattern', '*')}"
    if skill_id == "file.open_containing":
        return f"Find and open the location of '{params.get('query', '')}'"
    if skill_id == "note.create":
        content = params.get('content', '')
        preview = content[:40] + "..." if len(content) > 40 else content
        return f"Create note: \"{preview}\""
    if skill_id == "system.status":
        return "Get system status"
    if skill_id == "system.screenshot":
        return "Take screenshot"
    return skill_id
