"""Telegram approval-gate helpers for the Feature Orchestrator.

Approval/authorisation decisions funnel through here so the allowlist rule
and the mandatory Telegram gate are enforced in exactly one place.
"""
from __future__ import annotations

from jarvis_core.feature_models import FeatureRequest, FeatureStatus


def is_authorized_telegram_user(user_id, allowed_user_ids) -> bool:
    """Approval authority check - must be an integer in the allowlist."""
    return isinstance(user_id, int) and user_id in set(allowed_user_ids or [])


def can_transition(current: str, target: str) -> bool:
    """Whitelist of legal status transitions for the feature state machine.

    Phase 1 (Telegram Feature Development) enforces the strict linear route:

        awaiting_mode_selection → resource_scanning → resource_review →
        resource_review_complete (or cancelled/failed at any point)

    ONLY an explicit user Continue may move from ``resource_review`` to
    ``resource_review_complete``; Focus / Screenshot / Close / Edit / Expand /
    Collapse / Back / Refresh are UI substates that stay inside
    ``resource_review``. ``resource_review_complete`` is a hard stop in this
    phase - nothing (research/Ollama/ChatGPT/Cline) starts automatically.
    """
    allowed = {
        "awaiting_mode_selection": {"resource_scanning", "cancelled", "failed"},
        "resource_scanning": {"resource_review", "cancelled", "failed"},
        "resource_review": {"resource_review_complete", "cancelled", "failed"},
        # Part 9: ▶️ Continue enqueues RESEARCH exactly once, which resumes the
        # existing pipeline. The research stage moves the feature into
        # ``analyzing``; nothing else may leave this state automatically.
        "resource_review_complete": {"analyzing", "cancelled", "failed"},
        "resource_review_pending": {
            "resource_review", "resource_review_complete", "cancelled", "failed",
        },
        "received": {"resource_review_pending", "analyzing", "cancelled", "failed"},
        "analyzing": {"researching", "failed", "cancelled"},
        "researching": {"chatgpt_review_pending", "planning", "failed", "cancelled"},
        "chatgpt_review_pending": {"planning", "cancelled"},
        "planning": {"approval_pending", "failed", "cancelled"},
        "approval_pending": {"approved", "planning", "cancelled"},
        "approved": {"cline_ready", "cancelled"},
        "cline_ready": {"implementation_in_progress", "completed", "failed"},
        "implementation_in_progress": {"completed", "failed"},
        "completed": set(),
        "cancelled": set(),
        "failed": {"received"},  # retry path
    }
    return target in allowed.get(current, set())


def request_received_message(feature: FeatureRequest) -> str:
    title = feature.interpreted_objective or feature.original_request
    return (
        "\U0001f9e0 FEATURE REQUEST RECEIVED\n\n"
        f"{title}\n\n"
        "Status: Analyzing..."
    )


def research_complete_message(feature: FeatureRequest) -> str:
    research_note = "\u2705 Complete" if not feature.research_incomplete else "\u26a0\ufe0f Partial"
    return (
        "\U0001f50e RESEARCH COMPLETE\n\n"
        f"Feature: {feature.interpreted_objective}\n\n"
        f"Repository analysis: {research_note}\n"
        "Local AI analysis: \u2705 Complete\n\n"
        "ChatGPT consultation recommended."
    )


def chatgpt_review_ready_message(feature: FeatureRequest) -> str:
    return (
        "\U0001f9e0 CHATGPT REQUEST READY\n\n"
        f"Feature: {feature.interpreted_objective}\n\n"
        "Purpose:\n"
        "Ask ChatGPT to generate the final Cline implementation prompt.\n\n"
        f"Research: {feature.research_label} \u2705 Complete"
    )


def chatgpt_review_buttons(feature: FeatureRequest) -> dict:
    """Section E/H labels; callback data kept backwards-compatible."""
    fid = feature.feature_id
    return {
        "inline_keyboard": [
            [
                {"text": "\U0001f4cb View ChatGPT Request",
                 "callback_data": f"feature:chatgpt_show:{fid}"},
                {"text": "\U0001f4dd Submit / Replace Cline Prompt",
                 "callback_data": f"feature:review:{fid}"},
            ],
            [
                {"text": "\u23ed Skip ChatGPT",
                 "callback_data": f"feature:chatgpt_skip:{fid}"},
                {"text": "\U0001f6ab Cancel Feature",
                 "callback_data": f"feature:cancel:{fid}"},
            ],
            [
                {"text": "\U0001f4f8 Proof",
                 "callback_data": f"feature:proof:{fid}"},
                {"text": "\U0001f9e9 Apps / Tabs",
                 "callback_data": f"feature:apps:{fid}"},
            ],
        ]
    }


def review_analyzed_message(feature: FeatureRequest) -> str:
    analysis = feature.chatgpt_analysis or {}
    accepted = len(analysis.get("accepted") or [])
    conflicts = len(analysis.get("conflicts") or [])
    return (
        "\U0001f9e0 CHATGPT REVIEW ANALYZED\n\n"
        f"Accepted recommendations: {accepted}\n"
        f"Conflicts: {conflicts}\n"
        "Plan updated: \u2705"
    )


def plan_ready_message(feature: FeatureRequest) -> str:
    plan = feature.plan or {}
    files = plan.get("Files/modules likely affected") or []
    review = "\u2705" if feature.chatgpt_reviewed else "Skipped"
    research = "\u2705" if not feature.research_incomplete else "\u26a0\ufe0f partial"
    risk_note = (
        "\n\u26a0\ufe0f HIGH-RISK FEATURE - extra confirmation will be required."
        if feature.risk == "High" else ""
    )
    return (
        "\U0001f680 FEATURE PLAN READY\n\n"
        f"Feature: {feature.interpreted_objective}\n\n"
        f"Research: {research}\n"
        f"ChatGPT review: {review}\n"
        f"Risk: {feature.risk}\n"
        f"Likely files: {len(files)}{risk_note}"
    )


def plan_buttons(feature: FeatureRequest) -> dict:
    rows = [[
        {"text": "\u2705 Approve Cline",
         "callback_data": f"feature:approve:{feature.feature_id}"},
        {"text": "\U0001f4cb View Full Plan",
         "callback_data": f"feature:plan_view:{feature.feature_id}"},
    ]]
    rows.append([
        {"text": "\u270f\ufe0f Request Changes",
         "callback_data": f"feature:changes:{feature.feature_id}"},
        {"text": "\u274c Cancel",
         "callback_data": f"feature:cancel:{feature.feature_id}"},
    ])
    return {"inline_keyboard": rows}


def high_risk_message(feature: FeatureRequest) -> str:
    plan = feature.plan or {}
    risks = plan.get("Security considerations") or []
    risk_lines = "\n".join(f"\u2022 {r}" for r in risks[:5]) or "\u2022 Destructive/sensitive capability requested."
    return (
        "\u26a0\ufe0f HIGH-RISK IMPLEMENTATION\n\n"
        f"Feature: {feature.interpreted_objective}\n\n"
        "Major risks:\n"
        f"{risk_lines}\n\n"
        "Confirm only if you accept these risks."
    )


def high_risk_buttons(feature: FeatureRequest) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "\u2705 Confirm Implementation",
             "callback_data": f"feature:confirm_high_risk:{feature.feature_id}"},
            {"text": "\u274c Cancel",
             "callback_data": f"feature:cancel_high_risk:{feature.feature_id}"},
        ]]
    }


def cline_ready_message(feature: FeatureRequest) -> str:
    task = feature.cline_task or {}
    prompt = task.get("cline_prompt", "")
    preview = prompt[:1200]
    return (
        "\U0001f680 CLINE TASK READY\n\n"
        f"Feature: {feature.interpreted_objective}\n"
        f"Risk: {feature.risk}\n\n"
        "Approved Cline implementation prompt:\n\n"
        f"{preview}"
    )


def cancelled_message(feature: FeatureRequest) -> str:
    return (
        "\u274c FEATURE CANCELLED\n\n"
        f"Feature: {feature.interpreted_objective}\n\n"
        "Nothing was sent to Cline. Safe history preserved."
    )


def status_label(status: str) -> str:
    try:
        return FeatureStatus(status).value.replace("_", " ")
    except ValueError:
        return status


# ── V1.5: provenance / Cline-prompt workflow helpers ─────────────────────────


def chatgpt_output_received_message(feature: FeatureRequest) -> str:
    """Section F: proof chain for user-submitted ChatGPT output."""
    prov = feature.chatgpt_provenance or {}
    received = str(prov.get("submitted_at", ""))[:19].replace("T", " ")
    sha = str(prov.get("sha256", ""))
    return (
        "\u2705 CHATGPT OUTPUT RECEIVED\n\n"
        "Source: Submitted by authorized user\n"
        f"Feature: {feature.interpreted_objective}\n"
        f"Received: {received}\n"
        f"Content hash: {sha[:12]}\n\n"
        "The exact submitted text is preserved verbatim."
    )


def cline_prompt_ready_message(feature: FeatureRequest) -> str:
    """Section G: show ONLY the actual prompt destined for Cline."""
    return (
        "\U0001f4cb CLINE PROMPT READY\n\n"
        f"{(feature.cline_candidate_prompt or '')[:3000]}"
    )


def cline_prompt_buttons(feature: FeatureRequest) -> dict:
    fid = feature.feature_id
    return {
        "inline_keyboard": [
            [
                {"text": "\u2705 Approve Prompt",
                 "callback_data": f"feature:approve:{fid}"},
                {"text": "\U0001f4dd Edit / Replace Prompt",
                 "callback_data": f"feature:review:{fid}"},
            ],
            [
                {"text": "\U0001f4cb View Prompt",
                 "callback_data": f"feature:prompt_view:{fid}"},
                {"text": "\U0001f6ab Cancel",
                 "callback_data": f"feature:cancel:{fid}"},
            ],
        ]
    }


def prompt_updated_message(feature: FeatureRequest) -> str:
    """Section H acknowledgement derived from the submitted prompt itself."""
    prompt = feature.cline_candidate_prompt or ""
    bullets = [
        ln.strip().lstrip("-*\u2022 ").strip()
        for ln in prompt.splitlines()
        if ln.strip().startswith(("-", "*", "\u2022"))
    ][:5] or [ln.strip() for ln in prompt.splitlines() if ln.strip()][:3]
    summary = "\n".join(f"\u2022 {b[:140]}" for b in bullets) or "\u2022 (see exact prompt)"
    return (
        "\u270f\ufe0f CLINE PROMPT UPDATED\n\n"
        "Jarvis will ask Cline to:\n"
        f"{summary}\n\n"
        f"Current prompt: {feature.prompt_label} | "
        f"Source: User Edited | Based on Research {feature.research_label}"
    )


def more_research_buttons(feature: FeatureRequest) -> dict:
    """Section I buttons after NEED_MORE_RESEARCH."""
    fid = feature.feature_id
    return {
        "inline_keyboard": [
            [{"text": "\U0001f50e Re-run Research",
              "callback_data": f"feature:reresearch:{fid}"}],
            [
                {"text": "\U0001f4cb View Missing Evidence",
                 "callback_data": f"feature:missing:{fid}"},
                {"text": "\U0001f6ab Cancel",
                 "callback_data": f"feature:cancel:{fid}"},
            ],
        ]
    }


_PROMPT_SOURCE_LABELS = {
    "ChatGPT": "ChatGPT",
    "User Edited": "User Edited",
    "Jarvis Local": "Jarvis Local / ChatGPT Skipped",
}


def ready_for_cline_message(feature: FeatureRequest) -> str:
    """Section J final gate with real prompt provenance."""
    src = _PROMPT_SOURCE_LABELS.get(
        feature.prompt_source or "", feature.prompt_source
    )
    risk_note = (
        "\n\u26a0\ufe0f HIGH-RISK - second confirmation required."
        if feature.risk == "High" else ""
    )
    return (
        "\U0001f680 READY FOR CLINE\n\n"
        f"Feature: {feature.interpreted_objective}\n"
        f"Risk: {feature.risk}{risk_note}\n"
        f"Prompt source: {src}\n"
        f"Prompt: {feature.prompt_label} | Research: {feature.research_label}"
    )


def ready_for_cline_buttons(feature: FeatureRequest) -> dict:
    fid = feature.feature_id
    return {
        "inline_keyboard": [
            [
                {"text": "\u2705 Approve Cline Prompt",
                 "callback_data": f"feature:approve:{fid}"},
                {"text": "\U0001f4cb View Exact Prompt",
                 "callback_data": f"feature:prompt_view:{fid}"},
            ],
            [
                {"text": "\U0001f4dd Edit Prompt",
                 "callback_data": f"feature:review:{fid}"},
                {"text": "\U0001f6ab Cancel",
                 "callback_data": f"feature:cancel:{fid}"},
            ],
        ]
    }
