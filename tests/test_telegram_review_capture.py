"""Targeted tests: explicit Telegram ChatGPT review capture.

Regression for the real E2E bug where pasting a ChatGPT review as a normal
Telegram message created an accidental LIVE TASK with a Go/No confirmation
instead of being attached to the feature waiting for review.

The fix: the ChatGPT Review Ready message now ships a "Submit ChatGPT Review"
button that arms a temporary, explicit capture session bound to
(authorized user_id, chat_id, feature_id). Only after that button is pressed is
the next text message interpreted as the review.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from jarvis_core.feature_approval import chatgpt_review_buttons
from jarvis_core.feature_models import FeatureStatus
from jarvis_core.feature_orchestrator import ACTION_RESEARCH, FeatureOrchestrator
from jarvis_core.feature_store import FeatureStore
from jarvis_core.task_manager import TaskManager
from jarvis_core.telegram_service import TelegramService
from tests.test_telegram_integration import FakeAPI, _message, _settings


# ── Fixtures ─────────────────────────────────────────────────────────────────


class _FakeOllama:
    """Deterministic stand-in for ollama_service.ask_ollama."""

    def __init__(self):
        self.calls = []
        self.reply = "- Reuse existing services.\n- Add a focused module."
        self.error = None

    def __call__(self, message, mode=None, *, purpose="chat", **kwargs):
        self.calls.append({"message": message[:200], "purpose": purpose})
        if self.error is not None:
            return (None, "balanced", dict(self.error))
        return (self.reply, "qwen2.5:7b", None)


@pytest.fixture()
def ollama():
    fake = _FakeOllama()
    with patch("jarvis_core.ollama_service.ask_ollama", side_effect=fake):
        yield fake


@pytest.fixture()
def orch(ollama, tmp_path, monkeypatch):
    """Isolated FeatureOrchestrator over a temp store, worker disabled.

    ``ollama`` is injected so the research/analysis stages use the fast fake
    ask_ollama instead of the real (slow) model server. Background threads
    are made synchronous so resource-review scans never leak between tests.
    """
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


@pytest.fixture()
def tg(orch):
    """(service, orchestrator, fake_api) with service wired to the isolated orch."""
    api = FakeAPI()
    service = TelegramService(
        settings=_settings(123),
        api=api,
        orchestrator_instance=orch,
    )
    service._start_task_watcher = Mock()
    return service, orch, api


def _fake_web():
    def _research(objective):
        return {
            "sources": [{"url": "https://example.com/notes", "ok": True,
                         "snippet": "Notes export API overview.", "error": None}],
            "incomplete": False,
            "error": None,
        }

    return _research


def _researched(orch, objective="Jarvis add a simple local notes export feature"):
    """Create a feature stuck in chatgpt_review_pending (needs ollama fixture)."""
    submitted = orch.submit_feature_request(
        objective,
        source="Telegram",
        telegram_user_id=123,
        telegram_chat_id=100,
    )
    feature_id = submitted["feature_id"]
    with patch("jarvis_core.feature_research.web_research", _fake_web()):
        orch._run_action(ACTION_RESEARCH, feature_id)
    return feature_id


def _callback(callback_id, user_id, chat_id, data):
    return {
        "update_id": 10,
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}},
            "data": data,
        },
    }
# ── 1. Button present ────────────────────────────────────────────────────────


def test_review_ready_buttons_include_submit_chatgpt_review():
    feature = SimpleNamespace(feature_id="fid-12345678")
    buttons = chatgpt_review_buttons(feature)
    flat = [b["text"] for row in buttons["inline_keyboard"] for b in row]
    callbacks = [b["callback_data"] for row in buttons["inline_keyboard"] for b in row]
    assert "\U0001f4dd Submit / Replace Cline Prompt" in flat
    assert f"feature:review:fid-12345678" in callbacks
    assert "\U0001f4cb View ChatGPT Request" in flat
    assert "\u23ed Skip ChatGPT" in flat
    assert "\U0001f6ab Cancel Feature" in flat


# ── 2. Button arms capture ───────────────────────────────────────────────────


def test_submit_review_button_arms_exact_capture(tg):
    service, orch, api = tg
    feature_id = _researched(orch)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback("cb1", 123, 100, f"feature:review:{feature_id}"))

    assert service._feature_review_pending == {(123, 100): feature_id}
    message = api.messages[-1]["text"]
    assert "Paste the exact prompt you want Jarvis to use for Cline." in message
    assert "It will be attached to:\nSimple Local Notes Export Feature" in message
    assert "Send /cancel to exit prompt entry mode." in message


def test_submit_review_button_replaces_previous_capture(tg):
    service, orch, api = tg
    first = _researched(orch)
    second_id = _researched(orch, "Jarvis add a tiny test counter feature")
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback("cb1", 123, 100, f"feature:review:{first}"))
        service.process_update(_callback("cb2", 123, 100, f"feature:review:{second_id}"))

    assert service._feature_review_pending == {(123, 100): second_id}
    assert any(
        "Replaced your pending review entry" in m["text"] for m in api.messages
    )


# ── 3./4./5. Review text bypasses normal routing, is associated correctly,
#             and creates no LIVE TASK ────────────────────────────────────────


def test_review_text_bypasses_normal_command_router(tg):
    service, orch, api = tg
    feature_id = _researched(orch)
    normal = Mock()
    service._orchestrator = normal  # canary: normal command path must not run
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback("cb1", 123, 100, f"feature:review:{feature_id}"))
        service.process_update(
            _message(123, "The proposed architecture is appropriate. Keep it small.")
        )

    normal.submit.assert_not_called()
    normal.confirm.assert_not_called()
    service._orchestrator = orch  # restore for teardown hygiene
    assert service._feature_review_pending == {}


def test_review_text_attached_to_correct_feature(tg):
    service, orch, api = tg
    feature_id = _researched(orch)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback("cb1", 123, 100, f"feature:review:{feature_id}"))
        service.process_update(
            _message(123, "The proposed architecture is appropriate. Keep it small.")
        )

    feature = orch._store.get(feature_id)
    assert feature.chatgpt_review is not None
    assert "The proposed architecture is appropriate." in feature.chatgpt_review
    assert feature.status == FeatureStatus.chatgpt_review_pending.value


def test_review_text_creates_no_live_task(tg):
    service, orch, api = tg
    feature_id = _researched(orch)
    before = len(orch.task_manager.get_all_tasks())
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback("cb1", 123, 100, f"feature:review:{feature_id}"))
        service.process_update(_message(123, "ChatGPT Review: The proposed architecture…"))
    after = len(orch.task_manager.get_all_tasks())

    assert after == before, "pasted review must not create a new LIVE TASK"
    assert all(t.task_type in ("feature", "conversation") for t in orch.task_manager.get_all_tasks())


def test_review_received_message_sent(tg):
    service, orch, api = tg
    feature_id = _researched(orch)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback("cb1", 123, 100, f"feature:review:{feature_id}"))
        service.process_update(_message(123, "Reuse existing modules only."))

    assert any("CHATGPT OUTPUT RECEIVED" in m["text"] for m in api.messages)
    assert any("Source: Submitted by authorized user" in m["text"] for m in api.messages)
    assert any("Content hash:" in m["text"] for m in api.messages)
# ── 6./7. Analyze + final plan ───────────────────────────────────────────────


def test_review_gets_analyzed_and_plan_generated(tg, ollama):
    service, orch, api = tg
    feature_id = _researched(orch)
    ollama.reply = (
        "ACCEPTED:\n- Use the existing task manager\n"
        "REJECTED:\n- Rewrite the orchestrator\n"
        "CONFLICTS:\n- ChatGPT assumed no Telegram allowlist exists\n"
        "ADDITIONAL REQUIREMENTS:\n- Add rate limiting\n"
        "REVISED APPROACH:\n- Extend gmail_intent first\n"
    )
    from jarvis_core.feature_orchestrator import ACTION_BUILD_PLAN

    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback("cb1", 123, 100, f"feature:review:{feature_id}"))
        service.process_update(_message(123, "ChatGPT says reuse everything."))
        orch._run_action("analyze_review", feature_id)
        orch._run_action(ACTION_BUILD_PLAN, feature_id)

    feature = orch.get_feature(feature_id)
    assert feature["chatgpt_reviewed"] is True
    assert feature["chatgpt_analysis"]["accepted"] == ["Use the existing task manager"]
    assert feature["status"] == FeatureStatus.approval_pending.value
    assert feature["plan"] is not None


# ── 8. Capture clears after success ──────────────────────────────────────────


def test_capture_clears_after_successful_submission(tg):
    service, orch, api = tg
    feature_id = _researched(orch)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback("cb1", 123, 100, f"feature:review:{feature_id}"))
        assert service._feature_review_pending == {(123, 100): feature_id}
        service.process_update(_message(123, "Use the existing task manager."))

    assert service._feature_review_pending == {}


# ── 9. /cancel exits capture without cancelling the feature ──────────────────


def test_slash_cancel_exits_capture_only(tg):
    service, orch, api = tg
    feature_id = _researched(orch)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback("cb1", 123, 100, f"feature:review:{feature_id}"))
        service.process_update(_message(123, "/cancel"))

    assert service._feature_review_pending == {}
    feature = orch.get_feature(feature_id)
    assert feature["status"] == FeatureStatus.chatgpt_review_pending.value
    assert any(
        "still waiting" in m["text"] and "Submit / Replace Cline Prompt" in m["text"]
        for m in api.messages
    )


def test_review_cancel_button_exits_capture_only(tg):
    service, orch, api = tg
    feature_id = _researched(orch)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback("cb1", 123, 100, f"feature:review:{feature_id}"))
        service.process_update(_callback("cb2", 123, 100, f"feature:review_cancel:{feature_id}"))

    assert service._feature_review_pending == {}
    feature = orch.get_feature(feature_id)
    assert feature["status"] == FeatureStatus.chatgpt_review_pending.value


# ── 10. Authorization ────────────────────────────────────────────────────────


def test_unauthorized_user_cannot_arm_review_capture(tg):
    service, orch, api = tg
    feature_id = _researched(orch)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback("cb1", 999, 100, f"feature:review:{feature_id}"))

    assert service._feature_review_pending == {}
    assert any("not authorized" in m["text"] for m in api.messages)


# ── 11. Wrong feature state rejected ─────────────────────────────────────────


def test_review_capture_rejected_when_feature_not_waiting(tg, monkeypatch):
    service, orch, api = tg
    # Resource review now auto-continues into research; stub the work queue so
    # the fresh feature stays at `received` for this rejection path.
    monkeypatch.setattr(orch, "enqueue", lambda action, feature_id: None)
    fresh = orch.submit_feature_request(
        "Jarvis add a tiny test tool", source="Telegram",
        telegram_user_id=123, telegram_chat_id=100,
    )["feature_id"]  # status = received, not chatgpt_review_pending
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback("cb1", 123, 100, f"feature:review:{fresh}"))

    assert service._feature_review_pending == {}
    assert any("not waiting for a ChatGPT review" in m["text"] for m in api.messages)


# ── Restart: feature stays waiting, review-ready re-sent with buttons ────────


def test_restart_resends_review_ready_with_submit_button(tmp_path):
    from jarvis_core.feature_orchestrator import ACTION_RESEARCH

    store = FeatureStore(root=tmp_path / "features")
    first = FeatureOrchestrator(store=store, task_manager_instance=TaskManager())
    first.start_worker = lambda: None  # type: ignore[method-assign]
    first.set_telegram_channels(
        send=lambda chat_id, text, markup=None: None,
        is_authorized=lambda uid: uid == 123,
    )
    with patch("jarvis_core.feature_research.web_research", _fake_web()):
        submitted = first.submit_feature_request(
            "Jarvis add a simple local notes export feature",
            source="Telegram", telegram_user_id=123, telegram_chat_id=100,
        )
        first._run_action(ACTION_RESEARCH, submitted["feature_id"])
    first.stop_worker(timeout=1)

    # Simulate a restart over the same store.
    sent = []
    restarted = FeatureOrchestrator(
        store=FeatureStore(root=tmp_path / "features"),
        task_manager_instance=TaskManager(),
    )
    restarted.set_telegram_channels(
        send=lambda chat_id, text, markup=None: sent.append({"text": text, "markup": markup}),
        is_authorized=lambda uid: uid == 123,
    )
    restarted.start_worker()

    try:
        assert any("CHATGPT REQUEST READY" in s["text"] for s in sent)
        markups = [s["markup"] for s in sent if s["markup"]]
        flat = [b["text"] for m in markups for row in m["inline_keyboard"] for b in row]
        assert "📝 Submit / Replace Cline Prompt" in flat
    finally:
        restarted.stop_worker(timeout=1)


def test_normal_telegram_messages_work_when_capture_inactive(tg):
    service, orch, api = tg
    normal = Mock()
    normal.submit.return_value = {
        "task_id": "task-1", "status": "queued", "action": "created", "message": "queued",
    }
    with patch.object(service, "_orchestrator", normal):
        service.process_update(_message(123, "Hello"))

    assert normal.submit.call_args[0][0] == "Hello"
    kwargs = normal.submit.call_args.kwargs
    assert kwargs == {
        "source": "Telegram", "track_conversation": True, "conversation_id": "telegram:123",
    }
    assert service._feature_review_pending == {}


# ── 13. Feature requests still work ──────────────────────────────────────────


def test_feature_requests_still_routed_when_capture_inactive(tg):
    service, orch, api = tg
    with patch.object(service, "_feature_orchestrator", orch):
        # Feature-like free-form text now arms the mode-confirmation gate first.
        service.process_update(_message(123, "Jarvis add a simple notes export feature"))

    assert orch.list_features() == [], "feature must NOT be created before mode confirmation"
    assert service._mode_pending.get((123, 100)) == "Jarvis add a simple notes export feature"
    assert any("How should I handle this request?" in m["text"] for m in api.messages)

    # Tapping "Feature Development" creates the feature through the orchestrator.
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback("cb1", 123, 100, "feature:mode:feature"))

    features = orch.list_features()
    assert len(features) == 1
    # Telegram Feature Development auto-continues after the resource scan.
    assert features[0]["resource_review_completed"] is True
    assert features[0]["status"] == FeatureStatus.received.value
    assert service._feature_review_pending == {}
    assert service._mode_pending == {}


def test_typo_fused_verb_arms_clarification_gate_not_live_task(tg):
    """Regression T-1: 'adda note feature' (dropped-space typo) must NOT be queued
    as a LIVE TASK. It must arm the clarification gate and preserve the original
    message; only tapping 'Feature Development' may create a feature."""
    service, orch, api = tg
    # The `tg` fixture wires the REAL FeatureOrchestrator, so the real detector
    # runs -- this is what exercises the clarification-gate fix.
    service.process_update(_message(123, "adda note feature"))

    assert orch.list_features() == [], "feature must NOT be created before confirmation"
    # Original (typo'd) message is preserved verbatim for later submission.
    assert service._mode_pending.get((123, 100)) == "adda note feature"
    assert any("How should I handle this request?" in m["text"] for m in api.messages)
    # A feature-like free-text request must never be silently queued as a task.
    assert not any("Queued in Jarvis LIVE TASKS" in m["text"] for m in api.messages)

    # Only 'Feature Development' creates a feature.
    service.process_update(_callback("cb1", 123, 100, "feature:mode:feature"))

    features = orch.list_features()
    assert len(features) == 1
    assert features[0]["resource_review_completed"] is True
    assert features[0]["status"] == FeatureStatus.received.value
    assert service._mode_pending == {}
    assert service._feature_review_pending == {}



# ── 14./15. Skip ChatGPT / Cancel Feature still work ─────────────────────────


def test_skip_chatgpt_still_works(tg):
    service, orch, api = tg
    feature_id = _researched(orch)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback("cb1", 123, 100, f"feature:chatgpt_skip:{feature_id}"))

    feature = orch.get_feature(feature_id)
    assert feature["chatgpt_skipped"] is True


def test_cancel_feature_still_works(tg):
    service, orch, api = tg
    feature_id = _researched(orch)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback("cb1", 123, 100, f"feature:cancel:{feature_id}"))

    feature = orch.get_feature(feature_id)
    assert feature["status"] == FeatureStatus.cancelled.value
# ── Command-mode confirmation gate (Section B) ──────────────────────────────


def test_normal_freeform_message_never_arms_mode_gate(tg):
    service, orch, api = tg
    normal = Mock()
    normal.submit.return_value = {
        "task_id": "task-1", "status": "queued", "action": "created", "message": "queued",
    }
    with patch.object(service, "_orchestrator", normal):
        service.process_update(_message(123, "Hello"))

    assert service._mode_pending == {}
    assert not any("How should I handle this request?" in m["text"] for m in api.messages)
    assert normal.submit.call_args[0][0] == "Hello"


def test_mode_gate_normal_choice_routes_to_normal_command(tg):
    service, orch, api = tg
    normal = Mock()
    normal.submit.return_value = {
        "task_id": "task-1", "status": "queued", "action": "created", "message": "queued",
    }
    with patch.object(service, "_orchestrator", normal):
        with patch.object(service, "_feature_orchestrator", orch):
            service.process_update(_message(123, "add a simple notes export tool"))
            assert orch.list_features() == [], "no feature before the mode choice"

        service.process_update(_callback("cb2", 123, 100, "feature:mode:normal"))

    assert service._mode_pending == {}
    assert orch.list_features() == [], "Normal Command must not create a feature"
    assert normal.submit.call_args[0][0] == "add a simple notes export tool"


def test_mode_gate_cancel_choice_creates_nothing(tg):
    service, orch, api = tg
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_message(123, "add a simple notes export tool"))
        assert service._mode_pending.get((123, 100))

        service.process_update(_callback("cb3", 123, 100, "feature:mode:cancel"))

    assert orch.list_features() == []
    assert service._mode_pending == {}
    assert any("Cancelled" in m["text"] for m in api.messages)


def test_feature_prefix_bypasses_mode_gate(tg):
    service, orch, api = tg
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_message(123, "feature: add a simple notes export tool"))

    assert service._mode_pending == {}
    features = orch.list_features()
    assert len(features) == 1
    # feature: prefix is still a Telegram submission -> resource review gate
    assert features[0]["resource_review_completed"] is True
    assert features[0]["status"] == FeatureStatus.received.value


def test_normal_prefix_bypasses_mode_gate(tg):
    service, orch, api = tg
    normal = Mock()
    normal.submit.return_value = {
        "task_id": "task-1", "status": "queued", "action": "created", "message": "queued",
    }
    with patch.object(service, "_orchestrator", normal):
        with patch.object(service, "_feature_orchestrator", orch):
            service.process_update(
                _message(123, "normal: add a simple notes export tool")
            )

    assert service._mode_pending == {}
    assert orch.list_features() == []
    assert normal.submit.call_args[0][0] == "add a simple notes export tool"
