"""Targeted tests for the Feature-Orchestrator revision fix (Section A).

Regression guards for the real manual E2E bug where:
  * a change request appended ``(revised: ...)`` into the feature's interpreted
    objective ("revision text appended into the feature title"), and
  * a research-required revision only rebuilt the plan from STALE findings
    instead of re-inspecting the repository, leaving "Likely files: 0".

The fix makes feature identity immutable, stores change requests as structured
dicts, and distinguishes PLAN-ONLY vs RESEARCH-REQUIRED revisions.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jarvis_core.config import BASE_DIR
from jarvis_core.feature_models import FeatureStatus
from jarvis_core.feature_orchestrator import (
    ACTION_BUILD_PLAN,
    ACTION_RESEARCH,
    FeatureOrchestrator,
)
from jarvis_core.feature_research import _scan_repository
from jarvis_core.feature_store import FeatureStore
from jarvis_core.feature_utils import detect_revision_kind, propose_new_files
from jarvis_core.task_manager import TaskManager


class _FakeOllama:
    def __init__(self):
        self.reply = "- Reuse existing services.\n- Add a focused module."

    def __call__(self, message, mode=None, *, purpose="chat", **kwargs):
        return (self.reply, "qwen2.5:7b", None)


@pytest.fixture()
def ollama():
    with patch("jarvis_core.ollama_service.ask_ollama", side_effect=_FakeOllama()):
        yield


def _fake_web():
    def _research(objective):
        return {
            "sources": [{"url": "https://example.com/notes", "ok": True,
                         "snippet": "Notes export API overview.", "error": None}],
            "incomplete": False,
            "error": None,
        }

    return _research


@pytest.fixture()
def orch(ollama, tmp_path, monkeypatch):
    from tests.test_telegram_integration import patch_inline_feature_threads

    patch_inline_feature_threads(monkeypatch)
    store = FeatureStore(root=tmp_path / "features")
    orchestrator = FeatureOrchestrator(store=store, task_manager_instance=TaskManager())
    orchestrator.start_worker = lambda: None  # type: ignore[method-assign]
    orchestrator.set_telegram_channels(
        send=lambda chat_id, text, markup=None: None,
        is_authorized=lambda uid: uid == 123,
    )
    yield orchestrator
    orchestrator.stop_worker(timeout=1)


def _researched(orch, objective: str) -> str:
    """Create + research a feature; leaves it in chatgpt_review_pending."""
    submitted = orch.submit_feature_request(
        objective,
        source="Telegram",
        telegram_user_id=123,
        telegram_chat_id=100,
    )
    feature_id = submitted["feature_id"]
    # Telegram features now pause at the resource-review gate. Press
    # Continue Feature so research becomes an ACTIVE stage exactly as in
    # production (this is what makes later revision chaining observable).
    if (
        orch.get_feature(feature_id)["status"]
        == FeatureStatus.resource_review_pending.value
    ):
        orch.continue_after_resource_review(feature_id, telegram_user_id=123)
    with patch("jarvis_core.feature_research.web_research", _fake_web()):
        orch._run_action(ACTION_RESEARCH, feature_id)
    return feature_id


# ── 1. Identity immutability ────────────────────────────────────────────────


def test_request_changes_never_mutates_feature_identity(orch):
    feature_id = _researched(orch, "Jarvis add a simple local notes export feature")
    before = orch.get_feature(feature_id)

    result = orch.request_changes(
        feature_id,
        "Inspect the repository again and find the real Notes files before re-planning.",
        telegram_user_id=123,
    )

    assert result["status"] == "ok"
    after = orch.get_feature(feature_id)
    # Identity fields are immutable.
    assert after["original_request"] == before["original_request"]
    assert after["interpreted_objective"] == before["interpreted_objective"]
    assert "(revised" not in after["interpreted_objective"]


# ── 2. Change requests stored as structured dicts ───────────────────────────


def test_change_requests_stored_as_dicts(orch):
    feature_id = _researched(orch, "Jarvis add a simple local notes export feature")
    orch.request_changes(
        feature_id,
        "Re-word the plan to be shorter.",
        telegram_user_id=123,
    )
    feature = orch._store.get(feature_id)
    assert feature.change_requests, "change request must be recorded"
    entry = feature.change_requests[-1]
    assert isinstance(entry, dict), "change request must be a dict"
    assert {"at", "kind", "text"} <= set(entry)
    assert entry["kind"] in ("plan_only", "research_required")
    assert entry["text"] == "Re-word the plan to be shorter."


# ── 3. Plan-only revision enqueues BUILD_PLAN ───────────────────────────────


def test_plan_only_revision_rebuilds_plan_only(orch):
    feature_id = _researched(orch, "Jarvis add a simple local notes export feature")
    result = orch.request_changes(
        feature_id,
        "Re-word the plan to be clearer and shorter.",
        telegram_user_id=123,
    )
    assert result["action"] == "replanning"
    feature = orch._store.get(feature_id)
    assert feature.rerun_research is False
    assert feature.research_revision == 0
    chained = orch._chained.get(feature_id) or []
    assert ACTION_BUILD_PLAN in chained, "plan-only must rebuild the plan"

# ── 4. Research-required revision reruns repository analysis ────────────────


def test_research_required_revision_reruns_research(orch):
    feature_id = _researched(orch, "Jarvis add a simple local notes export feature")

    result = orch.request_changes(
        feature_id,
        "Inspect the repository again and identify the actual Notes files "
        "before regenerating the plan.",
        telegram_user_id=123,
    )

    assert result["action"] == "reresearching"
    assert result["research_revision"] >= 1
    feature = orch._store.get(feature_id)
    assert feature.rerun_research is True
    # Stale resolved evidence is cleared so we never rebuild from it.
    assert feature.affected_modules == []
    assert feature.verified_existing_files == []
    # ACTION_RESEARCH must have been queued.
    queued = []
    while True:
        try:
            queued.append(orch._queue.get_nowait())
        except Exception:
            break
    assert any(a == ACTION_RESEARCH for a, _ in queued), "research must be re-queued"


# ── 5. Research-required revision refreshes verified evidence ───────────────


def test_research_required_revision_refreshes_verified_evidence(orch):
    feature_id = _researched(orch, "Jarvis add a simple local notes export feature")
    orch.request_changes(
        feature_id,
        "Inspect the repository again and identify the actual Notes files.",
        telegram_user_id=123,
    )
    feature = orch._store.get(feature_id)
    assert feature.rerun_research is True

    with patch("jarvis_core.feature_research.web_research", _fake_web()):
        orch._run_action(ACTION_RESEARCH, feature_id)

    refreshed = orch._store.get(feature_id)
    assert refreshed.verified_existing_files, "re-analysis must re-establish verified files"
    for path in refreshed.verified_existing_files:
        candidate = Path(path) if Path(path).is_absolute() else BASE_DIR / path
        assert candidate.exists(), f"verified path must really exist: {path}"


# ── 6. Plan lists real verified files (not zero) for the notes feature ───────


def test_plan_lists_real_verified_files_for_notes(orch):
    feature_id = _researched(orch, "Jarvis add a simple local notes export feature")
    with patch("jarvis_core.feature_research.web_research", _fake_web()):
        orch._run_action(ACTION_BUILD_PLAN, feature_id)
    feature = orch._store.get(feature_id)
    assert feature.plan, "plan must exist"
    files = feature.plan.get("Files/modules likely affected") or []
    assert files, "files/modules must not be empty for a notes feature"
    proposed = feature.plan.get("New modules required") or []
    assert proposed, "proposed new modules should be listed"
    joined = "\n".join(map(str, files))
    assert "notes" in joined.lower() or any("note" in str(f).lower() for f in files)


# ── 7. Verified vs proposed separation ───────────────────────────────────────


def test_verified_and_proposed_are_separate(orch):
    feature_id = _researched(orch, "Jarvis add a simple local notes export feature")
    feature = orch._store.get(feature_id)
    for path in feature.verified_existing_files:
        candidate = Path(path) if Path(path).is_absolute() else BASE_DIR / path
        assert candidate.exists(), path
    for path in feature.proposed_new_files:
        assert "(proposed" in path
        bare = path.split(" (proposed")[0]
        candidate = Path(bare) if Path(bare).is_absolute() else BASE_DIR / bare
        assert not candidate.exists(), f"proposed module must not exist yet: {path}"


# ── 8. detect_revision_kind classifier ──────────────────────────────────────


def test_detect_revision_kind_research_required_markers():
    assert detect_revision_kind("Inspect the repository again and identify actual files.") == "research_required"
    assert detect_revision_kind("Verify the Notes service exists and find where notes are stored.") == "research_required"
    assert detect_revision_kind("Re-run research and validate storage architecture.") == "research_required"
    assert detect_revision_kind("Confirm which existing component should be reused.") == "research_required"


def test_detect_revision_kind_plan_only_markers():
    assert detect_revision_kind("Re-word the plan to be clearer.") == "plan_only"
    assert detect_revision_kind("Reformat the plan.") == "plan_only"
    assert detect_revision_kind("Make the summary simpler.") == "plan_only"


# ── 9. propose_new_files never pretends to be verified ──────────────────────


def test_propose_new_files_labeled_proposed():
    proposals = propose_new_files("Simple Local Notes Export Feature")
    assert proposals
    for item in proposals:
        assert "(proposed" in item
        assert item.startswith("jarvis_core/")


# ── 10. Scanner emits only real existing paths ───────────────────────────────


def test_scan_repository_emits_only_existing_paths():
    hits = _scan_repository("Simple Local Notes Export Feature")
    assert hits
    for hit in hits:
        path = Path(hit["path"])
        assert path.exists(), f"scanner emitted a non-existent path: {hit['path']}"
    plains = " ".join(h["plain"] for h in hits)
    assert "notes" in plains.lower()


# ── 11. Version bumping (R0 → R1) ────────────────────────────────────────────


def test_research_revision_increments(orch):
    feature_id = _researched(orch, "Jarvis add a simple local notes export feature")
    first = orch.request_changes(
        feature_id,
        "Inspect the repository again for real notes paths.",
        telegram_user_id=123,
    )
    assert first["research_revision"] == 1
# ── F. ChatGPT output provenance (exact, hashed, never rewritten) ────────────


def test_chatgpt_provenance_records_exact_sha256(orch):
    feature_id = _researched(orch, "Jarvis add a simple local notes export feature")
    payload = "ChatGPT clone: the proposed architecture is sound.\nReuse existing."
    orch.submit_chatgpt_review(feature_id, payload)
    feature = orch._store.get(feature_id)

    assert feature.chatgpt_review == payload, "exact submitted text must be preserved"
    assert feature.chatgpt_provenance
    prov = feature.chatgpt_provenance
    assert prov["source"] == "user_submitted_chatgpt_output"
    assert prov["feature_id"] == feature_id
    assert prov["text_length"] == len(payload)
    import hashlib

    assert prov["sha256"] == hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_chatgpt_output_not_rewritten_by_local_model(orch):
    feature_id = _researched(orch, "Jarvis add a simple local notes export feature")
    payload = "The local model must NOT rewrite this exact sentence."
    orch.submit_chatgpt_review(feature_id, payload)
    feature = orch._store.get(feature_id)
    assert feature.chatgpt_review == payload


# ── I. NEED_MORE_RESEARCH path ─────────────────────────────────────────────


def test_need_more_research_returns_to_phase1(orch):
    feature_id = _researched(orch, "Jarvis add a simple local notes export feature")
    result = orch.submit_chatgpt_review(
        feature_id,
        "NEED_MORE_RESEARCH\n- locate the real Notes storage\n- verify the export path",
    )
    assert result["action"] == "more_research"
    feature = orch._store.get(feature_id)
    assert feature.needs_more_research is True
    assert "Notes storage" in " ".join(feature.missing_evidence)
    assert feature.chatgpt_reviewed is False
    # ACTION_RESEARCH queued back to Phase 1.
    queued = []
    while True:
        try:
            queued.append(orch._queue.get_nowait())
        except Exception:
            break
    assert any(a == ACTION_RESEARCH for a, _ in queued), "must re-enter research"


def test_need_more_research_cleared_after_refresh(orch):
    feature_id = _researched(orch, "Jarvis add a simple local notes export feature")
    orch.submit_chatgpt_review(feature_id, "NEED_MORE_RESEARCH\n- find the notes storage")
    assert orch._store.get(feature_id).needs_more_research is True

    with patch("jarvis_core.feature_research.web_research", _fake_web()):
        orch._run_action(ACTION_RESEARCH, feature_id)

    refreshed = orch._store.get(feature_id)
    assert refreshed.needs_more_research is False
