"""Tests 9-13: System tools, calculator, and note creation."""
import pathlib
from unittest.mock import MagicMock, patch


# Test 9: get_system_status returns all 8 required keys
def test_system_status_keys():
    import psutil
    from jarvis_core.system_tools import get_system_status

    # Mock psutil to avoid actual CPU wait
    mock_mem = MagicMock()
    mock_mem.used = 8 * 1024 ** 3
    mock_mem.total = 16 * 1024 ** 3
    mock_mem.percent = 50.0

    mock_disk = MagicMock()
    mock_disk.used = 100 * 1024 ** 3
    mock_disk.total = 500 * 1024 ** 3
    mock_disk.percent = 20.0

    with patch("jarvis_core.system_tools.psutil.cpu_percent", return_value=25.0):
        with patch("jarvis_core.system_tools.psutil.virtual_memory", return_value=mock_mem):
            with patch("jarvis_core.system_tools.psutil.disk_usage", return_value=mock_disk):
                with patch("jarvis_core.system_tools.psutil.pids", return_value=list(range(100))):
                    result = get_system_status()

    required_keys = [
        "cpu_percent", "ram_used_gb", "ram_total_gb", "ram_percent",
        "disk_used_gb", "disk_total_gb", "disk_percent", "running_processes"
    ]
    for key in required_keys:
        assert key in result, f"Missing key: {key}"


# Test 10: safe_calculate("2+2") returns 4
def test_safe_calculate_addition():
    from calculator_tools import safe_calculate
    assert safe_calculate("2+2") == 4


# Test 11: safe_calculate("100/4") returns 25.0
def test_safe_calculate_division():
    from calculator_tools import safe_calculate
    assert safe_calculate("100/4") == 25.0


# Test 12: safe_calculate with eval-like expression raises ValueError
def test_safe_calculate_rejects_dangerous():
    import pytest
    from calculator_tools import safe_calculate
    with pytest.raises(ValueError):
        safe_calculate("__import__('os').system('calc')")


# Test 13: Note creation writes a .txt file with correct content
def test_note_creation(tmp_path):
    from unittest.mock import patch
    from jarvis_core.app_tools import create_note

    # Redirect NOTES_DIR to tmp_path for test isolation
    with patch("jarvis_core.app_tools.NOTES_DIR", tmp_path):
        result = create_note("Hello from test")

    assert result["status"] == "done"
    note_file = pathlib.Path(result["file"])
    assert note_file.exists()
    assert note_file.read_text(encoding="utf-8") == "Hello from test"
