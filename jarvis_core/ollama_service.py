import logging
import requests
from jarvis_core.config import OLLAMA_URL, LIGHT_MODEL, HEAVY_MODEL, SYSTEM_PROMPT

logger = logging.getLogger("jarvis.ollama")


def select_model(mode: str) -> str:
    """Return the appropriate model based on mode string."""
    if mode.lower() in ["heavy", "deep", "reasoning"]:
        model = HEAVY_MODEL
    else:
        model = LIGHT_MODEL
    logger.info("Model selected: %s (mode=%s)", model, mode)
    return model


def ask_ollama(message: str, mode: str = "light") -> tuple:
    """
    Send a message to Ollama and return (content, model, error).

    Returns a 3-tuple:
        - content: str | None  — the response text, or None on error
        - model:   str         — the model that was (or would have been) used
        - error:   dict | None — error dict if something went wrong, else None
    """
    model = select_model(mode)

    payload = {
        "model": model,
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
            model,
            {
                "status": "error",
                "message": "Ollama is not running. Please start Ollama and try again.",
            },
        )
    except requests.exceptions.Timeout:
        logger.error("Ollama request timed out")
        return (
            None,
            model,
            {
                "status": "error",
                "message": "Ollama request timed out. The model may be overloaded.",
            },
        )

    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError:
        if response.status_code == 404:
            logger.error("Model '%s' is not installed (HTTP 404)", model)
            return (
                None,
                model,
                {
                    "status": "error",
                    "message": f"Model '{model}' is not installed. Run 'ollama pull {model}' to install it.",
                },
            )
        logger.error("Ollama returned HTTP %s", response.status_code)
        return (
            None,
            model,
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
            model,
            {
                "status": "error",
                "message": "Unexpected response from Ollama. Please try again.",
            },
        )

    return (content, model, None)
