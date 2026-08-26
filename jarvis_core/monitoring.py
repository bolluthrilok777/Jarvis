"""Lightweight, server-side activity monitoring for Jarvis."""
from __future__ import annotations

import subprocess
import threading
import time
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path

from jarvis_core.config import NOTES_DIR
from jarvis_core.desktop_capabilities import DESKTOP_AVAILABLE

ALLOWED_INTERVALS = {15, 30, 60}
ALLOWED_SOUNDS = {"soft", "double", "alert", "gentle"}
ACTIVITY_CHECK_ENTRY = "Activity check opened. Add your update below.\n{}"


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


def _request_foreground(window) -> bool:
    """Safely request foreground focus for an already verified window handle."""
    try:
        import ctypes
        hwnd = int(getattr(window, "_hWnd", 0))
        if not hwnd:
            return False
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.keybd_event(0x12, 0, 0, 0)  # ALT grants foreground permission
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(0x12, 0, 0x0002, 0)
        return True
    except Exception:
        return False


def append_alert_to_activity_tab(
    path: Path, moment: datetime
) -> tuple[bool, str | None]:
    """Append an alert through the verified Notepad tab, avoiding disk conflicts."""
    if not DESKTOP_AVAILABLE:
        return False, "Desktop session unavailable; alert was written to the activity file only."
    try:
        import pyautogui
        import pygetwindow as gw

        def matching_windows():
            expected = path.name.lower()
            return [
                window for window in gw.getAllWindows()
                if expected in (getattr(window, "title", "") or "").lower()
                and "notepad" in (getattr(window, "title", "") or "").lower()
            ]

        windows = matching_windows()
        if not windows:
            # The activity file may be closed or an inactive tab. Opening the
            # same path activates it without modifying the file behind Notepad.
            subprocess.Popen(["notepad.exe", str(path)])
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                windows = matching_windows()
                if windows:
                    break
                time.sleep(0.25)
        if not windows:
            return False, "The existing activity tab could not be identified safely."

        window = windows[0]
        try:
            if getattr(window, "isMinimized", False):
                window.restore()
            window.activate()
        except Exception:
            # Packaged Windows 11 Notepad can report error code 0 from
            # activate(). Request foreground access only for the verified HWND.
            if not _request_foreground(window):
                return False, "The activity tab could not be focused safely."
        time.sleep(0.3)

        active = gw.getActiveWindow()
        active_title = (getattr(active, "title", "") or "").lower() if active else ""
        target_handle = getattr(window, "_hWnd", None)
        same_handle = bool(
            target_handle and active and getattr(active, "_hWnd", None) == target_handle
        )
        correct_title = path.name.lower() in active_title and "notepad" in active_title
        if not same_handle and not correct_title:
            return False, "The focused window was not the expected activity tab."

        # Put the caret in the editor area of the already verified window.
        pyautogui.click(
            window.left + window.width // 2,
            window.top + window.height // 2,
        )
        time.sleep(0.2)

        import pyperclip
        previous_clipboard = pyperclip.paste()
        block = (
            f"[{moment.strftime('%I:%M %p').lstrip('0')}]\n"
            f"{ACTIVITY_CHECK_ENTRY}"
        )
        try:
            pyautogui.hotkey("ctrl", "end")
            pyautogui.press("enter", presses=2, interval=0.05)
            pyperclip.copy(block)
            pyautogui.hotkey("ctrl", "v")
            pyautogui.hotkey("ctrl", "s")
            time.sleep(0.5)
        finally:
            pyperclip.copy(previous_clipboard)
        return True, None
    except Exception as exc:
        return False, f"Could not append the monitoring alert in Notepad: {exc}"


def play_sound(sound: str) -> None:
    if sound not in ALLOWED_SOUNDS:
        raise ValueError("Unknown monitoring sound.")
    try:
        import winsound
    except ImportError:
        # Headless node (no audio device / non-Windows): stay operational.
        time.sleep(0.2)
        return
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
        path = activity_file(now.date())
        warning = None
        if DESKTOP_AVAILABLE:
            if path.exists():
                _, warning = append_alert_to_activity_tab(path, now)
            else:
                path = append_activity(ACTIVITY_CHECK_ENTRY, now)
                try:
                    subprocess.Popen(["notepad.exe", str(path)])
                except Exception as exc:
                    warning = f"Could not open the activity file: {exc}"
        else:
            # Headless node: persist the prompt to the activity file directly.
            path = append_activity(ACTIVITY_CHECK_ENTRY, now)
            warning = "Headless node: activity check written to file instead of Notepad."
        with self._lock:
            self._state["last_alert"] = now.isoformat()
        result = {"status": "done", "activity_file": str(path), "timestamp": now.isoformat()}
        if warning:
            result["warning"] = warning
        return result

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
