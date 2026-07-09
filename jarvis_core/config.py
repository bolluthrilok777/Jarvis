import pathlib

APP_NAME = "Jarvis Local Agent - Phase 2"
OLLAMA_URL = "http://localhost:11434"

LIGHT_MODEL = "gemma2:2b"
HEAVY_MODEL = "qwen2.5:7b"

BASE_DIR = pathlib.Path("C:/Jarvis")
NOTES_DIR = BASE_DIR / "notes"
SCREENSHOT_DIR = BASE_DIR / "screenshots"
LOGS_DIR = BASE_DIR / "logs"
UI_FILE = BASE_DIR / "ui.html"

NOTES_DIR.mkdir(exist_ok=True)
SCREENSHOT_DIR.mkdir(exist_ok=True)
# LOGS_DIR is intentionally NOT created here — logging_config.py handles it

ACTION_EXPIRY_SECONDS = 300

SYSTEM_PROMPT = """
You are Jarvis, a local AI assistant running on a Windows laptop.
Use short and practical answers.
Default mode is lightweight because the laptop has 16GB RAM.
Use heavy reasoning only when requested.
Ask confirmation before dangerous actions like deleting files, sending emails, submitting forms, booking slots, payments, or server changes.
"""

DANGEROUS_WORDS = [
    "delete", "remove", "send email", "payment", "pay",
    "submit", "book slot", "server change", "production",
    "format", "shutdown", "restart", "erase"
]

ALLOWED_APPS = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "paint": "mspaint.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "powershell": "powershell.exe",
    "task manager": "taskmgr.exe",
    "explorer": "explorer.exe",
    "chrome": "chrome.exe",
    "edge": "msedge.exe"
}

ALLOWED_FOLDERS = {
    "downloads": pathlib.Path.home() / "Downloads",
    "desktop": pathlib.Path.home() / "Desktop",
    "documents": pathlib.Path.home() / "Documents",
    "pictures": pathlib.Path.home() / "Pictures",
    "jarvis": BASE_DIR,
    "notes": NOTES_DIR,
    "screenshots": SCREENSHOT_DIR
}

ALLOWED_ROOTS = [
    BASE_DIR,
    pathlib.Path.home() / "Desktop",
    pathlib.Path.home() / "Documents",
    pathlib.Path.home() / "Downloads",
]

ALLOWED_EXTENSIONS = [".txt", ".md", ".py", ".json", ".csv", ".log"]
