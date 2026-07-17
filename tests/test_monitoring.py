from datetime import date, datetime
from unittest.mock import patch

import pytest

from jarvis_core.monitoring import (
    ACTIVITY_CHECK_ENTRY, MonitoringService, activity_file,
    append_activity, append_alert_to_activity_tab, generate_schedule,
)


def test_15_minute_schedule():
    values = generate_schedule("09:00", "18:30", 15)
    assert values[0] == "09:15"
    assert values[-1] == "18:30"
    assert len(values) == 38


def test_30_minute_schedule():
    assert generate_schedule("09:00", "10:30", 30) == ["09:30", "10:00", "10:30"]


def test_60_minute_schedule():
    assert generate_schedule("09:00", "12:00", 60) == ["10:00", "11:00", "12:00"]


def test_invalid_interval_rejected():
    with pytest.raises(ValueError):
        generate_schedule("09:00", "18:30", 10)


def test_activity_path():
    with patch("jarvis_core.monitoring.NOTES_DIR") as notes:
        assert activity_file(date(2026, 7, 16)) == notes / "activity_2026-07-16.txt"


def test_activity_append_does_not_overwrite(tmp_path):
    with patch("jarvis_core.monitoring.NOTES_DIR", tmp_path):
        path = activity_file(date(2026, 7, 16))
        path.write_text("existing\n", encoding="utf-8")
        append_activity("new work", datetime(2026, 7, 16, 15, 45))
        content = path.read_text(encoding="utf-8")
    assert content.startswith("existing\n")
    assert "[3:45 PM]" in content
    assert "new work" in content


def test_trigger_plays_sound_and_opens_fixed_notepad(tmp_path):
    service = MonitoringService()
    with patch("jarvis_core.monitoring.NOTES_DIR", tmp_path), \
         patch("jarvis_core.monitoring.play_sound") as sound, \
         patch("jarvis_core.monitoring.subprocess.Popen") as opened:
        result = service.trigger()
    assert result["status"] == "done"
    sound.assert_called_once_with("soft")
    assert opened.call_args.args[0][0] == "notepad.exe"
    content = (tmp_path / datetime.now().strftime("activity_%Y-%m-%d.txt")).read_text(
        encoding="utf-8"
    )
    assert f"{ACTIVITY_CHECK_ENTRY}\n\n" in content


def test_monitoring_alerts_have_writing_space_and_blank_separator(tmp_path):
    with patch("jarvis_core.monitoring.NOTES_DIR", tmp_path):
        append_activity(ACTIVITY_CHECK_ENTRY, datetime(2026, 7, 16, 20, 15))
        path = append_activity(ACTIVITY_CHECK_ENTRY, datetime(2026, 7, 16, 20, 30))

    assert path.read_text(encoding="utf-8") == (
        "[8:15 PM]\n"
        "Activity check opened. Add your update below.\n"
        "{}\n\n"
        "[8:30 PM]\n"
        "Activity check opened. Add your update below.\n"
        "{}\n\n"
    )


def test_existing_activity_alert_is_appended_and_saved_inside_verified_notepad(tmp_path):
    path = tmp_path / "activity_2026-07-17.txt"
    path.write_text("existing", encoding="utf-8")
    window = type("Window", (), {
        "title": "*activity_2026-07-17.txt - Notepad",
        "isMinimized": False,
        "left": 10, "top": 20, "width": 800, "height": 600,
        "activate": lambda self: None,
    })()
    with patch("pygetwindow.getAllWindows", return_value=[window]), \
         patch("pygetwindow.getActiveWindow", return_value=window), \
         patch("pyautogui.click"), \
         patch("pyautogui.hotkey") as hotkey, \
         patch("pyautogui.press") as press, \
         patch("pyperclip.paste", return_value="original clipboard"), \
         patch("pyperclip.copy") as copy, \
         patch("jarvis_core.monitoring.time.sleep"):
        appended, warning = append_alert_to_activity_tab(
            path, datetime(2026, 7, 17, 13, 15)
        )

    assert appended is True
    assert warning is None
    assert [call.args for call in hotkey.call_args_list] == [
        ("ctrl", "end"), ("ctrl", "v"), ("ctrl", "s"),
    ]
    press.assert_called_once_with("enter", presses=2, interval=0.05)
    assert copy.call_args_list[0].args == (
        "[1:15 PM]\nActivity check opened. Add your update below.\n{}",
    )
    assert copy.call_args_list[-1].args == ("original clipboard",)


def test_activity_alert_never_types_when_wrong_window_is_active(tmp_path):
    path = tmp_path / "activity_2026-07-17.txt"
    path.write_text("existing", encoding="utf-8")
    window = type("Window", (), {
        "title": "activity_2026-07-17.txt - Notepad",
        "isMinimized": False,
        "activate": lambda self: None,
    })()
    wrong_window = type("Window", (), {"title": "Email - Browser"})()
    with patch("pygetwindow.getAllWindows", return_value=[window]), \
         patch("pygetwindow.getActiveWindow", return_value=wrong_window), \
         patch("pyautogui.click"), patch("pyautogui.hotkey") as hotkey, \
         patch("jarvis_core.monitoring.time.sleep"):
        appended, warning = append_alert_to_activity_tab(path, datetime.now())

    assert appended is False
    assert "not the expected activity tab" in warning
    hotkey.assert_not_called()


def test_packaged_notepad_activate_error_uses_verified_window_handle(tmp_path):
    path = tmp_path / "activity_2026-07-17.txt"
    path.write_text("existing", encoding="utf-8")
    window = type("Window", (), {
        "title": "activity_2026-07-17.txt - Notepad",
        "isMinimized": False,
        "_hWnd": 1234,
        "left": 100, "top": 200, "width": 800, "height": 600,
        "activate": lambda self: (_ for _ in ()).throw(RuntimeError("error code 0")),
    })()
    with patch("pygetwindow.getAllWindows", return_value=[window]), \
         patch("pygetwindow.getActiveWindow", return_value=window), \
         patch("jarvis_core.monitoring._request_foreground", return_value=True) as foreground, \
         patch("pyautogui.click"), patch("pyautogui.hotkey"), \
         patch("pyautogui.press"), patch("pyperclip.paste", return_value="old"), \
         patch("pyperclip.copy"), patch("jarvis_core.monitoring.time.sleep"):
        appended, warning = append_alert_to_activity_tab(path, datetime.now())

    assert appended is True
    assert warning is None
    foreground.assert_called_once_with(window)


def test_closed_activity_file_waits_for_notepad_tab_before_appending(tmp_path):
    path = tmp_path / "activity_2026-07-17.txt"
    path.write_text("existing", encoding="utf-8")
    window = type("Window", (), {
        "title": "activity_2026-07-17.txt - Notepad",
        "isMinimized": False,
        "left": 10, "top": 20, "width": 800, "height": 600,
        "activate": lambda self: None,
    })()
    with patch("pygetwindow.getAllWindows", side_effect=[[], [], [window]]), \
         patch("pygetwindow.getActiveWindow", return_value=window), \
         patch("pyautogui.click"), patch("pyautogui.hotkey"), patch("pyautogui.press"), \
         patch("pyperclip.paste", return_value="old"), patch("pyperclip.copy"), \
         patch("jarvis_core.monitoring.subprocess.Popen") as opened, \
         patch("jarvis_core.monitoring.time.sleep"), \
         patch("jarvis_core.monitoring.time.monotonic", side_effect=[0, 0.1, 0.2]):
        appended, warning = append_alert_to_activity_tab(path, datetime.now())

    assert appended is True
    assert warning is None
    opened.assert_called_once_with(["notepad.exe", str(path)])


def test_existing_activity_uses_notepad_instead_of_external_disk_append(tmp_path):
    service = MonitoringService()
    path = tmp_path / datetime.now().strftime("activity_%Y-%m-%d.txt")
    path.write_text("saved update\n", encoding="utf-8")
    with patch("jarvis_core.monitoring.NOTES_DIR", tmp_path), \
         patch("jarvis_core.monitoring.play_sound"), \
         patch("jarvis_core.monitoring.append_alert_to_activity_tab", return_value=(True, None)) as append_tab, \
         patch("jarvis_core.monitoring.subprocess.Popen") as opened:
        result = service.trigger()

    append_tab.assert_called_once()
    assert append_tab.call_args.args[0] == path
    opened.assert_not_called()
    assert result["status"] == "done"
    assert path.read_text(encoding="utf-8") == "saved update\n"


def test_pause_resume_stop():
    service = MonitoringService()
    with patch.object(service, "_run"):
        assert service.start("09:00", "18:30", 15, "double")["status"] == "running"
        assert service.pause()["status"] == "paused"
        assert service.resume()["status"] == "running"
        assert service.stop()["status"] == "stopped"
