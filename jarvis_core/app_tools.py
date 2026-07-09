import datetime
import logging
import subprocess
import webbrowser
from urllib.parse import quote_plus

from jarvis_core.config import ALLOWED_APPS, ALLOWED_FOLDERS, NOTES_DIR

logger = logging.getLogger("jarvis.app_tools")


def open_known_app(app_name: str) -> tuple[bool, str]:
    app_name = app_name.lower().strip()

    if app_name in ALLOWED_APPS:
        logger.info("Tool selected: open_known_app(%s)", app_name)
        subprocess.Popen(ALLOWED_APPS[app_name])
        logger.info("Tool completed: open_known_app(%s)", app_name)
        return True, f"Opened {app_name}."

    logger.info("open_known_app rejected unknown app: %s", app_name)
    return False, f"App not found in allowed list: {app_name}"


def open_known_folder(folder_name: str) -> tuple[bool, str]:
    folder_name = folder_name.lower().strip()

    if folder_name in ALLOWED_FOLDERS:
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
    webbrowser.open(url)
    logger.info("Google search: %s", query)
    return {"status": "done", "message": f"Searched Google for: {query}"}


def create_note(note_text: str) -> dict:
    logger.info("Tool selected: create_note")
    filename = datetime.datetime.now().strftime("note_%Y%m%d_%H%M%S.txt")
    file_path = NOTES_DIR / filename
    file_path.write_text(note_text, encoding="utf-8")
    logger.info("Tool completed: create_note, file: %s", file_path)
    return {"status": "done", "message": "Note created.", "file": str(file_path)}


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
