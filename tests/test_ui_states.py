from fastapi.testclient import TestClient

from app import app


client = TestClient(app)


def test_active_tasks_show_reframing_loader():
    html = client.get("/ui").text
    assert 'const isReframing = ["queued", "interpreting", "running"]' in html
    assert "Jarvis is reframing" in html
    assert 'class="loading-dots"' in html
    assert "@keyframes dotPulse" in html


def test_terminal_tasks_show_completed_reframe():
    html = client.get("/ui").text
    assert 'Jarvis reframed:</span>' in html
    assert '"partially_completed", "failed", "cancelled"' in html


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
    assert 'data-mode="light"' in html
    assert 'data-mode="heavy"' in html
    assert 'class="ollama-robot" src="/assets/ollama-robot.png"' in html
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
    assert "resolveOllamaMode(lineText)" in html
