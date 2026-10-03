"""Part 2 frontend tests — Architecture page (string-match, no browser).

Matches the project's existing UI test style (tests/test_ui_states.py): fetch
the served HTML and assert the expected structure, JS behaviour hooks, and the
state->block contract are present. Covers required cases 15-25.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


def _html():
    r = client.get("/architecture")
    assert r.status_code == 200
    return r.text


# 15. all 13 architecture blocks rendered (declared in the BLOCKS array + flow)
def test_13_architecture_blocks_present():
    html = _html()
    for bid in [
        "chrome_profile", "gpt_conversation", "master_requirement",
        "telegram_command", "gpt_architect", "gpt_output_capture",
        "your_approval", "kiro_executor", "permission_guard", "kiro_monitor",
        "kiro_result_capture", "gpt_review", "complete",
    ]:
        assert ('id:"' + bid + '"') in html, f"missing block {bid}"
    # Titles present
    assert 'title:"Chrome Profile"' in html
    assert 'title:"Complete"' in html
    # exactly 13 entries in the BLOCKS array (n:1..n:13)
    for n in range(1, 14):
        assert f"n:{n}," in html


# 16. correct active block for each important WorkflowState (mapping contract)
def test_state_block_mapping_contract_frontend():
    r = client.get("/api/workflows/state-mapping")
    assert r.status_code == 200
    m = r.json()["mapping"]
    # A representative check across the important states -> active block.
    assert m["INITIALIZING"]["chrome_profile"] == "ACTIVE"
    assert m["GPT_BOOTSTRAPPING"]["master_requirement"] == "ACTIVE"
    assert m["WAITING_FOR_COMMAND"]["telegram_command"] == "WAITING"
    assert m["GPT_RUNNING"]["gpt_architect"] == "ACTIVE"
    assert m["GPT_OUTPUT_READY"]["gpt_output_capture"] == "ACTIVE"
    assert m["WAITING_FOR_APPROVAL"]["your_approval"] == "WAITING"
    assert m["KIRO_STARTING"]["kiro_executor"] == "ACTIVE"
    assert m["KIRO_RUNNING"]["kiro_monitor"] == "ACTIVE"
    assert m["KIRO_WAITING_PERMISSION"]["permission_guard"] == "WAITING"
    assert m["KIRO_RESULT_READY"]["kiro_result_capture"] == "ACTIVE"
    assert m["GPT_REVIEWING"]["gpt_review"] == "ACTIVE"
    assert m["COMPLETED"]["complete"] == "SUCCESS"
    # earlier blocks are SUCCESS once past them
    assert m["GPT_RUNNING"]["chrome_profile"] == "SUCCESS"


# 17. clicking a block opens the correct details drawer
def test_click_block_opens_drawer():
    html = _html()
    assert "function openDrawer(blockId)" in html
    assert 'el.onclick = () => openDrawer(b.id);' in html
    assert 'id="drawer"' in html and 'id="drawer-body"' in html
    assert "function renderDrawerBody(blockId)" in html
    # keyboard accessible
    assert 'el.onkeydown = e => { if(e.key==="Enter"||e.key===" ")' in html


# 18. Master Requirement loads current prompt
def test_master_requirement_loads_prompt():
    html = _html()
    assert 'case "master_requirement":' in html
    assert 'id="mr-text"' in html
    assert "esc(w.master_gpt_prompt||\"\")" in html
    assert 'id="mr-count"' in html  # character count


# 19. saving Master Requirement updates backend (PATCH context via engine)
def test_master_requirement_saves_to_backend():
    html = _html()
    assert "async function saveMasterRequirement()" in html
    assert '"PATCH", "/api/workflows/"' in html
    assert "master_gpt_prompt: ta.value" in html
    assert "/context" in html


# 20. long prompt remains inside panel/layout (scroll containment)
def test_long_prompt_contained():
    html = _html()
    # prompt boxes and editors have max-height/overflow and wrap
    assert ".prompt-box {" in html
    assert "max-height:220px; overflow:auto; white-space:pre-wrap; word-break:break-word;" in html
    assert "textarea.prompt-edit {" in html
    # block cards crop preview
    assert "-webkit-line-clamp:2;" in html


# 21. Permission Guard configuration renders (ALLOW / ASK / DENY)
def test_permission_guard_renders():
    html = _html()
    assert 'case "permission_guard":' in html
    assert "function renderPermissionList()" in html
    assert "['ALLOW','ASK','DENY']" in html
    assert "async function savePermissionPolicy()" in html
    assert "/permission-policy" in html
    # representative rule labels
    assert "read_project_files:" in html
    assert "git_reset:" in html
    assert "credential_secret_operations:" in html


# 22. Pause/Resume/Cancel visibility correct (state-driven enable/disable)
def test_pause_resume_cancel_visibility():
    html = _html()
    assert "function updateControls(state)" in html
    assert 'id="ctl-pause"' in html and 'id="ctl-resume"' in html and 'id="ctl-cancel"' in html
    assert 'const canResume = state==="PAUSED";' in html
    assert "const canCancel = !isTerminal;" in html
    # controls call the engine via API
    assert 'api("POST", "/api/workflows/"' in html
    assert "function ctlPause()" in html and "function ctlResume()" in html and "function ctlCancel()" in html


# 23. event timeline updates
def test_event_timeline_updates():
    html = _html()
    assert 'id="timeline"' in html
    assert "function renderTimeline()" in html
    assert "function addEvent(ev)" in html
    # incremental append on event_added ws message
    assert 'case "event_added":' in html


# 24. WebSocket reconnect handling
def test_websocket_reconnect_handling():
    html = _html()
    assert "function connectWs(workflowId)" in html
    assert "new WebSocket(url)" in html
    assert "function scheduleReconnect(workflowId)" in html
    assert "wsReconnectDelay = Math.min(wsReconnectDelay*2, 15000);" in html
    assert "sock.onclose" in html


# 25. workflow switch does not leak old workflow state
def test_workflow_switch_no_leak():
    html = _html()
    assert "async function selectWorkflow(id)" in html
    # switching resets in-memory state before loading the new one
    assert "currentWorkflow = null; currentEvents = []; openBlockId = null; mrDirty = false;" in html
    # ws messages for a non-current workflow are ignored
    assert "if (msg.workflow_id && currentWorkflowId && msg.workflow_id!==currentWorkflowId) return;" in html


# Extra: page uses the Jarvis visual language + desktop-first status header.
def test_uses_jarvis_visual_language_and_header():
    html = _html()
    assert "--q-violet:#8b5cf6" in html
    assert "--q-cyan:#22d3ee" in html
    assert 'class="arch-header"' in html
    assert 'id="hdr-elapsed"' in html
    assert 'id="conn-text"' in html  # realtime connection indicator
    # feedback loop is represented
    assert "More Work?" in html
    assert "YES ↺ back to Kiro Executor" in html
