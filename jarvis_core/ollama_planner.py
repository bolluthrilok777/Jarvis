"""
jarvis_core/ollama_planner.py — Phase 4.5 Ollama Structured-Action Fallback Planner

Called only when the RuleMatcher returns None. Sends a structured JSON prompt
to Ollama and validates the response into an ActionPlan.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Union

from jarvis_core.action_models import ActionStep, ActionPlan

logger = logging.getLogger("jarvis.planner")


def _build_skill_list() -> str:
    """Return a compact skill summary for the prompt."""
    from jarvis_core.skill_registry import registry
    lines = []
    for sid in registry.list_skills():
        skill = registry.get_skill(sid)
        params = ", ".join(skill.required_params) if skill.required_params else "none"
        lines.append(f"  {sid}: required_params=[{params}]")
    return "\n".join(lines)


def _build_prompt(normalized: str, raw: str) -> str:
    skill_list = _build_skill_list()
    return (
        f'You are the Jarvis action planner. Infer spelling mistakes, omitted words, and intent. '
        f'The user said: "{normalized}"\n\n'
        f"Available skills (with required parameters):\n{skill_list}\n\n"
        f"Use only those skills. Calculator expressions must contain only numbers and + - * /. "
        f"For multi-step math, emit steps in dependency order using resolved numeric inputs. "
        f"Never invent applications or parameters. Ask for clarification only when intent is genuinely ambiguous.\n\n"
        f'Respond ONLY with a valid JSON object — no explanation, no markdown:\n'
        f'{{\n'
        f'  "steps": [\n'
        f'    {{"skill_id": "<skill_id>", "parameters": {{}}, "description": "<short desc>"}}\n'
        f'  ],\n'
        f'  "confidence": 0.0,\n'
        f'  "clarification_needed": false,\n'
        f'  "clarification_prompt": null\n'
        f'}}'
    )


def plan(normalized: str, raw: str) -> Union[ActionPlan, dict]:
    """
    Try to build an ActionPlan for the normalized command using Ollama.

    Returns an ActionPlan on success.
    Returns an error/clarification dict on any failure.
    """
    from jarvis_core.config import ENGINE_USE_OLLAMA_FALLBACK, CONFIDENCE_THRESHOLD
    from jarvis_core.ollama_service import ask_ollama
    from jarvis_core.skill_registry import registry, SkillNotFoundError

    if not ENGINE_USE_OLLAMA_FALLBACK:
        return {
            "status": "error",
            "message": "No rule matched and Ollama fallback is disabled.",
        }

    prompt = _build_prompt(normalized, raw)
    logger.info("Ollama planner: calling ask_ollama for command: %s", normalized[:80])

    try:
        content, model, error = ask_ollama(prompt, "heavy")
    except Exception as exc:
        logger.error("Ollama planner: ask_ollama raised: %s", exc)
        return {"status": "error", "message": "Ollama service unavailable."}

    if error:
        logger.error("Ollama planner: ask_ollama returned error: %s", error)
        return {"status": "error", "message": "Planner response was invalid. Please rephrase your command."}

    # ── Parse JSON ────────────────────────────────────────────────────────────
    # Strip markdown code fences if the model wrapped the JSON
    raw_content = content.strip()
    if raw_content.startswith("```"):
        raw_content = re.sub(r"^```[a-zA-Z]*\n?", "", raw_content)
        raw_content = raw_content.rstrip("`").strip()

    try:
        data = json.loads(raw_content)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Ollama planner: JSON parse failed: %s | raw=%s", exc, raw_content[:200])
        return {
            "status": "error",
            "message": "Planner response was invalid. Please rephrase your command.",
        }

    # ── Strip unknown skill IDs ────────────────────────────────────────────────
    known_skills = set(registry.list_skills())
    valid_steps = []
    for step_data in data.get("steps", []):
        sid = step_data.get("skill_id", "")
        if sid in known_skills:
            skill = registry.get_skill(sid)
            params = step_data.get("parameters", {})
            if isinstance(params, dict) and all(name in params for name in skill.required_params):
                valid_steps.append(step_data)
            else:
                logger.warning("Ollama planner: missing required parameters for %s", sid)
        else:
            logger.warning("Ollama planner: unknown skill_id stripped: %s", sid)
    data["steps"] = valid_steps

    if not valid_steps:
        return {
            "status": "clarification_needed",
            "message": "I could not identify a valid action. Could you rephrase your command?",
        }

    # ── Validate with Pydantic ────────────────────────────────────────────────
    try:
        # Inject raw/normalized command fields
        data["raw_command"] = raw
        data["normalized_command"] = normalized
        action_plan = ActionPlan.model_validate(data)
    except Exception as exc:
        logger.warning("Ollama planner: Pydantic validation failed: %s", exc)
        return {
            "status": "error",
            "message": "Planner response was invalid. Please rephrase your command.",
        }

    # ── Low-confidence gate ───────────────────────────────────────────────────
    calculator_steps = [s for s in action_plan.steps if s.skill_id == "calculator.calculate"]
    wants_calculator_gui = len(calculator_steps) > 1 or "calculator" in normalized.lower()
    if calculator_steps and wants_calculator_gui:
        if not any(s.skill_id == "app.open" and s.parameters.get("app_name") == "calculator"
                   for s in action_plan.steps):
            action_plan.steps.insert(0, ActionStep(
                skill_id="app.open", parameters={"app_name": "calculator"},
                description="Open Windows Calculator",
            ))
        for step in calculator_steps:
            step.parameters["use_gui"] = False
        calculator_steps[-1].parameters["use_gui"] = True

    if action_plan.confidence < CONFIDENCE_THRESHOLD:
        action_plan.clarification_needed = True

    return action_plan
