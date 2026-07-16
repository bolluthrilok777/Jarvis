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
