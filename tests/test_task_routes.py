"""
tests/test_task_routes.py — Phase 5A-1 Task Route Tests

Tests for the task API endpoints using FastAPI TestClient.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app import app

client = TestClient(app)


# ── POST /tasks/submit ────────────────────────────────────────────────────────

def test_submit_single_line():
    """Submit a single command line."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        with patch("jarvis_core.orchestrator.classify_risk", return_value="SAFE"):
            with patch("jarvis_core.orchestrator.classify_concurrency", return_value="background"):
                with patch("jarvis_core.orchestrator.build_interpreted_command", return_value="system status"):
                    response = client.post("/tasks/submit", json={"text": "system status"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["count"] == 1
    assert data["results"][0]["action"] == "created"


def test_submit_multiple_lines():
    """Multiple lines create separate tasks."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        with patch("jarvis_core.orchestrator.classify_risk", return_value="SAFE"):
            with patch("jarvis_core.orchestrator.classify_concurrency", return_value="background"):
                with patch("jarvis_core.orchestrator.build_interpreted_command", side_effect=lambda x: x):
                    response = client.post("/tasks/submit", json={"text": "system status\nopen calculator"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    assert data["results"][0]["task_id"] != data["results"][1]["task_id"]


def test_submit_blank_lines_ignored():
    """Blank lines are ignored."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        with patch("jarvis_core.orchestrator.classify_risk", return_value="SAFE"):
            with patch("jarvis_core.orchestrator.classify_concurrency", return_value="background"):
                with patch("jarvis_core.orchestrator.build_interpreted_command", side_effect=lambda x: x):
                    response = client.post("/tasks/submit", json={"text": "system status\n\n\nopen calculator"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2


def test_submit_all_blank():
    """All blank lines returns error."""
    response = client.post("/tasks/submit", json={"text": "\n\n  \n"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "error"


def test_submit_empty_text():
    """Empty text returns error."""
    response = client.post("/tasks/submit", json={"text": ""})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "error"


# ── GET /tasks ────────────────────────────────────────────────────────────────

def test_list_tasks_empty():
    """GET /tasks returns empty list when no tasks."""
    response = client.get("/tasks")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert isinstance(data["tasks"], list)


def test_list_tasks_after_submit():
    """GET /tasks returns submitted tasks."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        with patch("jarvis_core.orchestrator.classify_risk", return_value="SAFE"):
            with patch("jarvis_core.orchestrator.classify_concurrency", return_value="background"):
                with patch("jarvis_core.orchestrator.build_interpreted_command", return_value="system status"):
                    client.post("/tasks/submit", json={"text": "system status"})
    response = client.get("/tasks")
    assert response.status_code == 200
    data = response.json()
    assert len(data["tasks"]) >= 1


# ── GET /tasks/{task_id} ──────────────────────────────────────────────────────

def test_get_task_by_id():
    """GET /tasks/{task_id} returns the task."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        with patch("jarvis_core.orchestrator.classify_risk", return_value="SAFE"):
            with patch("jarvis_core.orchestrator.classify_concurrency", return_value="background"):
                with patch("jarvis_core.orchestrator.build_interpreted_command", return_value="system status"):
                    submit_resp = client.post("/tasks/submit", json={"text": "system status"})
    task_id = submit_resp.json()["results"][0]["task_id"]
    response = client.get(f"/tasks/{task_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["task"]["task_id"] == task_id


def test_get_task_not_found():
    """GET /tasks/{task_id} returns 404 for nonexistent task."""
    response = client.get("/tasks/nonexistent-id")
    assert response.status_code == 404


# ── POST /tasks/{task_id}/cancel ──────────────────────────────────────────────

def test_cancel_task():
    """POST /tasks/{task_id}/cancel cancels a queued task."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        with patch("jarvis_core.orchestrator.classify_risk", return_value="SAFE"):
            with patch("jarvis_core.orchestrator.classify_concurrency", return_value="background"):
                with patch("jarvis_core.orchestrator.build_interpreted_command", return_value="system status"):
                    submit_resp = client.post("/tasks/submit", json={"text": "system status"})
    task_id = submit_resp.json()["results"][0]["task_id"]
    response = client.post(f"/tasks/{task_id}/cancel")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "cancelled"


def test_cancel_nonexistent():
    """POST /tasks/{task_id}/cancel returns error for nonexistent task."""
    response = client.post("/tasks/nonexistent/cancel")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "error"


# ── POST /tasks/{task_id}/confirm ─────────────────────────────────────────────

def test_confirm_task():
    """POST /tasks/{task_id}/confirm confirms a waiting task."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        with patch("jarvis_core.orchestrator.classify_risk", return_value="PROTECTED"):
            with patch("jarvis_core.orchestrator.classify_concurrency", return_value="background"):
                with patch("jarvis_core.orchestrator.build_interpreted_command", return_value="delete file"):
                    submit_resp = client.post("/tasks/submit", json={"text": "delete file"})
    task_id = submit_resp.json()["results"][0]["task_id"]
    response = client.post(f"/tasks/{task_id}/confirm", json={"confirm": True})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "queued"


def test_reject_task():
    """POST /tasks/{task_id}/confirm with confirm=False cancels the task."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        with patch("jarvis_core.orchestrator.classify_risk", return_value="PROTECTED"):
            with patch("jarvis_core.orchestrator.classify_concurrency", return_value="background"):
                with patch("jarvis_core.orchestrator.build_interpreted_command", return_value="delete file"):
                    submit_resp = client.post("/tasks/submit", json={"text": "delete file"})
    task_id = submit_resp.json()["results"][0]["task_id"]
    response = client.post(f"/tasks/{task_id}/confirm", json={"confirm": False})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "cancelled"


# ── POST /tasks/clear-completed ───────────────────────────────────────────────

def test_clear_completed():
    """POST /tasks/clear-completed hides completed tasks."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        with patch("jarvis_core.orchestrator.classify_risk", return_value="SAFE"):
            with patch("jarvis_core.orchestrator.classify_concurrency", return_value="background"):
                with patch("jarvis_core.orchestrator.build_interpreted_command", return_value="system status"):
                    submit_resp = client.post("/tasks/submit", json={"text": "system status"})
    task_id = submit_resp.json()["results"][0]["task_id"]
    # Mark as completed via task_manager directly
    from jarvis_core.task_manager import task_manager
    task_manager.set_status(task_id, "completed")
    response = client.post("/tasks/clear-completed")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["hidden"] >= 1


# ── Duplicate Submission ──────────────────────────────────────────────────────

def test_duplicate_submission():
    """Submitting the same text twice creates two tasks."""
    with patch("jarvis_core.orchestrator.classify", return_value="command"):
        with patch("jarvis_core.orchestrator.classify_risk", return_value="SAFE"):
            with patch("jarvis_core.orchestrator.classify_concurrency", return_value="background"):
                with patch("jarvis_core.orchestrator.build_interpreted_command", return_value="system status"):
                    r1 = client.post("/tasks/submit", json={"text": "system status"})
                    r2 = client.post("/tasks/submit", json={"text": "system status"})
    assert r1.json()["results"][0]["task_id"] != r2.json()["results"][0]["task_id"]