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
