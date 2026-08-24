"""Shared conversational Jarvis flow used by HTTP and remote integrations."""
from __future__ import annotations

from jarvis_core.model_router import router as model_router
from jarvis_core.ollama_service import ask_ollama


def run_conversation(message: str, mode: str = "auto") -> dict:
    content, model, error = ask_ollama(message, mode)
    if error:
        return error
    return {
        "status": "done",
        "model": model,
        "reply": content,
        "summary": content,
        "routing": model_router.get_last(),
    }
