"""Part 2 UX-correction tests — Autonomous Workflow embedded in the Jarvis /ui.

String-match tests (matching the project's existing UI test style) that verify
the Architecture experience is integrated inside the main dashboard shell, that
first-time creation uses the production endpoint, that no developer API text is
exposed, and that navigation/WebSocket lifecycle behave correctly.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


def _ui():
    r = client.get("/ui")
    assert r.status_code == 200
    return r.text


# 9. Autonomous Workflow exists in the main Jarvis navigation
def test_autonomous_nav_present():
    h = _ui()
    assert 'id="nav-autonomous"' in h
    assert "Autonomous Workflow" in h
    assert "onclick=\"openArchitecture()\"" in h


# 10. architecture renders inside the Jarvis shell (embedded, not iframe/page)
def test_architecture_embedded_in_shell():
    h = _ui()
    assert 'id="arch-section"' in h
    assert "const AW = (function()" in h
    assert "<iframe" not in h.split('id="arch-section"')[1][:4000]  # no iframe around it
    # toggles with the dashboard container, no reload
    assert 'c.style.display="none"' in h
    assert "function openArchitecture()" in h and "function closeArchitecture()" in h


# 11. switching navigation hides/shows it correctly
def test_navigation_toggle_hide_show():
    h = _ui()
    assert 's.classList.add("aw-visible")' in h
    assert 's.classList.remove("aw-visible")' in h
    # any non-autonomous nav-btn returns to the dashboard
    assert 'if (btn.id === "nav-autonomous") return;' in h
    assert "closeArchitecture()" in h


# 12. no duplicate WebSocket after repeated navigation
def test_no_duplicate_websocket():
    h = _ui()
    # connectWs reuses an open/connecting socket for the same workflow
    assert "ws._wfId===id" in h
    assert "(ws.readyState===0 || ws.readyState===1)) return;" in h
    assert "function disconnectWs()" in h


# 13. "Waiting for workflow" shown when none selected
def test_waiting_for_workflow_state():
    h = _ui()
    assert "Waiting for workflow" in h
    # not stuck on Connecting when no workflow
    assert 'setConn("waiting","Waiting for workflow")' in h


# 3. empty state contains the Create Workflow UI (setup form)
def test_empty_state_has_create_form():
    h = _ui()
    assert 'id="aw-in-name"' in h
    assert 'id="aw-in-path"' in h
    assert 'id="aw-in-prompt"' in h
    assert "AW.create()" in h
    assert "No workflow configured yet." in h


# 4. no developer API text exposed to the user
def test_no_dev_api_text_exposed():
    h = _ui()
    assert "/api/workflows/dev/create" not in h
    assert "dev/create" not in h


# 5. Create Workflow uses the production endpoint + selects the created workflow
def test_create_uses_production_endpoint_and_selects():
    h = _ui()
    assert '"POST","/api/workflows"' in h  # production endpoint (not dev)
    # after create, select() is called with the new id
    assert "wfId=id; select(id);" in h


# 6. all 13 blocks are declared for rendering after creation
def test_thirteen_blocks_declared():
    h = _ui()
    for bid in ["chrome_profile", "gpt_conversation", "master_requirement",
                "telegram_command", "gpt_architect", "gpt_output_capture",
                "your_approval", "kiro_executor", "permission_guard",
                "kiro_monitor", "kiro_result_capture", "gpt_review", "complete"]:
        assert ('id:"' + bid + '"') in h


# + New Workflow control present, prefilled defaults (Project Opus, not hardcoded-only)
def test_new_workflow_and_defaults():
    h = _ui()
    assert "AW.showSetup()" in h
    assert 'DEFAULTS = { name:"Project Opus", path:"C:\\\\Project Opus" }' in h


# 14/15. Master Requirement + Permission Guard editors are embedded and persist via API
def test_master_requirement_and_permission_embedded():
    h = _ui()
    assert 'id="aw-mr-text"' in h and "AW.saveMR()" in h
    assert '"PATCH","/api/workflows/"' in h  # master requirement save
    assert "AW.savePerm()" in h and "/permission-policy" in h


# Existing standalone page keeps working (debug), also without dev text.
def test_standalone_architecture_still_available():
    r = client.get("/architecture")
    assert r.status_code == 200
    assert "/api/workflows/dev/create" not in r.text


# ── Layout correction: fills the main workspace, no reserved empty canvas ───

def test_architecture_nested_in_hud_shell_not_sibling():
    """#arch-section must live INSIDE #hud-shell (the real main-content host),
    as a sibling of .container — not as a top-level body sibling that creates a
    second column. Verified structurally: the section appears between the
    .container close and the #hud-shell close, before the body-level scrim."""
    h = _ui()
    shell_open = h.index('<div class="hud-shell" id="hud-shell">')
    arch_open = h.index('<section id="arch-section"')
    # The scrim/drawer are body-level overlays that come AFTER the shell closes.
    scrim = h.index('<div class="aw-scrim" id="aw-scrim"')
    assert shell_open < arch_open < scrim, "arch-section must be inside #hud-shell"
    # And it is placed right after the dashboard content mount comment.
    assert "AUTONOMOUS WORKFLOW mounts HERE, inside #hud-shell" in h


def test_architecture_fills_workspace_via_flex_not_margin_hack():
    """The section fills the space beside the sidebar as a flex child — no
    arbitrary margin-left/width offset hacks."""
    h = _ui()
    assert "#arch-section.aw-visible { display:flex; flex-direction:column; flex:1 1 auto;" in h
    # the old margin-left:220px hack must be gone
    assert "margin-left:220px; width:calc(100% - 220px)" not in h


def test_dashboard_removed_from_flow_when_open():
    """Opening Autonomous Workflow takes the dashboard container out of flow
    (display:none), and closing restores it — no invisible width reserved."""
    h = _ui()
    assert 'c.style.display="none"' in h   # openArchitecture hides container
    assert 'c.style.display="";' in h      # closeArchitecture restores it


def test_timeline_is_compact_panel():
    h = _ui()
    assert "flex:0 0 clamp(240px, 22%, 340px)" in h  # ~20-25% right panel
    assert ".aw-canvas { flex:1 1 auto;" in h          # architecture takes majority


def test_blocks_use_available_width():
    h = _ui()
    assert ".aw-block { position:relative; width:100%; max-width:560px;" in h


def test_setup_card_centered_with_max_width():
    h = _ui()
    assert ".aw-setup { max-width:640px; margin:8px auto;" in h


def test_nav_label_readable_and_has_tooltip():
    h = _ui()
    # Short data-label (fits the rail) + full tooltip + explicit width reset so
    # the active-state decorative ::after dot can't clip the label to "A...".
    assert 'data-label="Autonomous"' in h
    assert 'title="Autonomous Workflow"' in h
    assert ".sidebar .nav-btn::after {" in h
    assert "width:auto; height:auto;" in h  # label pseudo not clipped by 24px dot
