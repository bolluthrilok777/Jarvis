"""Final feature-plan generation for the Autonomous Feature Orchestrator.

Builds a structured implementation plan from research findings. File paths are
verified against the real repository; proposed new modules are clearly labelled
as proposals rather than existing paths.
"""
from __future__ import annotations

import logging
from pathlib import Path

from jarvis_core.config import BASE_DIR
from jarvis_core.feature_models import FeatureRequest, RiskLevel
from jarvis_core.feature_utils import classify_risk, redact_sensitive

logger = logging.getLogger("jarvis.feature.plan")


def _existing(paths: list[str]) -> list[str]:
    """Keep only paths that actually exist in this repository."""
    verified = []
    for path in paths:
        if not path or not str(path).strip():
            continue
        candidate = BASE_DIR / path if not Path(path).is_absolute() else Path(path)
        try:
            if candidate.exists():
                verified.append(str(candidate).replace("\\", "/"))
        except OSError:
            continue
    return list(dict.fromkeys(verified))


def _steps(feature: FeatureRequest) -> list[str]:
    steps: list[str] = []
    for rec in feature.model_recommendations:
        clean = redact_sensitive(str(rec))[:200]
        if clean and clean.lower() not in {s.lower() for s in steps}:
            steps.append(clean)

    # Always include the structural backbone so the plan is actionable even
    # when local reasoning was unavailable.
    backbone = [
        "Create a focused module for this capability inside jarvis_core/ (no separate app).",
        "Wire it through the existing orchestrator/task lifecycle instead of bypassing it.",
        f"Expose it via the {'Telegram allowlist flow' if feature.telegram_integration_needed else 'relevant Jarvis interface'}.",
        "Add targeted tests under tests/ mirroring the existing pytest style.",
    ]
    for item in backbone:
        if item not in steps:
            steps.append(item)
    return steps[:10]


def build_final_plan(feature: FeatureRequest) -> dict:
    """Produce the structured FINAL FEATURE PLAN.

    Only real, verified repository paths are shown as existing. Proposed new
    modules are clearly labelled as proposals. If repository evidence is too
    weak to ground the plan, ``more_research_required`` is set so the UI can
    surface ⚠️ MORE RESEARCH REQUIRED instead of a falsely confident plan.
    """
    affected_real = _existing(
        list(feature.verified_existing_files) or list(feature.affected_modules)
    )

    reuse = [
        "jarvis_core/orchestrator.py - command routing and task lifecycle",
        "jarvis_core/task_manager.py - LIVE TASKS queue and state transitions",
        "jarvis_core/model_router.py + ollama_service.py - local model routing",
    ]
    for module in affected_real[:6]:
        entry = f"{module} - identified by repository research"
        if entry not in reuse:
            reuse.insert(0, entry)
    reuse = reuse[:8]

    new_modules = list(feature.proposed_new_files) or _proposed_modules(feature)

    security = list(feature.security_implications) or [
        "Preserve the Telegram allowlist; no new unauthenticated surface.",
        "Do not persist credentials; redact secrets from any stored text.",
    ]

    testing = list(feature.testing_requirements) or [
        "Add focused pytest coverage under tests/ following existing style.",
        "Run targeted tests first; full suite only after they pass.",
    ]

    more_research = bool(feature.research_incomplete or feature.needs_more_research)

    plan = {
        "Feature": feature.interpreted_objective or feature.original_request,
        "Objective": feature.interpreted_objective or feature.original_request,
        "Existing components to reuse": reuse,
        "Files/modules likely affected": affected_real,
        "New modules required": new_modules,
        "Unverified assumptions": list(feature.unverified_assumptions)[:5],
        "Implementation steps": _steps(feature),
        "Security considerations": [redact_sensitive(s) for s in security][:6],
        "Testing strategy": testing[:6],
        "Rollback considerations": [
            "All changes stay on a dedicated feature branch; master is untouched until review.",
            "New modules are additive; revert by removing the branch.",
        ],
        "Estimated risk": classify_risk(
            f"{feature.interpreted_objective} {feature.original_request}",
            feature.security_implications + feature.model_recommendations,
        ) or RiskLevel.medium.value,
        "research_incomplete": bool(feature.research_incomplete),
        "chatgpt_reviewed": bool(feature.chatgpt_reviewed),
        "more_research_required": bool(more_research),
    }

    if feature.chatgpt_analysis:
        analysis = feature.chatgpt_analysis
        accepted = analysis.get("accepted") or []
        revised = analysis.get("revised_approach") or []
        if accepted:
            plan["ChatGPT recommendations adopted"] = [str(a)[:200] for a in accepted[:6]]
        if revised:
            head = [redact_sensitive(str(r))[:200] for r in revised[:5]]
            plan["Implementation steps"] = (head + plan["Implementation steps"][len(revised):])[:10]

    logger.info(
        "Final plan built for feature %s (risk=%s, files=%d)",
        feature.feature_id[:8], plan["Estimated risk"], len(affected_real),
    )
    return plan


def _proposed_modules(feature: FeatureRequest) -> list[str]:
    objective = str(feature.interpreted_objective or "").lower()
    words = set(objective.split())
    known = {
        "calendar": "calendar_integration_service.py",
        "memory": "memory_system_service.py",
        "monitoring": "monitoring_extension.py",
        "automation": "automation_service.py",
        "classification": "classification_tuner.py",
        "naukri": "naukri_monitor_service.py",
    }
    proposals = []
    for word, filename in known.items():
        if word in words:
            proposals.append(f"jarvis_core/{filename} (proposed new module)")
    return proposals or [
        "A focused service module inside jarvis_core/ (proposed; exact name at implementation time)"
    ]


def format_plan_text(plan: dict) -> str:
    """Render the plan as Telegram/plain-text friendly output."""
    lines = []
    if plan.get("more_research_required"):
        lines.append("\u26a0\ufe0f MORE RESEARCH REQUIRED")
        lines.append(
            "Jarvis could not yet fully verify the repository evidence needed to "
            "call this plan ready. Review the Unverified assumptions below."
        )
        lines.append("")
    lines += [
        f"Feature: {plan.get('Feature', '')}",
        f"Objective: {plan.get('Objective', '')}",
        "",
        "Existing components to reuse:",
    ]
    lines += [f"- {item}" for item in plan.get("Existing components to reuse", [])]
    lines.append("")
    lines.append("Files/modules likely affected:")
    files = plan.get("Files/modules likely affected") or ["(none verified)"]
    lines += [f"- {item}" for item in files]
    lines.append("")
    lines.append("New modules if required:")
    lines += [f"- {item}" for item in plan.get("New modules required", [])]
    unverified = plan.get("Unverified assumptions") or []
    if unverified:
        lines.append("")
        lines.append("Unverified assumptions:")
        lines += [f"- {item}" for item in unverified]
    lines.append("")
    lines.append("Implementation steps:")
    lines += [
        f"{i}. {step}" for i, step in enumerate(plan.get("Implementation steps", []), start=1)
    ]
    lines.append("")
    lines.append("Security considerations:")
    lines += [f"- {item}" for item in plan.get("Security considerations", [])]
    lines.append("")
    lines.append("Testing strategy:")
    lines += [f"- {item}" for item in plan.get("Testing strategy", [])]
    lines.append("")
    lines.append("Rollback considerations:")
    lines += [f"- {item}" for item in plan.get("Rollback considerations", [])]
    lines.append("")
    lines.append(f"Estimated risk: {plan.get('Estimated risk', 'Medium')}")
    return "\n".join(lines)