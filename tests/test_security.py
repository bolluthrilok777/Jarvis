"""Tests 14-22: Security-critical behaviors."""
import pathlib
import socket
import time
from unittest.mock import MagicMock, patch

import pytest


# Test 14: .exe extension is blocked
def test_file_extension_blocked():
    from jarvis_core.file_tools import read_text_file
    result = read_text_file("C:/Windows/system32/evil.exe")
    assert result["status"] == "blocked"


# Test 15: File larger than 1 MB is blocked
def test_file_size_blocked(tmp_path):
    # Create a real small file, then mock stat to return large size
    test_file = tmp_path / "big.txt"
    test_file.write_text("x", encoding="utf-8")

    mock_stat = MagicMock()
    mock_stat.st_size = 2 * 1024 * 1024  # 2 MB

    with patch("jarvis_core.file_tools.ALLOWED_ROOTS", [tmp_path]):
        with patch.object(pathlib.Path, "stat", return_value=mock_stat):
            from jarvis_core.file_tools import read_text_file
            result = read_text_file(str(test_file))

    assert result["status"] == "blocked"
    assert "large" in result["message"].lower() or "size" in result["message"].lower() or "1 MB" in result["message"]


# Test 16: Path outside ALLOWED_ROOTS is blocked
def test_file_outside_allowed_roots_blocked():
    from jarvis_core.file_tools import read_text_file
    # C:\Windows\secret.txt is outside all allowed roots
    result = read_text_file("C:/Windows/secret.txt")
    assert result["status"] == "blocked"
    assert "Access denied" in result["message"] or "blocked" in result["status"]


# Test 17: Google search URL has no raw spaces or unencoded &
def test_google_url_encoding():
    from urllib.parse import parse_qs, urlparse
    import webbrowser
    captured_urls = []

    with patch("jarvis_core.app_tools.webbrowser.open", side_effect=lambda url: captured_urls.append(url)):
        from jarvis_core.app_tools import google_search
        google_search("hello world & more")

    assert len(captured_urls) == 1
    url = captured_urls[0]
    assert " " not in url
    # Parse and verify the q parameter round-trips
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert params["q"][0] == "hello world & more"


# Test 18: Dangerous command triggers confirmation_required
def test_dangerous_command_requires_confirmation():
    from jarvis_core.models import CommandRequest
    from jarvis_core.command_dispatcher import run_agent_command
    req = CommandRequest(command="delete all files", mode="light")
    result = run_agent_command(req)
    assert result["status"] == "confirmation_required"
    assert "action_id" in result


# Test 19: Expired pending action returns "expired"
def test_expired_pending_action():
    from jarvis_core.models import ConfirmRequest
    from jarvis_core.command_dispatcher import confirm_action, pending_actions

    # Manually insert an expired action
    action_id = "test-expired-id-12345"
    pending_actions[action_id] = {
        "command": "delete something",
        "created_at": time.time() - 400,  # 400s ago > 300s expiry
    }

    req = ConfirmRequest(action_id=action_id, confirm=True)
    result = confirm_action(req)
    assert result["status"] == "expired"
    assert action_id not in pending_actions


# Test 19b: Confirmed action returns correct non-execution message
def test_confirmed_action_message():
    from jarvis_core.models import ConfirmRequest
    from jarvis_core.command_dispatcher import confirm_action, pending_actions
    import time
    import uuid

    action_id = str(uuid.uuid4())
    pending_actions[action_id] = {
        "command": "delete something",
        "created_at": time.time(),  # fresh — not expired
    }

    req = ConfirmRequest(action_id=action_id, confirm=True)
    result = confirm_action(req)

    assert result["status"] == "confirmed"
    # Must NOT imply the action was executed, scheduled, or queued
    msg = result["message"]
    assert "queued" not in msg.lower()
    assert "executed" not in msg.lower()
    assert "scheduled" not in msg.lower()
    assert "Confirmation recorded" in msg
    assert "not implemented" in msg
    assert action_id not in pending_actions


# Test 20: select_model("light") returns gemma2:2b
def test_select_model_light():
    from jarvis_core.ollama_service import select_model
    assert select_model("light") == "gemma2:2b"


# Test 21: select_model("heavy") returns qwen2.5:7b
def test_select_model_heavy():
    from jarvis_core.ollama_service import select_model
    assert select_model("heavy") == "qwen2.5:7b"


# Helper: check internet connectivity
def _has_internet() -> bool:
    try:
        socket.setdefaulttimeout(3)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("example.com", 443))
        return True
    except Exception:
        return False


# Test 22: Playwright reads https://example.com (skipped if no internet)
@pytest.mark.skipif(not _has_internet(), reason="No internet connection available")
def test_playwright_reads_example_com():
    from browser_tools import read_webpage
    result = read_webpage("https://example.com")
    assert result["status"] == "done"
    assert "url" in result
    assert "title" in result
    assert "content" in result
