from datetime import date, datetime
from unittest.mock import patch

import pytest

from jarvis_core.monitoring import (
    ACTIVITY_CHECK_ENTRY, MonitoringService, activity_file, append_activity,
    generate_schedule,
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
        "{\n\n}\n\n"
        "[8:30 PM]\n"
        "Activity check opened. Add your update below.\n"
        "{\n\n}\n\n"
    )


def test_pause_resume_stop():
    service = MonitoringService()
    with patch.object(service, "_run"):
        assert service.start("09:00", "18:30", 15, "double")["status"] == "running"
        assert service.pause()["status"] == "paused"
        assert service.resume()["status"] == "running"
        assert service.stop()["status"] == "stopped"
