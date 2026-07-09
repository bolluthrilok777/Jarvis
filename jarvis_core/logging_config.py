import logging
from logging.handlers import RotatingFileHandler
from jarvis_core.config import LOGS_DIR


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("jarvis")
    if logger.handlers:
        return logger

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        filename=LOGS_DIR / "jarvis.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.addHandler(console_handler)

    return logger
