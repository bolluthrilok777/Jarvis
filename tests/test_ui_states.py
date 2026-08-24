from fastapi.testclient import TestClient

from app import app


client = TestClient(app)


def test_active_tasks_show_actual_event_loader():
    html = client.get("/ui").text
    assert 'case "running": return { label: "Running"' in html
    assert 'case "queued": return { label: "Queued"' in html
    assert 'case "interpreting": return { label: "Interpreting"' in html
    assert "if (task.events && task.events.length)" in html
    assert 'return { text: last.message, cls: "" }' in html
    assert 'class="lt-spinner ${meta.cls' in html


def test_terminal_tasks_show_completed_reframe():
    html = client.get("/ui").text
    assert 'case "partially_completed": return { label: "Partial"' in html
    assert 'case "failed": return { label: "Failed"' in html
    assert 'case "cancelled": return { label: "Cancelled"' in html
    assert "if (task.interpreted_command" in html
    assert "t.result_summary" in html


def test_monitoring_modal_has_top_close_control():
    html = client.get("/ui").text
    assert 'aria-label="Close activity monitoring"' in html
    assert 'class="modal-close"' in html
    assert 'event.key==="Escape"' in html


def test_homepage_has_animated_monitoring_status():
    html = client.get("/ui").text
    assert 'id="monitor-live"' in html
    assert 'class="monitor-orbit"' in html
    assert "@keyframes monitorSpin" in html
    assert "refreshMonitoringStatus" in html
    assert "Monitoring active · Next" in html


def test_ollama_icon_is_animated_status_and_click_menu():
    html = client.get("/ui").text
    assert 'id="ollama-core"' in html
    assert 'aria-label="Ollama model and connection status"' in html
    assert 'id="ollama-menu" class="ollama-menu hidden"' in html
    assert 'data-mode="auto"' in html
    assert 'data-mode="fast"' in html
    assert 'data-mode="balanced"' in html
    assert 'data-mode="coding"' in html
    assert 'data-mode="quality"' in html
    assert 'data-mode="deep"' in html
    assert 'class="ollama-robot" src="/assets/ollama-robot.png"' in html
    assert 'class="ollama-robot-fallback"' in html
    assert "this.nextElementSibling.removeAttribute('hidden')" in html
    assert 'class="signal-wave wave-one"' in html
    assert 'class="signal-wave wave-two"' in html
    assert "@keyframes robotWave" in html
    assert ".ollama-core.mode-light { --robot:#20d7ff" in html
    assert ".ollama-core.mode-heavy { --robot:#9b6cff" in html
    assert "width:64px; height:64px" in html
    assert "transform:scale(1.48)" in html
    assert "refreshOllamaStatus" in html


def test_composer_uses_full_available_width():
    html = client.get("/ui").text
    assert "textarea { display:block; width:100%; max-width:none; min-height:112px" in html
    assert ".container { width:calc(100% - 220px); max-width:1240px" in html


def test_visible_composer_model_dropdown_removed():
    html = client.get("/ui").text
    assert '<select id="mode"' not in html
    assert "function resolveOllamaMode(text)" in html


def test_model_router_status_and_visual_effects_exist():
    html = client.get("/ui").text
    assert 'id="router-status"' in html
    assert 'id="visual-effects"' in html
    assert "initialiseVisualEffects" in html
    assert "updateRouterStatus" in html
