"""Cross-platform regression tests for cloud-ready Jarvis.

Covers:
* repository-root path resolution + JARVIS_BASE_DIR override
* no hardcoded Windows-drive paths left in runtime modules
* headless/desktop capability handling
* Linux-safe system monitoring
* environment overrides for Ollama configuration
"""
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── Phase B: central cross-platform path resolution ──────────────────────────

def test_base_dir_defaults_to_repository_root():
    from jarvis_core.config import BASE_DIR, REPO_ROOT
    assert BASE_DIR == REPO_ROOT.resolve()
    assert not str(BASE_DIR).lower().startswith("c:/project opus")


def test_derived_paths_live_under_base_dir():
    from jarvis_core.config import (
        BASE_DIR, NOTES_DIR, SCREENSHOT_DIR, LOGS_DIR, UI_FILE,
        CONFIG_DIR, DATA_DIR, JOBS_JD_DIR, GMAIL_DRAFTS_DIR,
        WEB_SCREENSHOT_DIR,
    )
    for derived in (NOTES_DIR, SCREENSHOT_DIR, LOGS_DIR, UI_FILE, CONFIG_DIR,
                    DATA_DIR, JOBS_JD_DIR, GMAIL_DRAFTS_DIR, WEB_SCREENSHOT_DIR):
        resolved = Path(derived).resolve()
        assert BASE_DIR == resolved or BASE_DIR in resolved.parents, \
            f"{derived} not under {BASE_DIR}"


def test_ui_file_exists_in_repository():
    """The /ui route depends on UI_FILE pointing at the real ui.html."""
    from jarvis_core.config import UI_FILE
    assert UI_FILE.name == "ui.html"
    assert UI_FILE.exists(), f"ui.html not found at {UI_FILE}"


def test_jarvis_base_dir_env_override(tmp_path):
    """JARVIS_BASE_DIR must relocate every derived path."""
    code = (
        "import sys; sys.path.insert(0, r'{root}');"
        "from jarvis_core.config import BASE_DIR, NOTES_DIR;"
        "print(BASE_DIR); print(NOTES_DIR)"
    ).format(root=REPO_ROOT)
    env = os.environ.copy()
    env["JARVIS_BASE_DIR"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=60,
    )
    assert result.returncode == 0, result.stderr
    base_line, notes_line = result.stdout.strip().splitlines()[-2:]
    assert Path(base_line) == tmp_path
    assert Path(notes_line) == tmp_path / "notes"


def test_gmail_paths_share_central_config():
    """Gmail credentials/state/config must resolve against BASE_DIR."""
    from jarvis_core import config
    from jarvis_core import gmail_service, gmail_monitor, draft_writer, jd_saver
    assert gmail_service.CONFIG_DIR == config.CONFIG_DIR
    assert gmail_service.DATA_DIR == config.DATA_DIR
    assert gmail_monitor.CONFIG_DIR == config.CONFIG_DIR
    assert gmail_monitor.NOTES_DIR == config.NOTES_DIR
    assert draft_writer.DRAFTS_DIR == config.GMAIL_DRAFTS_DIR
    assert jd_saver.DEFAULT_JD_FOLDER == config.JOBS_JD_DIR


def test_gmail_config_model_defaults_derive_from_base_dir():
    from jarvis_core.config import JOBS_JD_DIR, NOTES_DIR
    from jarvis_core.gmail_models import GmailConfig
    cfg = GmailConfig()
    # Defaults must resolve dynamically against BASE_DIR (portable), and must
    # never fall back to the legacy hardcoded C:\\Jarvis locations.
    assert cfg.save_jd_folder == str(JOBS_JD_DIR)
    assert cfg.log_folder == str(NOTES_DIR)
    assert "c:\\jarvis" not in cfg.save_jd_folder.lower().replace("/", "\\")
    assert "c:\\jarvis" not in cfg.log_folder.lower().replace("/", "\\")


def test_no_hardcoded_drive_paths_in_runtime_modules():
    """Runtime source must not hardcode C:/Jarvis or C:/Project Opus."""
    offenders = []
    targets = [
        REPO_ROOT / "app.py",
        REPO_ROOT / "browser_tools.py",
        REPO_ROOT / "calculator_tools.py",
        REPO_ROOT / "jarvis_core",
    ]
    banned = ("c:/jarvis", "c:\\jarvis", "c:/project opus", "c:\\project opus")
    for target in targets:
        files = [target] if target.is_file() else sorted(target.glob("*.py"))
        for py_file in files:
            text = py_file.read_text(encoding="utf-8").lower()
            if any(marker in text for marker in banned):
                offenders.append(str(py_file))
    assert not offenders, f"Hardcoded Windows paths remain in: {offenders}"


# ── Phase C: Linux-safe system monitoring ─────────────────────────────────────

def test_disk_target_matches_platform(monkeypatch):
    from jarvis_core import system_tools
    if sys.platform.startswith("win"):
        monkeypatch.setenv("SystemDrive", "Q:")
        assert system_tools._disk_target() == "Q:\\"
        monkeypatch.delenv("SystemDrive", raising=False)
        assert system_tools._disk_target() == "C:\\"
    else:
        monkeypatch.delenv("SystemDrive", raising=False)
        assert system_tools._disk_target() == "/"


def test_system_status_works_without_desktop(monkeypatch):
    """get_system_status must succeed even when no desktop is available."""
    from unittest.mock import MagicMock, patch
    from jarvis_core import system_tools

    mock_mem = MagicMock(used=8 * 1024 ** 3, total=16 * 1024 ** 3, percent=50.0)
    mock_disk = MagicMock(used=100 * 1024 ** 3, total=500 * 1024 ** 3, percent=20.0)
    monkeypatch.setattr(system_tools, "DESKTOP_AVAILABLE", False)
    with patch.object(system_tools.psutil, "cpu_percent", return_value=25.0), \
         patch.object(system_tools.psutil, "virtual_memory", return_value=mock_mem), \
         patch.object(system_tools.psutil, "disk_usage", return_value=mock_disk), \
         patch.object(system_tools.psutil, "pids", return_value=list(range(50))):
        data = system_tools.get_system_status(launch_task_manager=True)
    assert data["cpu_percent"] == 25.0
    assert data["running_processes"] == 50


def test_screenshot_returns_structured_refusal_when_headless(monkeypatch):
    from jarvis_core import system_tools
    monkeypatch.setattr(system_tools, "DESKTOP_AVAILABLE", False)
    result = system_tools.take_screenshot()
    assert result["status"] == "unsupported_on_this_node"


# ── Phase D: desktop-only capability gating ───────────────────────────────────

def test_require_desktop_allows_when_available():
    from jarvis_core.desktop_capabilities import require_desktop, DESKTOP_AVAILABLE
    if DESKTOP_AVAILABLE:
        assert require_desktop("x") is None


def test_calculator_gui_unsupported_on_headless(monkeypatch):
    import calculator_tools
    monkeypatch.setattr(calculator_tools, "DESKTOP_AVAILABLE", False)
    result = calculator_tools.drive_calculator_gui("2+2")
    assert result["status"] == "unsupported_on_this_node"
    # The pure evaluator keeps working everywhere.
    assert calculator_tools.safe_calculate("2+2") == 4


def test_app_open_refused_on_headless(monkeypatch):
    from jarvis_core import app_tools
    monkeypatch.setattr(app_tools, "DESKTOP_AVAILABLE", False)
    ok, message = app_tools.open_known_app("notepad")
    assert ok is False
    assert "desktop" in message.lower()


def test_notes_still_work_headless():
    """Filesystem skills must keep working without a desktop."""
    from jarvis_core.app_tools import create_note
    result = create_note("cross-platform smoke note")
    assert result["status"] == "done"


def test_skill_registry_headless_responses(monkeypatch):
    from jarvis_core import skill_registry
    monkeypatch.setattr(skill_registry, "DESKTOP_AVAILABLE", False)
    cases = [
        (skill_registry._exec_app_focus, ("notepad",)),
        (skill_registry._exec_app_close, ("notepad",)),
        (skill_registry._exec_notepad_open, ()),
        (skill_registry._exec_notepad_type, ("hello",)),
        (skill_registry._exec_notepad_save, ("notes", "demo.txt")),
    ]
    for executor, args in cases:
        result = executor(*args)
        assert result.get("status") == "unsupported_on_this_node", \
            f"{executor.__name__} returned {result}"


# ── Phase G: Ollama environment overrides ─────────────────────────────────────

def test_ollama_config_env_overrides(tmp_path):
    code = (
        "import sys; sys.path.insert(0, r'{root}');"
        "from jarvis_core.config import OLLAMA_URL, LIGHT_MODEL, HEAVY_MODEL;"
        "print(OLLAMA_URL); print(LIGHT_MODEL); print(HEAVY_MODEL)"
    ).format(root=REPO_ROOT)
    env = os.environ.copy()
    env["OLLAMA_URL"] = "http://10.0.0.5:11434"
    env["JARVIS_LIGHT_MODEL"] = "llama3.2:3b"
    env["JARVIS_HEAVY_MODEL"] = "qwen2.5:14b"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=60,
    )
    assert result.returncode == 0, result.stderr
    url, light, heavy = result.stdout.strip().splitlines()[-3:]
    assert url == "http://10.0.0.5:11434"
    assert light == "llama3.2:3b"
    assert heavy == "qwen2.5:14b"


def test_ollama_defaults_backward_compatible():
    from jarvis_core.config import OLLAMA_URL, LIGHT_MODEL, HEAVY_MODEL
    assert OLLAMA_URL == "http://localhost:11434"
    assert LIGHT_MODEL == "gemma2:2b"
    assert HEAVY_MODEL == "qwen2.5:7b"


# ── Phase F: web metadata stays relative ──────────────────────────────────────

def test_root_metadata_ui_is_relative():
    from fastapi.testclient import TestClient
    from app import app
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["ui"] == "/ui"
