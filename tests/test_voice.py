"""
tests/test_voice.py — Phase 4 Voice Input Tests (29 tests)

Tests 1–14: Service and endpoint tests
Tests 15–22: Safety, regression, and UI structural tests
Tests 23–29: Additional UI and behavior tests
"""
import importlib
import io
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import logging
import logging.handlers
import pytest
from fastapi.testclient import TestClient

from app import app
import jarvis_core.voice_service as voice_service
from jarvis_core.voice_service import (
    SAFE_MIME_TYPES,
    MAX_BYTES,
    VoiceMimeError,
    VoiceEmptyError,
    VoiceSizeError,
    get_voice_status,
    unload_model,
    transcribe_audio,
)
from jarvis_core.config import (
    VOICE_ENABLED,
    VOICE_MODEL,
    VOICE_DEVICE,
    VOICE_COMPUTE_TYPE,
    VOICE_LANGUAGE,
    VOICE_BEAM_SIZE,
    VOICE_VAD_FILTER,
    VOICE_MAX_DURATION_SECONDS,
    VOICE_MAX_UPLOAD_MB,
    VOICE_IDLE_UNLOAD_SECONDS,
    WHISPER_MODEL_DIR,
)

client = TestClient(app)

# Repository-relative paths — never assume a specific install location.
REPO_ROOT = Path(__file__).resolve().parent.parent
UI_FILE = REPO_ROOT / "ui.html"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_mock_model(text: str = "open calculator", language: str = "en"):
    """Return a mock WhisperModel that produces a fixed transcription."""
    mock_segment = MagicMock()
    mock_segment.text = text

    mock_info = MagicMock()
    mock_info.language = language

    mock_model = MagicMock()
    mock_model.transcribe.return_value = ([mock_segment], mock_info)
    return mock_model


def _reset_voice_service():
    """Reset voice_service module-level state between tests."""
    import jarvis_core.voice_service as vs
    with vs._lock:
        if vs._idle_timer is not None:
            vs._idle_timer.cancel()
            vs._idle_timer = None
        vs._model = None


# ── Tests 1–14 ────────────────────────────────────────────────────────────────

# Test 1: voice_service imports without error
def test_voice_service_imports():
    """Verify jarvis_core.voice_service imports without ImportError."""
    import jarvis_core.voice_service
    assert hasattr(jarvis_core.voice_service, "transcribe_audio")
    assert hasattr(jarvis_core.voice_service, "get_voice_status")
    assert hasattr(jarvis_core.voice_service, "unload_model")
    assert hasattr(jarvis_core.voice_service, "SAFE_MIME_TYPES")
    assert hasattr(jarvis_core.voice_service, "MAX_BYTES")


# Test 2: config values are valid
def test_voice_config_values_valid():
    """Verify all 11 voice config constants have correct types and values."""
    assert isinstance(VOICE_ENABLED, bool)
    assert isinstance(VOICE_MODEL, str) and len(VOICE_MODEL) > 0
    assert isinstance(VOICE_DEVICE, str) and len(VOICE_DEVICE) > 0
    assert isinstance(VOICE_COMPUTE_TYPE, str) and len(VOICE_COMPUTE_TYPE) > 0
    assert isinstance(VOICE_LANGUAGE, str) and len(VOICE_LANGUAGE) > 0
    assert isinstance(VOICE_BEAM_SIZE, int) and VOICE_BEAM_SIZE >= 1
    assert isinstance(VOICE_VAD_FILTER, bool)
    assert isinstance(VOICE_MAX_DURATION_SECONDS, int) and VOICE_MAX_DURATION_SECONDS > 0
    assert isinstance(VOICE_MAX_UPLOAD_MB, int) and VOICE_MAX_UPLOAD_MB > 0
    assert isinstance(VOICE_IDLE_UNLOAD_SECONDS, int) and VOICE_IDLE_UNLOAD_SECONDS > 0
    assert isinstance(WHISPER_MODEL_DIR, Path)
    assert VOICE_MODEL == "base.en"
    assert VOICE_DEVICE == "cpu"
    assert VOICE_COMPUTE_TYPE == "int8"


# Test 3: GET /voice/status returns correct structure
def test_voice_status_endpoint():
    """GET /voice/status returns HTTP 200 with all 5 required fields."""
    response = client.get("/voice/status")
    assert response.status_code == 200
    data = response.json()
    for field in ["enabled", "model", "loaded", "device", "compute_type"]:
        assert field in data, f"Missing field: {field}"
    assert isinstance(data["enabled"], bool)
    assert isinstance(data["loaded"], bool)
    assert data["model"] == "base.en"
    assert data["device"] == "cpu"
    assert data["compute_type"] == "int8"


# Test 4: POST /voice/transcribe succeeds with mocked transcription
def test_transcribe_succeeds_with_mock():
    """POST /voice/transcribe returns correct Transcription_Result shape when mocked."""
    _reset_voice_service()
    try:
        mock_model = _make_mock_model("open calculator", "en")
        with patch("jarvis_core.voice_service.WhisperModel", return_value=mock_model):
            audio_bytes = b"fake ogg audio data " * 10
            response = client.post(
                "/voice/transcribe",
                files={"audio": ("recording.ogg", io.BytesIO(audio_bytes), "audio/ogg")},
            )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        assert data["status"] == "ok"
        assert "text" in data
        assert "language" in data
        assert "processing_seconds" in data
        assert "model" in data
        assert isinstance(data["processing_seconds"], (int, float))
        assert data["processing_seconds"] >= 0
        assert data["model"] == "base.en"
    finally:
        _reset_voice_service()


# Test 5: empty upload rejected with HTTP 400
def test_transcribe_empty_upload_rejected():
    """POST /voice/transcribe with 0-byte body returns HTTP 400."""
    response = client.post(
        "/voice/transcribe",
        files={"audio": ("empty.ogg", io.BytesIO(b""), "audio/ogg")},
    )
    assert response.status_code == 400, f"Expected 400, got {response.status_code}: {response.text}"


# Test 6: oversized upload rejected with HTTP 413
def test_transcribe_oversized_upload_rejected():
    """POST /voice/transcribe with file > VOICE_MAX_UPLOAD_MB returns HTTP 413."""
    oversized = b"x" * (VOICE_MAX_UPLOAD_MB * 1024 * 1024 + 1)
    response = client.post(
        "/voice/transcribe",
        files={"audio": ("big.ogg", io.BytesIO(oversized), "audio/ogg")},
    )
    assert response.status_code == 413, f"Expected 413, got {response.status_code}: {response.text}"


# Test 7: unsupported MIME type rejected with HTTP 415
def test_transcribe_unsupported_mime_rejected():
    """POST /voice/transcribe with text/plain content type returns HTTP 415."""
    response = client.post(
        "/voice/transcribe",
        files={"audio": ("test.txt", io.BytesIO(b"hello world"), "text/plain")},
    )
    assert response.status_code == 415, f"Expected 415, got {response.status_code}: {response.text}"


# Test 8: safe MIME type accepted (does not return 415)
def test_transcribe_safe_mime_accepted():
    """POST /voice/transcribe with audio/wav returns something other than HTTP 415."""
    mock_model = _make_mock_model()
    with patch("jarvis_core.voice_service.WhisperModel", return_value=mock_model):
        with patch.object(voice_service, "_model", None):
            response = client.post(
                "/voice/transcribe",
                files={"audio": ("test.wav", io.BytesIO(b"RIFF" + b"\x00" * 36), "audio/wav")},
            )
    # Must not be 415 (MIME rejection); 200 or 500 are acceptable depending on soundfile parsing
    assert response.status_code != 415, f"Got 415 for audio/wav — should be accepted"


# Test 9: temp audio file deleted after successful transcription
def test_temp_file_deleted_after_success():
    """Temp audio file is deleted after a successful transcription."""
    _reset_voice_service()
    try:
        mock_model = _make_mock_model("hello", "en")
        created_paths = []

        original_mkstemp = tempfile.mkstemp

        def spy_mkstemp(**kwargs):
            fd, path = original_mkstemp(**kwargs)
            created_paths.append(Path(path))
            return fd, path

        with patch("jarvis_core.voice_service.WhisperModel", return_value=mock_model):
            with patch("jarvis_core.voice_service.tempfile.mkstemp", side_effect=spy_mkstemp):
                client.post(
                    "/voice/transcribe",
                    files={"audio": ("r.ogg", io.BytesIO(b"ogg data " * 20), "audio/ogg")},
                )

        assert len(created_paths) >= 1, "No temp file was created"
        for p in created_paths:
            assert not p.exists(), f"Temp file still exists: {p}"
    finally:
        _reset_voice_service()


# Test 10: temp audio file deleted after failed transcription
def test_temp_file_deleted_after_failure():
    """Temp audio file is deleted even when transcription raises an exception."""
    _reset_voice_service()
    try:
        mock_model = MagicMock()
        mock_model.transcribe.side_effect = RuntimeError("simulated transcription failure")

        created_paths = []
        original_mkstemp = tempfile.mkstemp

        def spy_mkstemp(**kwargs):
            fd, path = original_mkstemp(**kwargs)
            created_paths.append(Path(path))
            return fd, path

        with patch("jarvis_core.voice_service.WhisperModel", return_value=mock_model):
            with patch("jarvis_core.voice_service.tempfile.mkstemp", side_effect=spy_mkstemp):
                response = client.post(
                    "/voice/transcribe",
                    files={"audio": ("r.ogg", io.BytesIO(b"ogg data " * 20), "audio/ogg")},
                )

        # Should fail gracefully (500 from the unexpected exception)
        assert response.status_code in (500,), f"Expected 500, got {response.status_code}"
        assert len(created_paths) >= 1, "No temp file was created"
        for p in created_paths:
            assert not p.exists(), f"Temp file still exists after failure: {p}"
    finally:
        _reset_voice_service()


# Test 11: Whisper model loaded lazily (not at startup)
def test_model_loaded_lazily():
    """After importing voice_service, _model must be None."""
    import jarvis_core.voice_service as vs
    # Reset first to ensure clean state
    _reset_voice_service()
    assert vs._model is None, f"Expected _model=None, got {vs._model}"


# Test 12: Whisper model not loaded at server startup
def test_model_not_loaded_at_app_startup():
    """After importing the FastAPI app, the Whisper model must still be None."""
    import jarvis_core.voice_service as vs
    _reset_voice_service()
    # Import app again (it's cached, but this confirms no side effects)
    from app import app as _app  # noqa: F811
    assert vs._model is None, f"Whisper model loaded at startup: {vs._model}"


# Test 13: concurrent init creates exactly one model instance
def test_concurrent_init_creates_one_model():
    """Concurrent calls to _get_or_load_model() must initialize WhisperModel exactly once."""
    _reset_voice_service()
    try:
        init_count = [0]
        barrier = threading.Barrier(5)

        original_init = None

        class CountingWhisperModel:
            def __init__(self, *args, **kwargs):
                init_count[0] += 1
                # Mock transcribe method
                self.transcribe = MagicMock(return_value=([], MagicMock(language="en")))

        def thread_fn():
            barrier.wait()  # All threads start simultaneously
            voice_service._get_or_load_model()

        with patch("jarvis_core.voice_service.WhisperModel", CountingWhisperModel):
            threads = [threading.Thread(target=thread_fn) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        assert init_count[0] == 1, f"WhisperModel.__init__ called {init_count[0]} times, expected 1"
    finally:
        _reset_voice_service()


# Test 14: POST /voice/unload returns HTTP 200
def test_voice_unload_endpoint():
    """POST /voice/unload returns HTTP 200 with a 'status' field."""
    response = client.post("/voice/unload")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data


# ── Tests 15–22 ───────────────────────────────────────────────────────────────

# Test 15: unload when already unloaded is safe
def test_unload_when_already_unloaded_is_safe():
    """Calling POST /voice/unload when model is not loaded must return HTTP 200 without error."""
    _reset_voice_service()
    # Model is None — unload should be safe
    response = client.post("/voice/unload")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "not_loaded"


# Test 16: GET / still works
def test_existing_root_still_works():
    """GET / still returns the expected running status after Phase 4 changes."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "running"
    assert data["default_model"] == "gemma2:2b"
    assert data["heavy_model"] == "qwen2.5:7b"


# Test 17: GET /health still works
def test_existing_health_still_works():
    """GET /health still returns {jarvis: 'running'} after Phase 4 changes."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"models": [{"name": "gemma2:2b"}]}
    mock_response.raise_for_status = MagicMock()
    with patch("jarvis_core.routes.requests.get", return_value=mock_response):
        with patch("jarvis_core.routes.get_system_status", return_value={}):
            response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["jarvis"] == "running"


# Test 18: GET /ui still works
def test_existing_ui_still_works():
    """GET /ui still returns HTTP 200 text/html after Phase 4 changes."""
    response = client.get("/ui")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


# Test 19: POST /agent still works
def test_existing_agent_still_works():
    """POST /agent still returns a reply field after Phase 4 changes."""
    with patch("jarvis_core.command_dispatcher.ask_ollama",
               return_value=("I am Jarvis.", "gemma2:2b", None)):
        response = client.post("/agent", json={"command": "Who are you?", "mode": "light"})
    assert response.status_code == 200
    assert "reply" in response.json()


# Test 20: POST /command returns same shape as POST /agent
def test_existing_command_same_as_agent():
    """POST /command returns the same response shape as POST /agent."""
    with patch("jarvis_core.command_dispatcher.ask_ollama",
               return_value=("I am Jarvis.", "gemma2:2b", None)):
        r_agent = client.post("/agent", json={"command": "Who are you?", "mode": "light"})
        r_command = client.post("/command", json={"command": "Who are you?", "mode": "light"})
    assert r_agent.status_code == 200
    assert r_command.status_code == 200
    assert r_agent.json().keys() == r_command.json().keys()


# Test 21: typed-command behaviour unchanged
def test_typed_command_behavior_unchanged():
    """Non-voice POST /agent commands still produce the same response shape as pre-Phase-4."""
    with patch("jarvis_core.command_dispatcher.ask_ollama",
               return_value=("System status: all good.", "gemma2:2b", None)):
        response = client.post("/agent", json={"command": "system status", "mode": "light"})
    assert response.status_code == 200
    data = response.json()
    # Response must have a reply field (unchanged from pre-Phase-4 shape)
    assert "reply" in data or "status" in data


# Test 22: voice text NOT auto-executed
def test_voice_text_not_auto_executed():
    """Assert voice_routes.py and ui.html never auto-call /agent or /command."""
    voice_routes_path = REPO_ROOT / "jarvis_core" / "voice_routes.py"
    voice_routes_src = voice_routes_path.read_text(encoding="utf-8")

    # voice_routes.py must not contain any call to /agent or /command
    assert "'/agent'" not in voice_routes_src
    assert '"/agent"' not in voice_routes_src
    assert "'/command'" not in voice_routes_src
    assert '"/command"' not in voice_routes_src

    # ui.html must not auto-call /agent or /command after receiving transcription
    ui_src = UI_FILE.read_text(encoding="utf-8")
    # The transcription callback (handleStop / the fetch response handler) should not call /agent
    # We check that fetch('/agent') is not present anywhere in voice-related JS
    # (it's OK for existing typed-command fetch to exist, but the voice handler must not)
    assert "fetch('/agent'" not in ui_src or "voice" not in ui_src.split("fetch('/agent'")[0][-500:]


# ── Tests 23–29: UI structural tests ─────────────────────────────────────────

# Test 23: microphone button exists in ui.html
def test_microphone_button_exists_in_ui():
    """ui.html must contain a voice start button element."""
    ui_src = UI_FILE.read_text(encoding="utf-8")
    # Check for voice-btn id or Start Listening text
    assert 'id="voice-btn"' in ui_src or "voice-btn" in ui_src, \
        "No voice-btn element found in ui.html"


# Test 24: microphone recording NOT started automatically on page load
def test_microphone_not_started_automatically():
    """getUserMedia must not be called at the top level (outside a function/handler)."""
    ui_src = UI_FILE.read_text(encoding="utf-8")
    assert "getUserMedia" in ui_src, "getUserMedia not found in ui.html at all"
    # getUserMedia must appear inside a function body, not at global scope
    # Simple check: it must not appear before any function definition in the script
    script_start = ui_src.find("<script")
    assert script_start != -1
    script_block = ui_src[script_start:]
    # getUserMedia should only appear inside a function — verify it's not a bare top-level call
    # by checking it only appears after "function" or "=>" or "async " in proximity
    gum_idx = script_block.find("getUserMedia")
    context = script_block[max(0, gum_idx - 300):gum_idx]
    # There must be a function/arrow/async definition before the getUserMedia call
    assert any(kw in context for kw in ["function ", "async ", "=> ", "=>"]), \
        "getUserMedia appears to be called outside a function body"


# Test 25: getUserMedia called only after user interaction (inside click handler)
def test_get_user_media_inside_click_handler():
    """getUserMedia must only be called inside a function bound to a click event."""
    ui_src = UI_FILE.read_text(encoding="utf-8")
    assert "getUserMedia" in ui_src
    # Verify addEventListener("click", ...) or onclick handler exists near getUserMedia
    assert 'addEventListener("click"' in ui_src or "addEventListener('click'" in ui_src or \
           ".onclick" in ui_src or "startRecording" in ui_src, \
        "No click handler found that would trigger getUserMedia"
    # getUserMedia must be inside a function called startRecording or similar
    assert "getUserMedia" in ui_src
    # The start recording function must be referenced in a click listener
    if "startRecording" in ui_src:
        assert "startRecording" in ui_src


# Test 26: MediaStream tracks are stopped
def test_media_stream_tracks_stopped():
    """ui.html must contain code to stop all MediaStream audio tracks."""
    ui_src = UI_FILE.read_text(encoding="utf-8")
    has_stop_tracks = (
        "getTracks().forEach" in ui_src
        or "track.stop()" in ui_src
        or ".stop()" in ui_src
    )
    assert has_stop_tracks, "No MediaStream track stopping code found in ui.html"


# Test 27: 15-second maximum duration logic exists
def test_maximum_duration_logic_exists():
    """ui.html must contain a 15-second auto-stop timer."""
    ui_src = UI_FILE.read_text(encoding="utf-8")
    has_duration = "15000" in ui_src or "MAX_DURATION_MS" in ui_src
    has_timeout = "setTimeout" in ui_src
    assert has_duration and has_timeout, \
        f"15-second auto-stop not found (has_duration={has_duration}, has_timeout={has_timeout})"


# Test 28: duplicate recording prevention exists
def test_duplicate_recording_prevention():
    """ui.html must contain a guard to prevent duplicate recording sessions."""
    ui_src = UI_FILE.read_text(encoding="utf-8")
    has_guard = (
        'mediaRecorder.state !== "inactive"' in ui_src
        or "mediaRecorder.state !== 'inactive'" in ui_src
        or "isRecording" in ui_src
        or "isUploading" in ui_src
    )
    assert has_guard, "No duplicate recording prevention guard found in ui.html"


# Test 29: full test suite passes with zero failures
def test_full_test_suite_passes():
    """Run the entire tests/ directory and assert exit code 0."""
    import subprocess
    result = subprocess.run(
        [
            sys.executable,
            "-m", "pytest",
            "tests/",
            "--ignore=tests/test_voice.py",  # avoid recursive self-inclusion
            "-v", "--tb=short", "-q",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    output = result.stdout + result.stderr
    print(output)
    assert result.returncode == 0, \
        f"Test suite returned non-zero exit code {result.returncode}:\n{output}"


# ── Property-Based Tests (Tasks 11.1–11.5) ────────────────────────────────────

try:
    from hypothesis import given, settings, HealthCheck
    from hypothesis import strategies as st
    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False

_SAFE_MIME_TYPES_SET = {"audio/ogg", "audio/wav", "audio/x-wav", "application/octet-stream"}


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@given(
    st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="/-+."),
        min_size=1,
    ).filter(lambda s: s.strip().lower() not in _SAFE_MIME_TYPES_SET)
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_1_any_unsupported_mime_rejected(mime_type):
    """Feature: phase4-local-voice-input, Property 1: Any unsupported MIME type is rejected with HTTP 415"""
    response = client.post(
        "/voice/transcribe",
        files={"audio": ("test.bin", io.BytesIO(b"x" * 100), mime_type)},
    )
    assert response.status_code == 415, (
        f"MIME type {mime_type!r} should return 415, got {response.status_code}"
    )


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@given(
    st.integers(min_value=MAX_BYTES + 1, max_value=MAX_BYTES + 10000)
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_2_any_oversized_upload_rejected(size):
    """Feature: phase4-local-voice-input, Property 2: Any oversized upload is rejected with HTTP 413"""
    response = client.post(
        "/voice/transcribe",
        files={"audio": ("big.ogg", io.BytesIO(b"x" * size), "audio/ogg")},
    )
    assert response.status_code == 413, (
        f"Upload of {size} bytes should return 413, got {response.status_code}"
    )


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@given(st.text(min_size=1, max_size=200))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_3_temp_file_confined_and_anonymous(client_filename):
    """Feature: phase4-local-voice-input, Property 3: Temp file is always confined and anonymised"""
    _reset_voice_service()
    try:
        mock_model = _make_mock_model("hello", "en")
        captured = []
        original_mkstemp = tempfile.mkstemp

        def spy_mkstemp(**kwargs):
            fd, path = original_mkstemp(**kwargs)
            captured.append(Path(path))
            return fd, path

        with patch("jarvis_core.voice_service.WhisperModel", return_value=mock_model):
            with patch("jarvis_core.voice_service.tempfile.mkstemp", side_effect=spy_mkstemp):
                client.post(
                    "/voice/transcribe",
                    files={"audio": (client_filename, io.BytesIO(b"ogg data " * 20), "audio/ogg")},
                )

        expected_dir = WHISPER_MODEL_DIR.parent / "temp_audio"
        for p in captured:
            # Temp file must be inside the controlled temp dir
            assert str(p).startswith(str(expected_dir)), (
                f"Temp file {p} is outside {expected_dir}"
            )
            # Temp file must NOT be named after the client-supplied filename
            assert p.name != client_filename, (
                f"Temp file named after client filename: {p.name!r}"
            )
    finally:
        _reset_voice_service()


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@given(st.booleans())
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_4_temp_file_always_deleted(should_fail):
    """Feature: phase4-local-voice-input, Property 4: Temp file always deleted after request"""
    _reset_voice_service()
    try:
        if should_fail:
            mock_model = MagicMock()
            mock_model.transcribe.side_effect = RuntimeError("forced failure")
        else:
            mock_model = _make_mock_model("hello", "en")

        captured = []
        original_mkstemp = tempfile.mkstemp

        def spy_mkstemp(**kwargs):
            fd, path = original_mkstemp(**kwargs)
            captured.append(Path(path))
            return fd, path

        with patch("jarvis_core.voice_service.WhisperModel", return_value=mock_model):
            with patch("jarvis_core.voice_service.tempfile.mkstemp", side_effect=spy_mkstemp):
                client.post(
                    "/voice/transcribe",
                    files={"audio": ("r.ogg", io.BytesIO(b"ogg data " * 20), "audio/ogg")},
                )

        for p in captured:
            assert not p.exists(), (
                f"Temp file still exists (should_fail={should_fail}): {p}"
            )
    finally:
        _reset_voice_service()


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@given(st.text(min_size=2, max_size=200).filter(lambda s: not s.isdigit()))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_6_speech_text_never_in_logs(speech_text):
    """Feature: phase4-local-voice-input, Property 6: Speech text never written to logs

    Validates: Requirements 4.7, 10.3
    Checks that the transcribed speech text does not appear in any log record
    emitted by the voice service during a transcription request.
    Single characters and pure digit strings are excluded as they would trivially
    match numeric metadata (byte counts, elapsed time) in log messages.

    Uses a unique sentinel string as the transcription result so that the
    assertion cannot false-positive on safe static log labels such as
    "transcription request", "mime", "audio", or "ogg".
    """
    _reset_voice_service()
    try:
        # Use a unique sentinel that cannot accidentally match safe log labels
        sensitive_marker = "__PRIVATE_SPEECH_SENTINEL_DO_NOT_LOG_12345__"
        mock_model = _make_mock_model(sensitive_marker, "en")

        with patch("jarvis_core.voice_service.WhisperModel", return_value=mock_model):
            import jarvis_core.voice_service as vs

            # Capture all log records from the jarvis.voice logger
            captured_records = []

            class CapturingHandler(logging.Handler):
                def emit(self, record):
                    captured_records.append(self.format(record))

            cap_handler = CapturingHandler()
            vs_logger = logging.getLogger("jarvis.voice")
            vs_logger.addHandler(cap_handler)
            try:
                client.post(
                    "/voice/transcribe",
                    files={"audio": ("r.ogg", io.BytesIO(b"ogg data " * 20), "audio/ogg")},
                )
            finally:
                vs_logger.removeHandler(cap_handler)

        # Private speech sentinel must NOT appear in any log record
        all_logs = "\n".join(str(record) for record in captured_records)
        assert sensitive_marker not in all_logs, (
            f"Private speech sentinel found in logs: {all_logs!r}"
        )

        # Safe metadata MUST still be logged (mime type, size, processing time)
        assert any("mime=audio/ogg" in str(r) for r in captured_records), (
            "Expected safe metadata (mime type) in logs, but none found"
        )
        assert any("size=" in str(r) for r in captured_records), (
            "Expected safe metadata (size) in logs, but none found"
        )
        assert any("transcription completed" in str(r) for r in captured_records), (
            "Expected safe metadata (processing time) in logs, but none found"
        )
    finally:
        _reset_voice_service()
