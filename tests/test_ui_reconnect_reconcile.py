"""Realtime reconnect reconciliation regression tests (Part 5 UI fix).

Root cause under test: on WebSocket reconnect the frontend must reload the
AUTHORITATIVE workflow snapshot (and timeline) before continuing incremental
events, so events missed while the socket was down cannot leave the UI stale
(e.g. showing WAITING_FOR_COMMAND / iteration 0 after the backend already
advanced to GPT_OUTPUT_READY / iteration 1).

Two layers are covered without any ChatGPT/Telegram side effects:
- String-match tests over the served /ui HTML that lock in the reconcile-on-
  (re)connect code path and the no-duplicate-WS guard (project's UI test style).
- Backend-contract tests proving the authoritative snapshot the UI fetches
  (GET /api/workflows/{id}) reports exactly the state/iteration/blocks the UI
  must reconcile TO for a GPT_OUTPUT_READY workflow.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import jarvis_core.workflow_engine as we_mod
import jarvis_core.architecture_routes as ar_mod
from jarvis_core.workflow_store import WorkflowStore
from app import app

client = TestClient(app)


def _ui() -> str:
    r = client.get("/ui")
    assert r.status_code == 200
    return r.text


# ── String-match: reconcile-on-reconnect code path exists ────────────────────

# 4/5. reconnect fetches a fresh authoritative snapshot (not incremental-only)
def test_reconnect_fetches_authoritative_snapshot():
    h = _ui()
    # A dedicated reconcile function exists and is invoked from onopen.
    assert "function reconcileSnapshot(id)" in h
    assert "reconcileSnapshot(id)" in h
    # It GETs the authoritative workflow snapshot + events.
    assert '"/api/workflows/"+encodeURIComponent(id)' in h
    assert '"/api/workflows/"+encodeURIComponent(id)+"/events"' in h
    # It reconciles by applying the fetched workflow.
    assert "applyWorkflow(d.workflow)" in h


# reconcile is wired specifically into the socket open handler
def test_reconcile_wired_into_onopen():
    h = _ui()
    # onopen sets Connected AND kicks the authoritative reconcile.
    assert 'setConn("ok","Connected")' in h
    onopen = h.split("sock.onopen=")[1][:700]
    assert "reconcileSnapshot(id)" in onopen


# 9. no duplicate WebSocket connection after reconnect/navigation
def test_no_duplicate_ws_after_reconnect():
    h = _ui()
    assert "ws._wfId===id" in h
    assert "(ws.readyState===0 || ws.readyState===1)) return;" in h
    assert "function disconnectWs()" in h
    # scheduleReconnect only continues while the same workflow is selected.
    assert "if(wsRun&&wfId===id) connectWs(id)" in h


# the client also still honours the server-pushed snapshot message
def test_client_still_applies_pushed_snapshot():
    h = _ui()
    assert 'if(m.type==="snapshot")' in h and "applyWorkflow(m.workflow)" in h


# ── Backend contract: what the UI reconciles TO ─────────────────────────────

@pytest.fixture()
def engine(tmp_path, monkeypatch):
    store = WorkflowStore(db_path=tmp_path / "reconcile.db")
    eng = we_mod.WorkflowEngine(store=store)
    monkeypatch.setattr(we_mod, "workflow_engine", eng)
    monkeypatch.setattr(ar_mod, "workflow_engine", eng)
    yield eng
    eng.close()


def _output_ready_workflow(eng):
    """Drive a workflow to GPT_OUTPUT_READY exactly as Part 5 leaves it."""
    wf = eng.create_workflow(project_name="Reconcile")
    wid = wf.workflow_id
    eng.update_workflow_context(
        wid, selected_chrome_profile_key="chrome-profile:Default",
        selected_chrome_account_email="x@y.com",
        master_gpt_prompt="master",
        chatgpt_conversation_url="https://chatgpt.com/c/keep",
        chatgpt_window_hwnd=999)
    eng.transition(wid, "INITIALIZING")
    eng.transition(wid, "GPT_BOOTSTRAPPING")
    eng.transition(wid, "WAITING_FOR_COMMAND")
    # follow-up command lifecycle
    eng.update_workflow_context(wid, current_user_command="PART 5 TELEGRAM LIVE TEST COMPLETE")
    eng.set_metadata(wid, "current_command", {"text": "PART 5 TELEGRAM LIVE TEST COMPLETE",
                                              "telegram_message_id": 697})
    eng.transition(wid, "GPT_RUNNING")
    eng.set_metadata(wid, "latest_gpt_response", {"ready_at": "2026-09-04T10:04:05+05:30",
                                                 "iteration": 1, "copy_available": True})
    eng.update_workflow_context(wid, iteration_number=1)
    eng.transition(wid, "GPT_OUTPUT_READY")
    return wid


# 1/2/6/7. authoritative snapshot reports GPT_OUTPUT_READY + iteration 1
def test_snapshot_reports_output_ready(engine):
    wid = _output_ready_workflow(engine)
    r = client.get(f"/api/workflows/{wid}")
    assert r.status_code == 200
    w = r.json()["workflow"]
    assert w["state"] == "GPT_OUTPUT_READY"
    assert w["iteration_number"] == 1
    assert w["current_user_command"] == "PART 5 TELEGRAM LIVE TEST COMPLETE"
    assert w["latest_gpt_response"]["copy_available"] is True


# 8. block presentation maps correctly for the reconciled state
def test_snapshot_block_mapping(engine):
    wid = _output_ready_workflow(engine)
    w = client.get(f"/api/workflows/{wid}").json()["workflow"]
    blocks = w["blocks"]
    # Telegram Command + GPT Architect are done; GPT Output Capture is active.
    assert blocks["telegram_command"] == "SUCCESS"
    assert blocks["gpt_architect"] == "SUCCESS"
    assert blocks["gpt_output_capture"] == "ACTIVE"
    # Earlier blocks also complete.
    assert blocks["chrome_profile"] == "SUCCESS"
    assert blocks["gpt_conversation"] == "SUCCESS"
    assert blocks["master_requirement"] == "SUCCESS"


# 3. the stale local state (WAITING_FOR_COMMAND) differs from authoritative —
#    i.e. a reconcile is genuinely required (guards against a no-op test).
def test_stale_vs_authoritative_differs(engine):
    wid = _output_ready_workflow(engine)
    w = client.get(f"/api/workflows/{wid}").json()["workflow"]
    stale_blocks = ar_mod._presentation_for_state("WAITING_FOR_COMMAND")
    # Stale would show telegram_command WAITING + gpt_output_capture PENDING.
    assert stale_blocks["telegram_command"] == "WAITING"
    assert stale_blocks["gpt_output_capture"] == "PENDING"
    # Authoritative differs → reconcile changes the UI.
    assert w["blocks"]["telegram_command"] != stale_blocks["telegram_command"]
    assert w["blocks"]["gpt_output_capture"] != stale_blocks["gpt_output_capture"]


# ── WebSocket route + handshake regression (Part 2/5 WS fix) ─────────────────
# Root cause of the browser "WebSocket connection failed": the live uvicorn had
# no WebSocket library (websockets/wsproto), so it rejected the upgrade. The
# route path itself is correct and agrees with the frontend. These tests lock in
# the route/handshake/snapshot contract and the frontend↔backend path agreement.

import jarvis_core.architecture_routes as _ar


# 1. the registered WS route exists at the documented path — proven
# behaviorally: connecting to /ws/workflows/<id> completes the handshake (a
# missing route would raise before any message). This is the authoritative
# "route exists" contract, independent of framework-internal route structures.
def test_ws_route_registered():
    with client.websocket_connect("/ws/workflows/*") as ws:
        # Wildcard channel accepts the connection (no per-id snapshot); the
        # successful context-manager entry means the route handled the upgrade.
        assert ws is not None


# 5. the frontend uses the SAME route the backend registers
def test_frontend_ws_path_matches_backend():
    h = _ui()
    # frontend builds ws(s)://host/ws/workflows/<id>
    assert '"/ws/workflows/"+encodeURIComponent(id)' in h
    # backend router declares the same suffix (no /api prefix on the ws_router)
    import inspect
    src = inspect.getsource(_ar)
    assert '@ws_router.websocket("/ws/workflows/{workflow_id}")' in src


@pytest.fixture()
def ws_engine(tmp_path, monkeypatch):
    store = WorkflowStore(db_path=tmp_path / "ws.db")
    eng = we_mod.WorkflowEngine(store=store)
    monkeypatch.setattr(we_mod, "workflow_engine", eng)
    monkeypatch.setattr(ar_mod, "workflow_engine", eng)
    yield eng
    eng.close()


# 2/3/4. handshake succeeds, initial snapshot received, correct workflow id
def test_ws_handshake_snapshot(ws_engine):
    wf = ws_engine.create_workflow(project_name="WS")
    wid = wf.workflow_id
    with client.websocket_connect(f"/ws/workflows/{wid}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"
        assert msg["workflow_id"] == wid
        assert msg["workflow"]["workflow_id"] == wid


# 7. REST /api/workflows remains unchanged (still returns the list contract)
def test_rest_workflows_unchanged(ws_engine):
    ws_engine.create_workflow(project_name="WS2")
    r = client.get("/api/workflows")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and isinstance(body["workflows"], list)


# 6. reconnect still reconciles authoritative state (Part 5 fix preserved)
def test_reconnect_reconcile_preserved():
    h = _ui()
    assert "function reconcileSnapshot(id)" in h
    assert "wsConnectedOnce=true; reconcileSnapshot(id)" in h
