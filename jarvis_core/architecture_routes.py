"""Part 2 — Architecture UI: REST + WebSocket API for the workflow engine.

This is the visual/realtime control layer. It NEVER touches the locked Part 1
files directly and NEVER writes SQLite: every mutation goes through
``workflow_engine`` (the Part 1 authoritative service). After each successful
mutation it publishes a lightweight, secret-free message onto
``workflow_event_bus`` so subscribed WebSocket clients update live.

It also serves the standalone ``architecture.html`` page, exposes the
frontend presentation-state mapping (WorkflowState -> block states) so the UI
and backend agree, and stores the Permission Guard policy as safe workflow
metadata (Part 9 will later consume it; Part 2 only configures it).

No Chrome/ChatGPT/Kiro side effects happen here — those are later Parts.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from jarvis_core.config import BASE_DIR
from jarvis_core.workflow_engine import (
    ConcurrencyError,
    InvalidTransition,
    TerminalWorkflowError,
    WorkflowError,
    WorkflowNotFound,
    workflow_engine,
)
from jarvis_core.workflow_events_bus import (
    MSG_EVENT_ADDED,
    MSG_STATE_TRANSITION,
    MSG_WORKFLOW_CANCELLED,
    MSG_WORKFLOW_COMPLETED,
    MSG_WORKFLOW_CREATED,
    MSG_WORKFLOW_FAILED,
    MSG_WORKFLOW_PAUSED,
    MSG_WORKFLOW_RESUMED,
    MSG_WORKFLOW_UPDATED,
    workflow_event_bus,
)
from jarvis_core.workflow_states import WorkflowState, allowed_targets, is_terminal

logger = logging.getLogger("jarvis.architecture")

router = APIRouter(prefix="/api/workflows", tags=["architecture"])
ws_router = APIRouter()
page_router = APIRouter()

ARCHITECTURE_HTML = BASE_DIR / "architecture.html"

# Permission Guard policy is stored on a well-known metadata key of the
# workflow. Part 2 configures it; Part 9 (Kiro) will later enforce it.
PERMISSION_POLICY_KEY = "permission_policy"

# Default proposed Permission Guard rules (frontend presentation defaults are
# kept here too so the backend is the single source of truth). Decisions are
# strictly one of ALLOW / ASK / DENY.
DEFAULT_PERMISSION_POLICY: dict[str, str] = {
    "read_project_files": "ALLOW",
    "write_edit_project_files": "ALLOW",
    "run_tests": "ALLOW",
    "normal_python_commands": "ALLOW",
    "normal_npm_node_commands": "ALLOW",
    "delete_files": "ASK",
    "install_unusual_packages": "ASK",
    "git_push": "ASK",
    "deployment": "ASK",
    "external_side_effects": "ASK",
    "git_reset": "DENY",
    "git_clean": "DENY",
    "destructive_repo_operations": "DENY",
    "credential_secret_operations": "DENY",
}

_VALID_DECISIONS = {"ALLOW", "ASK", "DENY"}


# ── Authoritative state -> architecture-block presentation mapping ──────────
# Block ids (fixed frontend contract, in flow order):
BLOCK_IDS = [
    "chrome_profile",       # 1
    "gpt_conversation",     # 2
    "master_requirement",   # 3
    "telegram_command",     # 4
    "gpt_architect",        # 5
    "gpt_output_capture",   # 6
    "your_approval",        # 7
    "kiro_executor",        # 8
    "permission_guard",     # 9
    "kiro_monitor",         # 10
    "kiro_result_capture",  # 11
    "gpt_review",           # 12
    "complete",             # 13
]

# Presentation states (frontend-only; NOT WorkflowState values):
# PENDING, ACTIVE, WAITING, SUCCESS, FAILED, PAUSED, CANCELLED.
#
# For each WorkflowState we describe which blocks are SUCCESS (done),
# which are ACTIVE/WAITING (current), and everything else defaults to PENDING.
# This is derived on the backend so the UI and tests share one contract.

# Linear "progress index" of each block along the happy path (loop excluded).
_BLOCK_ORDER = {bid: i for i, bid in enumerate(BLOCK_IDS)}


def _presentation_for_state(state: str) -> dict[str, str]:
    """Return {block_id: presentation_state} for a given WorkflowState value."""
    # active_blocks: currently running / waiting. done_up_to: blocks before the
    # active phase are SUCCESS. waiting flag marks a block WAITING vs ACTIVE.
    S = WorkflowState
    mapping: dict[str, dict] = {
        S.IDLE.value: {"active": [], "done_before": None},
        S.INITIALIZING.value: {"active": ["chrome_profile"], "done_before": "chrome_profile", "flavor": "ACTIVE"},
        S.GPT_BOOTSTRAPPING.value: {
            "active": ["chrome_profile", "gpt_conversation", "master_requirement"],
            "done_before": "chrome_profile", "flavor": "ACTIVE",
        },
        S.WAITING_FOR_COMMAND.value: {
            "active": ["telegram_command"],
            "done_before": "telegram_command", "flavor": "WAITING",
        },
        S.GPT_RUNNING.value: {
            "active": ["gpt_architect"], "done_before": "gpt_architect", "flavor": "ACTIVE",
        },
        S.GPT_OUTPUT_READY.value: {
            "active": ["gpt_output_capture"], "done_before": "gpt_output_capture", "flavor": "ACTIVE",
        },
        S.WAITING_FOR_APPROVAL.value: {
            "active": ["your_approval"], "done_before": "your_approval", "flavor": "WAITING",
        },
        S.KIRO_STARTING.value: {
            "active": ["kiro_executor"], "done_before": "kiro_executor", "flavor": "ACTIVE",
        },
        S.KIRO_RUNNING.value: {
            "active": ["kiro_executor", "kiro_monitor"], "done_before": "kiro_executor", "flavor": "ACTIVE",
        },
        S.KIRO_WAITING_PERMISSION.value: {
            "active": ["permission_guard"], "done_before": "permission_guard", "flavor": "WAITING",
        },
        S.KIRO_RESULT_READY.value: {
            "active": ["kiro_result_capture"], "done_before": "kiro_result_capture", "flavor": "ACTIVE",
        },
        S.GPT_REVIEWING.value: {
            "active": ["gpt_review"], "done_before": "gpt_review", "flavor": "ACTIVE",
        },
        S.COMPLETED.value: {"active": ["complete"], "done_before": "complete", "flavor": "SUCCESS", "all_done": True},
        S.PAUSED.value: {"active": [], "done_before": None, "overlay": "PAUSED"},
        S.FAILED.value: {"active": [], "done_before": None, "overlay": "FAILED"},
        S.CANCELLED.value: {"active": [], "done_before": None, "overlay": "CANCELLED"},
    }
    spec = mapping.get(state, {"active": [], "done_before": None})
    result = {bid: "PENDING" for bid in BLOCK_IDS}

    done_before = spec.get("done_before")
    if spec.get("all_done"):
        for bid in BLOCK_IDS:
            result[bid] = "SUCCESS"
        return result
    if done_before is not None:
        cutoff = _BLOCK_ORDER[done_before]
        for bid, idx in _BLOCK_ORDER.items():
            if idx < cutoff:
                result[bid] = "SUCCESS"
    flavor = spec.get("flavor", "ACTIVE")
    for bid in spec.get("active", []):
        result[bid] = flavor
    # Overlays (PAUSED/FAILED/CANCELLED) are reported per-workflow separately;
    # the frontend renders them as an overlay over the last known progress.
    return result


def state_block_mapping() -> dict[str, dict[str, str]]:
    """Full mapping of every WorkflowState -> block presentation states."""
    return {s.value: _presentation_for_state(s.value) for s in WorkflowState}


# ── Request models (request bodies only, per project convention) ────────────


class ContextUpdateRequest(BaseModel):
    fields: dict[str, Any]


class ReasonRequest(BaseModel):
    reason: Optional[str] = ""


class CreateWorkflowRequest(BaseModel):
    project_name: str = ""
    project_id: Optional[str] = None
    project_path: Optional[str] = None
    master_gpt_prompt: Optional[str] = None


class TransitionRequest(BaseModel):
    target: str
    message: Optional[str] = ""


class PermissionPolicyRequest(BaseModel):
    policy: dict[str, str]


class ChromeProfileSelectRequest(BaseModel):
    profile_key: str


class ChromeTestOpenRequest(BaseModel):
    # Optional: test a profile before saving it. Defaults to the saved one.
    profile_key: Optional[str] = None
    request_id: Optional[str] = None


class StartWorkflowRequest(BaseModel):
    # No body needed; present for future options.
    pass


class KiroHandoffRequest(BaseModel):
    # Optional: caller may pin the exact window + project; both default to the
    # current Project Opus window / configured project path.
    target_hwnd: Optional[int] = None
    project_path: Optional[str] = None


class KiroMismatchDecisionRequest(BaseModel):
    """USER's decision for a detected Kiro session-mismatch."""
    # One of: continue | new | pause | cancel.
    decision: str


# Chrome-profile realtime message types (published via the Part 2 event bus).
MSG_CHROME_PROFILES_REFRESHED = "chrome_profiles_refreshed"
MSG_CHROME_PROFILE_SELECTED = "chrome_profile_selected"
MSG_CHROME_PROFILE_OPENING = "chrome_profile_opening"
MSG_CHROME_PROFILE_FOCUSED = "chrome_profile_focused"
MSG_CHROME_PROFILE_VERIFIED = "chrome_profile_verified"
MSG_CHROME_PROFILE_FAILED = "chrome_profile_failed"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _workflow_payload(wf) -> dict:
    """Full, secret-free workflow payload including prompt/config fields the UI
    needs to display in the details drawer (prompts are intentionally shown)."""
    data = wf.public()
    # Prompt / config / output fields the UI must surface in the drawer.
    data.update(
        {
            "master_gpt_prompt": wf.master_gpt_prompt,
            "current_user_command": wf.current_user_command,
            "latest_gpt_output": wf.latest_gpt_output,
            "approved_gpt_prompt": wf.approved_gpt_prompt,
            "latest_kiro_result": wf.latest_kiro_result,
            "chatgpt_window_hwnd": wf.chatgpt_window_hwnd,
            "master_gpt_prompt_len": len(wf.master_gpt_prompt or ""),
            "latest_gpt_output_len": len(wf.latest_gpt_output or ""),
            "latest_kiro_result_len": len(wf.latest_kiro_result or ""),
            # Part 5: Telegram command provenance + GPT-output-ready markers so
            # the Telegram Command / GPT Architect / GPT Output Capture drawers
            # can reflect the follow-up command lifecycle (no response text).
            "current_command_meta": (wf.metadata or {}).get("current_command") or {},
            "latest_gpt_response": (wf.metadata or {}).get("latest_gpt_response") or {},
            # Part B: WAITING_FOR_COMMAND Telegram readiness notification status.
            "telegram_waiting_notification": (wf.metadata or {}).get("telegram_waiting_notification") or {},
            # Part 6: captured GPT output markers (hash/len/time/iteration) and
            # the approval record (status/token/message id/decision time) so the
            # GPT Output Capture + Your Approval drawers render the real lifecycle.
            "latest_gpt_output_hash": (wf.metadata or {}).get("latest_gpt_output_hash"),
            "latest_gpt_output_captured_at": (wf.metadata or {}).get("latest_gpt_output_captured_at"),
            "latest_gpt_output_iteration": (wf.metadata or {}).get("latest_gpt_output_iteration"),
            "approval": (wf.metadata or {}).get("approval") or {},
            "edited_gpt_prompt": (wf.metadata or {}).get("edited_gpt_prompt"),
            # Part 7: Kiro session/window/project identity + lock owner so the
            # Kiro Executor drawer renders the real submission lifecycle.
            "kiro": (wf.metadata or {}).get("kiro") or {},
            "kiro_last_error": (wf.metadata or {}).get("kiro_last_error") or {},
            "kiro_manual_handoff": (wf.metadata or {}).get("kiro_manual_handoff") or {},
            # Kiro application + session ownership (KIRO EXECUTOR card).
            "kiro_binding": (wf.metadata or {}).get("kiro_binding") or {},
            "kiro_session_mismatch": (wf.metadata or {}).get("kiro_session_mismatch") or {},
            "permission_policy": (wf.metadata or {}).get(
                PERMISSION_POLICY_KEY, dict(DEFAULT_PERMISSION_POLICY)
            ),
            "blocks": _presentation_for_state(wf.state),
        }
    )
    return data


def _publish(workflow_id: str, msg_type: str, wf, *, extra: Optional[dict] = None) -> None:
    """Broadcast a workflow message onto the bus (never raises)."""
    try:
        payload = {"type": msg_type, "workflow_id": workflow_id,
                   "workflow": _workflow_payload(wf)}
        if extra:
            payload.update(extra)
        workflow_event_bus.publish(workflow_id, payload)
    except Exception:  # realtime is best-effort; never break the API call
        logger.debug("Failed to publish %s for %s", msg_type, workflow_id, exc_info=True)


def _map_engine_error(exc: Exception) -> HTTPException:
    if isinstance(exc, WorkflowNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, TerminalWorkflowError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, InvalidTransition):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ConcurrencyError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, WorkflowError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


# ── Page ──────────────────────────────────────────────────────────────────


@page_router.get("/architecture")
def architecture_ui():
    if not ARCHITECTURE_HTML.exists():
        logger.error("architecture.html not found at %s", ARCHITECTURE_HTML)
        raise HTTPException(status_code=500, detail="Architecture UI file not found.")
    return FileResponse(str(ARCHITECTURE_HTML), media_type="text/html")


# ── REST: read ──────────────────────────────────────────────────────────────


@router.get("")
def list_workflows():
    """List all workflows (safe public view). Active + terminal, newest first."""
    workflows = [_workflow_payload(w) for w in workflow_engine.list_workflows()]
    active = [w for w in workflows if not is_terminal(w["state"])]
    return {"status": "ok", "workflows": workflows, "active_count": len(active)}


@router.post("")
def create_workflow(req: CreateWorkflowRequest):
    """Production workflow creation for the normal UI.

    Validates required fields, creates through ``WorkflowEngine`` only (no
    direct SQLite), persists an optional master requirement, and publishes a
    realtime ``workflow_created`` event. Distinct from the dev endpoint.
    """
    name = (req.project_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="project_name is required")
    wf = workflow_engine.create_workflow(
        project_name=name,
        project_id=req.project_id,
        project_path=(req.project_path or None),
    )
    # Optional initial master requirement, set via the engine (not a raw write).
    prompt = (req.master_gpt_prompt or "").strip()
    if prompt:
        try:
            wf = workflow_engine.update_workflow_context(
                wf.workflow_id, master_gpt_prompt=prompt
            )
        except Exception as exc:
            raise _map_engine_error(exc) from exc
    _publish(wf.workflow_id, MSG_WORKFLOW_CREATED, wf)
    _publish_latest_event(wf.workflow_id)
    return {"status": "ok", "workflow": _workflow_payload(wf)}


@router.get("/state-mapping")
def get_state_mapping():
    """Expose the authoritative WorkflowState -> block presentation mapping."""
    return {
        "status": "ok",
        "blocks": BLOCK_IDS,
        "mapping": state_block_mapping(),
        "presentation_states": ["PENDING", "ACTIVE", "WAITING", "SUCCESS",
                                "FAILED", "PAUSED", "CANCELLED"],
    }


@router.get("/{workflow_id}")
def get_workflow(workflow_id: str):
    wf = workflow_engine.get_workflow(workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return {"status": "ok", "workflow": _workflow_payload(wf),
            "allowed_targets": sorted(t.value for t in allowed_targets(wf.state))}


@router.get("/{workflow_id}/events")
def get_events(workflow_id: str, limit: Optional[int] = None):
    wf = workflow_engine.get_workflow(workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    events = workflow_engine.get_events(workflow_id, limit=limit)
    return {"status": "ok", "events": [e.model_dump() for e in events]}


# ── REST: context update (via engine only) ──────────────────────────────────


@router.patch("/{workflow_id}/context")
def update_context(workflow_id: str, req: ContextUpdateRequest):
    """Update non-state context fields (e.g. master_gpt_prompt) via the engine.

    The engine rejects attempts to change state-machine columns, so this can
    never be used for arbitrary state mutation.
    """
    try:
        wf = workflow_engine.update_workflow_context(workflow_id, **req.fields)
    except Exception as exc:
        raise _map_engine_error(exc) from exc
    _publish(workflow_id, MSG_WORKFLOW_UPDATED, wf,
             extra={"fields": sorted(req.fields.keys())})
    _publish_latest_event(workflow_id)
    return {"status": "ok", "workflow": _workflow_payload(wf)}


# ── REST: lifecycle controls (via engine only) ─────────────────────────────


@router.post("/{workflow_id}/pause")
def pause_workflow(workflow_id: str, req: ReasonRequest):
    try:
        wf = workflow_engine.pause(workflow_id, reason=req.reason or "")
    except Exception as exc:
        raise _map_engine_error(exc) from exc
    _publish(workflow_id, MSG_WORKFLOW_PAUSED, wf)
    _publish_latest_event(workflow_id)
    return {"status": "ok", "workflow": _workflow_payload(wf)}


@router.post("/{workflow_id}/resume")
def resume_workflow(workflow_id: str):
    try:
        wf = workflow_engine.resume(workflow_id)
    except Exception as exc:
        raise _map_engine_error(exc) from exc
    _publish(workflow_id, MSG_WORKFLOW_RESUMED, wf)
    _publish_latest_event(workflow_id)
    return {"status": "ok", "workflow": _workflow_payload(wf)}


@router.post("/{workflow_id}/cancel")
def cancel_workflow(workflow_id: str, req: ReasonRequest):
    try:
        wf = workflow_engine.cancel(workflow_id, reason=req.reason or "")
    except Exception as exc:
        raise _map_engine_error(exc) from exc
    _publish(workflow_id, MSG_WORKFLOW_CANCELLED, wf)
    _publish_latest_event(workflow_id)
    return {"status": "ok", "workflow": _workflow_payload(wf)}


# ── REST: Permission Guard policy (safe metadata only) ──────────────────────


@router.get("/{workflow_id}/permission-policy")
def get_permission_policy(workflow_id: str):
    wf = workflow_engine.get_workflow(workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    policy = (wf.metadata or {}).get(PERMISSION_POLICY_KEY, dict(DEFAULT_PERMISSION_POLICY))
    return {"status": "ok", "policy": policy, "defaults": dict(DEFAULT_PERMISSION_POLICY),
            "decisions": sorted(_VALID_DECISIONS)}


@router.put("/{workflow_id}/permission-policy")
def set_permission_policy(workflow_id: str, req: PermissionPolicyRequest):
    """Persist the Permission Guard policy as safe workflow metadata.

    Never stores credentials. Only ALLOW/ASK/DENY decisions per known rule key.
    Stored via a dedicated engine metadata helper so Part 1's public API is
    used rather than a direct DB write.
    """
    wf = workflow_engine.get_workflow(workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    clean: dict[str, str] = {}
    for key, value in req.policy.items():
        decision = str(value).upper().strip()
        if decision not in _VALID_DECISIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid decision {value!r} for {key!r}; must be ALLOW/ASK/DENY",
            )
        # Guard against credential-ish rule keys ever being persisted with data.
        clean[str(key)] = decision
    try:
        wf = workflow_engine.set_metadata(workflow_id, PERMISSION_POLICY_KEY, clean)
    except Exception as exc:
        raise _map_engine_error(exc) from exc
    _publish(workflow_id, MSG_WORKFLOW_UPDATED, wf, extra={"permission_policy": clean})
    return {"status": "ok", "policy": clean}


# ── Development-only helpers (explicitly dev/verification) ───────────────────
# These exist so Part 2 can be visually verified without Chrome/ChatGPT/Kiro.
# They still go through WorkflowEngine and never write SQLite directly.


@router.post("/dev/create")
def dev_create_workflow(req: CreateWorkflowRequest):
    """DEV ONLY: create a workflow for verification/testing."""
    wf = workflow_engine.create_workflow(
        project_name=req.project_name or "Untitled",
        project_id=req.project_id,
        project_path=req.project_path,
    )
    _publish(wf.workflow_id, MSG_WORKFLOW_CREATED, wf)
    return {"status": "ok", "workflow": _workflow_payload(wf)}


@router.post("/{workflow_id}/dev/transition")
def dev_transition(workflow_id: str, req: TransitionRequest):
    """DEV ONLY: drive a state transition through the engine for verification.

    This is explicitly a development/verification control. It performs NO side
    effects — it only advances the persistent state machine so the UI can be
    exercised end to end. Real transitions in production will be driven by
    later Parts, not by this endpoint.
    """
    try:
        wf = workflow_engine.transition(workflow_id, req.target, message=req.message or "")
    except Exception as exc:
        raise _map_engine_error(exc) from exc
    msg_type = MSG_STATE_TRANSITION
    if is_terminal(wf.state):
        msg_type = (
            MSG_WORKFLOW_COMPLETED
            if wf.state == WorkflowState.COMPLETED.value
            else MSG_WORKFLOW_CANCELLED
        )
    elif wf.state == WorkflowState.FAILED.value:
        msg_type = MSG_WORKFLOW_FAILED
    _publish(workflow_id, msg_type, wf)
    _publish_latest_event(workflow_id)
    return {"status": "ok", "workflow": _workflow_payload(wf),
            "allowed_targets": sorted(t.value for t in allowed_targets(wf.state))}


@router.delete("/{workflow_id}")
def dev_delete_workflow(workflow_id: str):
    """DEV ONLY: delete a verification workflow so it does not linger.

    Uses the store's delete via the engine so no direct SQLite access happens
    from the route layer.
    """
    ok = workflow_engine.delete_workflow(workflow_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return {"status": "ok", "deleted": workflow_id}


def _publish_latest_event(workflow_id: str) -> None:
    """Publish the most recent event as an event_added message (best-effort)."""
    try:
        events = workflow_engine.get_events(workflow_id, limit=None)
        if events:
            workflow_event_bus.publish(
                workflow_id,
                {"type": MSG_EVENT_ADDED, "workflow_id": workflow_id,
                 "event": events[-1].model_dump()},
            )
    except Exception:
        logger.debug("Failed to publish latest event for %s", workflow_id, exc_info=True)


# ── Chrome Profile (Part 3) ──────────────────────────────────────────────────
# Routes delegate ALL OS logic to ChromeProfileController and ALL persistence to
# WorkflowEngine. Selecting/testing a profile is configuration + verification;
# it never advances the workflow state machine (that is Part 4).


def _publish_chrome(workflow_id: str, msg_type: str, payload: dict) -> None:
    try:
        workflow_event_bus.publish(
            workflow_id,
            {"type": msg_type, "workflow_id": workflow_id, "chrome": payload},
        )
    except Exception:
        logger.debug("Failed to publish %s for %s", msg_type, workflow_id, exc_info=True)


@page_router.get("/api/chrome/profiles")
def chrome_profiles():
    """List the real Chrome profiles discovered on this machine."""
    from jarvis_core.chrome_profile_controller import chrome_profile_controller

    profiles = chrome_profile_controller.discover_profiles()
    try:
        exe = chrome_profile_controller.resolve_chrome_executable()
        exe_found = True
    except Exception:
        exe, exe_found = None, False
    return {"status": "ok", "profiles": profiles,
            "chrome_executable_found": exe_found}


@router.post("/{workflow_id}/chrome-profile")
def chrome_profile_select(workflow_id: str, req: ChromeProfileSelectRequest):
    """Persist the selected Chrome profile onto the workflow (via the engine)."""
    from jarvis_core.chrome_profile_controller import chrome_profile_controller

    wf = workflow_engine.get_workflow(workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    profile = chrome_profile_controller.get_profile(req.profile_key)
    if profile is None:
        raise HTTPException(status_code=400, detail=f"Unknown profile {req.profile_key!r}")
    try:
        wf = workflow_engine.update_workflow_context(
            workflow_id,
            selected_chrome_profile_key=profile["profile_key"],
            selected_chrome_account_email=profile.get("account_email"),
        )
    except Exception as exc:
        raise _map_engine_error(exc) from exc
    _publish(workflow_id, MSG_WORKFLOW_UPDATED, wf,
             extra={"fields": ["selected_chrome_profile_key",
                               "selected_chrome_account_email"]})
    _publish_chrome(workflow_id, MSG_CHROME_PROFILE_SELECTED, {
        "profile_key": profile["profile_key"],
        "account_email": profile.get("account_email"),
        "display_name": profile.get("display_name"),
    })
    _publish_latest_event(workflow_id)
    return {"status": "ok", "workflow": _workflow_payload(wf), "profile": profile}


@router.post("/{workflow_id}/chrome-profile/test-open")
def chrome_profile_test_open(workflow_id: str, req: ChromeTestOpenRequest):
    """Focus-or-launch the selected (or given) Chrome profile and VERIFY it.

    This is the first real side effect in the new workflow, so it is wrapped in
    Part 1 action idempotency: a replayed request with the same request_id does
    NOT launch again. It never advances the workflow state machine.
    """
    from jarvis_core.chrome_profile_controller import (
        ProfileNotFound,
        ProfileVerificationError,
        ChromeExecutableNotFound,
        chrome_profile_controller,
    )

    wf = workflow_engine.get_workflow(workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    profile_key = req.profile_key or wf.selected_chrome_profile_key
    if not profile_key:
        raise HTTPException(status_code=400, detail="No Chrome profile selected")
    if chrome_profile_controller.get_profile(profile_key) is None:
        raise HTTPException(status_code=400, detail=f"Unknown profile {profile_key!r}")

    # Idempotent action id: stable per (workflow, profile, request).
    request_id = req.request_id or uuid.uuid4().hex
    action_id = f"chrome-test-open:{workflow_id}:{profile_key}:{request_id}"
    started, action = workflow_engine.begin_action(
        workflow_id, action_id, action_type="chrome_test_open",
        metadata={"profile_key": profile_key},
    )
    if not started:
        # Duplicate replay — do NOT launch again.
        return {"status": "ok", "duplicate": True,
                "action_status": action.status,
                "result_ref": action.result_ref}

    _publish_chrome(workflow_id, MSG_CHROME_PROFILE_OPENING, {"profile_key": profile_key})
    try:
        result = chrome_profile_controller.open_or_focus(profile_key)
    except (ProfileNotFound, ChromeExecutableNotFound, ProfileVerificationError) as exc:
        workflow_engine.fail_action(workflow_id, action_id, error=str(exc),
                                    error_code=type(exc).__name__)
        _publish_chrome(workflow_id, MSG_CHROME_PROFILE_FAILED,
                        {"profile_key": profile_key, "error": str(exc)})
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # unexpected
        workflow_engine.fail_action(workflow_id, action_id, error=str(exc),
                                    error_code="ChromeTestOpenError")
        _publish_chrome(workflow_id, MSG_CHROME_PROFILE_FAILED,
                        {"profile_key": profile_key, "error": str(exc)})
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Verified success (open_or_focus raises on mismatch, so result is verified
    # for launch; for focus we double-check the verified flag).
    if not result.get("verified"):
        workflow_engine.fail_action(
            workflow_id, action_id,
            error=f"Profile verification failed (observed {result.get('observed')})",
            error_code="ProfileVerificationError",
        )
        _publish_chrome(workflow_id, MSG_CHROME_PROFILE_FAILED,
                        {"profile_key": profile_key, "observed": result.get("observed")})
        raise HTTPException(status_code=409, detail="Profile verification failed")

    workflow_engine.complete_action(
        workflow_id, action_id,
        result_ref=f"hwnd={result.get('hwnd')};verified={result.get('verified')}",
        metadata={"action": result.get("action"), "hwnd": result.get("hwnd")},
    )
    _publish_chrome(workflow_id, MSG_CHROME_PROFILE_FOCUSED,
                    {"profile_key": profile_key, "hwnd": result.get("hwnd"),
                     "action": result.get("action"), "focus_failed": result.get("focus_failed")})
    _publish_chrome(workflow_id, MSG_CHROME_PROFILE_VERIFIED,
                    {"profile_key": profile_key, "observed": result.get("observed"),
                     "verified": result.get("verified")})
    return {"status": "ok", "result": result, "action_id": action_id}


@router.get("/{workflow_id}/chrome-profile/status")
def chrome_profile_status(workflow_id: str):
    """Report the selected profile, open/closed state, windows and verification."""
    from jarvis_core.chrome_profile_controller import chrome_profile_controller

    wf = workflow_engine.get_workflow(workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    key = wf.selected_chrome_profile_key
    if not key:
        return {"status": "ok", "selected_profile_key": None,
                "selected_account_email": None, "currently_open": False,
                "window_count": 0, "verified": False, "last_error": None}
    profile = chrome_profile_controller.get_profile(key)
    windows = chrome_profile_controller.get_profile_windows(key) if profile else []
    # Report the most recent chrome_test_open action's outcome, if any.
    last_error = None
    verified = False
    for act in reversed(workflow_engine.list_actions(workflow_id)):
        if act.action_type == "chrome_test_open":
            if act.status == "FAILED":
                last_error = act.error
            elif act.status == "COMPLETED":
                verified = "verified=True" in (act.result_ref or "")
            break
    return {
        "status": "ok",
        "selected_profile_key": key,
        "selected_account_email": wf.selected_chrome_account_email,
        "profile_exists": profile is not None,
        "currently_open": bool(windows),
        "window_count": len(windows),
        "verified": verified,
        "last_error": last_error,
    }


# ── ChatGPT bootstrap (Part 4) ───────────────────────────────────────────────
# Routes delegate ALL browser automation to WorkflowBootstrapService /
# ChatGPTBrowserController and ALL persistence/transitions to WorkflowEngine.


@router.post("/{workflow_id}/start")
def start_workflow(workflow_id: str, req: StartWorkflowRequest):
    """Start the Autonomous Workflow: validate + schedule the ChatGPT bootstrap.

    Returns immediately with the current workflow snapshot; the long-running
    browser work streams progress over the WebSocket. Does not block on
    ChatGPT generation.
    """
    from jarvis_core.workflow_bootstrap_service import (
        BootstrapError,
        workflow_bootstrap_service,
    )

    if workflow_engine.get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    try:
        workflow_bootstrap_service.start(workflow_id)
    except BootstrapError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    wf = workflow_engine.get_workflow(workflow_id)
    return {"status": "ok", "scheduled": True, "workflow": _workflow_payload(wf)}


@router.get("/{workflow_id}/chatgpt/status")
def chatgpt_status(workflow_id: str):
    """Report the ChatGPT conversation bootstrap status for a workflow."""
    from jarvis_core.workflow_bootstrap_service import (
        BootstrapError,
        workflow_bootstrap_service,
    )

    try:
        return {"status": "ok", **workflow_bootstrap_service.get_status(workflow_id)}
    except BootstrapError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{workflow_id}/chatgpt/open-conversation")
def chatgpt_open_conversation(workflow_id: str):
    """Focus the selected profile and open the SAVED conversation (no new chat)."""
    from jarvis_core.chatgpt_browser_controller import ChatGPTError
    from jarvis_core.workflow_bootstrap_service import (
        BootstrapError,
        workflow_bootstrap_service,
    )

    if workflow_engine.get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    try:
        result = workflow_bootstrap_service.open_conversation(workflow_id)
    except BootstrapError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ChatGPTError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _publish_chrome(workflow_id, "chatgpt_conversation_opened",
                    {"same": result.get("same"), "hwnd": result.get("hwnd")})
    return {"status": "ok", "result": result}


# ── Part 7: Kiro (approved prompt → exact project session) ───────────────────
# Routes DELEGATE to WorkflowKiroService; no desktop automation lives here.


@router.post("/{workflow_id}/kiro/start")
def kiro_start(workflow_id: str):
    """Consume the Part 6 approval and submit the approved prompt into Kiro."""
    from jarvis_core.workflow_kiro_service import (
        KiroServiceError, workflow_kiro_service,
    )
    if workflow_engine.get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    try:
        result = workflow_kiro_service.start(workflow_id)
    except KiroServiceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "ok", "result": result}


@router.get("/{workflow_id}/kiro/status")
def kiro_status(workflow_id: str):
    from jarvis_core.workflow_kiro_service import (
        KiroServiceError, workflow_kiro_service,
    )
    try:
        return {"status": "ok", "kiro": workflow_kiro_service.get_status(workflow_id)}
    except KiroServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{workflow_id}/kiro/focus")
def kiro_focus(workflow_id: str):
    from jarvis_core.workflow_kiro_service import (
        KiroServiceError, workflow_kiro_service,
    )
    if workflow_engine.get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    try:
        return {"status": "ok", "result": workflow_kiro_service.focus(workflow_id)}
    except KiroServiceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{workflow_id}/kiro/verify-project")
def kiro_verify_project(workflow_id: str):
    from jarvis_core.workflow_kiro_service import (
        KiroServiceError, workflow_kiro_service,
    )
    if workflow_engine.get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    try:
        return {"status": "ok", "result": workflow_kiro_service.verify_project(workflow_id)}
    except KiroServiceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{workflow_id}/kiro/grant-handoff")
def kiro_grant_handoff(workflow_id: str, req: Optional[KiroHandoffRequest] = None):
    """Record the USER's explicit exclusive-control handoff for the existing
    Project Opus Kiro window (single-use). Delegates to the service."""
    from jarvis_core.workflow_kiro_service import (
        KiroServiceError, workflow_kiro_service,
    )
    if workflow_engine.get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    target_hwnd = req.target_hwnd if req is not None else None
    project_path = req.project_path if req is not None else None
    try:
        record = workflow_kiro_service.grant_handoff(
            workflow_id, target_hwnd=target_hwnd, project_path=project_path)
    except KiroServiceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _publish(workflow_id, "kiro_manual_handoff_granted",
             workflow_engine.get_workflow(workflow_id))
    return {"status": "ok", "handoff": record}


@router.get("/{workflow_id}/kiro/session")
def kiro_session(workflow_id: str):
    """Report the Jarvis-owned Kiro session binding + live ownership match
    (for the KIRO EXECUTOR card). Read-only; never types/clicks."""
    from jarvis_core.jarvis_kiro_session import JarvisKiroSessionManager
    if workflow_engine.get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    mgr = JarvisKiroSessionManager()
    binding = mgr.get_binding(workflow_id)
    try:
        match = mgr.active_session_matches(workflow_id) if binding else {"match": False, "reason": "no_binding"}
    except Exception as exc:  # never let a desktop read break the card
        match = {"match": False, "reason": f"error:{exc}"}
    return {"status": "ok", "binding": binding or {},
            "mismatch": mgr.get_mismatch(workflow_id) or {}, "ownership": match}


@router.post("/{workflow_id}/kiro/session-decision")
def kiro_session_decision(workflow_id: str, req: KiroMismatchDecisionRequest):
    """Apply a session-mismatch decision (continue/new/pause/cancel) from the
    Jarvis portal (parity with the Telegram buttons)."""
    from jarvis_core.workflow_kiro_service import (
        KiroServiceError, workflow_kiro_service,
    )
    if workflow_engine.get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    try:
        result = workflow_kiro_service.resume_after_confirmation(
            workflow_id, req.decision)
    except KiroServiceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "ok", "result": result}


# ── WebSocket ────────────────────────────────────────────────────────────────


@ws_router.websocket("/ws/workflows/{workflow_id}")
async def workflow_ws(websocket: WebSocket, workflow_id: str):
    """Realtime channel for one workflow (or "*" for all workflows).

    On connect the client receives an initial snapshot, then incremental
    messages. A disconnected/slow client can never break the engine: the bus
    drops messages for full/closed subscribers, and cleanup is guaranteed.
    """
    await websocket.accept()
    sub = workflow_event_bus.subscribe(workflow_id)
    try:
        # Initial snapshot (skip for the wildcard channel).
        if workflow_id != "*":
            wf = workflow_engine.get_workflow(workflow_id)
            if wf is not None:
                await websocket.send_json(
                    {"type": "snapshot", "workflow_id": workflow_id,
                     "workflow": _workflow_payload(wf),
                     "events": [e.model_dump() for e in
                                workflow_engine.get_events(workflow_id)]}
                )
            else:
                await websocket.send_json(
                    {"type": "error", "workflow_id": workflow_id,
                     "detail": "Workflow not found"}
                )

        # Pump messages from the bus queue to the client. Also drain any inbound
        # client frames (e.g. ping) so disconnects are detected promptly.
        async def _reader():
            try:
                while True:
                    await websocket.receive_text()
            except Exception:
                return

        reader_task = asyncio.create_task(_reader())
        try:
            while True:
                message = await sub.queue.get()
                await websocket.send_json(message)
        finally:
            reader_task.cancel()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("WebSocket error for %s", workflow_id, exc_info=True)
    finally:
        workflow_event_bus.unsubscribe(workflow_id, sub)
        try:
            await websocket.close()
        except Exception:
            pass
