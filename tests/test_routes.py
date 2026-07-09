"""Tests 2-8: FastAPI route behaviors."""
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Import the app (must be importable; all deps already in jarvis_core)
from app import app

client = TestClient(app)


# Test 2: App is a FastAPI instance with correct routes
def test_app_is_fastapi_instance():
    from fastapi import FastAPI
    assert isinstance(app, FastAPI)

    # Collect all registered route paths, walking into included routers.
    # FastAPI stores included routers as _IncludedRouter objects whose
    # actual routes live on .original_router.routes.
    def _collect_paths(routes):
        paths = set()
        for route in routes:
            if hasattr(route, "path"):
                paths.add(route.path)
            # Walk into _IncludedRouter objects
            orig = getattr(route, "original_router", None)
            if orig is not None and hasattr(orig, "routes"):
                paths |= _collect_paths(orig.routes)
        return paths

    paths = _collect_paths(app.routes)
    for expected in ["/", "/health", "/ui", "/chat", "/agent", "/command", "/confirm-action"]:
        assert expected in paths, f"Route {expected} not found in {paths}"


# Test 3: GET / returns correct structure
def test_root_returns_running():
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "running"
    assert data["default_model"] == "gemma2:2b"
    assert data["heavy_model"] == "qwen2.5:7b"


# Test 4: GET /health with mocked Ollama running
def test_health_ollama_running():
    mock_response = MagicMock()
    mock_response.json.return_value = {"models": [{"name": "gemma2:2b"}]}
    mock_response.raise_for_status = MagicMock()
    with patch("jarvis_core.routes.requests.get", return_value=mock_response):
        with patch("jarvis_core.routes.get_system_status", return_value={}):
            response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["jarvis"] == "running"
    assert data["ollama"] == "running"


# Test 5: GET /health with mocked Ollama unavailable
def test_health_ollama_unavailable():
    with patch("jarvis_core.routes.requests.get", side_effect=Exception("connection refused")):
        with patch("jarvis_core.routes.get_system_status", return_value={}):
            response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["jarvis"] == "running"
    assert data["ollama"] == "not reachable"


# Test 6: GET /ui returns 200 HTML
def test_ui_returns_html():
    response = client.get("/ui")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


# Test 7: POST /agent with mocked Ollama returns reply
def test_agent_returns_reply():
    # "Who are you" goes to AI fallback
    with patch("jarvis_core.command_dispatcher.ask_ollama",
               return_value=("I am Jarvis.", "gemma2:2b", None)):
        response = client.post("/agent", json={"command": "Who are you?", "mode": "light"})
    assert response.status_code == 200
    data = response.json()
    assert "reply" in data


# Test 8: POST /command returns same shape as POST /agent
def test_command_same_as_agent():
    with patch("jarvis_core.command_dispatcher.ask_ollama",
               return_value=("I am Jarvis.", "gemma2:2b", None)):
        r_agent = client.post("/agent", json={"command": "Who are you?", "mode": "light"})
        r_command = client.post("/command", json={"command": "Who are you?", "mode": "light"})
    assert r_agent.status_code == 200
    assert r_command.status_code == 200
    assert r_agent.json().keys() == r_command.json().keys()
