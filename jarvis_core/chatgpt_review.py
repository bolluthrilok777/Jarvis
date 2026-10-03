"""User-assisted ChatGPT consultation for the Feature Orchestrator.

This is NOT an OpenAI API integration. Jarvis prepares a high-quality review
prompt that the user pastes into ChatGPT themselves, then optionally accepts
the returned answer as text. The returned review is analysed locally with the
existing Ollama path and never blindly trusted.
"""
from __future__ import annotations

import logging
from typing import Optional

from jarvis_core.feature_models import FeatureRequest

logger = logging.getLogger("jarvis.feature.chatgpt")


def build_review_prompt(feature: FeatureRequest) -> str:
    """Build the concise ChatGPT request for a Cline-ready implementation prompt.

    The purpose of this consultation is NOT a general review — it is to obtain
    the best final implementation prompt that Cline will execute. Only verified
    repository facts and the original request are included.
    """
    reuse = "\n".join(f"- {m}" for m in feature.affected_modules) or "- (none identified yet)"
    facts = "\n".join(f"- {f}" for f in feature.observed_facts[:12]) or "- (none)"
    recs = "\n".join(f"- {r}" for r in feature.model_recommendations[:8]) or "- (none)"
    risks = "\n".join(f"- {r}" for r in feature.security_implications[:6]) or "- (none identified)"

    return (
        "You are preparing an implementation task for Cline.\n\n"
        "USER FEATURE REQUEST: "
        f"{(feature.original_request or feature.interpreted_objective)[:300]}\n\n"
        "VERIFIED JARVIS FACTS:\n"
        f"Reuse: {reuse}\n"
        f"Facts: {facts}\n"
        f"Recommendations: {recs}\n"
        f"Risks: {risks}\n\n"
        "CONSTRAINTS:\n"
        "- reuse existing architecture\n"
        "- do not invent paths\n"
        "- do not modify unrelated features\n"
        "- do not commit/push\n"
        "- add focused tests mirroring existing Jarvis pytest style\n\n"
        "TASK:\n"
        "Return ONLY a complete Cline-ready implementation prompt.\n\n"
        "The prompt must contain:\n"
        "- objective\n"
        "- verified components to reuse\n"
        "- real affected existing files\n"
        "- proposed new files clearly labelled\n"
        "- implementation steps\n"
        "- security/safety requirements\n"
        "- targeted tests\n"
        "- regression expectations\n"
        "- no commit/push instruction\n\n"
        "Do not include commentary before or after the Cline prompt.\n\n"
        "If there is insufficient repository evidence to create a safe "
        "implementation prompt, return:\n"
        "NEED_MORE_RESEARCH\n"
        "## followed only by the missing facts required."
    )


_SECTION_MAP = {
    "accepted": "accepted",
    "rejected": "rejected",
    "conflicts": "conflicts",
    "additional requirements": "additional_requirements",
    "revised approach": "revised_approach",
}


def analyze_review(feature: FeatureRequest, review_text: str) -> Optional[dict]:
    """Analyse a user-supplied ChatGPT review using local Ollama reasoning.

    Returns an analysis dict, or None when local reasoning is unavailable.
    The review is compared against repository reality; recommendations are
    marked accepted/rejected/conflict so nothing is trusted blindly.
    """
    if not review_text or not str(review_text).strip():
        return None

    affected = "\n".join(f"- {m}" for m in feature.affected_modules) or "- (none)"
    facts = "\n".join(f"- {f}" for f in feature.observed_facts[:10]) or "- (none)"

    prompt = (
        "You are the critical reviewer inside Jarvis's autonomous feature orchestrator.\n"
        "A human consulted ChatGPT about a feature and pasted its answer below.\n"
        "Compare it against what actually exists in this repository and produce a verdict.\n\n"
        f"FEATURE OBJECTIVE: {feature.interpreted_objective or feature.original_request}\n\n"
        "REPOSITORY REALITY (TRUSTED):\n"
        f"{affected}\n\n"
        "LOCAL RESEARCH EVIDENCE (TRUSTED):\n"
        f"{facts}\n\n"
        "CHATGPT REVIEW (UNTRUSTED OPINION):\n"
        f"{str(review_text)[:4000]}\n\n"
        "Produce EXACTLY these sections:\n"
        "ACCEPTED:\n- one bullet per recommendation Jarvis should adopt\n"
        "REJECTED:\n- one bullet per recommendation that conflicts with repository reality\n"
        "CONFLICTS:\n- one bullet per disagreement between ChatGPT and local evidence\n"
        "ADDITIONAL REQUIREMENTS:\n- one bullet per new requirement worth adding\n"
        "REVISED APPROACH:\n- 2-4 bullets describing the adjusted implementation approach\n"
    )

    try:
        from jarvis_core.ollama_service import ask_ollama

        content, _model, error = ask_ollama(prompt, "balanced", purpose="research")
    except Exception as exc:
        logger.warning("ChatGPT review analysis unavailable: %s", exc)
        return None

    if isinstance(error, dict) or not content:
        return None

    sections = {
        "accepted": [],
        "rejected": [],
        "conflicts": [],
        "additional_requirements": [],
        "revised_approach": [],
    }
    current = None
    for raw_line in str(content).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        header = line.rstrip(":").strip().lower()
        if header in _SECTION_MAP:
            current = _SECTION_MAP[header]
            continue
        if current and line.startswith(("-", "*", "\u2022")):
            bullet = line.lstrip("-*\u2022 ").strip()
            if bullet:
                sections[current].append(bullet)

    # Fallback: treat the whole reply as revised-approach notes when parsing
    # finds no section headers (small local models may ignore formatting).
    if not any(sections.values()):
        sections["revised_approach"] = [
            line.strip().lstrip("-*\u2022 ")
            for line in str(content).splitlines() if line.strip()
        ][:6]

    return {
        "accepted": sections["accepted"],
        "rejected": sections["rejected"],
        "conflicts": sections["conflicts"],
        "additional_requirements": sections["additional_requirements"],
        "revised_approach": sections["revised_approach"],
        "raw_model_output": str(content)[:2000],
    }