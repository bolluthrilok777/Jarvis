"""Structured Feature Orchestrator progress for the LIVE TASKS UI.

Computes the 3-phase / per-step timeline purely from the *persisted real state*
of a FeatureRequest (status, flags, history). The UI never parses display
strings: everything it needs to render progress comes from this structured dict.

No secrets are exposed — only stage labels, timestamps and computed meta.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timezone
from typing import Optional

from jarvis_core.feature_models import FeatureRequest

# Phase/step catalogue. Order matters: it defines linear workflow progression.
PHASES = [
    {
        "id": "research",
        "label": "Research & Analysis",
        "steps": [
            {"id": "received", "label": "Feature request received"},
            {"id": "analyzing", "label": "Feature request analyzed"},
            {"id": "researching", "label": "Repository architecture inspected"},
            {"id": "web_research", "label": "Web / documentation research"},
            {"id": "ollama_analysis", "label": "Local model analysis"},
            {"id": "research_summary", "label": "Research summary generated"},
        ],
    },
    {
        "id": "chatgpt",
        "label": "ChatGPT Review",
        "steps": [
            {"id": "prompt_generated", "label": "ChatGPT prompt generated"},
            {"id": "waiting_review", "label": "Waiting for ChatGPT review"},
            {"id": "review_analyzed", "label": "ChatGPT review analyzed"},
            {"id": "recommendations", "label": "Recommendations reconciled"},
        ],
    },
    {
        "id": "plan",
        "label": "Plan & Approval",
        "steps": [
            {"id": "planning", "label": "Creating implementation plan"},
            {"id": "risk_classified", "label": "Risk classification"},
            {"id": "waiting_approval", "label": "Telegram approval requested"},
            {"id": "approval_received", "label": "Approval received"},
            {"id": "cline_ready", "label": "Cline task generated"},
        ],
    },
]

_TERMINAL = {"completed", "cancelled", "failed", "cline_ready", "approved"}

# Added at the FRONT of the timeline only for Telegram features, which pause
# for a real resource review before any research begins.
RESOURCE_REVIEW_PHASE = {
    "id": "resources",
    "label": "Resource Review",
    "steps": [
        {"id": "resource_inspection",
         "label": "Review open applications and Chrome tabs"},
    ],
}


def _parse(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        value = datetime.fromisoformat(str(iso))
    except (ValueError, TypeError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _seconds_since(iso: Optional[str]) -> Optional[int]:
    start = _parse(iso)
    if start is None:
        return None
    return max(0, int((_now_utc() - start).total_seconds()))


def _history_map(feature: FeatureRequest) -> dict[str, str]:
    """latest 'at' per history stage."""
    out: dict[str, str] = {}
    for event in feature.history:
        out[event.stage] = event.at
    return out
def _step_state(feature, step_id, hm, skipped, reviewed, has_plan,
                approved, cline_ready, cancelled, failed):
    """Return (status, started_at, completed_at) for one step."""
    status_raw = feature.status
    started = hm.get(step_id)
    updated = feature.updated_at

    def _done():
        return ("done", started, hm.get(step_id) or updated)

    if step_id == "received":
        return _done()

    # ── Resource review gate (Telegram features only) ───────────────────
    # While paused at resource_review_pending, the resource step is "waiting
    # for you" and every later workflow step is still pending - nothing is
    # computing and nothing auto-continues.
    if status_raw == "resource_review_pending":
        if step_id == "resource_inspection":
            started = (
                hm.get("resource_review_pending")
                or feature.current_stage_started_at
            )
            return ("waiting", started, None)
        return ("pending", None, None)

    if status_raw == "resource_scanning":
        if step_id == "resource_inspection":
            started = (
                hm.get("resource_scanning")
                or feature.current_stage_started_at
            )
            return ("running", started, None)
        return ("pending", None, None)

    if status_raw == "resource_review":
        if step_id == "resource_inspection":
            started = (
                hm.get("resource_review")
                or feature.current_stage_started_at
            )
            return ("waiting", started, None)
        return ("pending", None, None)

    # Phase-1 hard stop: review completed, next-phase pipeline still pending.
    if status_raw == "resource_review_complete":
        if step_id == "resource_inspection":
            return _done()
        return ("pending", None, None)

    if step_id == "resource_inspection":
        if (
            feature.resource_review_completed
            or status_raw not in ("received", "resource_review_pending")
        ):
            return _done()
        return ("pending", None, None)

    if step_id == "analyzing":
        if status_raw == "analyzing":
            return ("running", started or feature.created_at, None)
        if status_raw != "received":
            return _done()
        return ("pending", None, None)

    if step_id == "researching":
        if status_raw in ("analyzing", "researching"):
            return ("running", started or feature.updated_at, None)
        if status_raw != "received":
            return _done()
        return ("pending", None, None)

    if step_id == "web_research":
        web_ok = any(str(f).startswith("[WEB-UNTRUSTED]") for f in feature.observed_facts)
        if web_ok:
            return _done()
        progressed = status_raw not in ("received", "analyzing")
        if progressed and feature.research_incomplete:
            return ("skipped", started, hm.get("researching"))
        if status_raw in ("chatgpt_review_pending", "planning", "approval_pending",
                          "approved", "cline_ready") or status_raw in _TERMINAL:
            return _done()
        return ("pending", None, None)

    if step_id == "ollama_analysis":
        if feature.model_recommendations:
            return _done()
        progressed = status_raw not in ("received", "analyzing")
        if progressed and feature.research_incomplete:
            return ("skipped", started, hm.get("researching"))
        if status_raw in ("researching", "analyzing"):
            return ("running", started or feature.updated_at, None)
        if status_raw in ("chatgpt_review_pending", "planning", "approval_pending",
                          "approved", "cline_ready") or status_raw in _TERMINAL:
            return ("skipped", started, hm.get("researching"))
        return ("pending", None, None)

    if step_id == "research_summary":
        if status_raw in ("chatgpt_review_pending", "planning", "approval_pending",
                          "approved", "cline_ready") or status_raw in _TERMINAL:
            return _done()
        if status_raw == "researching":
            return ("running", started or feature.updated_at, None)
        return ("pending", None, None)

    if step_id == "prompt_generated":
        if feature.chatgpt_prompt:
            return _done()
        if skipped:
            return ("skipped", started, feature.updated_at)
        if status_raw == "chatgpt_review_pending":
            return ("running", started or feature.updated_at, None)
        if status_raw in ("planning", "approval_pending", "approved", "cline_ready") or status_raw in _TERMINAL:
            return ("skipped", started, feature.updated_at)
        return ("pending", None, None)

    if step_id == "waiting_review":
        if skipped:
            return ("skipped", started, feature.updated_at)
        if reviewed:
            return _done()
        if status_raw == "chatgpt_review_pending":
            return ("waiting", started or hm.get("prompt_generated"), None)
        if status_raw in ("planning", "approval_pending", "approved", "cline_ready"):
            return _done()
        return ("pending", None, None)
    if step_id == "review_analyzed":
        if reviewed:
            return _done()
        if skipped:
            return ("skipped", started, feature.updated_at)
        if status_raw == "chatgpt_review_pending":
            return ("waiting", None, None)
        if status_raw in ("planning", "approval_pending", "approved", "cline_ready"):
            return ("skipped", started, feature.updated_at)
        return ("pending", None, None)

    if step_id == "recommendations":
        if reviewed:
            return _done()
        if skipped:
            return ("skipped", started, feature.updated_at)
        if status_raw in ("planning", "approval_pending", "approved", "cline_ready"):
            return ("skipped", started, feature.updated_at)
        if status_raw == "chatgpt_review_pending":
            return ("waiting", None, None)
        return ("pending", None, None)

    if step_id == "planning":
        if status_raw == "planning":
            return ("running", started or feature.created_at, None)
        if has_plan or status_raw in ("approval_pending", "approved", "cline_ready") or status_raw in _TERMINAL:
            return _done()
        return ("pending", None, None)

    if step_id == "risk_classified":
        if has_plan or status_raw in ("approval_pending", "approved", "cline_ready") or status_raw in _TERMINAL:
            return _done()
        if status_raw == "planning":
            return ("running", started, None)
        return ("pending", None, None)

    if step_id == "waiting_approval":
        if status_raw == "approval_pending":
            return ("waiting", started or feature.updated_at, None)
        if approved or status_raw in ("approved", "cline_ready") or status_raw in _TERMINAL:
            return _done()
        return ("pending", None, None)

    if step_id == "approval_received":
        if approved or status_raw == "approved":
            return _done()
        if status_raw == "approval_pending":
            return ("waiting", None, None)
        return ("pending", None, None)

    if step_id == "cline_ready":
        if cline_ready or status_raw == "cline_ready":
            return _done()
        return ("pending", None, None)

    return ("pending", None, None)


def _phase_status(steps: list[dict]) -> str:
    """Aggregate phase status: done / running / waiting / skipped / cancelled / pending."""
    statuses = [s["status"] for s in steps]
    if "failed" in statuses:
        return "failed"
    if statuses and all(s in ("done", "skipped") for s in statuses):
        return "done"
    if statuses and all(s in ("done", "skipped", "cancelled") for s in statuses):
        return "cancelled" if "cancelled" in statuses else "done"
    if "waiting" in statuses:
        return "waiting"
    if "running" in statuses:
        return "running"
    return "pending"


def _overall_status(feature: FeatureRequest) -> str:
    if feature.status == "cancelled":
        return "CANCELLED"
    if feature.status == "failed":
        return "FAILED"
    if feature.status in ("completed", "implementation_in_progress", "cline_ready"):
        return "COMPLETED"
    if feature.status == "resource_scanning":
        return "SCANNING RESOURCES"
    if feature.status in ("resource_review_pending", "resource_review"):
        return "WAITING FOR RESOURCE REVIEW"
    if feature.status == "resource_review_complete":
        return "RESOURCE REVIEW COMPLETED"
    if feature.status == "approval_pending":
        if feature.risk == "High" and not feature.approved_at and not feature.high_risk_confirmed:
            return "WAITING FOR HIGH-RISK CONFIRMATION"
        return "WAITING FOR APPROVAL"
    if feature.status == "chatgpt_review_pending":
        return "WAITING FOR CHATGPT REVIEW"
    return "RUNNING"
def _build_phases(feature: FeatureRequest) -> list[dict]:
    status = feature.status
    hm = _history_map(feature)
    skipped = bool(feature.chatgpt_skipped)
    reviewed = bool(feature.chatgpt_reviewed) or bool(feature.chatgpt_analysis)
    has_plan = bool(feature.plan)
    approved = bool(feature.approved_at)
    cline_ready = bool(feature.cline_task)
    cancelled = status == "cancelled"
    failed = status == "failed"

    # Telegram features first pause for a real resource review; Web features
    # skip that phase entirely and keep the original timeline.
    phase_templates = PHASES
    if feature.source == "Telegram":
        phase_templates = [RESOURCE_REVIEW_PHASE, *PHASES]

    phases: list[dict] = []
    for phase in phase_templates:
        phase_steps = []
        for step in phase["steps"]:
            step_status, started_at, completed_at = _step_state(
                feature, step["id"], hm, skipped, reviewed,
                has_plan, approved, cline_ready, cancelled, failed,
            )
            phase_steps.append({
                "id": step["id"],
                "label": step["label"],
                "status": step_status,
                "started_at": started_at,
                "completed_at": completed_at,
            })
        phases.append({
            "id": phase["id"],
            "label": phase["label"],
            "status": _phase_status(phase_steps),
            "steps": phase_steps,
        })

    # Terminal cancellation/failure should mark all still-pending/future steps as
    # cancelled so the timeline reads "stopped here", not "pending forever".
    if cancelled or failed:
        for phase in phases:
            for step in phase["steps"]:
                if step["status"] in ("pending", "running", "waiting"):
                    step["status"] = "cancelled"
        for phase in phases:
            phase["status"] = _phase_status(phase["steps"])

    return phases


def _locate_current(phases: list[dict]):
    """Return (current_phase_id, current_stage_label, next_stage_label)."""
    flat = []
    for phase in phases:
        for step in phase["steps"]:
            flat.append({"phase": phase["id"], "label": step["label"], "status": step["status"]})

    active_idx = None
    for i, step in enumerate(flat):
        if step["status"] in ("running", "waiting"):
            active_idx = i
            break
    if active_idx is None:
        for i in range(len(flat) - 1, -1, -1):
            if flat[i]["status"] in ("done", "skipped", "cancelled"):
                active_idx = i
                break
    if active_idx is None:
        active_idx = 0

    current_phase = flat[active_idx]["phase"]
    current_label = flat[active_idx]["label"]

    next_label = ""
    for i in range(active_idx + 1, len(flat)):
        if flat[i]["status"] in ("pending", "running", "waiting"):
            next_label = flat[i]["label"]
            break
    return current_phase, current_label, next_label
def _progress_percent(flat: list[dict]) -> int:
    """completed-equivalent / applicable steps. Skipped counts as complete so
    progress can still reach 100% when a phase is skipped."""
    active_idx = None
    for i, step in enumerate(flat):
        if step["status"] in ("running", "waiting"):
            active_idx = i
            break
    applicable = len(flat) if active_idx is None else max(1, active_idx + 1)
    completed = 0
    for i in range(min(applicable, len(flat))):
        if flat[i]["status"] in ("done", "skipped", "cancelled"):
            completed += 1
    return int(round(completed / applicable * 100))


def _remaining_steps(phases: list[dict]) -> int:
    count = 0
    for phase in phases:
        for step in phase["steps"]:
            if step["status"] in ("pending", "running", "waiting"):
                count += 1
    return count


def _stage_timings(feature: FeatureRequest, phases: list[dict]) -> dict:
    elapsed = max(0, _seconds_since(feature.created_at) or 0)
    stage_started_at = feature.current_stage_started_at
    stage_elapsed = max(0, _seconds_since(stage_started_at) or 0)
    overall = _overall_status(feature)

    eta_seconds = None
    eta_reason = "Calculating..."
    if overall in ("CANCELLED", "COMPLETED", "FAILED"):
        eta_seconds = 0
        eta_reason = "Done"
    elif "WAITING" in overall:
        eta_seconds = None
        eta_reason = "Waiting for user action"
    else:
        durations = []
        times = [_parse(e.at) for e in feature.history]
        times = [t for t in times if t is not None]
        for i in range(1, len(times)):
            d = (times[i] - times[i - 1]).total_seconds()
            if 0 < d < 3600 * 12:
                durations.append(d)
        remaining = _remaining_steps(phases)
        if durations and remaining > 0:
            avg = sum(durations) / len(durations)
            eta_seconds = int(avg * remaining)
            eta_reason = "Estimate from stage history"
        else:
            eta_seconds = None
            eta_reason = "Calculating..."

    return {
        "created_at": feature.created_at,
        "elapsed_seconds": elapsed,
        "stage_elapsed_seconds": stage_elapsed,
        "stage_started_at": stage_started_at,
        "eta_seconds": eta_seconds,
        "eta_reason": eta_reason,
    }


def build_progress(feature: FeatureRequest) -> dict:
    """Structured progress for one FeatureRequest (safe, UI-ready)."""
    phases = _build_phases(feature)
    flat = []
    for phase in phases:
        flat.extend(phase["steps"])
    current_phase, current_label, next_label = _locate_current(phases)
    percent = _progress_percent(flat)
    timings = _stage_timings(feature, phases)

    return {
        "feature_id": feature.feature_id,
        "overall_status": _overall_status(feature),
        "current_phase": current_phase,
        "current_stage": current_label,
        "next_stage": next_label,
        "progress_percent": min(100, max(0, percent)),
        "started_at": feature.created_at,
        "elapsed_seconds": timings["elapsed_seconds"],
        "stage_started_at": timings["stage_started_at"],
        "stage_elapsed_seconds": timings["stage_elapsed_seconds"],
        "eta_seconds": timings["eta_seconds"],
        "eta_reason": timings["eta_reason"],
        "cancel_allowed": feature.status not in ("completed", "cancelled", "failed"),
        "cancel_requested": any(e.stage == "cancelled" for e in feature.history),
        "phases": phases,
    }
