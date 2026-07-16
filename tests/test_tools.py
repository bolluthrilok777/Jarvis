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


def test_open_calculator_reuses_existing_window():
    from jarvis_core.app_tools import open_known_app
    window = MagicMock(width=400, height=600, isMinimized=False)
    with patch("pygetwindow.getWindowsWithTitle", return_value=[window]), \
         patch("jarvis_core.app_tools.subprocess.Popen") as launch:
        success, message = open_known_app("calculator")
    assert success is True
    assert "Reused existing" in message
    window.activate.assert_called_once()
    launch.assert_not_called()


def test_open_calculator_closes_existing_duplicates():
    from jarvis_core.app_tools import open_known_app
    primary = MagicMock(width=400, height=600, isMinimized=False)
    duplicate = MagicMock(width=400, height=600, isMinimized=False)
    with patch("pygetwindow.getWindowsWithTitle", return_value=[primary, duplicate]), \
         patch("jarvis_core.app_tools.subprocess.Popen") as launch:
        success, _ = open_known_app("calculator")
    assert success is True
    primary.activate.assert_called_once()
    duplicate.close.assert_called_once()
    launch.assert_not_called()


def test_open_calculator_launches_only_when_no_window_exists():
    from jarvis_core.app_tools import open_known_app
    with patch("pygetwindow.getWindowsWithTitle", return_value=[]), \
         patch("jarvis_core.app_tools.subprocess.Popen") as launch:
        success, message = open_known_app("calculator")
    assert success is True
    assert message == "Opened calculator."
    launch.assert_called_once()


def test_open_named_file_location_searches_only_allowed_roots(tmp_path):
    from jarvis_core.skill_registry import _exec_file_open_containing
    resume = tmp_path / "Reddy Charan Resume.pdf"
    resume.write_bytes(b"pdf")
    with patch("jarvis_core.config.ALLOWED_ROOTS", [tmp_path]), \
         patch("subprocess.Popen") as opened:
        result = _exec_file_open_containing("reddy charan resume")
    assert result["status"] == "done"
    assert result["file"] == str(resume.resolve())
    opened.assert_called_once_with(["explorer.exe", f"/select,{resume.resolve()}"])


def test_file_locator_treats_resume_as_optional_descriptor(tmp_path):
    from jarvis_core.skill_registry import _exec_file_open_containing
    resume = tmp_path / "Reddy_Charan.pdf"
    resume.write_bytes(b"pdf")
    with patch("jarvis_core.config.ALLOWED_ROOTS", [tmp_path]), \
         patch("subprocess.Popen") as opened:
        result = _exec_file_open_containing("reddy charan resume")
    assert result["status"] == "done"
    assert result["file"] == str(resume.resolve())
    opened.assert_called_once()


def test_open_named_file_location_reports_missing(tmp_path):
    from jarvis_core.skill_registry import _exec_file_open_containing
    with patch("jarvis_core.config.ALLOWED_ROOTS", [tmp_path]), \
         patch("subprocess.Popen") as opened:
        result = _exec_file_open_containing("reddy charan resume")
    assert result["status"] == "error"
    assert "No allowed file matched" in result["message"]
    opened.assert_not_called()


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
    assert note_file.read_text(encoding="utf-8").endswith("Hello from test")
    assert result["topic"] == "hello_test"


def test_similar_notes_append_to_same_topic(tmp_path):
    from jarvis_core.app_tools import create_note

    with patch("jarvis_core.app_tools.NOTES_DIR", tmp_path):
        first = create_note("Project Jarvis calculator follow up")
        second = create_note("Jarvis calculator testing notes")

    assert first["file"] == second["file"]
    assert second["appended"] is True
    content = pathlib.Path(first["file"]).read_text(encoding="utf-8")
    assert "Project Jarvis calculator follow up" in content
    assert "Jarvis calculator testing notes" in content


def test_youtube_latest_opens_first_video():
    from jarvis_core.skill_registry import _exec_browser_youtube_play_latest

    response = MagicMock()
    response.text = '{"videoId":"abcdefghijk"}'
    response.raise_for_status.return_value = None
    with patch("requests.get", return_value=response) as request_get, \
         patch("jarvis_core.skill_registry.webbrowser.open") as browser_open:
        result = _exec_browser_youtube_play_latest("podcast")

    assert result["status"] == "done"
    assert "sp=CAI%253D" in request_get.call_args.args[0]
    browser_open.assert_called_once_with(
        "https://www.youtube.com/watch?v=abcdefghijk&autoplay=1"
    )


def test_calculator_gui_continues_after_packaged_app_activate_error():
    from calculator_tools import drive_calculator_gui

    window = MagicMock()
    window.isMinimized = False
    window.left, window.top, window.width, window.height = 10, 20, 400, 600
    window.activate.side_effect = RuntimeError("Windows error code 0")
    active = MagicMock()
    active.title = "Calculator"
    with patch("pygetwindow.getWindowsWithTitle", return_value=[window]), \
         patch("pygetwindow.getActiveWindow", return_value=active), \
         patch("pyautogui.click") as click, \
         patch("pyautogui.press") as press, \
         patch("pyautogui.hotkey") as hotkey, \
         patch("pyperclip.paste", side_effect=["old", "600"]), \
         patch("pyperclip.copy"), \
         patch("calculator_tools._time.sleep"):
        result = drive_calculator_gui("674 - 74")

    assert result["gui_ok"] is True
    click.assert_called_once_with(210, 30)
    hotkey.assert_any_call("ctrl", "v")
    press.assert_called_with("enter")


def test_youtube_research_saves_only_verified_metadata(tmp_path):
    from jarvis_core.skill_registry import _exec_browser_youtube_research
    payload = '\"videoId\":\"abcdefghijk\",\"title\":{\"runs\":[{\"text\":\"Ollama Tutorial\"}]}'
    response = MagicMock(text=payload)
    with patch("requests.get", return_value=response), \
         patch("webbrowser.open") as opened, \
         patch("jarvis_core.config.NOTES_DIR", tmp_path):
        result = _exec_browser_youtube_research("Ollama tutorials")
    assert result["status"] == "done"
    assert result["transcript_available"] is False
    assert "Transcript unavailable" in pathlib.Path(result["file"]).read_text(encoding="utf-8")
    opened.assert_called_once()


def test_calculator_gui_retries_focus_inside_verified_window():
    from calculator_tools import drive_calculator_gui

    window = MagicMock()
    window.isMinimized = False
    window.left, window.top, window.width, window.height = 10, 20, 400, 600
    unfocused = MagicMock(title="Jarvis")
    focused = MagicMock(title="Calculator")
    with patch("pygetwindow.getWindowsWithTitle", return_value=[window]), \
         patch("pygetwindow.getActiveWindow", side_effect=[unfocused, focused]), \
         patch("pyautogui.click") as click, \
         patch("pyautogui.press"), patch("pyautogui.hotkey"), \
         patch("pyperclip.paste", side_effect=["old", "600"]), patch("pyperclip.copy"), \
         patch("calculator_tools._time.sleep"):
        result = drive_calculator_gui("674 - 74")

    assert result["gui_ok"] is True
    assert click.call_args_list[-1].args == (210, 320)


def test_calculator_gui_ignores_stale_offscreen_windows():
    from calculator_tools import drive_calculator_gui

    stale = MagicMock(left=5000, top=100, width=700, height=900, isMinimized=False)
    visible = MagicMock(left=20, top=30, width=400, height=600, isMinimized=False)
    active = MagicMock(title="Calculator")
    metrics = {76: 0, 77: 0, 78: 1920, 79: 1080}
    with patch("pygetwindow.getWindowsWithTitle", return_value=[stale, visible]), \
         patch("pygetwindow.getActiveWindow", return_value=active), \
         patch("ctypes.windll.user32.GetSystemMetrics", side_effect=lambda key: metrics[key]), \
         patch("pyautogui.click") as click, patch("pyautogui.press"), \
         patch("pyautogui.hotkey"), patch("pyperclip.paste", side_effect=["old", "600"]), \
         patch("pyperclip.copy"), patch("calculator_tools._time.sleep"):
        result = drive_calculator_gui("674 - 74")

    assert result["gui_ok"] is True
    click.assert_called_once_with(220, 40)
    visible.activate.assert_called_once()
    stale.activate.assert_not_called()


def test_calculator_gui_accepts_secondary_monitor_window():
    from calculator_tools import drive_calculator_gui

    secondary = MagicMock(left=3338, top=111, width=502, height=791, isMinimized=False)
    active = MagicMock(title="Calculator")
    metrics = {76: 0, 77: 0, 78: 3840, 79: 2160}
    with patch("pygetwindow.getWindowsWithTitle", return_value=[secondary]), \
         patch("pygetwindow.getActiveWindow", return_value=active), \
         patch("ctypes.windll.user32.GetSystemMetrics", side_effect=lambda key: metrics[key]), \
         patch("pyautogui.click") as click, patch("pyautogui.press"), \
         patch("pyautogui.hotkey"), patch("pyperclip.paste", side_effect=["old", "1540"]), \
         patch("pyperclip.copy"), patch("calculator_tools._time.sleep"):
        result = drive_calculator_gui("895 + 645")

    assert result["gui_ok"] is True
    click.assert_called_once_with(3589, 121)
