"""Part 11 — Approval → Implementation / Execution bridge.

Parts 8-10 stop the feature at ``approval_pending``; approval finalises to
``cline_ready`` (an approved Cline task is produced). Part 11 adds the missing
bridge that hands the approved task to the EXISTING ClineBridge and drives:

    cline_ready → implementation_in_progress → completed | failed

Kiro is used only to build Jarvis. The Jarvis-runtime Cline integration
(``cline_bridge``) is preserved; the real implementation run reports its
outcome back via ``record_implementation_result`` (success is never faked).

These tests cover: normal start-once, double approval/start, duplicate
callback, unauthorized rejection, successful implementation, failure, restart
during implementation (no duplicate execution), and result/proof persistence.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from jarvis_core.feature_models import FeatureRequest, FeatureStatus
from jarvis_core.feature_orchestrator import (
    ACTION_BUILD_PLAN,
    ACTION_RESEARCH,
    FeatureOrchestrator,
)
from jarvis_core.feature_store import FeatureStore
from jarvis_core.task_manager import TaskManager


class _FakeOllama:
    def __init__(self):
        self.reply = "- Reuse existing services.\n- Add a focused module."
        self.error = None

    def __call__(self, message, mode=None, *, purpose="chat", **kwargs):
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
    orchestrator.start_worker = lambda: None  # type: ignore[method-assign]
    orchestrator.set_telegram_channels(
        send=lambda chat_id, text, markup=None: None,
        is_authorized=lambda uid: uid == 123,
    )
    yield orchestrator
    orchestrator.stop_worker(timeout=1)


def _fake_web(ok=True):
    def _research(objective):
        return {
            "sources": [{"url": "https://example.com/docs", "ok": ok,
                         "snippet": "Notes API." if ok else "",
                         "error": None if ok else "ConnectionError"}],
            "incomplete": not ok,
            "error": None if ok else "ConnectionError",
        }
    return _research


def _approved(orch, telegram_user_id=123):
    """Drive a feature all the way to cline_ready (approved)."""
    fid = orch.submit_feature_request(
        "Jarvis add a simple local notes export feature", source="Telegram",
        telegram_user_id=123, telegram_chat_id=100,
    )["feature_id"]
    with patch("jarvis_core.feature_research.web_research", _fake_web(ok=True)):
        orch.continue_after_resource_review(fid, telegram_user_id=123)
        orch._run_action(ACTION_RESEARCH, fid)
    orch.skip_chatgpt(fid)
    orch._run_action(ACTION_BUILD_PLAN, fid)
    assert orch.get_feature(fid)["status"] == FeatureStatus.approval_pending.value
    res = orch.approve_cline(fid, telegram_user_id)
    assert res["status"] == "ok"
    assert orch.get_feature(fid)["status"] == FeatureStatus.cline_ready.value
    return fid


# ── A. Normal approval → implementation starts exactly once ──────────────────


def test_part11_start_implementation_once(orch, ollama):
    fid = _approved(orch)
    with patch("jarvis_core.cline_bridge.ClineBridge.mark_handoff",
               return_value=True) as mh:
        res = orch.start_implementation(fid, telegram_user_id=123)

    assert res["status"] == "ok"
    assert res["action"] == "implementation_started"
    assert mh.call_count == 1, "handed to Cline exactly once"
    feat = orch.get_feature(fid)
    assert feat["status"] == FeatureStatus.implementation_in_progress.value
    assert feat["implementation_started"] is True


# ── B. Double approval / double start → implementation once ──────────────────


def test_part11_double_start_implements_once(orch, ollama):
    fid = _approved(orch)
    with patch("jarvis_core.cline_bridge.ClineBridge.mark_handoff",
               return_value=True) as mh:
        first = orch.start_implementation(fid, telegram_user_id=123)
        second = orch.start_implementation(fid, telegram_user_id=123)

    assert first["status"] == "ok"
    assert second["status"] == "already_running"
    assert mh.call_count == 1, "no duplicate Cline handoff"


def test_part11_double_approval_then_start_once(orch, ollama):
    """A second approval on the now cline_ready feature is rejected, and start
    still launches implementation exactly once."""
    fid = _approved(orch)
    second_approve = orch.approve_cline(fid, 123)
    assert second_approve["status"] == "error", "re-approval rejected"

    with patch("jarvis_core.cline_bridge.ClineBridge.mark_handoff",
               return_value=True) as mh:
        orch.start_implementation(fid, 123)
        orch.start_implementation(fid, 123)
    assert mh.call_count == 1


# ── C. Duplicate Telegram callback → implementation once ─────────────────────


def test_part11_duplicate_callback_implements_once(orch, ollama):
    fid = _approved(orch)
    with patch("jarvis_core.cline_bridge.ClineBridge.mark_handoff",
               return_value=True) as mh:
        for _ in range(3):  # same callback delivered three times
            orch.start_implementation(fid, telegram_user_id=123)
    assert mh.call_count == 1


# ── D. Unauthorized approval / start → rejected ──────────────────────────────


def test_part11_unauthorized_start_rejected(orch, ollama):
    fid = _approved(orch)
    with patch("jarvis_core.cline_bridge.ClineBridge.mark_handoff",
               return_value=True) as mh:
        res = orch.start_implementation(fid, telegram_user_id=999)

    assert res["status"] == "error"
    assert mh.call_count == 0, "unauthorized user starts nothing"
    feat = orch.get_feature(fid)
    assert feat["implementation_started"] is False
    assert feat["status"] == FeatureStatus.cline_ready.value


# ── E. Successful implementation → completed + proof persisted ───────────────


def test_part11_success_completes_and_persists_proof(orch, ollama):
    fid = _approved(orch)
    orch.start_implementation(fid, 123)
    proof_before = len(orch._store.get(fid).proof_history)

    res = orch.record_implementation_result(
        fid, success=True, summary="Notes export module added", verified=True)

    assert res["status"] == "ok"
    feat = orch.get_feature(fid)
    assert feat["status"] == FeatureStatus.completed.value
    # (7)(H) result + proof persisted.
    model = orch._store.get(fid)
    assert model.implementation_result["success"] is True
    assert model.implementation_result["verified"] is True
    assert model.implementation_result["summary"] == "Notes export module added"
    assert len(model.proof_history) == proof_before + 1
    assert model.proof_history[-1]["phase"] == "Implement & Verify"


# ── F. Implementation failure → failed, never fake success ───────────────────


def test_part11_failure_marks_failed_never_fakes_success(orch, ollama):
    fid = _approved(orch)
    orch.start_implementation(fid, 123)

    res = orch.record_implementation_result(
        fid, success=False, error="Cline could not modify app.py")

    assert res["status"] == "ok"
    feat = orch.get_feature(fid)
    assert feat["status"] == FeatureStatus.failed.value
    model = orch._store.get(fid)
    assert model.implementation_result["success"] is False
    assert "app.py" in model.implementation_result["error"]
    # Never fabricated a completion.
    assert feat["status"] != FeatureStatus.completed.value


def test_part11_result_only_valid_while_in_progress(orch, ollama):
    """A result cannot be recorded before implementation has started, and a
    second result after a terminal state is ignored (idempotent)."""
    fid = _approved(orch)
    early = orch.record_implementation_result(fid, success=True, summary="x")
    assert early["status"] == "error", "cannot record before starting"

    orch.start_implementation(fid, 123)
    orch.record_implementation_result(fid, success=True, summary="done")
    again = orch.record_implementation_result(fid, success=False, error="late")
    assert again["status"] == "already_recorded"
    assert orch.get_feature(fid)["status"] == FeatureStatus.completed.value


# ── G. Restart during implementation → no duplicate execution ────────────────


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


def test_part11_restart_during_implementation_no_duplicate(orch, ollama, store_root):
    fid = _approved(orch)
    with patch("jarvis_core.cline_bridge.ClineBridge.mark_handoff",
               return_value=True):
        orch.start_implementation(fid, 123)
    assert orch.get_feature(fid)["status"] == \
        FeatureStatus.implementation_in_progress.value

    restarted = _restart(store_root)
    try:
        # In-progress must NOT be auto-resumed (would re-invoke Cline).
        assert all(f != fid for _, f in restarted._resume_actions)
        assert restarted.get_feature(fid)["status"] == \
            FeatureStatus.implementation_in_progress.value
        # A start attempt after restart is still a no-op (persisted guard).
        with patch("jarvis_core.cline_bridge.ClineBridge.mark_handoff",
                   return_value=True) as mh:
            again = restarted.start_implementation(fid, 123)
        assert again["status"] == "already_running"
        assert mh.call_count == 0, "restart never re-launches implementation"
        # The reported result can still complete the feature after restart.
        restarted.record_implementation_result(fid, success=True, summary="done")
        assert restarted.get_feature(fid)["status"] == \
            FeatureStatus.completed.value
    finally:
        restarted.stop_worker(timeout=1)


# ── H. Preservation of feature context through execution ─────────────────────


def test_part11_preserves_feature_context(orch, ollama):
    fid = _approved(orch)
    before: FeatureRequest = orch._store.get(fid)
    orig_request = before.original_request
    plan = before.plan
    approved_at = before.approved_at
    chat_id = before.telegram_chat_id

    orch.start_implementation(fid, 123)
    orch.record_implementation_result(fid, success=True, summary="done", verified=True)

    after: FeatureRequest = orch._store.get(fid)
    assert after.feature_id == fid
    assert after.original_request == orig_request
    assert after.plan == plan
    assert after.approved_at == approved_at
    assert after.telegram_chat_id == chat_id
    assert after.cline_task is not None
    assert after.observed_facts, "research preserved"
    assert len(after.history) >= 3, "history preserved/extended"
