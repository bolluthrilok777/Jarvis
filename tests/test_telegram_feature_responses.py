"""Telegram feature-workflow button routing tests.

Every visible ``feature:*`` inline button of the Autonomous Feature Orchestrator
workflow must be routed by ``TelegramService`` and produce a real response:

  Feature Development (mode) -> Resource Review (resrc_*) -> Research Complete
  -> ChatGPT Request Ready (chatgpt_show / review / chatgpt_skip / proof /
     apps / cancel) -> Plan & Approval (plan_view / prompt_view / approve /
     confirm_high_risk / cancel_high_risk / changes / missing / reresearch).

Regression: the dispatcher referenced ``_proof_action``, ``_approve_action``,
``_model_view_action``, ``_high_risk_action``, ``_arm_changes``,
``_missing_action`` and ``_reresearch_action`` before they were implemented -
pressing those buttons raised ``AttributeError`` instead of answering. These
tests drive the REAL callback path (``process_update``) against a real
FeatureOrchestrator with offline research/scan, so every button is proven in
production wiring.
"""
from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from jarvis_core import resource_manager
from jarvis_core.feature_models import FeatureStatus
from jarvis_core.feature_orchestrator import (
    ACTION_BUILD_PLAN,
    ACTION_RESEARCH,
    FeatureOrchestrator,
)
from jarvis_core.feature_store import FeatureStore
from jarvis_core.task_manager import TaskManager
from jarvis_core.telegram_service import TelegramService

from tests.test_telegram_integration import (
    FakeAPI,
    _message,
    _settings,
    patch_inline_feature_threads,
)


class _FakeOllama:
    def __call__(self, message, mode=None, *, purpose="chat", **kwargs):
        return ("- Reuse existing services.\n- Add a focused module.",
                "qwen2.5:7b", None)


def _fake_web():
    def _research(objective):
        return {
            "sources": [{"url": "https://example.com/notes", "ok": True,
                         "snippet": "Notes export API overview.",
                         "error": None}],
            "incomplete": False,
            "error": None,
        }

    return _research


def _snapshot(feature_id, generation=1):
    return {
        "generation": generation,
        "feature_id": feature_id,
        "applications": [{
            "resource_id": f"r{generation}-a1", "feature_id": feature_id,
            "resource_type": "application", "title": "Calculator",
            "display_name": "Calculator", "pid": 100, "window_handle": 100,
            "executable": "calc.exe", "close_allowed": True,
            "close_requires_confirmation": False,
        }],
        "tabs": [],
        "chrome_detected": False,
        "chrome_controllable": False,
        "chrome_reason": "",
        "scanned_at": "",
    }


@pytest.fixture()
def ollama():
    with patch("jarvis_core.ollama_service.ask_ollama",
               side_effect=_FakeOllama()):
        yield


@pytest.fixture()
def orch(ollama, tmp_path, monkeypatch):
    """Isolated FeatureOrchestrator: offline research, inline threads, an
    empty real-looking scan (no live Windows/Chrome introspection)."""
    patch_inline_feature_threads(monkeypatch)
    monkeypatch.setattr(
        resource_manager.resource_registry, "scan",
        lambda feature_id=None, cdp_url=None: _snapshot(feature_id))
    store = FeatureStore(root=tmp_path / "features")
    orchestrator = FeatureOrchestrator(
        store=store, task_manager_instance=TaskManager())
    orchestrator.start_worker = lambda: None  # type: ignore[method-assign]
    orchestrator._sent_telegram = []
    orchestrator.set_telegram_channels(
        send=lambda chat_id, text, markup=None: orchestrator._sent_telegram.append(
            {"text": text, "markup": markup}),
        is_authorized=lambda uid: uid == 123,
    )
    # Proof artifacts must never leak into the real repo during tests. Patch
    # feature_proof.BASE_DIR together with PROOF_DIR so relative_to(BASE_DIR)
    # inside capture_proof keeps resolving.
    monkeypatch.setattr("jarvis_core.feature_proof.BASE_DIR", tmp_path)
    monkeypatch.setattr(
        "jarvis_core.feature_proof.PROOF_DIR",
        tmp_path / "data" / "features" / "proofs")
    yield orchestrator
    orchestrator.stop_worker(timeout=1)


@pytest.fixture()
def tg(orch):
    api = FakeAPI()
    service = TelegramService(
        settings=_settings(123), api=api, orchestrator_instance=orch)
    service._start_task_watcher = Mock()
    return service, orch, api


def _callback(callback_id, user_id, chat_id, data):
    return {
        "update_id": 900,
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}},
            "data": data,
        },
    }


def _researched(orch, objective="Jarvis add a simple local notes export feature"):
    """Create + research a feature; leaves it in chatgpt_review_pending."""
    submitted = orch.submit_feature_request(
        objective, source="Telegram",
        telegram_user_id=123, telegram_chat_id=100)
    feature_id = submitted["feature_id"]
    with patch("jarvis_core.feature_research.web_research", _fake_web()):
        orch._run_action(ACTION_RESEARCH, feature_id)
    return feature_id


def _all_texts(orch, api):
    """Service-sent messages + orchestrator-sent Telegram payloads."""
    return " ".join(
        [m["text"] for m in api.messages]
        + [s["text"] for s in getattr(orch, "_sent_telegram", [])])


def _all_markups(orch, api):
    return (
        [s["markup"] for s in getattr(orch, "_sent_telegram", []) if s["markup"]]
        + [m["reply_markup"] for m in api.messages if m["reply_markup"]]
    )


def _planned(orch, feature_id):
    """Drive an already-researched feature to approval_pending via skip."""
    orch.skip_chatgpt(feature_id)
    orch._run_action(ACTION_BUILD_PLAN, feature_id)
    return feature_id


# ── Dispatch guards ──────────────────────────────────────────────────────────


def test_previously_missing_feature_handlers_exist():
    """The buttons the dispatcher routes must have real methods (regression:
    they were referenced but not implemented -> AttributeError on press)."""
    for name in ("_proof_action", "_approve_action", "_model_view_action",
                 "_high_risk_action", "_arm_changes", "_missing_action",
                 "_reresearch_action", "_apps_action", "_skip_chatgpt_action",
                 "_chatgpt_show_action", "_cancel_feature_action"):
        assert callable(getattr(TelegramService, name, None)), name


def test_notes_feature_telegram_flow_reaches_chatgpt_request_ready(tg):
    """'add a new notes feature' -> Feature Development -> Resource Review
    auto-continue -> Research Complete -> ChatGPT Request Ready keyboard with
    every visible button routed."""
    service, orch, api = tg

    service.process_update(
        _message(123, "add a new feature of notes monitor", chat_id=100))
    texts = " ".join(m["text"] for m in api.messages)
    assert "How should I handle this request?" in texts

    service.process_update(_callback("ig1", 123, 100, "feature:mode:feature"))
    features = orch.list_features()
    assert len(features) == 1, "feature must be created by the Feature closure"
    fid = features[0]["feature_id"]
    feature = orch.get_feature(fid)
    assert "notes" in (feature["interpreted_objective"] or "").lower()
    assert feature["status"] == FeatureStatus.resource_review_pending.value

    # Explicit Continue from Resource Review:
    service.process_update(_callback("ig2", 123, 100, f"feature:resrc_continue:{fid}"))

    with patch("jarvis_core.feature_research.web_research", _fake_web()):
        orch._run_action(ACTION_RESEARCH, fid)

    feature = orch.get_feature(fid)
    assert feature["status"] == FeatureStatus.chatgpt_review_pending.value
    texts = _all_texts(orch, api)
    assert "RESEARCH COMPLETE" in texts
    assert "CHATGPT REQUEST READY" in texts

    flat = [b for mk in _all_markups(orch, api)
            for row in mk["inline_keyboard"] for b in row]
    callbacks = [b["callback_data"] for b in flat]
    for cb in (f"feature:chatgpt_show:{fid}", f"feature:review:{fid}",
               f"feature:chatgpt_skip:{fid}", f"feature:cancel:{fid}",
               f"feature:proof:{fid}", f"feature:apps:{fid}"):
        assert cb in callbacks, f"missing routed button {cb}"


# ── ChatGPT Request Ready stage buttons ──────────────────────────────────────


def test_view_chatgpt_request_button_responds(tg):
    service, orch, api = tg
    fid = _researched(orch)
    service.process_update(
        _callback("cb_show", 123, 100, f"feature:chatgpt_show:{fid}"))
    last = api.messages[-1]["text"]
    assert "ChatGPT request:" in last
    callbacks = [c[1] for c in api.callbacks]
    assert "Showing request" in callbacks


def test_proof_button_responds_with_current_work_proof(tg):
    service, orch, api = tg
    fid = _researched(orch)
    service.process_update(
        _callback("cb_proof", 123, 100, f"feature:proof:{fid}"))
    callbacks = [c[1] for c in api.callbacks]
    assert "Proof" in callbacks
    texts = " ".join(m["text"] for m in api.messages)
    assert "CURRENT WORK PROOF" in texts
    assert "Phase: Analyze & Verify" in texts


def test_skip_chatgpt_button_marks_skipped(tg):
    service, orch, api = tg
    fid = _researched(orch)
    service.process_update(
        _callback("cb_skip", 123, 100, f"feature:chatgpt_skip:{fid}"))
    assert orch.get_feature(fid)["chatgpt_skipped"] is True
    callbacks = [c[1] for c in api.callbacks]
    assert "Skipping ChatGPT" in callbacks


def test_apps_tabs_keyboard_back_routes_to_chatgpt_view(tg):
    service, orch, api = tg
    fid = _researched(orch)
    service.process_update(
        _callback("cb_at", 123, 100, f"feature:apps:{fid}"))
    last = api.messages[-1]
    assert "RESOURCES USED FOR THIS FEATURE" in last["text"]
    flat = [b for row in last["reply_markup"]["inline_keyboard"] for b in row]
    callbacks = [b["callback_data"] for b in flat]
    assert f"feature:chatgpt_show:{fid}" in callbacks  # Back button
    assert f"feature:apps_refresh:{fid}" in callbacks  # Refresh = NEW scan
    assert f"feature:cancel:{fid}" in callbacks        # Cancel Feature button


# ── Plan & Approval stage buttons ────────────────────────────────────────────


def test_plan_view_button_responds_with_full_plan(tg):
    service, orch, api = tg
    fid = _planned(orch, _researched(orch))
    service.process_update(
        _callback("cb_plan", 123, 100, f"feature:plan_view:{fid}"))
    texts = " ".join(m["text"] for m in api.messages)
    assert "FULL FEATURE PLAN" in texts
    assert "Objective:" in texts
    assert "Implementation steps:" in texts


def test_prompt_view_button_shows_exact_cline_prompt(tg):
    service, orch, api = tg
    fid = _researched(orch)
    model = orch._store.get(fid)
    model.status = FeatureStatus.approval_pending.value
    model.cline_candidate_prompt = "Implement the notes export module exactly."
    model.prompt_source = "ChatGPT"
    orch._store.save(model)
    service.process_update(
        _callback("cb_pv", 123, 100, f"feature:prompt_view:{fid}"))
    texts = " ".join(m["text"] for m in api.messages)
    assert "EXACT CLINE PROMPT" in texts
    assert "Implement the notes export module exactly." in texts


def test_approve_button_advances_feature_to_cline_ready(tg):
    service, orch, api = tg
    fid = _planned(orch, _researched(orch))
    service.process_update(
        _callback("cb_approve", 123, 100, f"feature:approve:{fid}"))
    feature = orch.get_feature(fid)
    assert feature["status"] == FeatureStatus.cline_ready.value
    assert feature["cline_task"] is not None
    texts = " ".join(m["text"] for m in api.messages)
    assert "Cline task is ready" in texts


def test_approve_button_high_risk_asks_for_second_confirmation(tg):
    """'Approve' on a HIGH-RISK feature returns high_risk_confirmation_required;
    the Confirm Implementation button afterwards finalises the approval."""
    service, orch, api = tg
    fid = _planned(orch, _researched(orch))
    model = orch._store.get(fid)
    model.risk = "High"
    orch._store.save(model)

    service.process_update(
        _callback("cb_hr1", 123, 100, f"feature:approve:{fid}"))
    assert orch.get_feature(fid)["status"] == FeatureStatus.approval_pending.value
    texts = " ".join(m["text"] for m in api.messages)
    assert "High-risk feature: confirm implementation" in texts

    service.process_update(
        _callback("cb_hr2", 123, 100, f"feature:confirm_high_risk:{fid}"))
    assert orch.get_feature(fid)["status"] == FeatureStatus.cline_ready.value


def test_cancel_high_risk_button_cancels_feature(tg):
    service, orch, api = tg
    fid = _planned(orch, _researched(orch))
    model = orch._store.get(fid)
    model.risk = "High"
    orch._store.save(model)
    service.process_update(
        _callback("cb_hrx", 123, 100, f"feature:cancel_high_risk:{fid}"))
    assert orch.get_feature(fid)["status"] == FeatureStatus.cancelled.value


def test_request_changes_button_arms_and_text_drives_revision(tg):
    """'Request Changes' arms a capture; the next text becomes a plan
    revision (re-plan path), never a LIVE TASK."""
    service, orch, api = tg
    fid = _planned(orch, _researched(orch))

    service.process_update(
        _callback("cb_ch", 123, 100, f"feature:changes:{fid}"))
    assert service._feature_changes_pending == {(123, 100): fid}
    texts = " ".join(m["text"] for m in api.messages)
    assert "Describe what should change." in texts

    service.process_update(_message(123, "Make the plan simpler"))
    assert service._feature_changes_pending == {}
    feature = orch.get_feature(fid)
    assert len(feature["change_requests"]) == 1
    texts = " ".join(m["text"] for m in api.messages)
    assert "Change request accepted" in texts


def test_missing_evidence_button_responds(tg):
    service, orch, api = tg
    fid = _researched(orch)
    model = orch._store.get(fid)
    model.missing_evidence = ["Notes storage location unverified"]
    orch._store.save(model)
    service.process_update(
        _callback("cb_miss", 123, 100, f"feature:missing:{fid}"))
    callbacks = [c[1] for c in api.callbacks]
    assert "Missing evidence" in callbacks
    texts = " ".join(m["text"] for m in api.messages)
    assert "MISSING EVIDENCE" in texts
    assert "Notes storage location unverified" in texts


def test_reresearch_button_triggers_research_required_revision(tg):
    service, orch, api = tg
    fid = _planned(orch, _researched(orch))
    service.process_update(
        _callback("cb_rr", 123, 100, f"feature:reresearch:{fid}"))
    callbacks = [c[1] for c in api.callbacks]
    assert "Re-running research" in callbacks
    feature = orch.get_feature(fid)
    assert feature["research_revision"] == 1
    assert len(feature["change_requests"]) >= 1
    texts = " ".join(m["text"] for m in api.messages)
    assert "Re-inspecting the repository" in texts