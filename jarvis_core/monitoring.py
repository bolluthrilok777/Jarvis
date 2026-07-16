"""Lightweight, server-side activity monitoring for Jarvis."""
from __future__ import annotations

import subprocess
import threading
import time
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path

from jarvis_core.config import NOTES_DIR

ALLOWED_INTERVALS = {15, 30, 60}
ALLOWED_SOUNDS = {"soft", "double", "alert", "gentle"}


def generate_schedule(start: str, end: str, interval: int) -> list[str]:
    if interval not in ALLOWED_INTERVALS:
        raise ValueError("Interval must be 15, 30, or 60 minutes.")
    start_time = datetime.strptime(start, "%H:%M")
    end_time = datetime.strptime(end, "%H:%M")
    if end_time <= start_time:
        raise ValueError("End time must be later than start time.")
    current = start_time + timedelta(minutes=interval)
    values = []
    while current <= end_time:
        values.append(current.strftime("%H:%M"))
        current += timedelta(minutes=interval)
    return values


def activity_file(day: date | None = None) -> Path:
    return NOTES_DIR / f"activity_{(day or date.today()).isoformat()}.txt"


def append_activity(entry: str, when: datetime | None = None) -> Path:
    moment = when or datetime.now()
    path = activity_file(moment.date())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{moment.strftime('%I:%M %p').lstrip('0')}]\n{entry.strip()}\n\n")
    return path


def play_sound(sound: str) -> None:
    if sound not in ALLOWED_SOUNDS:
        raise ValueError("Unknown monitoring sound.")
    import winsound
    patterns = {
        "soft": [(700, 900)],
        "double": [(850, 350), (0, 150), (850, 350)],
        "alert": [(1050, 500), (800, 500), (1050, 500)],
        "gentle": [(550, 650), (700, 650)],
    }
    for frequency, duration in patterns[sound]:
        if frequency:
            winsound.Beep(frequency, duration)
        else:
            time.sleep(duration / 1000)


class MonitoringService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = {
            "status": "stopped", "start": "09:00", "end": "18:30",
            "interval": 15, "sound": "soft", "next_alert": None,
            "last_alert": None,
        }

    def snapshot(self) -> dict:
        with self._lock:
            state = dict(self._state)
        state["schedule"] = generate_schedule(state["start"], state["end"], state["interval"])
        state["activity_file"] = str(activity_file())
        return state

    def start(self, start: str, end: str, interval: int, sound: str) -> dict:
        schedule = generate_schedule(start, end, interval)
        if sound not in ALLOWED_SOUNDS:
            raise ValueError("Unknown monitoring sound.")
        self.stop()
        with self._lock:
            self._state.update(status="running", start=start, end=end,
                               interval=interval, sound=sound,
                               next_alert=self._next(schedule), last_alert=None)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="jarvis-monitor")
        self._thread.start()
        return self.snapshot()

    def pause(self) -> dict:
        with self._lock:
            if self._state["status"] == "running":
                self._state["status"] = "paused"
        return self.snapshot()

    def resume(self) -> dict:
        with self._lock:
            if self._state["status"] == "paused":
                self._state["status"] = "running"
        return self.snapshot()

    def stop(self) -> dict:
        self._stop.set()
        with self._lock:
            self._state["status"] = "stopped"
            self._state["next_alert"] = None
        return self.snapshot()

    def trigger(self) -> dict:
        with self._lock:
            sound = self._state["sound"]
        play_sound(sound)
        now = datetime.now()
        path = append_activity("Activity check opened. Add your update below.", now)
        try:
            subprocess.Popen(["notepad.exe", str(path)])
        except Exception:
            pass
        with self._lock:
            self._state["last_alert"] = now.isoformat()
        return {"status": "done", "activity_file": str(path), "timestamp": now.isoformat()}

    def _next(self, schedule: list[str]) -> str | None:
        now = datetime.now().strftime("%H:%M")
        return next((value for value in schedule if value > now), None)

    def _run(self) -> None:
        last_key = None
        while not self._stop.wait(1):
            with self._lock:
                state = dict(self._state)
            if state["status"] != "running":
                continue
            key = datetime.now().strftime("%Y-%m-%d %H:%M")
            schedule = generate_schedule(state["start"], state["end"], state["interval"])
            if key[-5:] in schedule and key != last_key:
                self.trigger()
                last_key = key
                with self._lock:
                    self._state["next_alert"] = self._next(schedule)


monitoring = MonitoringService()
