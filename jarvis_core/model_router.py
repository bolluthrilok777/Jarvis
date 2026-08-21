"""Hardware-aware, explainable model routing for local Jarvis requests.

This module is deliberately independent from Ollama HTTP calls.  It decides
*whether* AI is required and, if it is, which locally installed model is the
smallest safe choice.  Callers can expose the returned metadata directly in
the UI instead of inventing a progress label.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


BASE_DIR = Path("C:/Project Opus")
SETTINGS_FILE = BASE_DIR / "config" / "model_router.json"

MODEL_PROFILES = {
    "fast": {"model": "gemma2:2b", "label": "FAST · GEMMA2 2B"},
    "balanced": {"model": "qwen2.5:7b", "label": "BALANCED · QWEN 7B"},
    "coding": {"model": "qwen2.5-coder:7b", "label": "CODING · QWEN CODER 7B"},
    "quality": {"model": "gemma4:12b", "label": "QUALITY · GEMMA 12B"},
    "deep": {"model": "qwen3.6:latest", "label": "DEEP · QWEN 36B"},
}

VALID_MODES = frozenset({"auto", *MODEL_PROFILES})
DIRECT_LABEL = "DIRECT EXECUTION · AI BYPASSED"


@dataclass(frozen=True)
class RouteDecision:
    execution: str  # "direct" or "ollama"
    model: Optional[str]
    mode: str
    label: str
    reason: str
    purpose: str
    escalated: bool = False
    timestamp: str = ""

    def public(self) -> dict:
        return asdict(self)


class ModelRouter:
    """Single source of truth for model choice and visible router state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mode = self._load_mode()
        self._last = RouteDecision(
            execution="ollama", model="gemma2:2b", mode=self._mode,
            label="AUTO · READY", reason="Waiting for a request.", purpose="idle",
            timestamp=self._now(),
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _load_mode(self) -> str:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            mode = str(data.get("mode", "auto")).lower()
            return mode if mode in VALID_MODES else "auto"
        except (OSError, ValueError, TypeError):
            return "auto"

    def _save_mode(self) -> None:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps({"mode": self._mode}, indent=2), encoding="utf-8")

    def get_mode(self) -> str:
        with self._lock:
            return self._mode

    def set_mode(self, mode: str) -> str:
        normalized = (mode or "").strip().lower()
        if normalized not in VALID_MODES:
            raise ValueError(f"Unsupported model mode: {mode}")
        with self._lock:
            self._mode = normalized
            self._save_mode()
            if normalized == "auto":
                self._last = RouteDecision(
                    execution="ollama", model="gemma2:2b", mode="auto",
                    label="AUTO · READY", reason="Automatic routing selected.", purpose="idle",
                    timestamp=self._now(),
                )
            else:
                profile = MODEL_PROFILES[normalized]
                self._last = RouteDecision(
                    execution="ollama", model=profile["model"], mode=normalized,
                    label=profile["label"], reason=f"Manual {normalized.title()} mode selected.",
                    purpose="idle", timestamp=self._now(),
                )
        return normalized

    def get_last(self) -> dict:
        with self._lock:
            return self._last.public()

    def _record(self, decision: RouteDecision) -> RouteDecision:
        with self._lock:
            self._last = decision
        return decision

    def record_direct(self, purpose: str, reason: str = "Matched a verified local tool.") -> RouteDecision:
        return self._record(RouteDecision(
            execution="direct", model=None, mode="direct", label=DIRECT_LABEL,
            reason=reason, purpose=purpose, timestamp=self._now(),
        ))

    def record_manual_model(self, model: str, purpose: str) -> RouteDecision:
        """Expose legacy direct model-id selection without treating it as Auto."""
        return self._record(RouteDecision(
            execution="ollama", model=model, mode="manual", label=f"MANUAL · {model.upper()}",
            reason="Specific local model selected.", purpose=purpose, timestamp=self._now(),
        ))

    def decide(
        self,
        message: str,
        requested_mode: Optional[str] = None,
        *,
        purpose: str = "chat",
        direct_available: bool = False,
        validation_failed: bool = False,
    ) -> RouteDecision:
        """Choose a route without loading a model or making a network call.

        Quality escalation is intentionally narrow: auto mode may select Gemma
        12B only after a real validation failure is reported by the caller.
        Deep is manual-only and is never selected here while in auto mode.
        """
        requested = (requested_mode or self.get_mode()).strip().lower()
        text = message or ""
        lower = text.lower()

        if direct_available:
            return self.record_direct(purpose)

        if requested != "auto":
            profile = MODEL_PROFILES[requested]
            return self._record(RouteDecision(
                execution="ollama", model=profile["model"], mode=requested,
                label=profile["label"], reason=f"Manual {requested.title()} mode selected.",
                purpose=purpose, timestamp=self._now(),
            ))

        if validation_failed:
            profile = MODEL_PROFILES["quality"]
            return self._record(RouteDecision(
                execution="ollama", model=profile["model"], mode="auto",
                label="AUTO · GEMMA 12B", reason="Escalated after validation failed.",
                purpose=purpose, escalated=True, timestamp=self._now(),
            ))

        if purpose == "gmail_classification":
            return self._auto("gemma2:2b", "AUTO · GEMMA2 2B", "Fast Gmail classification.", purpose)
        if purpose in {"gmail_draft", "planner"}:
            return self._auto("qwen2.5:7b", "AUTO · QWEN 7B", "Structured reasoning is required.", purpose)

        coding = bool(re.search(r"\b(code|python|javascript|typescript|sql|regex|bug|debug|refactor|test|stack trace|function|class|api)\b", lower))
        complex_request = (
            len(text) > 420
            or bool(re.search(r"\b(architecture|implement|refactor|debug|review|multi[- ]step|compare|analyse|analyze|strategy|detailed)\b", lower))
        )
        if coding:
            if complex_request:
                return self._auto("qwen2.5-coder:7b", "AUTO · QWEN CODER 7B", "Complex coding request.", purpose)
            return self._auto("qwen2.5-coder:3b", "AUTO · QWEN CODER 3B", "Small coding or syntax request.", purpose)
        if complex_request or purpose in {"reasoning", "draft"}:
            return self._auto("qwen2.5:7b", "AUTO · QWEN 7B", "General reasoning request.", purpose)
        return self._auto("gemma2:2b", "AUTO · GEMMA2 2B", "Simple chat or intent request.", purpose)

    def _auto(self, model: str, label: str, reason: str, purpose: str) -> RouteDecision:
        return self._record(RouteDecision(
            execution="ollama", model=model, mode="auto", label=label,
            reason=reason, purpose=purpose, timestamp=self._now(),
        ))


router = ModelRouter()
