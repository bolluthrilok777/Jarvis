"""Part 5 tests — Telegram command → SAME ChatGPT conversation.

ALL browser side effects are mocked. Three layers are tested:

- ChatGPTBrowserController's TARGETED follow-up probes (get_chat_state /
  is_generating / is_response_complete / submit_command /
  wait_for_followup_generation_start / wait_for_followup_complete) driven with
  injected UIA-string sequences — the URL never changes for a follow-up, so
  completion is proven by the Stop control disappearing and Copy/Regenerate
  appearing, stable across observations (never a fixed sleep).
- WorkflowCommandService with run_inline=True + a fake ChatGPT controller + the
  real WorkflowEngine over a temp DB, proving: workflow resolution +
  disambiguation, durable command persistence BEFORE the GPT_RUNNING
  transition, restore/verify same conversation, submit-once idempotency by
  Telegram message id, recovery (no resend), reject-while-running, timeout →
  FAILED, and GPT_OUTPUT_READY + latest_gpt_response metadata.
- The existing Telegram bot routing branch: a free-text message is treated as a
  workflow command ONLY when a workflow is waiting, reusing the existing auth.
"""
from __future__ import annotations

import pytest

import jarvis_core.workflow_engine as we_mod
import jarvis_core.architecture_routes as ar_mod
import jarvis_core.chatgpt_browser_controller as cbc_mod
import jarvis_core.workflow_command_service as wcs_mod
from jarvis_core.chatgpt_browser_controller import (
    ChatGPTBrowserController,
    ChatGPTError,
    GenerationTimeoutError,
)
from jarvis_core.workflow_command_service import (
    WorkflowCommandService,
    CommandError,
    NoActiveWorkflowError,
    AmbiguousWorkflowError,
    CommandBusyError,
    ACTION_USER_COMMAND,
)
from jarvis_core.workflow_store import WorkflowStore


# ── Controller-level fakes (follow-up probes) ────────────────────────────────

def make_controller(*, strings_sequence=None, verified=True, hwnd=777,
                    clipboard_url="https://chatgpt.com/c/abc123"):
    calls = {"keys": [], "typed": [], "clip_set": [], "clip_get": 0, "strings": 0,
             "clicks": []}
    seq = list(strings_sequence or [["Message ChatGPT"]])
    clip = {"value": ""}

    def focus_verify_fn(profile_key):
        return {"verified": verified, "hwnd": hwnd, "requested": profile_key,
                "observed": profile_key}

    def window_strings_fn(h):
        calls["strings"] += 1
        idx = min(calls["strings"] - 1, len(seq) - 1)
        return seq[idx]

    def send_keys_fn(chord):
        calls["keys"].append(chord)
        if chord == "ctrl+c":
            clip["value"] = clipboard_url

    def type_text_fn(text):
        calls["typed"].append(text)

    def clipboard_get_fn():
        calls["clip_get"] += 1
        return clip["value"]

    def clipboard_set_fn(text):
        calls["clip_set"].append(text)
        clip["value"] = text

    def foreground_fn():
        return hwnd

    def click_point_fn(x, y):
        calls["clicks"].append((x, y))

    c = ChatGPTBrowserController(
        focus_verify_fn=focus_verify_fn,
        window_strings_fn=window_strings_fn,
        send_keys_fn=send_keys_fn,
        type_text_fn=type_text_fn,
        clipboard_get_fn=clipboard_get_fn,
        clipboard_set_fn=clipboard_set_fn,
        foreground_fn=foreground_fn,
        click_point_fn=click_point_fn,
    )
    c._calls = calls
    return c


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch):
    monkeypatch.setattr(cbc_mod, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(cbc_mod, "NAV_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(cbc_mod, "COMPOSER_FOCUS_SETTLE_SECONDS", 0.0)


# 1. get_chat_state reports generating from a Stop control
def test_get_chat_state_generating():
    c = make_controller(strings_sequence=[["Stop generating"]])
    st = c.get_chat_state(777)
    assert st["generating"] is True
    assert st["has_controls"] is False


# 2. get_chat_state reports completed controls
def test_get_chat_state_completed_controls():
    c = make_controller(strings_sequence=[["Copy", "Regenerate", "Message ChatGPT"]])
    st = c.get_chat_state(777)
    assert st["generating"] is False
    assert st["has_controls"] is True
    assert st["composer_ready"] is True


# 3. is_generating True while streaming
def test_is_generating_true():
    c = make_controller(strings_sequence=[["stop streaming"]])
    assert c.is_generating(777) is True


# 4. is_generating False when idle
def test_is_generating_false():
    c = make_controller(strings_sequence=[["Message ChatGPT"]])
    assert c.is_generating(777) is False


# 5. is_response_complete True only when not generating AND controls present
def test_is_response_complete_true():
    c = make_controller(strings_sequence=[["Copy", "Regenerate"]])
    assert c.is_response_complete(777) is True


# 6. is_response_complete False while a Stop control is present
def test_is_response_complete_false_while_generating():
    c = make_controller(strings_sequence=[["Copy", "Stop generating"]])
    assert c.is_response_complete(777) is False


# 7. is_response_complete False with no completed controls
def test_is_response_complete_false_no_controls():
    c = make_controller(strings_sequence=[["Message ChatGPT"]])
    assert c.is_response_complete(777) is False


# 8. submit_command pastes EXACT text and submits exactly once
def test_submit_command_pastes_exact_once():
    c = make_controller(clipboard_url="https://chatgpt.com/c/keep")
    c.submit_command(777, "EXACT FOLLOWUP COMMAND")
    assert "EXACT FOLLOWUP COMMAND" in c._calls["clip_set"]
    assert c._calls["keys"].count("ctrl+v") == 1
    assert c._calls["keys"].count("enter") == 1


# 9. submit_command rejects empty text
def test_submit_command_empty_rejected():
    c = make_controller()
    with pytest.raises(ChatGPTError):
        c.submit_command(777, "   ")


# 10. submit_command refuses when not on a ChatGPT page (safety)
def test_submit_command_refuses_off_chatgpt():
    c = make_controller(clipboard_url="https://example.com/search")
    with pytest.raises(ChatGPTError):
        c.submit_command(777, "hello")
    # No paste/submit happened.
    assert c._calls["keys"].count("ctrl+v") == 0


# 11. submit_command clicks the composer before pasting (focus, not omnibox)
def test_submit_command_focuses_composer():
    c = make_controller(clipboard_url="https://chatgpt.com/c/keep")
    # window_rect uses the real helper; injected click hook records the click.
    import jarvis_core.windows_resource_manager as wrm
    orig = getattr(wrm, "window_rect", None)
    wrm.window_rect = lambda h: {"x": 0, "y": 0, "width": 1000, "height": 800}
    try:
        c.submit_command(777, "focus me")
    finally:
        if orig is not None:
            wrm.window_rect = orig
    assert c._calls["clicks"], "composer should be clicked before paste"


# 12. submit_command restores the prior clipboard
def test_submit_command_restores_clipboard():
    c = make_controller(clipboard_url="https://chatgpt.com/c/keep")
    c._clipboard_set_fn("USER CLIPBOARD")
    c.submit_command(777, "cmd")
    assert c._calls["clip_set"][-1] == "USER CLIPBOARD"


# 13. follow-up generation START detected from the Stop control
def test_followup_start_detected():
    c = make_controller(strings_sequence=[["thinking"], ["Stop generating"]])
    assert c.wait_for_followup_generation_start(777, timeout=1.0) is True


# 14. follow-up generation START timeout returns False (fast reply case)
def test_followup_start_timeout_returns_false():
    c = make_controller(strings_sequence=[["Message ChatGPT"]])
    assert c.wait_for_followup_generation_start(777, timeout=0.05) is False


# 15. follow-up completion is positive + stable
def test_followup_complete_positive_stable():
    seq = [
        ["Stop generating"],
        ["Stop generating"],
        ["Copy", "Regenerate"],
        ["Copy", "Regenerate"],
        ["Copy", "Regenerate"],
    ]
    c = make_controller(strings_sequence=seq)
    res = c.wait_for_followup_complete(777, timeout=5.0)
    assert res["complete"] is True
    assert res["observations"] >= cbc_mod.COMPLETION_STABLE_OBSERVATIONS


# 16. a single missing Stop is NOT enough (stability required)
def test_followup_complete_requires_stability():
    seq = [
        ["Copy", "Regenerate"],   # transient
        ["Stop generating"],      # back to generating -> resets
        ["Stop generating"],
    ]
    c = make_controller(strings_sequence=seq)
    with pytest.raises(GenerationTimeoutError):
        c.wait_for_followup_complete(777, timeout=0.1)


# 17. follow-up completion requires the completed controls (Copy/Regenerate)
def test_followup_complete_requires_controls():
    # Not generating, but NO completed controls ever appear → never complete.
    c = make_controller(strings_sequence=[["some idle text"]])
    with pytest.raises(GenerationTimeoutError):
        c.wait_for_followup_complete(777, timeout=0.1)


# 18. completion never relies on a fixed sleep (source guarantee)
def test_followup_no_fixed_sleep():
    import inspect
    src = inspect.getsource(ChatGPTBrowserController.wait_for_followup_complete)
    assert "COMPLETION_STABLE_OBSERVATIONS" in src
    assert "has_controls" in src and "generating" in src
    assert "sleep(30)" not in src and "sleep(60)" not in src
    assert "POLL_INTERVAL" in src


# 19. follow-up completion does NOT depend on a URL transition
def test_followup_complete_ignores_url():
    import inspect
    src = inspect.getsource(ChatGPTBrowserController.wait_for_followup_complete)
    # The follow-up path must be accessibility-only (no address-bar read).
    assert "read_current_url" not in src
    assert "_url_has_conversation_id" not in src


# ── Structured probe (ChatGPTPageStateProbe) — LIVE-calibrated control names ──

# 19b. GENERATING classified from the real 'Stop answering' control
def test_probe_generating_stop_answering():
    c = make_controller(strings_sequence=[["Ask anything", "Stop answering"]])
    p = c.probe_page_state(777)
    assert p["state"] == "GENERATING"
    assert p["stop_visible"] is True
    assert p["composer_ready"] is True


# 19c. COMPLETED_CANDIDATE from real 'Copy response' + 'Response actions'
def test_probe_completed_candidate():
    c = make_controller(strings_sequence=[["Ask anything", "Copy response",
                                           "Response actions"]])
    p = c.probe_page_state(777)
    assert p["state"] == "COMPLETED_CANDIDATE"
    assert p["stop_visible"] is False
    assert p["copy_visible"] is True
    assert p["response_actions_visible"] is True


# 19d. IDLE when only the composer is present (no Stop, no completed controls)
def test_probe_idle():
    c = make_controller(strings_sequence=[["Ask anything"]])
    p = c.probe_page_state(777)
    assert p["state"] == "IDLE"
    assert p["composer_ready"] is True
    assert p["stop_visible"] is False


# 19e. UNKNOWN is a valid result when the composer cannot be found
def test_probe_unknown_valid():
    c = make_controller(strings_sequence=[["loading..."]])
    p = c.probe_page_state(777)
    assert p["state"] == "UNKNOWN"
    assert p["composer_ready"] is False


# 19f. the real Send control name is recognised (idle-with-draft)
def test_probe_send_prompt_recognised():
    c = make_controller(strings_sequence=[["Ask anything", "Send prompt"]])
    p = c.probe_page_state(777)
    assert p["send_visible"] is True
    # Send present but no Stop → not generating.
    assert p["stop_visible"] is False


# 19g. 'Stop answering' drives is_generating True (calibrated name)
def test_is_generating_stop_answering():
    c = make_controller(strings_sequence=[["Stop answering"]])
    assert c.is_generating(777) is True


# ── Service-level fake + fixture ─────────────────────────────────────────────

class FakeChatGPT:
    """Records the follow-up call sequence; never touches a browser."""

    def __init__(self, *, same=True, hwnd=999, gen_started=True,
                 complete=None, fail_complete=False, fail_submit=False):
        self.same = same
        self.hwnd = hwnd
        self.gen_started = gen_started
        self.complete = complete or {"complete": True, "observations": 3,
                                      "had_controls": True}
        self.fail_complete = fail_complete
        self.fail_submit = fail_submit
        self.submit_count = 0
        self.calls = []

    def restore_conversation(self, profile_key, url, expected_email=None):
        self.calls.append(("restore", profile_key, url))
        return {"hwnd": self.hwnd, "requested_url": url,
                "observed_url": url if self.same else "https://chatgpt.com/c/OTHER",
                "same": self.same}

    def restore_workflow_owned_conversation(self, *, workflow_id, conversation_url,
                                            window_binding, tab_identity=None,
                                            selected_profile_key=None,
                                            selected_account_email=None):
        # Migrated Part-5 authority: reuse the persisted Jarvis-owned binding;
        # NEVER legacy profile discovery. Records that the binding path was used.
        self.calls.append(("restore_binding", workflow_id,
                           (window_binding or {}).get("hwnd")))
        return {"hwnd": self.hwnd, "requested_url": conversation_url,
                "observed_url": conversation_url if self.same else "https://chatgpt.com/c/OTHER",
                "same": self.same, "reused": True, "recovered": False,
                "identity": tab_identity or (window_binding or {}).get("tab_identity"),
                "window_binding": window_binding}

    def submit_command(self, hwnd, text):
        self.submit_count += 1
        self.calls.append(("submit", text))
        if self.fail_submit:
            raise ChatGPTError("SUBMIT_FAILED")
        return {"submitted": True}

    def wait_for_followup_generation_start(self, hwnd, timeout=None):
        self.calls.append(("gen_start", hwnd))
        return self.gen_started

    def wait_for_followup_complete(self, hwnd, timeout=None):
        self.calls.append(("gen_complete", hwnd))
        if self.fail_complete:
            raise GenerationTimeoutError("did not complete")
        return dict(self.complete)


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    store = WorkflowStore(db_path=tmp_path / "p5.db")
    engine = we_mod.WorkflowEngine(store=store)
    monkeypatch.setattr(we_mod, "workflow_engine", engine)
    monkeypatch.setattr(ar_mod, "workflow_engine", engine)
    events = []

    def publish_fn(wid, etype, payload):
        events.append((wid, etype, payload))

    fake = FakeChatGPT()
    service = WorkflowCommandService(
        engine=engine, chatgpt_controller=fake, publish_fn=publish_fn,
        run_inline=True,
    )
    monkeypatch.setattr(wcs_mod, "workflow_command_service", service)
    yield engine, service, fake, events
    engine.close()


def _waiting_workflow(engine, *, name="P5",
                      profile="chrome-profile:Profile 7",
                      url="https://chatgpt.com/c/live1"):
    """Create a workflow already at WAITING_FOR_COMMAND (as Part 4 leaves it)."""
    wf = engine.create_workflow(project_name=name)
    wid = wf.workflow_id
    engine.update_workflow_context(
        wid, selected_chrome_profile_key=profile,
        selected_chrome_account_email="x@y.com",
        master_gpt_prompt="master", chatgpt_conversation_url=url,
        chatgpt_window_hwnd=999)
    engine.transition(wid, "INITIALIZING")
    engine.transition(wid, "GPT_BOOTSTRAPPING")
    engine.transition(wid, "WAITING_FOR_COMMAND")
    return wid


# 20. no waiting workflow → NoActiveWorkflowError
def test_no_active_workflow(svc):
    engine, service, fake, _ = svc
    with pytest.raises(NoActiveWorkflowError):
        service.submit_command(text="hi", telegram_message_id=1)


# 21. exactly one waiting workflow is auto-resolved
def test_single_waiting_auto_resolved(svc):
    engine, service, fake, _ = svc
    wid = _waiting_workflow(engine)
    r = service.submit_command(text="do it", telegram_message_id=5)
    assert r["workflow_id"] == wid


# 22. multiple waiting workflows → AmbiguousWorkflowError with candidates
def test_ambiguous_workflows(svc):
    engine, service, fake, _ = svc
    a = _waiting_workflow(engine, name="Alpha")
    b = _waiting_workflow(engine, name="Beta")
    with pytest.raises(AmbiguousWorkflowError) as ei:
        service.submit_command(text="which one", telegram_message_id=6)
    ids = {c["workflow_id"] for c in ei.value.candidates}
    assert {a, b} <= ids


# 23. explicit workflow_id targets exactly that workflow
def test_explicit_workflow_id(svc):
    engine, service, fake, _ = svc
    a = _waiting_workflow(engine, name="Alpha")
    b = _waiting_workflow(engine, name="Beta")
    r = service.submit_command(text="cmd", telegram_message_id=7, workflow_id=b)
    assert r["workflow_id"] == b


# 24. empty command rejected
def test_empty_command_rejected(svc):
    engine, service, fake, _ = svc
    _waiting_workflow(engine)
    with pytest.raises(CommandError):
        service.submit_command(text="   ", telegram_message_id=8)


# 25. missing saved conversation → rejected
def test_missing_conversation_rejected(svc):
    engine, service, fake, _ = svc
    wf = engine.create_workflow(project_name="NoConv")
    wid = wf.workflow_id
    engine.update_workflow_context(wid, selected_chrome_profile_key="chrome-profile:Profile 7")
    engine.transition(wid, "INITIALIZING")
    engine.transition(wid, "GPT_BOOTSTRAPPING")
    engine.transition(wid, "WAITING_FOR_COMMAND")
    with pytest.raises(CommandError):
        service.submit_command(text="cmd", telegram_message_id=9, workflow_id=wid)


# 26. the EXACT command is persisted durably (context + metadata)
def test_command_persisted_exactly(svc):
    engine, service, fake, _ = svc
    wid = _waiting_workflow(engine)
    service.submit_command(text="EXACT COMMAND TEXT", telegram_message_id=11,
                           telegram_chat_id=100, telegram_user_id=42)
    wf = engine.get_workflow(wid)
    assert wf.current_user_command == "EXACT COMMAND TEXT"
    meta = (wf.metadata or {}).get("current_command") or {}
    assert meta["text"] == "EXACT COMMAND TEXT"
    assert meta["telegram_message_id"] == 11
    assert meta["telegram_chat_id"] == 100
    assert meta["command_hash"]


# 27. persistence happens BEFORE the GPT_RUNNING transition (durability order)
def test_persist_before_running_transition(svc):
    engine, service, fake, events = svc
    wid = _waiting_workflow(engine)
    # Fail at submit so the run stops after the RUNNING transition; the command
    # must already be persisted regardless of the later failure.
    fake.fail_submit = True
    service.submit_command(text="persist me", telegram_message_id=12)
    wf = engine.get_workflow(wid)
    assert wf.current_user_command == "persist me"
    # The received event precedes the persisted/state change.
    types = [e[1] for e in events if e[0] == wid]
    assert types.index(wcs_mod.EV_COMMAND_RECEIVED) < types.index(wcs_mod.EV_COMMAND_PERSISTED)


# 28. successful command reaches GPT_OUTPUT_READY + metadata
def test_success_reaches_output_ready(svc):
    engine, service, fake, _ = svc
    wid = _waiting_workflow(engine)
    service.submit_command(text="build me a thing", telegram_message_id=13)
    wf = engine.get_workflow(wid)
    assert wf.state == "GPT_OUTPUT_READY"
    assert fake.submit_count == 1
    rr = (wf.metadata or {}).get("latest_gpt_response") or {}
    assert rr["ready_at"]
    assert rr["copy_available"] is True
    assert rr["iteration"] == 1
    assert wf.iteration_number == 1


# 29. submit happens exactly once (single Enter path via fake counter)
def test_submit_exactly_once(svc):
    engine, service, fake, _ = svc
    _waiting_workflow(engine)
    service.submit_command(text="once", telegram_message_id=14)
    assert fake.submit_count == 1


# 30. restores + verifies the SAME conversation (via the binding-aware path)
def test_restores_same_conversation(svc):
    engine, service, fake, _ = svc
    wid = _waiting_workflow(engine, url="https://chatgpt.com/c/samechat")
    _seed_binding(engine, wid)
    service.submit_command(text="cmd", telegram_message_id=15)
    # The migrated Part-5 path restores via the Jarvis-owned window binding for
    # this exact workflow (not the legacy profile restore).
    assert any(c[0] == "restore_binding" and c[1] == wid for c in fake.calls)
    assert not any(c[0] == "restore" for c in fake.calls)
    wf = engine.get_workflow(wid)
    assert wf.state == "GPT_OUTPUT_READY"


# 31. conversation mismatch → FAILED (never proceeds in the wrong chat)
def test_conversation_mismatch_fails(svc):
    engine, service, fake, _ = svc
    fake.same = False
    wid = _waiting_workflow(engine)
    service.submit_command(text="cmd", telegram_message_id=16)
    wf = engine.get_workflow(wid)
    assert wf.state == "FAILED"
    assert fake.submit_count == 0  # never submitted into a mismatched chat


# 32. generation timeout → FAILED with the timeout code
def test_generation_timeout_fails(svc):
    engine, service, fake, _ = svc
    fake.fail_complete = True
    wid = _waiting_workflow(engine)
    service.submit_command(text="cmd", telegram_message_id=17)
    wf = engine.get_workflow(wid)
    assert wf.state == "FAILED"
    assert "GenerationTimeoutError" in (wf.last_error_code or "")


# 33. idempotent submit by Telegram message id — recovery does NOT resend
def test_idempotent_no_resend_on_recovery(svc):
    engine, service, fake, _ = svc
    wid = _waiting_workflow(engine)
    # Simulate a prior run that already submitted this exact message id.
    engine.transition(wid, "GPT_RUNNING")
    action_id = f"{ACTION_USER_COMMAND}:{wid}:18"
    engine.begin_action(wid, action_id, action_type="chatgpt_user_command")
    engine.complete_action(wid, action_id, result_ref="prior-run")
    # Re-run the command body directly (recovery path).
    service._run_command({
        "workflow_id": wid, "command": "recover", "telegram_message_id": 18,
        "telegram_chat_id": None, "command_hash": "h",
    })
    assert fake.submit_count == 0  # NOT resent
    assert engine.get_workflow(wid).state == "GPT_OUTPUT_READY"


# 34. reject a 2nd command while one is running (single-flight)
def test_reject_second_command_while_running(svc):
    engine, service, fake, _ = svc
    wid = _waiting_workflow(engine)
    # Mark active as if a command is mid-flight, then a new submit is rejected.
    with service._active_lock:
        service._active.add(wid)
    with pytest.raises(CommandBusyError):
        service.submit_command(text="second", telegram_message_id=19, workflow_id=wid)


# 35. explicit workflow already GPT_RUNNING → busy
def test_explicit_running_is_busy(svc):
    engine, service, fake, _ = svc
    wid = _waiting_workflow(engine)
    engine.transition(wid, "GPT_RUNNING")
    with pytest.raises(CommandBusyError):
        service.resolve_target_workflow(workflow_id=wid)


# 36. events published in the expected order for a success
def test_event_sequence(svc):
    engine, service, fake, events = svc
    wid = _waiting_workflow(engine)
    service.submit_command(text="cmd", telegram_message_id=20)
    types = [e[1] for e in events if e[0] == wid]
    for a, b in [
        (wcs_mod.EV_COMMAND_RECEIVED, wcs_mod.EV_COMMAND_PERSISTED),
        (wcs_mod.EV_COMMAND_PERSISTED, wcs_mod.EV_CONVERSATION_RESTORING),
        (wcs_mod.EV_CONVERSATION_VERIFIED, wcs_mod.EV_COMMAND_SUBMITTED),
        (wcs_mod.EV_GENERATION_STARTED, wcs_mod.EV_GENERATION_COMPLETE),
        (wcs_mod.EV_GENERATION_COMPLETE, wcs_mod.EV_OUTPUT_READY),
    ]:
        assert types.index(a) < types.index(b), f"{a} should precede {b}"


# 37. get_status surfaces the Part 5 command + response metadata
def test_get_status(svc):
    engine, service, fake, _ = svc
    wid = _waiting_workflow(engine)
    service.submit_command(text="status me", telegram_message_id=21)
    st = service.get_status(wid)
    assert st["state"] == "GPT_OUTPUT_READY"
    assert st["current_user_command"] == "status me"
    assert st["latest_gpt_response"]["ready_at"]


# ── Telegram routing branch ──────────────────────────────────────────────────

class FakeAPI:
    def __init__(self):
        self.messages = []
        self.edits = []
        self._next_id = 1000

    def send_message(self, chat_id, text, *, reply_markup=None):
        self._next_id += 1
        self.messages.append({"chat_id": chat_id, "text": text,
                              "reply_markup": reply_markup, "message_id": self._next_id})
        return {"message_id": self._next_id}

    def edit_message_text(self, chat_id, message_id, text, *, reply_markup=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return {"message_id": message_id}

    def answer_callback_query(self, callback_query_id, text=""):
        pass

    def close(self):
        pass


def _tg_settings(*ids):
    from jarvis_core.telegram_config import TelegramSettings
    return TelegramSettings(bot_token="t", allowed_user_ids=frozenset(ids),
                            completion_timeout_seconds=1)


def _tg_message(user_id, text, chat_id=100, message_id=500):
    return {"update_id": 1, "message": {
        "from": {"id": user_id}, "chat": {"id": chat_id},
        "text": text, "message_id": message_id}}


def _make_tg_service(engine, monkeypatch):
    from jarvis_core.telegram_service import TelegramService
    api = FakeAPI()
    service = TelegramService(settings=_tg_settings(42), api=api)
    # Point the command-service singleton the routing branch imports at our
    # engine-backed instance so no real browser/engine is touched.
    cmd = WorkflowCommandService(engine=engine, chatgpt_controller=FakeChatGPT(),
                                 run_inline=True)
    monkeypatch.setattr(wcs_mod, "workflow_command_service", cmd)
    return service, api, cmd


# 38. free text with NO waiting workflow does NOT hit the command service
def test_routing_ignored_when_no_waiting(tmp_path, monkeypatch):
    store = WorkflowStore(db_path=tmp_path / "r1.db")
    engine = we_mod.WorkflowEngine(store=store)
    monkeypatch.setattr(we_mod, "workflow_engine", engine)
    service, api, cmd = _make_tg_service(engine, monkeypatch)
    handled = service._route_workflow_command(42, 100, "hello there", 500)
    assert handled is False
    engine.close()


# 39. free text WITH a waiting workflow is routed as the command
def test_routing_consumes_when_waiting(tmp_path, monkeypatch):
    store = WorkflowStore(db_path=tmp_path / "r2.db")
    engine = we_mod.WorkflowEngine(store=store)
    monkeypatch.setattr(we_mod, "workflow_engine", engine)
    service, api, cmd = _make_tg_service(engine, monkeypatch)
    wid = _waiting_workflow(engine)
    handled = service._route_workflow_command(42, 100, "please build X", 501)
    assert handled is True
    wf = engine.get_workflow(wid)
    assert wf.state == "GPT_OUTPUT_READY"
    assert wf.current_user_command == "please build X"
    engine.close()


# 40. unauthorized user never reaches the command service
def test_routing_unauthorized_blocked(tmp_path, monkeypatch):
    store = WorkflowStore(db_path=tmp_path / "r3.db")
    engine = we_mod.WorkflowEngine(store=store)
    monkeypatch.setattr(we_mod, "workflow_engine", engine)
    service, api, cmd = _make_tg_service(engine, monkeypatch)
    _waiting_workflow(engine)
    service.process_update(_tg_message(999, "sneak command"))
    # The unauthorized reply is sent; no workflow advanced.
    assert any("not authorized" in m["text"].lower() for m in api.messages)
    assert not [w for w in engine.list_active_workflows() if w.state == "GPT_OUTPUT_READY"]
    engine.close()


# 41. ambiguous routing shows a disambiguation prompt (no command submitted)
def test_routing_ambiguous_prompts(tmp_path, monkeypatch):
    store = WorkflowStore(db_path=tmp_path / "r4.db")
    engine = we_mod.WorkflowEngine(store=store)
    monkeypatch.setattr(we_mod, "workflow_engine", engine)
    service, api, cmd = _make_tg_service(engine, monkeypatch)
    _waiting_workflow(engine, name="Alpha")
    _waiting_workflow(engine, name="Beta")
    handled = service._route_workflow_command(42, 100, "ambiguous", 502)
    assert handled is True
    assert any("multiple workflows" in m["text"].lower() for m in api.messages)
    # A numeric follow-up resolves it.
    service.process_update(_tg_message(42, "1", message_id=503))
    ready = [w for w in engine.list_active_workflows() if w.state == "GPT_OUTPUT_READY"]
    assert len(ready) == 1
    engine.close()


# ── Fix C: single GPT response watcher ownership ──────────────────────────────

def test_single_watcher_fires_exactly_once_per_command(svc):
    """The GPT response watcher (gen_start + gen_complete) runs exactly ONCE for
    a command, and the workflow ends GPT_OUTPUT_READY (watcher terminated)."""
    engine, service, fake, _ = svc
    wid = _waiting_workflow(engine)
    service.submit_command(text="do it once", telegram_message_id=101)
    assert fake.calls.count(("gen_start", fake.hwnd)) == 1
    assert fake.calls.count(("gen_complete", fake.hwnd)) == 1
    wf = engine.get_workflow(wid)
    assert wf.state == "GPT_OUTPUT_READY"
    # Watcher terminated: no longer active after the command completed.
    assert service.is_active(wid) is False


def test_no_watcher_when_workflow_is_running_reject_duplicate(svc):
    """A second command while the workflow is already GPT_RUNNING is rejected —
    a duplicate watcher is never started."""
    engine, service, fake, _ = svc
    wid = _waiting_workflow(engine)
    # Simulate an in-flight command (active) + GPT_RUNNING.
    engine.transition(wid, "GPT_RUNNING")
    with service._active_lock:
        service._active.add(wid)
    with pytest.raises(CommandBusyError):
        service.submit_command(text="second", telegram_message_id=102,
                               workflow_id=wid)
    # No new watcher calls were made by the rejected submit.
    assert fake.calls.count(("gen_start", fake.hwnd)) == 0


def test_no_watcher_from_waiting_for_approval(svc):
    """A command cannot be submitted while the workflow sits in
    WAITING_FOR_APPROVAL, so no GPT watcher runs in that state."""
    engine, service, fake, _ = svc
    wid = _waiting_workflow(engine)
    engine.transition(wid, "GPT_RUNNING")
    engine.transition(wid, "GPT_OUTPUT_READY")
    engine.transition(wid, "WAITING_FOR_APPROVAL")
    with pytest.raises(CommandError):
        service.submit_command(text="nope", telegram_message_id=103,
                               workflow_id=wid)
    assert fake.calls.count(("gen_start", fake.hwnd)) == 0


# ── Part 5 migration: restore via persisted Jarvis-owned window binding ───────

def _seed_binding(engine, wid, *, hwnd=999, rid="42,999,4,0,0,7"):
    engine.set_metadata(wid, "jarvis_browser_window", {
        "owner": "jarvis", "workflow_id": wid,
        "selected_profile_key": "chrome-profile:Profile 7",
        "selected_profile_directory": "Profile 7",
        "selected_account_email": "x@y.com",
        "hwnd": hwnd, "launch_token": "tok",
        "tab_identity": {"control_mode": "uia", "host_hwnd": hwnd,
                         "runtime_id": rid, "tab_index": 0,
                         "owner": "jarvis", "workflow_id": wid},
        "verified": True})


def test_part5_uses_window_binding_not_legacy_restore(svc):
    engine, service, fake, _ = svc
    wid = _waiting_workflow(engine)
    _seed_binding(engine, wid)
    service.submit_command(text="do it", telegram_message_id=201)
    # The binding-aware restore was used; the legacy restore_conversation was NOT.
    assert any(c[0] == "restore_binding" for c in fake.calls)
    assert not any(c[0] == "restore" for c in fake.calls)
    wf = engine.get_workflow(wid)
    assert wf.state == "GPT_OUTPUT_READY"
    assert wf.last_error_code is None            # no ProfileVerificationError


def test_part5_binding_carries_hwnd(svc):
    engine, service, fake, _ = svc
    wid = _waiting_workflow(engine)
    _seed_binding(engine, wid, hwnd=10029300)
    service.submit_command(text="cmd", telegram_message_id=202)
    rb = [c for c in fake.calls if c[0] == "restore_binding"]
    assert rb and rb[0][2] == 10029300           # bound HWND passed through


def test_part5_one_telegram_one_submit_binding(svc):
    engine, service, fake, _ = svc
    wid = _waiting_workflow(engine)
    _seed_binding(engine, wid)
    service.submit_command(text="once", telegram_message_id=203)
    assert fake.submit_count == 1
    # A second submit for the same (now GPT_OUTPUT_READY) workflow is rejected.
    from jarvis_core.workflow_command_service import CommandError as _CE
    with pytest.raises(_CE):
        service.submit_command(text="again", telegram_message_id=204, workflow_id=wid)
    assert fake.submit_count == 1
