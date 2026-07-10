import logging
import re
import time
import uuid
import webbrowser

from jarvis_core.config import DANGEROUS_WORDS, ACTION_EXPIRY_SECONDS
from jarvis_core.models import CommandRequest, ConfirmRequest
from jarvis_core.app_tools import (
    open_known_app, open_known_folder, google_search,
    create_note, list_notes, list_models,
)
from jarvis_core.file_tools import read_text_file
from jarvis_core.system_tools import get_system_status, take_screenshot
from jarvis_core.ollama_service import ask_ollama
from browser_tools import read_webpage, screenshot_webpage
from calculator_tools import open_calculator_and_calculate

logger = logging.getLogger("jarvis.dispatcher")

pending_actions: dict = {}


def _is_expired(action: dict) -> bool:
    return (time.time() - action["created_at"]) > ACTION_EXPIRY_SECONDS


def confirm_action(req: ConfirmRequest) -> dict:
    action = pending_actions.get(req.action_id)
    if not action:
        return {"status": "not_found", "message": "No pending action found."}
    if _is_expired(action):
        pending_actions.pop(req.action_id, None)
        return {"status": "expired", "message": "Action has expired. Please reissue the command."}
    if not req.confirm:
        pending_actions.pop(req.action_id, None)
        return {"status": "cancelled", "message": "Action cancelled."}
    original_command = action["command"]
    pending_actions.pop(req.action_id, None)
    return {
        "status": "confirmed",
        "message": "Confirmation recorded, but execution of this protected action is not implemented in the current safe version.",
        "command": original_command,
    }


def run_agent_command(req: CommandRequest) -> dict:
    text = req.command.strip()
    cmd = text.lower()
    logger.info("Command received: %s", text)

    # --- Calculator: combined open+calculate (must come before generic "open calculator") ---
    if "calculator" in cmd and ("calculate" in cmd or "execute" in cmd):
        match = re.search(r"(?:calculate|execute)\s+(.+)$", text, re.IGNORECASE)
        if not match:
            return {"status": "error", "message": "Please include a calculation after the word calculate or execute."}
        expression = match.group(1).strip()
        logger.info("Tool selected: open_calculator_and_calculate")
        try:
            result = open_calculator_and_calculate(expression)
            logger.info("Tool completed: open_calculator_and_calculate, result: %s", result)
            return {
                "status": "done",
                "message": "Calculator opened and calculation executed.",
                "expression": expression,
                "result": result,
            }
        except ZeroDivisionError:
            return {"status": "error", "message": "Division by zero is not allowed."}
        except Exception as error:
            logger.error("Calculator error: %s", error)
            return {"status": "error", "message": str(error)}

    # --- Browser: read webpage ---
    if cmd.startswith("read webpage "):
        url = text[len("read webpage "):].strip()
        logger.info("Tool selected: read_webpage")
        try:
            return read_webpage(url)
        except Exception as error:
            logger.error("read_webpage failed: %s", error)
            return {"status": "error", "message": str(error)}

    # --- Browser: website screenshot (two prefixes) ---
    if cmd.startswith("website screenshot "):
        url = text[len("website screenshot "):].strip()
        logger.info("Tool selected: screenshot_webpage")
        try:
            return screenshot_webpage(url)
        except Exception as error:
            logger.error("screenshot_webpage failed: %s", error)
            return {"status": "error", "message": str(error)}

    if cmd.startswith("take website screenshot "):
        url = text[len("take website screenshot "):].strip()
        logger.info("Tool selected: screenshot_webpage")
        try:
            return screenshot_webpage(url)
        except Exception as error:
            logger.error("screenshot_webpage failed: %s", error)
            return {"status": "error", "message": str(error)}

    # --- Dangerous command confirmation ---
    if any(word in cmd for word in DANGEROUS_WORDS):
        action_id = str(uuid.uuid4())
        pending_actions[action_id] = {"command": req.command, "created_at": time.time()}
        logger.info("Dangerous command queued for confirmation: %s", text)
        return {
            "status": "confirmation_required",
            "action_id": action_id,
            "message": "This action needs your confirmation before Jarvis can continue.",
        }

    # --- System status ---
    if cmd in ["system status", "status", "pc status", "laptop status"]:
        logger.info("Tool selected: get_system_status")
        return {"status": "done", "message": "System status fetched.", "system": get_system_status()}

    # --- Screenshot ---
    if cmd in ["take screenshot", "screenshot", "capture screen"]:
        logger.info("Tool selected: take_screenshot")
        file_path = take_screenshot()
        return {"status": "done", "message": "Screenshot captured.", "file": file_path}

    # --- Open app (generic prefix) ---
    if cmd.startswith("open app "):
        app_name = text.replace("open app", "", 1).strip()
        success, message = open_known_app(app_name)
        return {"status": "done" if success else "error", "message": message}

    # --- Open folder (generic prefix) ---
    if cmd.startswith("open folder "):
        folder_name = text.replace("open folder", "", 1).strip()
        success, message = open_known_folder(folder_name)
        return {"status": "done" if success else "error", "message": message}

    # --- Named app shortcuts (all route through open_known_app) ---
    if cmd in ["open notepad", "start notepad"]:
        success, message = open_known_app("notepad")
        return {"status": "done" if success else "error", "message": message}

    if cmd in ["open calculator", "open calc", "start calculator"]:
        success, message = open_known_app("calculator")
        return {"status": "done" if success else "error", "message": message}

    # --- Named folder shortcuts ---
    if cmd in ["open downloads", "open downloads folder"]:
        success, message = open_known_folder("downloads")
        return {"status": "done" if success else "error", "message": message}

    if cmd in ["open desktop", "open desktop folder"]:
        success, message = open_known_folder("desktop")
        return {"status": "done" if success else "error", "message": message}

    if cmd in ["open documents", "open documents folder"]:
        success, message = open_known_folder("documents")
        return {"status": "done" if success else "error", "message": message}

    if cmd in ["open jarvis", "open jarvis folder"]:
        success, message = open_known_folder("jarvis")
        return {"status": "done" if success else "error", "message": message}

    if cmd in ["open notes", "open notes folder"]:
        success, message = open_known_folder("notes")
        return {"status": "done" if success else "error", "message": message}

    if cmd in ["open screenshots", "open screenshots folder"]:
        success, message = open_known_folder("screenshots")
        return {"status": "done" if success else "error", "message": message}

    # --- Open website ---
    if cmd.startswith("open website "):
        url = text.replace("open website", "", 1).strip()
        if not url.startswith("http"):
            url = "https://" + url
        webbrowser.open(url)
        return {"status": "done", "message": f"Opened website: {url}"}

    # --- Generic "open <domain>" shortcut ---
    if cmd.startswith("open "):
        target = text.replace("open", "", 1).strip()
        if "." in target and " " not in target:
            url = target
            if not url.startswith("http"):
                url = "https://" + url
            webbrowser.open(url)
            return {"status": "done", "message": f"Opened website: {url}"}

    # --- Google search (two prefixes, both use quote_plus via google_search()) ---
    if cmd.startswith("search google for "):
        query = text.replace("search google for", "", 1).strip()
        return google_search(query)

    if cmd.startswith("google "):
        query = text.replace("google", "", 1).strip()
        return google_search(query)

    # --- Notes ---
    if cmd.startswith("create note "):
        note_text = text.replace("create note", "", 1).strip()
        return create_note(note_text)

    if cmd in ["list notes", "show notes"]:
        return list_notes()

    # --- File reading ---
    if cmd.startswith("read file "):
        file_path_text = text.replace("read file", "", 1).strip()
        return read_text_file(file_path_text)

    # --- Ollama model list ---
    if cmd in ["list models", "ollama list"]:
        return list_models()

    # --- AI fallback ---
    logger.info("Tool selected: ask_ollama (fallback)")
    content, model, error = ask_ollama(
        "User said: " + req.command + "\nReply as Jarvis. Keep it short and useful.",
        req.mode,
    )
    if error:
        return error
    return {"status": "ai_response", "model": model, "reply": content}
