import logging
import pathlib

from jarvis_core.config import ALLOWED_ROOTS, ALLOWED_EXTENSIONS

logger = logging.getLogger("jarvis.file")


def read_text_file(file_path_text: str) -> dict:
    """Read a text file with path traversal protection.

    Resolves the path fully before any checks so that ``..`` sequences and
    symlinks cannot be used to escape the allowed root directories.
    """
    raw = pathlib.Path(file_path_text.strip().strip('"'))

    try:
        resolved = raw.resolve()
    except Exception:
        return {"status": "blocked", "message": "Invalid path."}

    # Resolve each allowed root as well so comparisons are canonical.
    allowed = [r.resolve() for r in ALLOWED_ROOTS]
    if not any(resolved.is_relative_to(root) for root in allowed):
        logger.error("File access blocked (outside allowed roots): %s", resolved)
        return {
            "status": "blocked",
            "message": "Access denied. Jarvis can only read files in allowed directories.",
        }

    if not resolved.exists():
        return {"status": "error", "message": "File not found."}

    if resolved.suffix.lower() not in ALLOWED_EXTENSIONS:
        return {
            "status": "blocked",
            "message": "For safety, Jarvis can read only txt, md, py, json, csv, and log files in this version.",
        }

    if resolved.stat().st_size > 1024 * 1024:
        return {
            "status": "blocked",
            "message": "File is too large. Maximum allowed size is 1 MB.",
        }

    content = resolved.read_text(encoding="utf-8", errors="ignore")

    logger.info("File read successfully: %s", resolved)

    return {
        "status": "done",
        "file": str(resolved),
        "content": content[:5000],
    }
