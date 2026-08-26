import os
import pathlib

APP_NAME = "Jarvis Local Agent - Phase 2"

# ── Cross-platform filesystem configuration ──────────────────────────────────
# All runtime locations are derived from BASE_DIR so the SAME repository runs
# unchanged on Windows (C:\\Users\\...\\Jarvis) and Linux/cloud (/opt/jarvis).
#
# Resolution order:
#   1. JARVIS_BASE_DIR environment variable (explicit override, e.g. a cloud
#      data volume such as /var/lib/jarvis)
#   2. The repository root (folder that contains this config.py's package),
#      which is where ui.html, notes/, config/, data/, etc. already live.
# Never hardcode drive letters or /opt/jarvis anywhere else in the codebase.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE_DIR = pathlib.Path(os.environ.get("JARVIS_BASE_DIR") or REPO_ROOT).resolve()

USER_HOME = pathlib.Path.home()
ONEDRIVE_DIR = USER_HOME / "OneDrive"
NOTES_DIR = BASE_DIR / "notes"
SCREENSHOT_DIR = BASE_DIR / "screenshots"
LOGS_DIR = BASE_DIR / "logs"
UI_FILE = BASE_DIR / "ui.html"
OLLAMA_ROBOT_IMAGE = BASE_DIR / "assets" / "ollama-robot.png"
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
JOBS_JD_DIR = BASE_DIR / "jobs_jd"
GMAIL_DRAFTS_DIR = NOTES_DIR / "gmail_drafts"
WEB_SCREENSHOT_DIR = BASE_DIR / "web_screenshots"

NOTES_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
# LOGS_DIR is intentionally NOT created here — logging_config.py handles it

# ── Ollama Configuration (environment-overridable for cloud deployments) ─────
# Defaults stay byte-for-byte compatible with the previous hardcoded values.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
LIGHT_MODEL = os.environ.get("JARVIS_LIGHT_MODEL", "gemma2:2b")
HEAVY_MODEL = os.environ.get("JARVIS_HEAVY_MODEL", "qwen2.5:7b")

# Additional Ollama models available for selection
AVAILABLE_OLLAMA_MODELS = [
    {"id": "gemma2:2b", "name": "Gemma 2 (2B)", "description": "Fast & lightweight", "category": "light"},
    {"id": "qwen2.5:7b", "name": "Qwen 2.5 (7B)", "description": "Balanced performance", "category": "medium"},
    {"id": "qwen2.5-coder:3b", "name": "Qwen Coder (3B)", "description": "Small coding tasks", "category": "coding"},
    {"id": "qwen2.5-coder:7b", "name": "Qwen Coder (7B)", "description": "Implementation and debugging", "category": "coding"},
    {"id": "gemma4:12b", "name": "Gemma 4 (12B)", "description": "Quality validation only", "category": "quality"},
    {"id": "qwen3.6:latest", "name": "Qwen 3.6 (36B)", "description": "Manual Deep mode only", "category": "deep"},
]

# Free API key mode configuration
FREE_API_KEY_ENABLED = True
FREE_API_KEY_URL = "https://api.openai.com/v1/chat/completions"  # Can be changed to any OpenAI-compatible API
FREE_API_KEY_MODEL = "gpt-4o-mini"  # Default model for API key mode

# Current active mode (persisted across requests)
ACTIVE_MODE = "auto"  # "auto", "light", "medium", "heavy", or a specific model ID, or "api_key"

ACTION_EXPIRY_SECONDS = 300

SYSTEM_PROMPT = """
You are Jarvis, a local AI assistant running on the user's machine.
Use short and practical answers.
Default mode is lightweight because the machine may have limited RAM.
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

_ALLOWED_ROOTS = [
    BASE_DIR,
    USER_HOME / "Desktop",
    USER_HOME / "Documents",
    USER_HOME / "Downloads",
]
# OneDrive redirect folders only exist on some Windows machines; including
# them unconditionally is harmless on Windows but meaningless on Linux.
if ONEDRIVE_DIR.exists():
    _ALLOWED_ROOTS.extend([
        ONEDRIVE_DIR / "Desktop",
        ONEDRIVE_DIR / "Documents",
    ])
ALLOWED_ROOTS = _ALLOWED_ROOTS

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
