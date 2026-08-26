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
        # Relative so the same metadata is correct behind a reverse proxy,
        # on localhost, or on a cloud VM.
        "ui": "/ui",
        "default_model": LIGHT_MODEL,
        "heavy_model": HEAVY_MODEL,
    }


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

    results = orchestrator.submit_multiple(non_empty, conversation_id="web")
    return {"status": "ok", "count": len(results), "results": results}


@router.get("/tasks")
def tasks_list():
    """List all visible tasks."""
    from jarvis_core.task_manager import task_manager

    return {"status": "ok", "tasks": task_manager.get_public_tasks()}


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
