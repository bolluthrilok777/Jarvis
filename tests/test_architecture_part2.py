"""Part 2 backend tests — Architecture REST + WebSocket API.

Covers the 14 required backend behaviours. The API layer uses the module-level
``workflow_engine`` singleton, so each test points that singleton at a private
temporary SQLite DB (via the store) and restores it afterwards. No live DB rows
are created.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import jarvis_core.workflow_engine as we_mod
from jarvis_core.workflow_store import WorkflowStore
from app import app


@pytest.fixture()
def temp_engine(tmp_path, monkeypatch):
    """Repoint the workflow_engine singleton at a temp DB for the test."""
    store = WorkflowStore(db_path=tmp_path / "arch_test.db")
    engine = we_mod.WorkflowEngine(store=store)
    # The routes module imported `workflow_engine` by reference at import time,
    # so patch it there too.
    import jarvis_core.architecture_routes as ar_mod
    monkeypatch.setattr(we_mod, "workflow_engine", engine)
    monkeypatch.setattr(ar_mod, "workflow_engine", engine)
    yield engine
    engine.close()


@pytest.fixture()
def client(temp_engine):
    return TestClient(app)


def _create(client, name="T"):
    r = client.post("/api/workflows/dev/create", json={"project_name": name})
    assert r.status_code == 200
    return r.json()["workflow"]["workflow_id"]


# ── UX correction: production create endpoint ───────────────────────────────

def test_production_create_workflow(client, temp_engine):
    """POST /api/workflows creates via the engine and persists the prompt."""
    r = client.post("/api/workflows", json={
        "project_name": "Project Opus",
        "project_path": "C:/Project Opus",
        "master_gpt_prompt": "Build the thing",
    })
    assert r.status_code == 200
    wf = r.json()["workflow"]
    assert wf["state"] == "IDLE"
    assert wf["project_name"] == "Project Opus"
    assert wf["master_gpt_prompt"] == "Build the thing"
    assert len(wf["blocks"]) == 13
    # It really went through the engine/store (Part 1).
    stored = temp_engine.get_workflow(wf["workflow_id"])
    assert stored is not None and stored.master_gpt_prompt == "Build the thing"


def test_production_create_requires_project_name(client):
    r = client.post("/api/workflows", json={"project_name": "  "})
    assert r.status_code == 400


def test_production_create_publishes_workflow_created(client):
    """A WS subscriber on the wildcard channel sees workflow_created."""
    with client.websocket_connect("/ws/workflows/*") as ws:
        r = client.post("/api/workflows", json={"project_name": "WSCreate"})
        wid = r.json()["workflow"]["workflow_id"]
        got = False
        for _ in range(4):
            m = ws.receive_json()
            if m.get("type") == "workflow_created" and m.get("workflow_id") == wid:
                got = True
                break
        assert got


def test_production_create_then_selectable_and_persists(client, temp_engine, tmp_path):
    """Created workflow is listed, and persists across a new engine instance."""
    r = client.post("/api/workflows", json={"project_name": "Persist", "master_gpt_prompt": "P"})
    wid = r.json()["workflow"]["workflow_id"]
    lst = client.get("/api/workflows").json()["workflows"]
    assert any(w["workflow_id"] == wid for w in lst)
    # Reopen a fresh engine over the same DB → still there.
    from jarvis_core.workflow_engine import WorkflowEngine
    from jarvis_core.workflow_store import WorkflowStore
    eng2 = WorkflowEngine(store=WorkflowStore(db_path=temp_engine._store.db_path))
    try:
        again = eng2.get_workflow(wid)
        assert again is not None and again.master_gpt_prompt == "P"
    finally:
        eng2.close()


def _transition(client, wid, target):
    return client.post(f"/api/workflows/{wid}/dev/transition", json={"target": target})


# 1. GET workflow
def test_get_workflow(client):
    wid = _create(client, "Alpha")
    r = client.get(f"/api/workflows/{wid}")
    assert r.status_code == 200
    body = r.json()
    assert body["workflow"]["workflow_id"] == wid
    assert body["workflow"]["state"] == "IDLE"
    assert "blocks" in body["workflow"]
    assert len(body["workflow"]["blocks"]) == 13


def test_get_missing_workflow_404(client):
    assert client.get("/api/workflows/nope").status_code == 404


# 2. list workflows
def test_list_workflows(client):
    _create(client, "A")
    _create(client, "B")
    r = client.get("/api/workflows")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert len(body["workflows"]) >= 2
    assert body["active_count"] >= 2


# 3. events endpoint
def test_events_endpoint(client):
    wid = _create(client, "A")
    _transition(client, wid, "INITIALIZING")
    r = client.get(f"/api/workflows/{wid}/events")
    assert r.status_code == 200
    events = r.json()["events"]
    types = [e["event_type"] for e in events]
    assert "workflow_created" in types
    assert "state_transition" in types
    # deterministic order by sequence
    seqs = [e["sequence"] for e in events]
    assert seqs == sorted(seqs)


# 4. context update uses WorkflowEngine
def test_context_update_via_engine(client, temp_engine):
    wid = _create(client, "A")
    r = client.patch(f"/api/workflows/{wid}/context",
                     json={"fields": {"master_gpt_prompt": "Build X"}})
    assert r.status_code == 200
    assert r.json()["workflow"]["master_gpt_prompt"] == "Build X"
    # confirm it actually went through the engine/store
    assert temp_engine.get_workflow(wid).master_gpt_prompt == "Build X"


# 5. cannot mutate state arbitrarily
def test_cannot_mutate_state_arbitrarily(client):
    wid = _create(client, "A")
    # state is a state-machine column; engine rejects it via context update
    r = client.patch(f"/api/workflows/{wid}/context", json={"fields": {"state": "COMPLETED"}})
    assert r.status_code == 400
    # unchanged
    assert client.get(f"/api/workflows/{wid}").json()["workflow"]["state"] == "IDLE"


# 6. pause API valid
def test_pause_api_valid(client):
    wid = _create(client, "A")
    _transition(client, wid, "INITIALIZING")
    _transition(client, wid, "GPT_BOOTSTRAPPING")
    _transition(client, wid, "WAITING_FOR_COMMAND")
    r = client.post(f"/api/workflows/{wid}/pause", json={"reason": "hold"})
    assert r.status_code == 200
    body = r.json()["workflow"]
    assert body["state"] == "PAUSED"
    assert body["resume_state"] == "WAITING_FOR_COMMAND"


# 7. resume API valid
def test_resume_api_valid(client):
    wid = _create(client, "A")
    _transition(client, wid, "INITIALIZING")
    _transition(client, wid, "GPT_BOOTSTRAPPING")
    _transition(client, wid, "WAITING_FOR_COMMAND")
    client.post(f"/api/workflows/{wid}/pause", json={"reason": "hold"})
    r = client.post(f"/api/workflows/{wid}/resume")
    assert r.status_code == 200
    assert r.json()["workflow"]["state"] == "WAITING_FOR_COMMAND"


# 8. cancel API valid
def test_cancel_api_valid(client):
    wid = _create(client, "A")
    _transition(client, wid, "INITIALIZING")
    r = client.post(f"/api/workflows/{wid}/cancel", json={"reason": "stop"})
    assert r.status_code == 200
    assert r.json()["workflow"]["state"] == "CANCELLED"


# 9. terminal state controls rejected appropriately
def test_terminal_controls_rejected(client):
    wid = _create(client, "A")
    _transition(client, wid, "INITIALIZING")
    client.post(f"/api/workflows/{wid}/cancel", json={"reason": "stop"})
    # pause / resume / another transition on a terminal workflow must 409
    assert client.post(f"/api/workflows/{wid}/pause", json={}).status_code == 409
    assert client.post(f"/api/workflows/{wid}/resume").status_code == 409
    assert _transition(client, wid, "GPT_BOOTSTRAPPING").status_code == 409


# 10. WebSocket connects
def test_websocket_connects(client):
    wid = _create(client, "A")
    with client.websocket_connect(f"/ws/workflows/{wid}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"
        assert msg["workflow"]["workflow_id"] == wid
        assert len(msg["events"]) >= 1


# 11. WebSocket receives state transition
def test_websocket_receives_state_transition(client):
    wid = _create(client, "A")
    with client.websocket_connect(f"/ws/workflows/{wid}") as ws:
        ws.receive_json()  # snapshot
        _transition(client, wid, "INITIALIZING")
        # We should receive a state_transition (and an event_added). Read a few.
        got_transition = False
        for _ in range(4):
            m = ws.receive_json()
            if m.get("type") == "state_transition" and m["workflow"]["state"] == "INITIALIZING":
                got_transition = True
                break
        assert got_transition


# 12. WebSocket receives context update / event
def test_websocket_receives_context_update(client):
    wid = _create(client, "A")
    with client.websocket_connect(f"/ws/workflows/{wid}") as ws:
        ws.receive_json()  # snapshot
        client.patch(f"/api/workflows/{wid}/context",
                     json={"fields": {"master_gpt_prompt": "Hello"}})
        got_update = False
        for _ in range(4):
            m = ws.receive_json()
            if m.get("type") == "workflow_updated" and m["workflow"]["master_gpt_prompt"] == "Hello":
                got_update = True
                break
        assert got_update


# 13. disconnected client doesn't break engine
def test_disconnected_client_does_not_break_engine(client, temp_engine):
    wid = _create(client, "A")
    with client.websocket_connect(f"/ws/workflows/{wid}") as ws:
        ws.receive_json()
    # After the client disconnected, engine + API keep working normally.
    r = _transition(client, wid, "INITIALIZING")
    assert r.status_code == 200
    assert temp_engine.get_workflow(wid).state == "INITIALIZING"
    # A subsequent publish (context update) also must not raise.
    assert client.patch(f"/api/workflows/{wid}/context",
                        json={"fields": {"master_gpt_prompt": "ok"}}).status_code == 200


# 14. multiple workflow clients remain isolated
def test_multiple_workflow_clients_isolated(client):
    wid_a = _create(client, "A")
    wid_b = _create(client, "B")
    with client.websocket_connect(f"/ws/workflows/{wid_a}") as ws_a, \
         client.websocket_connect(f"/ws/workflows/{wid_b}") as ws_b:
        ws_a.receive_json()  # snapshot A
        ws_b.receive_json()  # snapshot B
        # Transition only A.
        _transition(client, wid_a, "INITIALIZING")
        # A must receive A's transition; B must NOT receive A's workflow_id.
        a_ok = False
        for _ in range(4):
            m = ws_a.receive_json()
            if m.get("workflow_id") == wid_a and m.get("type") == "state_transition":
                a_ok = True
                break
        assert a_ok
        # Now transition B and confirm B's stream carries B only.
        _transition(client, wid_b, "INITIALIZING")
        b_ok = False
        for _ in range(4):
            m = ws_b.receive_json()
            assert m.get("workflow_id") in (wid_b, None)  # never A's id
            if m.get("workflow_id") == wid_b and m.get("type") == "state_transition":
                b_ok = True
                break
        assert b_ok


# Extra: state-mapping endpoint exposes the 13-block contract.
def test_state_mapping_contract(client):
    r = client.get("/api/workflows/state-mapping")
    assert r.status_code == 200
    body = r.json()
    assert len(body["blocks"]) == 13
    assert body["mapping"]["GPT_RUNNING"]["gpt_architect"] == "ACTIVE"
    assert body["mapping"]["COMPLETED"]["complete"] == "SUCCESS"
    assert set(body["presentation_states"]) == {
        "PENDING", "ACTIVE", "WAITING", "SUCCESS", "FAILED", "PAUSED", "CANCELLED"
    }


# Extra: permission policy round-trips as safe metadata (ALLOW/ASK/DENY only).
def test_permission_policy_roundtrip(client):
    wid = _create(client, "A")
    r = client.get(f"/api/workflows/{wid}/permission-policy")
    assert r.status_code == 200
    assert r.json()["policy"]["git_reset"] == "DENY"
    # Save a modified policy.
    r = client.put(f"/api/workflows/{wid}/permission-policy",
                   json={"policy": {"delete_files": "DENY", "run_tests": "ALLOW"}})
    assert r.status_code == 200
    assert client.get(f"/api/workflows/{wid}/permission-policy").json()["policy"]["delete_files"] == "DENY"
    # Invalid decision rejected.
    assert client.put(f"/api/workflows/{wid}/permission-policy",
                      json={"policy": {"delete_files": "MAYBE"}}).status_code == 400
