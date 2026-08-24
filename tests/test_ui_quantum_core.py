from fastapi.testclient import TestClient

from app import app


client = TestClient(app)


def test_quantum_core_is_open_and_has_fourteen_orbital_threads():
    html = client.get("/ui").text
    assert 'class="qc-orbit-field"' in html
    assert html.count('class="qc-ring qc-ring--') == 14
    assert 'class="qc-bracket ' not in html
    assert 'class="qc-scan"' not in html


def test_quantum_core_uses_real_task_and_model_state_hooks():
    html = client.get("/ui").text
    assert "window.updateQuantumTaskState" in html
    assert 'core.setAttribute("data-task-load"' in html
    assert 'core.classList.toggle("has-running-tasks"' in html
    assert ".qc-core-card.theme-shift .qc-orbit-field" in html
    assert "qcOrbitRephase" in html
    assert 'var modelLabel = "MODEL · "' in html


def test_quantum_core_pauses_when_hidden_and_respects_reduced_motion():
    html = client.get("/ui").text
    assert 'document.addEventListener("visibilitychange", syncVisibility)' in html
    assert "body.quantum-paused .qc-orbit-field" in html
    assert "prefers-reduced-motion: reduce" in html
