import pathlib

APP_NAME = "Jarvis Local Agent - Phase 2"
OLLAMA_URL = "http://localhost:11434"

LIGHT_MODEL = "gemma2:2b"
HEAVY_MODEL = "qwen2.5:7b"

BASE_DIR = pathlib.Path("C:/Jarvis")
USER_HOME = pathlib.Path.home()
ONEDRIVE_DIR = USER_HOME / "OneDrive"
NOTES_DIR = BASE_DIR / "notes"
SCREENSHOT_DIR = BASE_DIR / "screenshots"
LOGS_DIR = BASE_DIR / "logs"
UI_FILE = BASE_DIR / "ui.html"
OLLAMA_ROBOT_IMAGE = BASE_DIR / "assets" / "ollama-robot.png"

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
    USER_HOME / "Desktop",
    USER_HOME / "Documents",
    USER_HOME / "Downloads",
    ONEDRIVE_DIR / "Desktop",
    ONEDRIVE_DIR / "Documents",
]

ALLOWED_EXTENSIONS = [".txt", ".md", ".py", ".json", ".csv", ".log"]

# ── Voice Input (Phase 4) ─────────────────────────────────────────────────
VOICE_ENABLED: bool = True
VOICE_MODEL: str = "base.en"
VOICE_DEVICE: str = "cpu"
VOICE_COMPUTE_TYPE: str = "int8"
VOICE_LANGUAGE: str = "en"
VOICE_BEAM_SIZE: int = 1
VOICE_VAD_FILTER: bool = True
VOICE_MAX_DURATION_SECONDS: int = 15
VOICE_MAX_UPLOAD_MB: int = 10
VOICE_IDLE_UNLOAD_SECONDS: int = 120
WHISPER_MODEL_DIR: pathlib.Path = BASE_DIR / "models" / "whisper"

WHISPER_MODEL_DIR.mkdir(parents=True, exist_ok=True)

# ── Action Engine (Phase 4.5) ─────────────────────────────────────────────
CONFIDENCE_THRESHOLD: float = 0.7
ACTION_ENGINE_ENABLED: bool = True
ENGINE_USE_OLLAMA_FALLBACK: bool = True
ENGINE_MAX_STEPS: int = 5
