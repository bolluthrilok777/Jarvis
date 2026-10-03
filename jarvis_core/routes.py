import logging

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional

from jarvis_core.config import (
    APP_NAME, OLLAMA_URL, LIGHT_MODEL, HEAVY_MODEL, OLLAMA_ROBOT_IMAGE,
    UI_FILE, AVAILABLE_OLLAMA_MODELS, FREE_API_KEY_ENABLED,
)
from jarvis_core.models import (
    ActivityEntryRequest, ChatRequest, CommandRequest, ConfirmRequest,
    MonitoringRequest, TaskSubmitRequest, TaskConfirmRequest,
)
from jarvis_core.ollama_service import (
    get_active_mode, set_active_mode, set_api_key,
    get_api_key, get_installed_ollama_models,
)
from jarvis_core.model_router import MODEL_PROFILES, router as model_router
from jarvis_core.system_tools import get_system_status
from jarvis_core.command_dispatcher import run_agent_command, confirm_action

logger = logging.getLogger("jarvis.routes")

router = APIRouter()


# ── Model Management Models ──────────────────────────────────────────────────


class SetModeRequest(BaseModel):
    mode: str


class SetApiKeyRequest(BaseModel):
    api_key: str


# ── Existing Routes ──────────────────────────────────────────────────────────


@router.get("/")
def root():
    return {
        "name": APP_NAME,
        "status": "running",
        "ui": "http://127.0.0.1:8000/ui",
        "default_model": LIGHT_MODEL,
        "heavy_model": HEAVY_MODEL,
    }


@router.get("/browser-bind/{token}")
def browser_bind(token: str):
    """Harmless marker page for the dedicated-window launch-provenance binding.

    Jarvis launches a dedicated Chrome window at
    ``/browser-bind/<launch_token>`` and then proves that the resulting new
    window/tab is the one it created by matching the unique ``token`` in this
    URL. The page contains NO secrets and NO workflow data — only the token,
    which is itself an unguessable random value whose sole purpose is exact
    window/tab correlation. Requires no user interaction.
    """
    from fastapi.responses import HTMLResponse
    import html as _html
    safe = _html.escape(str(token))[:80]
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Jarvis Browser Binding</title></head>"
        "<body style='font-family:sans-serif'>"
        "<h3>Jarvis Browser Binding</h3>"
        f"<p data-jarvis-bind-token='{safe}'>token: {safe}</p>"
        "</body></html>"
    )
    return HTMLResponse(content=body)


@router.get("/api/runtime-info")
def runtime_info():
    """Read-only diagnostics of the LIVE Jarvis runtime (no secrets).

    Reports the exact interpreter serving this process, loaded module paths,
    dependency availability and whether the real UIA/Chrome tab backend works
    right now - used to prove Telegram actions run inside the project .venv.
    """
    import importlib
    import os
    import sys

    info: dict = {
        "pid": os.getpid(),
        "sys_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "cwd": os.getcwd(),
    }

    modules: dict[str, str] = {}
    for name in (
        "resource_manager", "browser_resource_manager",
        "uia_browser_manager", "screenshot_service",
        "windows_resource_manager",
    ):
        try:
            module = importlib.import_module(f"jarvis_core.{name}")
            modules[name] = str(getattr(module, "__file__", "") or "")
        except Exception as exc:  # surfaced, never swallowed silently
            modules[name] = f"IMPORT FAILED: {type(exc).__name__}: {exc}"
    info["jarvis_modules"] = modules

    deps: dict[str, str] = {}
    for dep in ("pywinauto", "comtypes", "psutil", "requests", "PIL"):
        try:
            dep_module = importlib.import_module(dep)
            deps[dep] = str(getattr(dep_module, "__version__", "") or "ok")
        except Exception as exc:
            deps[dep] = f"MISSING ({type(exc).__name__})"
    info["dependencies"] = deps

    # Live browser diagnostics (bounded by uia_browser_manager deadlines).
    chrome: dict = {}
    try:
        from jarvis_core.browser_resource_manager import discover_tabs

        scan = discover_tabs()
        chrome["chrome_detected"] = bool(scan.get("chrome_detected"))
        chrome["uia_usable"] = bool(scan.get("controllable"))
        chrome["control_mode"] = str(scan.get("control_mode") or "")
        chrome["tabs_discovered"] = len(scan.get("tabs") or [])
        chrome["tab_titles"] = [
            str(t.get("title") or "") for t in (scan.get("tabs") or [])[:30]
        ]
        chrome["fallback_failure"] = str(scan.get("reason") or "")
    except Exception as exc:
        chrome["uia_usable"] = False
        chrome["fallback_failure"] = f"{type(exc).__name__}: {exc}"
    info["chrome"] = chrome
    return info


@router.get("/health")
def health():
    try:
        response = requests.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        response.raise_for_status()
        models = response.json().get("models", [])
        return {
            "jarvis": "running",
            "ollama": "running",
            "installed_models": [m.get("name") for m in models],
            "default_model": LIGHT_MODEL,
            "heavy_model": HEAVY_MODEL,
            "active_mode": get_active_mode(),
            "router": model_router.get_last(),
            "system": get_system_status(),
        }
    except Exception as e:
        return {
            "jarvis": "running",
            "ollama": "not reachable",
            "error": str(e),
            "active_mode": get_active_mode(),
            "router": model_router.get_last(),
            "system": get_system_status(),
        }


@router.get("/ui")
def jarvis_ui():
    if not UI_FILE.exists():
        logger.error("ui.html not found at %s", UI_FILE)
        raise HTTPException(status_code=500, detail="UI file not found.")
    return FileResponse(str(UI_FILE), media_type="text/html")


@router.get("/assets/ollama-robot.png")
def ollama_robot_image():
    if not OLLAMA_ROBOT_IMAGE.exists():
        raise HTTPException(status_code=404, detail="Ollama robot image not found.")
    return FileResponse(str(OLLAMA_ROBOT_IMAGE), media_type="image/png")


@router.post("/chat")
def chat(req: ChatRequest):
    from jarvis_core.conversation_service import run_conversation

    result = run_conversation(req.message, req.mode)
    if result.get("status") != "done":
        return result
    return {
        "model": result["model"],
        "reply": result["reply"],
        "routing": result["routing"],
    }


@router.post("/agent")
def agent(req: CommandRequest):
    return run_agent_command(req)


@router.post("/command")
def command(req: CommandRequest):
    return run_agent_command(req)


@router.post("/confirm-action")
def confirm_action_route(req: ConfirmRequest):
    return confirm_action(req)


@router.post("/action")
def action(req: CommandRequest):
    from jarvis_core.action_engine import engine
    return engine.run(req)


# ── Model Management Routes ──────────────────────────────────────────────────


@router.get("/models")
def get_models():
    """Get all available models and current configuration."""
    installed = get_installed_ollama_models()
    active_mode = get_active_mode()
    has_api_key = bool(get_api_key())
    
    # Mark which models are installed
    available = []
    for m in AVAILABLE_OLLAMA_MODELS:
        available.append({
            **m,
            "installed": m["id"] in installed,
        })
    
    return {
        "status": "ok",
        "active_mode": active_mode,
        "available_models": available,
        "installed_models": installed,
        "api_key_configured": has_api_key,
        "api_key_enabled": FREE_API_KEY_ENABLED,
        "light_model": LIGHT_MODEL,
        "heavy_model": HEAVY_MODEL,
        "router": model_router.get_last(),
        "profiles": [
            {"id": key, **value}
            for key, value in MODEL_PROFILES.items()
        ],
    }


@router.post("/models/mode")
def set_mode(req: SetModeRequest):
    """Set the active model mode."""
    valid_modes = ["auto", "fast", "balanced", "coding", "quality", "deep", "api_key", "light", "medium", "heavy"]
    # Also allow any model ID from the available list
    valid_model_ids = [m["id"] for m in AVAILABLE_OLLAMA_MODELS]
    
    if req.mode not in valid_modes and req.mode not in valid_model_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode '{req.mode}'. Valid modes: {', '.join(valid_modes)}. Valid model IDs: {', '.join(valid_model_ids[:5])}..."
        )
    
    try:
        set_active_mode(req.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "active_mode": req.mode}


@router.post("/models/api-key")
def set_api_key_route(req: SetApiKeyRequest):
    """Set the API key for free API key mode."""
    if not req.api_key or not req.api_key.strip():
        raise HTTPException(status_code=400, detail="API key cannot be empty")
    set_api_key(req.api_key.strip())
    return {"status": "ok", "message": "API key configured successfully"}


@router.get("/models/api-key")
def get_api_key_status():
    """Check if API key is configured (without exposing the key)."""
    has_key = bool(get_api_key())
    return {
        "status": "ok",
        "configured": has_key,
        "enabled": FREE_API_KEY_ENABLED,
    }


# ── Phase 5A-1 Task API ───────────────────────────────────────────────────────


@router.post("/tasks/submit")
def tasks_submit(req: TaskSubmitRequest):
    """Submit one or more command lines. Multi-line text creates separate tasks."""
    from jarvis_core.orchestrator import orchestrator

    lines = req.text.split("\n")
    non_empty = [l.strip() for l in lines if l.strip()]
    if not non_empty:
        return {"status": "error", "message": "No non-empty lines to submit."}

    from jarvis_core.feature_orchestrator import feature_orchestrator

    results = []
    for line in non_empty:
        # Explicit intent prefixes bypass auto-detection/confirmation.
        lowered = line.lower()
        if lowered.startswith("feature:"):
            payload = line.split(":", 1)[1].strip()
            if payload:
                results.append(feature_orchestrator.submit_feature_request(payload, source="Web"))
            continue
        if lowered.startswith("normal:"):
            payload = line.split(":", 1)[1].strip()
            if payload:
                results.append(orchestrator.submit(payload, conversation_id="web"))
            continue
        if feature_orchestrator.is_feature_request(line):
            results.append(feature_orchestrator.submit_feature_request(line, source="Web"))
        else:
            results.append(orchestrator.submit(line, conversation_id="web"))
    return {"status": "ok", "count": len(results), "results": results}


@router.get("/tasks")
def tasks_list():
    """List all visible tasks.

    Feature-orchestrator tasks carry a structured ``feature_progress`` block
    (phases, steps, timings, cancel_allowed) computed from the real persisted
    FeatureRequest state — the UI never parses display strings.
    """
    from jarvis_core.task_manager import task_manager
    from jarvis_core.feature_progress import build_progress

    tasks = task_manager.get_public_tasks()
    for task in tasks:
        if task.get("task_type") == "feature":
            from jarvis_core.feature_orchestrator import feature_orchestrator

            feature = feature_orchestrator._store.by_task_id(task["task_id"])
            if feature is not None:
                task["feature_progress"] = build_progress(feature)
    return {"status": "ok", "tasks": tasks}


@router.get("/tasks/{task_id}")
def tasks_get(task_id: str):
    """Get a single task by ID."""
    from jarvis_core.task_manager import task_manager

    task = task_manager.get_public_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": "ok", "task": task}


@router.post("/tasks/{task_id}/cancel")
def tasks_cancel(task_id: str):
    """Cancel a task."""
    from jarvis_core.orchestrator import orchestrator

    result = orchestrator.cancel(task_id)
    return result


@router.post("/tasks/{task_id}/confirm")
def tasks_confirm(task_id: str, req: TaskConfirmRequest):
    """Confirm or reject a task waiting for confirmation."""
    from jarvis_core.orchestrator import orchestrator

    if req.confirm:
        result = orchestrator.confirm(task_id)
    else:
        result = orchestrator.cancel(task_id)
    return result


@router.post("/tasks/clear-completed")
def tasks_clear_completed():
    """Hide all completed/failed/cancelled tasks."""
    from jarvis_core.task_manager import task_manager

    count = task_manager.clear_completed()
    return {"status": "ok", "hidden": count}


# ── Autonomous Feature Orchestrator API ──────────────────────────────────────


class FeatureSubmitRequest(BaseModel):
    text: str


class FeatureReviewRequest(BaseModel):
    review: str


class FeatureDecisionRequest(BaseModel):
    decision: str  # "cancel" | "changes" | "skip_review"
    instructions: Optional[str] = None


@router.post("/features/submit")
def features_submit(req: FeatureSubmitRequest):
    """Submit a feature-development request from the Jarvis web command box."""
    from jarvis_core.feature_orchestrator import feature_orchestrator

    result = feature_orchestrator.submit_feature_request(req.text, source="Web")
    if result.get("status") == "not_a_feature":
        raise HTTPException(status_code=400, detail=result.get("message", "Not a feature request."))
    return result


@router.get("/features")
def features_list(status: Optional[str] = None):
    """List feature requests (safe public view)."""
    from jarvis_core.feature_orchestrator import feature_orchestrator

    return {"status": "ok", "features": feature_orchestrator.list_features(status)}


@router.get("/features/{feature_id}")
def features_get(feature_id: str):
    """Full feature detail including plan, Cline task and history."""
    from jarvis_core.feature_orchestrator import feature_orchestrator
    from jarvis_core.feature_progress import build_progress

    feature = feature_orchestrator.get_feature(feature_id)
    if feature is None:
        raise HTTPException(status_code=404, detail="Feature not found")
    raw = feature_orchestrator._store.get(feature_id)
    if raw is not None:
        feature["feature_progress"] = build_progress(raw)
    return {"status": "ok", "feature": feature}


@router.post("/features/{feature_id}/review")
def features_review(feature_id: str, req: FeatureReviewRequest):
    """Accept the user-pasted ChatGPT review for local analysis."""
    from jarvis_core.feature_orchestrator import feature_orchestrator

    return feature_orchestrator.submit_chatgpt_review(feature_id, req.review)


@router.post("/features/{feature_id}/skip-review")
def features_skip_review(feature_id: str):
    from jarvis_core.feature_orchestrator import feature_orchestrator

    return feature_orchestrator.skip_chatgpt(feature_id)


@router.post("/features/{feature_id}/decision")
def features_decision(feature_id: str, req: FeatureDecisionRequest):
    """Cancel or request changes from the Web UI.

    Approval and high-risk confirmation are intentionally NOT available here:
    the Telegram approval gate is mandatory (allowlisted users only).
    """
    from jarvis_core.feature_orchestrator import feature_orchestrator

    if req.decision == "cancel":
        return feature_orchestrator.cancel(feature_id)
    if req.decision == "changes":
        return feature_orchestrator.request_changes(feature_id, req.instructions or "")
    raise HTTPException(
        status_code=400,
        detail="Unsupported decision. Approvals must be granted via Telegram.",
    )


@router.get("/monitoring")
def monitoring_status():
    from jarvis_core.monitoring import monitoring
    return {"status": "ok", "monitoring": monitoring.snapshot()}


@router.post("/monitoring/start")
def monitoring_start(req: MonitoringRequest):
    from jarvis_core.monitoring import monitoring
    try:
        return {"status": "ok", "monitoring": monitoring.start(
            req.start, req.end, req.interval, req.sound
        )}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/monitoring/control/{action}")
def monitoring_control(action: str):
    from jarvis_core.monitoring import monitoring
    if action == "pause":
        state = monitoring.pause()
    elif action == "resume":
        state = monitoring.resume()
    elif action == "stop":
        state = monitoring.stop()
    elif action == "test":
        return monitoring.trigger()
    else:
        raise HTTPException(status_code=404, detail="Unknown monitoring action")
    return {"status": "ok", "monitoring": state}


@router.post("/monitoring/activity")
def monitoring_activity(req: ActivityEntryRequest):
    from jarvis_core.monitoring import append_activity
    if not req.entry.strip():
        raise HTTPException(status_code=400, detail="Activity entry cannot be blank")
    return {"status": "done", "file": str(append_activity(req.entry))}


@router.get("/apps")
def apps_list():
    from jarvis_core.app_adapters import public_adapters
    return {"status": "ok", "apps": public_adapters()}


@router.get("/api/system-status")
def api_system_status():
    """Return metrics only while the user has the live panel open."""
    from jarvis_core.system_tools import get_system_status, is_live_monitor_active
    if not is_live_monitor_active():
        raise HTTPException(status_code=409, detail="System monitor is not active")
    data = get_system_status(launch_task_manager=False)
    data["status"] = "ok"
    return data


@router.post("/api/system-monitor/start")
def api_system_monitor_start():
    """Enable on-demand live system metric collection for the open panel."""
    from jarvis_core.system_tools import start_live_monitor
    start_live_monitor()
    return {"status": "ok", "active": True}


@router.post("/api/system-monitor/stop")
def api_system_monitor_stop():
    """Disable live metric collection when the panel is closed."""
    from jarvis_core.system_tools import stop_live_monitor
    stop_live_monitor()
    return {"status": "ok", "active": False}
