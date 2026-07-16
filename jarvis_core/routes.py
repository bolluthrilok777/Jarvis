import logging

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from jarvis_core.config import (
    APP_NAME, OLLAMA_URL, LIGHT_MODEL, HEAVY_MODEL, UI_FILE,
)
from jarvis_core.models import (
    ActivityEntryRequest, ChatRequest, CommandRequest, ConfirmRequest,
    MonitoringRequest, TaskSubmitRequest, TaskConfirmRequest,
)
from jarvis_core.ollama_service import ask_ollama
from jarvis_core.system_tools import get_system_status
from jarvis_core.command_dispatcher import run_agent_command, confirm_action

logger = logging.getLogger("jarvis.routes")

router = APIRouter()


@router.get("/")
def root():
    return {
        "name": APP_NAME,
        "status": "running",
        "ui": "http://127.0.0.1:8000/ui",
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
            "system": get_system_status(),
        }
    except Exception as e:
        return {
            "jarvis": "running",
            "ollama": "not reachable",
            "error": str(e),
            "system": get_system_status(),
        }


@router.get("/ui")
def jarvis_ui():
    if not UI_FILE.exists():
        logger.error("ui.html not found at %s", UI_FILE)
        raise HTTPException(status_code=500, detail="UI file not found.")
    return FileResponse(str(UI_FILE), media_type="text/html")


@router.post("/chat")
def chat(req: ChatRequest):
    content, model, error = ask_ollama(req.message, req.mode)
    if error:
        return error
    return {"model": model, "reply": content}


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


# ── Phase 5A-1 Task API ───────────────────────────────────────────────────────


@router.post("/tasks/submit")
def tasks_submit(req: TaskSubmitRequest):
    """Submit one or more command lines. Multi-line text creates separate tasks."""
    from jarvis_core.orchestrator import orchestrator

    lines = req.text.split("\n")
    non_empty = [l.strip() for l in lines if l.strip()]
    if not non_empty:
        return {"status": "error", "message": "No non-empty lines to submit."}

    results = orchestrator.submit_multiple(non_empty)
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
