"""Feature Orchestrator proof/evidence system (V1).

The user wants proof that a real employee-like process is running: what phase
Jarvis is in, what it is inspecting, and where the evidence lives.

Safety rules enforced here:
- V1 captures TEXT/evidence proofs only. Screen screenshots are intentionally
  NOT taken: the current screen may expose .env contents, tokens, credentials,
  browser cookies or personal tabs, and V1 has no reliable way to guarantee a
  secret-free capture. A text artifact with verified repository paths is
  always safe.
- Every stored string passes through redact_sensitive before persistence.
- Storage stays lightweight: JSON metadata per snapshot, capped history.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from jarvis_core.config import BASE_DIR
from jarvis_core.feature_models import FeatureRequest
from jarvis_core.feature_utils import redact_sensitive
from jarvis_core.timezone_utils import jarvis_now

PROOF_DIR = BASE_DIR / "data" / "features" / "proofs"

_MAX_HISTORY = 20


def _proof_file(feature_id: str, seq: int) -> Path:
    return PROOF_DIR / feature_id[:8] / f"proof_{seq:03d}.json"


def _safe_paths(paths) -> list[str]:
    """Keep only real, existing repository paths (never invent evidence)."""
    out = []
    for p in paths or []:
        candidate = Path(str(p))
        if not candidate.is_absolute():
            candidate = BASE_DIR / candidate
        try:
            if candidate.exists():
                out.append(str(candidate).replace("\\", "/"))
        except OSError:
            continue
    return list(dict.fromkeys(out))[:8]


def capture_proof(
    feature: FeatureRequest,
    *,
    phase: str,
    stage: str,
    description: str,
    related_paths=None,
    proof_type: str = "text",
) -> dict:
    """Record one safe proof snapshot for a main phase transition."""
    seq = len(feature.proof_history) + 1
    entry = {
        "seq": seq,
        "phase": phase,
        "stage": stage,
        "at": jarvis_now().isoformat(),
        "proof_type": proof_type,
        # V1 rule: no screenshots. The screen may contain secrets (.env, bot
        # token, OAuth cookies); a text/evidence proof never can.
        "screenshot": None,
        "screenshot_note": (
            "Text evidence only - screen capture skipped because the desktop "
            "may contain secrets (.env, tokens, credentials)."
        ),
        "related_paths": _safe_paths(related_paths),
        "description": redact_sensitive(str(description))[:300],
    }

    # Lightweight on-disk artifact so proof survives restarts and can be
    # inspected without loading every feature.
    try:
        target = _proof_file(feature.feature_id, seq)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "feature_id": feature.feature_id,
            "feature": feature.interpreted_objective or feature.original_request,
            **entry,
        }
        target.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        entry["artifact"] = str(target.relative_to(BASE_DIR)).replace("\\", "/")
    except OSError:
        entry["artifact"] = None

    feature.proof_history.append(entry)
    feature.proof_history = feature.proof_history[-_MAX_HISTORY:]
    return dict(entry)


def latest_proof(feature: FeatureRequest) -> Optional[dict]:
    return dict(feature.proof_history[-1]) if feature.proof_history else None


def proof_message(feature: FeatureRequest) -> str:
    """📸 CURRENT WORK PROOF — safe text summary for Telegram/Web."""
    proof = latest_proof(feature)
    title = feature.interpreted_objective or feature.original_request
    header = (
        "\U0001f4f8 CURRENT WORK PROOF\n\n"
        f"Feature: {title}\n"
    )
    if proof is None:
        return (
            header
            + "Phase: (not started)\n"
            + "Evidence: research has not captured a snapshot yet."
        )

    local_time = str(proof.get("at", ""))[:19].replace("T", " ")
    paths = proof.get("related_paths") or []
    path_lines = "\n".join(f"- {p}" for p in paths) or "- (no repository paths in this stage)"
    return (
        header
        + f"Phase: {proof.get('phase')}\n"
        f"Current: {proof.get('stage')}\n"
        f"Time: {local_time}\n"
        f"Evidence:\n{path_lines}\n\n"
        f"{proof.get('description', '')}\n\n"
        f"Proof type: {proof.get('proof_type')} (text evidence; "
        "screenshot skipped to protect secrets)"
    )
