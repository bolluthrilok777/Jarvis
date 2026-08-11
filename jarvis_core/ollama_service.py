import json
import logging
import os
import requests
from jarvis_core.config import (
    OLLAMA_URL, LIGHT_MODEL, HEAVY_MODEL, SYSTEM_PROMPT,
    AVAILABLE_OLLAMA_MODELS, FREE_API_KEY_ENABLED, FREE_API_KEY_URL,
    FREE_API_KEY_MODEL, ACTIVE_MODE,
)

logger = logging.getLogger("jarvis.ollama")

# In-memory store for the API key (set via UI)
_api_key = None
# In-memory store for the current active mode
_current_mode = ACTIVE_MODE


def get_active_mode() -> str:
    """Get the currently active mode."""
    return _current_mode


def set_active_mode(mode: str) -> None:
    """Set the currently active mode."""
    global _current_mode
    _current_mode = mode
    logger.info("Active mode set to: %s", mode)


def set_api_key(key: str) -> None:
    """Store the API key in memory."""
    global _api_key
    _api_key = key
    logger.info("API key set (length: %d chars)", len(key) if key else 0)


def get_api_key() -> str:
    """Get the stored API key."""
    return _api_key if _api_key else ""


def select_model(mode: str = None) -> str:
    """Return the appropriate model based on mode string.
    
    Supports:
    - "auto": Smart selection based on input complexity
    - "light": Use LIGHT_MODEL
    - "medium": Use HEAVY_MODEL (medium fallback)
    - "heavy": Use HEAVY_MODEL
    - Specific model ID (e.g. "llama3.2:3b"): Use that exact model
    - "api_key": Use the API key model (not Ollama)
    """
    if mode is None:
        mode = _current_mode
    
    mode_lower = mode.lower()
    
    if mode_lower == "auto":
        # Auto mode will be resolved at query time based on input
        return "auto"
    elif mode_lower == "light":
        return LIGHT_MODEL
    elif mode_lower in ("medium", "heavy", "deep", "reasoning"):
        return HEAVY_MODEL
    elif mode_lower == "api_key":
        return FREE_API_KEY_MODEL
    else:
        # Check if it's a specific model ID from our list
        for m in AVAILABLE_OLLAMA_MODELS:
            if m["id"] == mode_lower:
                return m["id"]
        # Fallback to light model
        logger.warning("Unknown mode '%s', falling back to light", mode)
        return LIGHT_MODEL


def resolve_auto_mode(message: str) -> str:
    """Smart selection: use heavy model for complex queries, light for simple ones."""
    complex_patterns = r'\b(analyze|compare|reason|strategy|detailed|complex|step by step|research|explain|summarize|write|code|program|debug|refactor|architecture|design|plan)\b'
    import re
    if re.search(complex_patterns, message, re.IGNORECASE) or len(message) > 220:
        return HEAVY_MODEL
    return LIGHT_MODEL


def ask_ollama(message: str, mode: str = None) -> tuple:
    """
    Send a message to Ollama and return (content, model, error).

    Returns a 3-tuple:
        - content: str | None  — the response text, or None on error
        - model:   str         — the model that was (or would have been) used
        - error:   dict | None — error dict if something went wrong, else None
    """
    if mode is None:
        mode = _current_mode
    
    # If API key mode, route to API key handler
    if mode.lower() == "api_key":
        return ask_api_key(message)
    
    # Resolve model
    selected = select_model(mode)
    
    # If auto mode, resolve based on input
    if selected == "auto":
        selected = resolve_auto_mode(message)
    
    payload = {
        "model": selected,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
        "stream": False,
        "keep_alive": "30s",
        "options": {
            "num_ctx": 2048,
            "temperature": 0.4,
        },
    }

    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json=payload,
            timeout=180,
        )
    except requests.exceptions.ConnectionError:
        logger.error("Ollama is not running (ConnectionError)")
        return (
            None,
            selected,
            {
                "status": "error",
                "message": "Ollama is not running. Please start Ollama and try again.",
            },
        )
    except requests.exceptions.Timeout:
        logger.error("Ollama request timed out")
        return (
            None,
            selected,
            {
                "status": "error",
                "message": "Ollama request timed out. The model may be overloaded.",
            },
        )

    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError:
        if response.status_code == 404:
            logger.error("Model '%s' is not installed (HTTP 404)", selected)
            return (
                None,
                selected,
                {
                    "status": "error",
                    "message": f"Model '{selected}' is not installed. Run 'ollama pull {selected}' to install it.",
                },
            )
        logger.error("Ollama returned HTTP %s", response.status_code)
        return (
            None,
            selected,
            {
                "status": "error",
                "message": f"Ollama returned an error (HTTP {response.status_code}). Please try again.",
            },
        )

    try:
        content = response.json()["message"]["content"]
    except KeyError:
        logger.error("Unexpected response structure from Ollama")
        return (
            None,
            selected,
            {
                "status": "error",
                "message": "Unexpected response from Ollama. Please try again.",
            },
        )

    return (content, selected, None)


def ask_api_key(message: str) -> tuple:
    """
    Send a message using the configured API key (OpenAI-compatible).
    Returns (content, model, error) tuple.
    """
    api_key = get_api_key()
    if not api_key:
        return (
            None,
            FREE_API_KEY_MODEL,
            {
                "status": "error",
                "message": "API key not configured. Please set your API key in the model settings.",
            },
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": FREE_API_KEY_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
        "temperature": 0.4,
        "max_tokens": 2048,
    }

    try:
        response = requests.post(
            FREE_API_KEY_URL,
            headers=headers,
            json=payload,
            timeout=60,
        )
    except requests.exceptions.ConnectionError:
        logger.error("API key endpoint not reachable")
        return (
            None,
            FREE_API_KEY_MODEL,
            {
                "status": "error",
                "message": "API endpoint is not reachable. Check your internet connection and API URL.",
            },
        )
    except requests.exceptions.Timeout:
        logger.error("API key request timed out")
        return (
            None,
            FREE_API_KEY_MODEL,
            {
                "status": "error",
                "message": "API request timed out. The service may be overloaded.",
            },
        )

    try:
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return (content, FREE_API_KEY_MODEL, None)
    except requests.exceptions.HTTPError as e:
        status = response.status_code
        if status == 401:
            msg = "Invalid API key. Please check your key and try again."
        elif status == 429:
            msg = "Rate limited. Please wait and try again."
        elif status == 500:
            msg = "API server error. Please try again later."
        else:
            msg = f"API returned HTTP {status}: {response.text[:200]}"
        logger.error("API key HTTP error: %s", msg)
        return (None, FREE_API_KEY_MODEL, {"status": "error", "message": msg})
    except (KeyError, json.JSONDecodeError) as e:
        logger.error("Unexpected API response: %s", e)
        return (
            None,
            FREE_API_KEY_MODEL,
            {
                "status": "error",
                "message": "Unexpected response from API. Please try again.",
            },
        )


def get_installed_ollama_models() -> list:
    """Fetch the list of installed models from Ollama."""
    try:
        response = requests.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        response.raise_for_status()
        models = response.json().get("models", [])
        return [m.get("name") for m in models]
    except Exception as e:
        logger.warning("Could not fetch installed models: %s", e)
        return []