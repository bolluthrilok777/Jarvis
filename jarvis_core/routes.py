import logging

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from jarvis_core.config import (
    APP_NAME, OLLAMA_URL, LIGHT_MODEL, HEAVY_MODEL, UI_FILE,
)
from jarvis_core.models import ChatRequest, CommandRequest, ConfirmRequest
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
