import pathlib

APP_NAME = "Jarvis Local Agent - Phase 2"
OLLAMA_URL = "http://localhost:11434"

# ── Model Configuration ──────────────────────────────────────────────────────
LIGHT_MODEL = "gemma2:2b"
HEAVY_MODEL = "qwen2.5:7b"

# Additional Ollama models available for selection
AVAILABLE_OLLAMA_MODELS = [
    {"id": "gemma2:2b", "name": "Gemma 2 (2B)", "description": "Fast & lightweight", "category": "light"},
    {"id": "qwen2.5:7b", "name": "Qwen 2.5 (7B)", "description": "Balanced performance", "category": "medium"},
    {"id": "llama3.2:3b", "name": "Llama 3.2 (3B)", "description": "Efficient & capable", "category": "light"},
    {"id": "llama3.1:8b", "name": "Llama 3.1 (8B)", "description": "Strong reasoning", "category": "medium"},
    {"id": "mistral:7b", "name": "Mistral (7B)", "description": "Fast & accurate", "category": "medium"},
    {"id": "mixtral:8x7b", "name": "Mixtral (8x7B)", "description": "Heavy reasoning", "category": "heavy"},
    {"id": "deepseek-r1:7b", "name": "DeepSeek R1 (7B)", "description": "Deep reasoning", "category": "heavy"},
    {"id": "phi3:3.8b", "name": "Phi-3 (3.8B)", "description": "Microsoft efficient", "category": "light"},
    {"id": "nomic-embed-text:v1.5", "name": "Nomic Embed", "description": "Embeddings only", "category": "special"},
]

# Free API key mode configuration
FREE_API_KEY_ENABLED = True
FREE_API_KEY_URL = "https://api.openai.com/v1/chat/completions"  # Can be changed to any OpenAI-compatible API
FREE_API_KEY_MODEL = "gpt-4o-mini"  # Default model for API key mode

# Current active mode (persisted across requests)
ACTIVE_MODE = "auto"  # "auto", "light", "medium", "heavy", or a specific model ID, or "api_key"

BASE_DIR = pathlib.Path("C:/Project Opus")
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