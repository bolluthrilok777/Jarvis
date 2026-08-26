import datetime
import logging
import re
import subprocess
import time
import webbrowser
from urllib.parse import quote_plus

from jarvis_core.config import ALLOWED_APPS, ALLOWED_FOLDERS, NOTES_DIR
from jarvis_core.desktop_capabilities import DESKTOP_AVAILABLE

logger = logging.getLogger("jarvis.app_tools")

_UNSUPPORTED_MSG = (
    "This command needs a desktop session, which is not available on this "
    "node. Jarvis is running headless; all other features remain operational."
)


def open_known_app(app_name: str) -> tuple[bool, str]:
    app_name = app_name.lower().strip()

    if app_name in ALLOWED_APPS:
        if not DESKTOP_AVAILABLE:
            logger.info("open_known_app refused on headless node: %s", app_name)
            return False, _UNSUPPORTED_MSG
        logger.info("Tool selected: open_known_app(%s)", app_name)
        if app_name in ("calculator", "calc"):
            # Import shared helpers to avoid duplicate-launch
            from calculator_tools import (
                _is_calculator_running, _launch_calculator_once,
                _find_calculator_hwnd, _focus_hwnd,
            )
            if _is_calculator_running():
                # Reuse the existing window
                hwnd = _find_calculator_hwnd()
                if hwnd:
                    _focus_hwnd(hwnd)
                    logger.info("Reused existing Calculator window (hwnd=%s)", hwnd)
                    return True, "Reused existing calculator window."
            # Not running — launch once
            if _launch_calculator_once():
                import time
                time.sleep(1.0)  # give UWP window time to appear
                hwnd = _find_calculator_hwnd()
                if hwnd:
                    _focus_hwnd(hwnd)
                return True, "Opened calculator."
            return False, "Calculator could not be launched."

        if app_name == "task manager":
            if _focus_existing_window("Task Manager"):
                return True, "Reused existing Task Manager window."
            subprocess.Popen(ALLOWED_APPS[app_name])
            if _wait_for_window("Task Manager"):
                return True, "Opened task manager."
            return False, "Task Manager was launched but no window appeared."
        subprocess.Popen(ALLOWED_APPS[app_name])
        logger.info("Tool completed: open_known_app(%s)", app_name)
        return True, f"Opened {app_name}."

    logger.info("open_known_app rejected unknown app: %s", app_name)
    return False, f"App not found in allowed list: {app_name}"


def _focus_existing_calculator() -> bool:
    """Focus one existing Calculator window and avoid launching a duplicate."""
    try:
        import pygetwindow as gw
        windows = [
            window for window in gw.getWindowsWithTitle("Calculator")
            if getattr(window, "width", 0) > 0 and getattr(window, "height", 0) > 0
        ]
        if not windows:
            return False
        window = windows[0]
        # Calculator has no unsaved document state. Consolidate duplicates left
        # by earlier commands so future calculations visibly use one window.
        for duplicate in windows[1:]:
            try:
                duplicate.close()
            except Exception:
                logger.warning("Could not close a duplicate Calculator window")
        try:
            if getattr(window, "isMinimized", False):
                window.restore()
            window.activate()
        except Exception:
            # The GUI calculation helper performs stronger verified focusing.
            # The existing window must still be reused rather than duplicated.
            pass
        return True
    except Exception as exc:
        logger.warning("Could not inspect existing Calculator windows: %s", exc)
        return False


def _focus_existing_window(title: str) -> bool:
    """Restore and focus a visible window with the requested title."""
    try:
        import pygetwindow as gw
        windows = [
            window for window in gw.getWindowsWithTitle(title)
            if getattr(window, "width", 0) > 0 and getattr(window, "height", 0) > 0
        ]
        if not windows:
            return False
        window = windows[0]
        if getattr(window, "isMinimized", False):
            window.restore()
        window.activate()
        return True
    except Exception as exc:
        logger.warning("Could not focus %s window: %s", title, exc)
        return False


def _wait_for_window(title: str, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _focus_existing_window(title):
            return True
        time.sleep(0.25)
    return False


def _wait_for_calculator_window(timeout: float = 8.0) -> bool:
    """Wait for any Calculator window to appear.

    The UWP Windows Calculator (calc.exe on Win10/11) may expose its window
    under the title 'Calculator' only after a short delay, or pygetwindow
    may not detect it at all on some builds.  We therefore also accept a
    successful Popen launch as "good enough" after a reasonable wait.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Try pygetwindow first (works on most machines)
        if _focus_existing_calculator():
            time.sleep(0.8)
            _focus_existing_calculator()
            return True
        # Fallback: check for any process named 'Calculator' via tasklist
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq CalculatorApp.exe", "/NH"],
                capture_output=True, text=True, timeout=2,
            )
            if "CalculatorApp" in result.stdout:
                time.sleep(1.0)  # give UWP window time to fully render
                _focus_existing_calculator()
                return True
        except Exception:
            pass
        time.sleep(0.25)
    # Last resort: if we reach timeout, assume the launch succeeded anyway
    # (UWP apps often appear visible to the user but invisible to pygetwindow)
    return True


def open_known_folder(folder_name: str) -> tuple[bool, str]:
    folder_name = folder_name.lower().strip()

    if folder_name in ALLOWED_FOLDERS:
        if not DESKTOP_AVAILABLE:
            logger.info("open_known_folder refused on headless node: %s", folder_name)
            return False, _UNSUPPORTED_MSG
        logger.info("Tool selected: open_known_folder(%s)", folder_name)
        subprocess.Popen(["explorer", str(ALLOWED_FOLDERS[folder_name])])
        logger.info("Tool completed: open_known_folder(%s)", folder_name)
        return True, f"Opened {folder_name} folder."

    logger.info("open_known_folder rejected unknown folder: %s", folder_name)
    return False, f"Folder not found: {folder_name}"


def google_search(query: str) -> dict:
    logger.info("Tool selected: google_search")
    encoded = quote_plus(query)
    url = f"https://www.google.com/search?q={encoded}"
    try:
        subprocess.Popen(f'start "" "{url}"', shell=True)
    except Exception:
        webbrowser.open(url)
    logger.info("Google search: %s", query)
    return {"status": "done", "message": f"Searched Google for: {query}"}


def create_note(note_text: str) -> dict:
    logger.info("Tool selected: create_note")
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    keywords = _note_keywords(note_text)
    file_path = _find_similar_note(keywords)
    created = file_path is None
    if created:
        topic = "_".join(keywords[:3]) if keywords else "general_notes"
        file_path = NOTES_DIR / f"{topic}.txt"
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"[{timestamp}] {note_text.strip()}"
    with file_path.open("a", encoding="utf-8") as handle:
        if file_path.stat().st_size:
            handle.write("\n")
        handle.write(entry)
    logger.info("Tool completed: create_note, file: %s", file_path)
    action = "created" if created else "updated"
    return {
        "status": "done", "message": f"Topic note {action}.",
        "file": str(file_path), "topic": file_path.stem, "appended": not created,
    }


_NOTE_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "for", "from", "in",
    "is", "it", "my", "of", "on", "or", "that", "the", "this", "to",
    "was", "with", "note", "notes", "down", "remember",
}


def _note_keywords(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return list(dict.fromkeys(w for w in words if len(w) > 2 and w not in _NOTE_STOP_WORDS))


def _find_similar_note(keywords: list[str]):
    if not keywords:
        fallback = NOTES_DIR / "general_notes.txt"
        return fallback if fallback.exists() else None
    wanted = set(keywords)
    best_path = None
    best_score = 0.0
    for path in NOTES_DIR.glob("*.txt"):
        try:
            sample = f"{path.stem} {path.read_text(encoding='utf-8')[-4000:]}"
        except OSError:
            continue
        existing = set(_note_keywords(sample))
        score = len(wanted & existing) / max(1, min(len(wanted), len(existing)))
        if score > best_score:
            best_path, best_score = path, score
    return best_path if best_score >= 0.5 else None


def list_notes() -> dict:
    logger.info("Tool selected: list_notes")
    notes = sorted(NOTES_DIR.glob("*.txt"), reverse=True)
    logger.info("Tool completed: list_notes, count: %d", len(notes))
    return {
        "status": "done",
        "message": "Notes listed.",
        "notes": [str(note) for note in notes[:20]],
    }


def list_models() -> dict:
    logger.info("Tool selected: list_models")
    result = subprocess.run(["ollama", "list"], capture_output=True, text=True)
    logger.info("Tool completed: list_models")
    return {"status": "done", "message": result.stdout}
