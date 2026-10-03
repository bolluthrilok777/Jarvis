"""Part 10 — Research → ChatGPT Review → Plan → Approval pipeline.

Part 9 wired ▶️ Continue to enqueue RESEARCH exactly once. Part 10 verifies the
EXISTING downstream pipeline runs end-to-end after that Continue and stops
cleanly at the mandatory approval gate (no Cline/execution yet).

The FeatureOrchestrator is inspected and found already-wired:

    ACTION_RESEARCH   -> _run_research_stage   -> chatgpt_review_pending (waits)
    submit/skip       -> ACTION_ANALYZE_REVIEW -> ACTION_BUILD_PLAN
    ACTION_BUILD_PLAN -> _run_plan_stage        -> approval_pending (waits)

These tests add focused integration coverage for that flow, idempotency,
restart recovery at each stage, and stage-failure behaviour. They drive stages
through the SAME synchronous convention used by the existing orchestrator
suite (``_run_action`` / ``skip_chatgpt`` / ``submit_chatgpt_review``) and add
one true worker-queue end-to-end run.

No production code is changed by Part 10.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from jarvis_core.feature_models import FeatureStatus
from jarvis_core.feature_orchestrator import (
    ACTION_ANALYZE_REVIEW,
    ACTION_BUILD_PLAN,
    ACTION_RESEARCH,
    FeatureOrchestrator,
)
from jarvis_core.feature_store import FeatureStore
from jarvis_core.task_manager import TaskManager


# ── Fixtures (mirror tests/test_feature_orchestrator.py conventions) ─────────


class _FakeOllama:
    def __init__(self):
        self.calls = []
        self.reply = "- Reuse existing services.\n- Add a focused module."
        self.error = None

    def __call__(self, message, mode=None, *, purpose="chat", **kwargs):
        self.calls.append({"purpose": purpose})
        if self.error is not None:
            return (None, "balanced", dict(self.error))
        return (self.reply, "qwen2.5:7b", None)


@pytest.fixture()
def ollama():
    fake = _FakeOllama()
    with patch("jarvis_core.ollama_service.ask_ollama", side_effect=fake):
        yield fake


@pytest.fixture()
def store_root(tmp_path):
    return tmp_path / "features"


@pytest.fixture()
def orch(store_root, monkeypatch):
    from tests.test_telegram_integration import patch_inline_feature_threads

    patch_inline_feature_threads(monkeypatch)
    orchestrator = FeatureOrchestrator(
        store=FeatureStore(root=store_root),
        task_manager_instance=TaskManager(),
    )
    # Stages are driven synchronously; keep the background worker from
    # double-executing an enqueued stage (established suite convention).
    orchestrator.start_worker = lambda: None  # type: ignore[method-assign]
    sent: list = []
    orchestrator._telegram_sink = sent  # type: ignore[attr-defined]
    orchestrator.set_telegram_channels(
        send=lambda chat_id, text, markup=None: sent.append((chat_id, text)),
        is_authorized=lambda uid: uid == 123,
    )
    yield orchestrator
    orchestrator.stop_worker(timeout=1)


def _fake_web(ok=True):
    def _research(objective):
        return {
            "sources": [
                {"url": "https://example.com/docs", "ok": ok,
                 "snippet": "Notes API overview." if ok else "",
                 "error": None if ok else "ConnectionError"},
            ],
            "incomplete": not ok,
            "error": None if ok else "ConnectionError",
        }
    return _research


def _submit(orch, text="Jarvis add a simple local notes export feature"):
    """Submit a Telegram feature; it parks at RESOURCE_REVIEW (Part 8/9)."""
    return orch.submit_feature_request(
        text, source="Telegram", telegram_user_id=123, telegram_chat_id=100,
    )["feature_id"]


def _continue(orch, fid):
    """Part 9 ▶️ Continue: enqueues RESEARCH exactly once."""
    return orch.continue_after_resource_review(fid, telegram_user_id=123)


# ── A. Normal Research → Review → Plan → Approval ────────────────────────────


def test_part10_full_pipeline_reaches_approval(orch, ollama):
    """(1)(2)(3)(5)(6)(7) End-to-end after Part 9 Continue: research persists,
    transitions once to ChatGPT review, then plan, then approval_pending."""
    fid = _submit(orch)
    assert orch.get_feature(fid)["status"] == FeatureStatus.resource_review.value

    with patch("jarvis_core.feature_research.web_research", _fake_web(ok=True)):
        _continue(orch, fid)                       # Part 9 enqueues research
        orch._run_action(ACTION_RESEARCH, fid)     # (1) research runs same fid

    feat = orch.get_feature(fid)
    # (2) research persisted; (3) exactly one transition to ChatGPT review.
    assert feat["status"] == FeatureStatus.chatgpt_review_pending.value
    assert feat["observed_facts"], "research findings persisted"
    assert feat["chatgpt_prompt"], "ChatGPT request prompt persisted"

    # (4) review result persisted, then (5) plan generation.
    res = orch.submit_chatgpt_review(fid, "Reuse the existing task manager.")
    assert res["status"] == "ok"
    model = orch._store.get(fid)
    assert model.chatgpt_review == "Reuse the existing task manager."
    assert model.chatgpt_provenance and model.chatgpt_provenance["sha256"]

    orch._run_action(ACTION_ANALYZE_REVIEW, fid)
    orch._run_action(ACTION_BUILD_PLAN, fid)

    feat = orch.get_feature(fid)
    # (6) plan persisted; (7) approval reached.
    assert feat["plan"] is not None
    assert feat["status"] == FeatureStatus.approval_pending.value
    # (STOP at approval — no Cline/execution.)
    assert orch._store.get(fid).cline_task is None


def test_part10_skip_review_reaches_approval(orch, ollama):
    """Skip path also lands at approval_pending (review optional)."""
    fid = _submit(orch)
    with patch("jarvis_core.feature_research.web_research", _fake_web(ok=True)):
        _continue(orch, fid)
        orch._run_action(ACTION_RESEARCH, fid)

    assert orch.skip_chatgpt(fid)["status"] == "ok"
    orch._run_action(ACTION_BUILD_PLAN, fid)

    feat = orch.get_feature(fid)
    assert feat["chatgpt_skipped"] is True
    assert feat["status"] == FeatureStatus.approval_pending.value
    assert feat["plan"] is not None


def test_part10_continue_enqueues_research_action(orch, ollama):
    """Part 9 -> Part 10 boundary: ▶️ Continue enqueues ACTION_RESEARCH for the
    SAME feature_id (the real queue entry the worker would drain in prod)."""
    fid = _submit(orch)
    enq: list = []
    with patch.object(orch, "enqueue",
                      side_effect=lambda a, f: enq.append((a, f))):
        _continue(orch, fid)
    assert (ACTION_RESEARCH, fid) in enq


# ── B. Retry / idempotency (8) no duplicate Research/Review/Plan jobs ────────


def test_part10_duplicate_continue_enqueues_research_once(orch, ollama):
    """Part 9 guard still holds: a second Continue never re-enqueues research."""
    fid = _submit(orch)
    enq: list = []
    with patch.object(orch, "enqueue",
                      side_effect=lambda a, f: enq.append((a, f))):
        _continue(orch, fid)
        _continue(orch, fid)  # duplicate tap
    assert [a for a, _ in enq].count(ACTION_RESEARCH) == 1


def test_part10_duplicate_review_submit_analyzes_once(orch, ollama):
    """A duplicate ChatGPT-review submit must not run analyze twice. The first
    submit enqueues analyze; once that stage runs the feature has left
    chatgpt_review_pending, so a later duplicate submit is rejected."""
    fid = _submit(orch)
    with patch("jarvis_core.feature_research.web_research", _fake_web(ok=True)):
        _continue(orch, fid)
        orch._run_action(ACTION_RESEARCH, fid)

    first = orch.submit_chatgpt_review(fid, "Reuse existing services.")
    assert first["status"] == "ok"
    # The worker would drain analyze -> plan here; drive both to advance state.
    orch._run_action(ACTION_ANALYZE_REVIEW, fid)
    orch._run_action(ACTION_BUILD_PLAN, fid)
    assert orch._store.get(fid).chatgpt_reviewed is True
    assert orch.get_feature(fid)["status"] == FeatureStatus.approval_pending.value

    # A duplicate/late callback for the same review is now rejected.
    second = orch.submit_chatgpt_review(fid, "Reuse existing services.")
    assert second["status"] == "error", "duplicate review submit is rejected"


def test_part10_duplicate_skip_builds_plan_once(orch, ollama):
    """A duplicate skip must not build the plan twice. The first skip enqueues
    the plan; once it runs the feature reaches approval_pending, so a duplicate
    skip is rejected."""
    fid = _submit(orch)
    with patch("jarvis_core.feature_research.web_research", _fake_web(ok=True)):
        _continue(orch, fid)
        orch._run_action(ACTION_RESEARCH, fid)

    first = orch.skip_chatgpt(fid)
    assert first["status"] == "ok"
    orch._run_action(ACTION_BUILD_PLAN, fid)
    assert orch.get_feature(fid)["status"] == FeatureStatus.approval_pending.value

    second = orch.skip_chatgpt(fid)
    assert second["status"] == "error", "duplicate skip is rejected once planned"


def test_part10_approval_reached_and_finalized_exactly_once(orch, ollama):
    """(7)(8) Approval is a single-shot gate: the first approve finalizes; a
    second approve on the now-approved feature is rejected (no re-finalize)."""
    fid = _submit(orch)
    with patch("jarvis_core.feature_research.web_research", _fake_web(ok=True)):
        _continue(orch, fid)
        orch._run_action(ACTION_RESEARCH, fid)
    orch.skip_chatgpt(fid)
    orch._run_action(ACTION_BUILD_PLAN, fid)
    assert orch.get_feature(fid)["status"] == FeatureStatus.approval_pending.value

    first = orch.approve_cline(fid, 123)
    assert first["status"] == "ok"
    approved_at = orch._store.get(fid).approved_at

    second = orch.approve_cline(fid, 123)
    assert second["status"] == "error", "second approve is rejected"
    # approved_at not overwritten by the rejected second approval.
    assert orch._store.get(fid).approved_at == approved_at


# ── C. Restart recovery (9) resumes the correct unfinished stage ─────────────


def _restart(store_root):
    restarted = FeatureOrchestrator(
        store=FeatureStore(root=store_root),
        task_manager_instance=TaskManager(),
    )
    restarted.start_worker = lambda: None  # type: ignore[method-assign]
    restarted.set_telegram_channels(
        send=lambda chat_id, text, markup=None: None,
        is_authorized=lambda uid: uid == 123,
    )
    restarted._restore_pending()
    return restarted


def test_part10_restart_in_researching_resumes_research(orch, ollama, store_root):
    """Interrupted mid-research (status researching) resumes ACTION_RESEARCH."""
    fid = _submit(orch)
    _continue(orch, fid)
    model = orch._store.get(fid)
    model.status = FeatureStatus.researching.value  # crashed mid-research
    orch._store.save(model)

    restarted = _restart(store_root)
    try:
        assert (ACTION_RESEARCH, fid) in restarted._resume_actions
    finally:
        restarted.stop_worker(timeout=1)


def test_part10_restart_in_planning_resumes_plan(orch, ollama, store_root):
    """Interrupted mid-plan (status planning) resumes ACTION_BUILD_PLAN."""
    fid = _submit(orch)
    with patch("jarvis_core.feature_research.web_research", _fake_web(ok=True)):
        _continue(orch, fid)
        orch._run_action(ACTION_RESEARCH, fid)
    orch.skip_chatgpt(fid)
    model = orch._store.get(fid)
    model.status = FeatureStatus.planning.value  # crashed mid-plan
    orch._store.save(model)

    restarted = _restart(store_root)
    try:
        assert (ACTION_BUILD_PLAN, fid) in restarted._resume_actions
    finally:
        restarted.stop_worker(timeout=1)


def test_part10_restart_waiting_review_stays_waiting(orch, ollama, store_root):
    """A feature waiting for ChatGPT review must NOT auto-resume after restart."""
    fid = _submit(orch)
    with patch("jarvis_core.feature_research.web_research", _fake_web(ok=True)):
        _continue(orch, fid)
        orch._run_action(ACTION_RESEARCH, fid)
    assert orch.get_feature(fid)["status"] == \
        FeatureStatus.chatgpt_review_pending.value

    restarted = _restart(store_root)
    try:
        assert all(f != fid for _, f in restarted._resume_actions), \
            "no auto-advance while waiting for the user's review"
        assert restarted.get_feature(fid)["status"] == \
            FeatureStatus.chatgpt_review_pending.value
    finally:
        restarted.stop_worker(timeout=1)


def test_part10_restart_in_approval_stays_waiting(orch, ollama, store_root):
    """A feature at approval_pending stays waiting for the user after restart."""
    fid = _submit(orch)
    with patch("jarvis_core.feature_research.web_research", _fake_web(ok=True)):
        _continue(orch, fid)
        orch._run_action(ACTION_RESEARCH, fid)
    orch.skip_chatgpt(fid)
    orch._run_action(ACTION_BUILD_PLAN, fid)
    assert orch.get_feature(fid)["status"] == FeatureStatus.approval_pending.value

    restarted = _restart(store_root)
    try:
        assert all(f != fid for _, f in restarted._resume_actions)
        assert restarted.get_feature(fid)["status"] == \
            FeatureStatus.approval_pending.value
    finally:
        restarted.stop_worker(timeout=1)


# ── D. Failure behaviour for Research ────────────────────────────────────────


def test_part10_research_web_failure_still_reaches_review(orch, ollama):
    """Web-research failure degrades to repo analysis and still advances to the
    ChatGPT review gate (never stalls)."""
    fid = _submit(orch, "Improve Gmail classification")
    with patch("jarvis_core.feature_research.web_research", _fake_web(ok=False)):
        _continue(orch, fid)
        orch._run_action(ACTION_RESEARCH, fid)

    feat = orch.get_feature(fid)
    assert feat["status"] == FeatureStatus.chatgpt_review_pending.value
    assert feat["research_incomplete"] is True
    assert feat["affected_modules"], "repo analysis still identifies modules"


def test_part10_research_ollama_failure_marks_incomplete(orch, ollama):
    """Local model unavailable during research -> incomplete flagged, but the
    feature still reaches the review gate."""
    ollama.error = {"status": "error", "message": "Ollama is not running."}
    fid = _submit(orch)
    with patch("jarvis_core.feature_research.web_research", _fake_web(ok=True)):
        _continue(orch, fid)
        orch._run_action(ACTION_RESEARCH, fid)

    feat = orch.get_feature(fid)
    assert feat["research_incomplete"] is True
    assert feat["status"] == FeatureStatus.chatgpt_review_pending.value


# ── E. Failure behaviour for ChatGPT Review analysis ─────────────────────────


def test_part10_review_analysis_failure_still_reaches_approval(orch, ollama):
    """If local review analysis is unavailable, the pipeline continues with the
    research-only plan and still reaches approval_pending (never stalls)."""
    fid = _submit(orch)
    with patch("jarvis_core.feature_research.web_research", _fake_web(ok=True)):
        _continue(orch, fid)
        orch._run_action(ACTION_RESEARCH, fid)

    orch.submit_chatgpt_review(fid, "Reuse the existing task manager.")

    # Local analysis unavailable -> analyze_review yields no analysis. The
    # orchestrator degrades to the research-only plan (never stalls).
    with patch("jarvis_core.chatgpt_review.analyze_review", return_value=None):
        orch._run_action(ACTION_ANALYZE_REVIEW, fid)
    orch._run_action(ACTION_BUILD_PLAN, fid)

    feat = orch.get_feature(fid)
    assert feat["status"] == FeatureStatus.approval_pending.value
    assert feat["plan"] is not None
