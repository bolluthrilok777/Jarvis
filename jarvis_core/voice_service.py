"""
jarvis_core/voice_service.py — Phase 4 Local Voice Transcription

Provides lazy-loading Whisper model management, audio validation,
and transcription for the Jarvis push-to-talk voice feature.

Public API:
    transcribe_audio(audio_bytes, content_type) -> dict
    get_voice_status() -> dict
    unload_model() -> dict
"""

import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from faster_whisper import WhisperModel

from jarvis_core.config import (
    VOICE_ENABLED,
    VOICE_MODEL,
    VOICE_DEVICE,
    VOICE_COMPUTE_TYPE,
    VOICE_LANGUAGE,
    VOICE_BEAM_SIZE,
    VOICE_VAD_FILTER,
    VOICE_MAX_UPLOAD_MB,
    VOICE_IDLE_UNLOAD_SECONDS,
    WHISPER_MODEL_DIR,
)

logger = logging.getLogger("jarvis.voice")

# ── Task 3.1: Custom exceptions ───────────────────────────────────────────────


class VoiceMimeError(ValueError):
    """Raised when the uploaded audio has an unsupported MIME type."""


class VoiceEmptyError(ValueError):
    """Raised when the uploaded audio has zero bytes."""


class VoiceSizeError(ValueError):
    """Raised when the uploaded audio exceeds the configured size limit."""


# ── Task 3.1: Module-level state ──────────────────────────────────────────────

# NOTE: audio/webm and audio/mp4 are intentionally excluded because the patched
# faster_whisper/audio.py uses soundfile (not PyAV), which supports OGG, WAV,
# and FLAC only.  Browsers send OGG when the codec is Opus, which is the safe
# container to request from MediaRecorder.
SAFE_MIME_TYPES: frozenset = frozenset({
    "audio/ogg",
    "audio/wav",
    "audio/x-wav",
    "application/octet-stream",
})

MAX_BYTES: int = VOICE_MAX_UPLOAD_MB * 1024 * 1024

_model: Optional[WhisperModel] = None      # protected by _lock
_lock: threading.Lock = threading.Lock()
_last_used: float = 0.0
_idle_timer: Optional[threading.Timer] = None


# ── Task 3.3: Idle unload helpers ─────────────────────────────────────────────


def _reschedule_idle_timer() -> None:
    """Cancel any pending idle timer and schedule a new one.

    MUST be called while _lock is held.
    """
    global _idle_timer
    if _idle_timer is not None:
        _idle_timer.cancel()
    _idle_timer = threading.Timer(VOICE_IDLE_UNLOAD_SECONDS, _idle_unload)
    _idle_timer.daemon = True
    _idle_timer.start()


def _idle_unload() -> None:
    """Unload the Whisper model after the idle timeout fires."""
    global _model, _idle_timer
    with _lock:
        if _model is not None:
            _model = None
            _idle_timer = None
            logger.info("Voice: Whisper model unloaded (idle timeout)")


# ── Task 3.2: Lazy model loader ───────────────────────────────────────────────


def _get_or_load_model() -> WhisperModel:
    """Return the loaded WhisperModel, loading it on first call.

    Thread-safe: only one thread will ever call WhisperModel(...).
    _reschedule_idle_timer() is called inside the lock on every successful
    access so that the timeout resets each time the model is used.
    """
    global _model, _last_used
    with _lock:
        if _model is None:
            logger.info(
                "Voice: loading Whisper model '%s' (%s %s)",
                VOICE_MODEL,
                VOICE_DEVICE,
                VOICE_COMPUTE_TYPE,
            )
            _model = WhisperModel(
                VOICE_MODEL,
                device=VOICE_DEVICE,
                compute_type=VOICE_COMPUTE_TYPE,
                download_root=str(WHISPER_MODEL_DIR),
            )
            logger.info("Voice: Whisper model loaded")
        _last_used = time.monotonic()
        _reschedule_idle_timer()
        return _model


# ── Task 3.4 + 3.5: MIME → extension mapping ─────────────────────────────────


def _mime_to_extension(content_type: str) -> str:
    """Return a file extension for the given (already-normalised) MIME type."""
    mapping = {
        "audio/ogg": ".ogg",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "application/octet-stream": ".bin",
    }
    return mapping.get(content_type, ".bin")


# ── Task 3.4 + 3.5: Transcription ────────────────────────────────────────────


def transcribe_audio(audio_bytes: bytes, content_type: str) -> dict:
    """Validate, save to a secure temp file, transcribe, then clean up.

    Args:
        audio_bytes: Raw audio data read from the uploaded file.
        content_type: The MIME type reported by the client (e.g. "audio/ogg").

    Returns:
        A dict with keys: status, text, language, processing_seconds, model.

    Raises:
        VoiceMimeError: Unsupported content type.
        VoiceEmptyError: Zero-byte upload.
        VoiceSizeError: Upload exceeds MAX_BYTES.
    """
    # ── 3.4: Validation ──────────────────────────────────────────────────────
    ct = (content_type or "").split(";")[0].strip().lower()

    if ct not in SAFE_MIME_TYPES:
        raise VoiceMimeError(f"Unsupported audio type: {ct!r}")

    if len(audio_bytes) == 0:
        raise VoiceEmptyError("Empty audio upload")

    if len(audio_bytes) > MAX_BYTES:
        raise VoiceSizeError(
            f"Upload exceeds {VOICE_MAX_UPLOAD_MB} MB limit "
            f"({len(audio_bytes)} bytes received)"
        )

    logger.info(
        "Voice: transcription request — mime=%s size=%d bytes",
        ct,
        len(audio_bytes),
    )

    # ── 3.5: File handling, transcription, and cleanup ───────────────────────
    temp_dir = WHISPER_MODEL_DIR.parent / "temp_audio"
    temp_dir.mkdir(parents=True, exist_ok=True)

    suffix = _mime_to_extension(ct)
    tmp_path: Optional[Path] = None

    try:
        fd, tmp_str = tempfile.mkstemp(
            suffix=suffix,
            dir=str(temp_dir),
            prefix=secrets.token_hex(8) + "_",
        )
        tmp_path = Path(tmp_str)

        with os.fdopen(fd, "wb") as f:
            f.write(audio_bytes)

        # Load model (lazy, thread-safe)
        model = _get_or_load_model()

        t0 = time.monotonic()
        segments, info = model.transcribe(
            str(tmp_path),
            language=VOICE_LANGUAGE,
            beam_size=VOICE_BEAM_SIZE,
            vad_filter=VOICE_VAD_FILTER,
        )

        # Materialise the generator before returning so the temp file
        # can be safely deleted in the finally block.
        text = " ".join(seg.text for seg in segments).strip()
        elapsed = round(time.monotonic() - t0, 2)

        # DO NOT log text — privacy requirement (Requirement 4.7 / 10.3)
        logger.info("Voice: transcription completed in %.2fs", elapsed)

        return {
            "status": "ok",
            "text": text,
            "language": info.language,
            "processing_seconds": elapsed,
            "model": VOICE_MODEL,
        }

    finally:
        # Always delete the temp file, even on exception
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError as exc:
                logger.warning(
                    "Voice: failed to delete temp file %s — %s",
                    tmp_path.name,
                    exc,
                )


# ── Task 3.6: Status and explicit unload ─────────────────────────────────────


def get_voice_status() -> dict:
    """Return current voice subsystem status (no lock needed — read-only snapshot)."""
    return {
        "enabled": VOICE_ENABLED,
        "model": VOICE_MODEL,
        "loaded": _model is not None,
        "device": VOICE_DEVICE,
        "compute_type": VOICE_COMPUTE_TYPE,
    }


def unload_model() -> dict:
    """Explicitly unload the Whisper model from memory.

    Safe to call when the model is already unloaded.

    Returns:
        A dict with keys: status ("unloaded" | "not_loaded"), message.
    """
    global _model, _idle_timer
    with _lock:
        if _model is not None:
            _model = None
            if _idle_timer is not None:
                _idle_timer.cancel()
                _idle_timer = None
            logger.info("Voice: Whisper model unloaded (explicit request)")
            return {
                "status": "unloaded",
                "message": "Whisper model has been unloaded from memory.",
            }
        else:
            # Cancel any lingering timer even if model is already None
            if _idle_timer is not None:
                _idle_timer.cancel()
                _idle_timer = None
            return {
                "status": "not_loaded",
                "message": "Whisper model was not loaded.",
            }
