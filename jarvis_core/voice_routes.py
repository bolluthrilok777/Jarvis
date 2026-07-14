"""
jarvis_core/voice_routes.py — Phase 4 Voice Input API Endpoints

Registers three routes under the /voice prefix:
    POST /voice/transcribe   — upload audio, get transcription
    GET  /voice/status       — check model loaded state
    POST /voice/unload       — explicitly unload the Whisper model
"""
import logging

from fastapi import APIRouter, File, HTTPException, UploadFile

from jarvis_core.config import VOICE_ENABLED
from jarvis_core.voice_service import (
    VoiceEmptyError,
    VoiceMimeError,
    VoiceSizeError,
    get_voice_status,
    transcribe_audio,
    unload_model,
)

logger = logging.getLogger("jarvis.voice_routes")

router = APIRouter(prefix="/voice", tags=["voice"])


@router.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    """Accept a multipart audio upload and return a transcription."""
    if not VOICE_ENABLED:
        raise HTTPException(status_code=503, detail="Voice input is disabled.")

    audio_bytes = await audio.read()
    ct = audio.content_type or "application/octet-stream"

    logger.info(
        "Voice: /transcribe request — filename=%s content_type=%s",
        audio.filename or "<none>",
        ct,
    )

    try:
        result = transcribe_audio(audio_bytes, ct)
        return result
    except VoiceMimeError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except VoiceEmptyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except VoiceSizeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Voice: unexpected transcription error — %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=500, detail="Transcription failed.") from exc


@router.get("/status")
def voice_status():
    """Return the current voice subsystem status."""
    return get_voice_status()


@router.post("/unload")
def voice_unload():
    """Explicitly unload the Whisper model from memory."""
    return unload_model()
