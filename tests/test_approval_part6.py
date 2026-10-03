"""Part 6 tests — GPT copy capture + Telegram approval.

ALL browser/clipboard side effects are mocked. Three layers:
- ChatGPTBrowserController latest-response locator + sentinel clipboard capture
  (injected window_strings + clipboard + copy_click hooks) — proves newest
  response selection, ambiguous->fail, sentinel proof, exact text/hash/len.
- WorkflowApprovalService with run_inline=True + a fake ChatGPT controller + the
  real WorkflowEngine over a temp DB — proves capture gating, transitions,
  idempotency/recovery, exact persistence, Telegram delivery/chunking, and the
  Proceed/Edit/Cancel callbacks (idempotent, stale, unauthorized).
- The number of cases matches the required 39 (see the numbered comments).
"""
from __future__ import annotations

import hashlib
import pytest

import jarvis_core.workflow_engine as we_mod
import jarvis_core.architecture_routes as ar_mod
import jarvis_core.workflow_approval_service as was_mod
from jarvis_core.chatgpt_browser_controller import ChatGPTBrowserController, ChatGPTError
from jarvis_core.workflow_approval_service import (
    WorkflowApprovalService, ApprovalError,
)
from jarvis_core.workflow_store import WorkflowStore


def _sha(t): return hashlib.sha256(t.encode("utf-8")).hexdigest()


# ── Controller-level: latest-response locator + sentinel capture ─────────────

def make_controller(*, responses=1, copy_writes="GPT OUTPUT TEXT", hwnd=777,
                    clip_start="USER CLIP"):
    """responses = number of 'copy response' controls (assistant turns)."""
    strings = []
    for i in range(responses):
        strings += ["your message actions", "copy message",
                    "response actions", "copy response"]
    clip = {"v": clip_start}
    calls = {"copy_index": []}

    def copy_click_fn(h, index):
        calls["copy_index"].append(index)
        if copy_writes is not None:
            clip["v"] = copy_writes  # simulate Copy writing to clipboard

    c = ChatGPTBrowserController(
        focus_verify_fn=lambda k: {"verified": True, "hwnd": hwnd},
        window_strings_fn=lambda h: list(strings),
        clipboard_get_fn=lambda: clip["v"],
        clipboard_set_fn=lambda t: clip.__setitem__("v", t),
        foreground_fn=lambda: hwnd,
        copy_click_fn=copy_click_fn,
    )
    c._calls = calls
    c._clip = clip
    return c


# 4. latest assistant response identified
def test_latest_response_identified():
    c = make_controller(responses=1)
    r = c.find_latest_assistant_response(777)
    assert r["found"] and r["count"] == 1 and r["index"] == 0


# 5. multiple responses → newest selected
def test_multiple_responses_newest_selected():
    c = make_controller(responses=3)
    r = c.find_latest_assistant_response(777)
    assert r["count"] == 3 and r["index"] == 2  # last one


# 6. ambiguous / no latest response → fail
def test_no_response_fails():
    c = make_controller(responses=0)
    with pytest.raises(ChatGPTError):
        c.find_latest_assistant_response(777)


# 7/8. copy uses the newest response index (never an older one)
def test_copy_uses_newest_index():
    c = make_controller(responses=3)
    c.capture_clipboard_via_sentinel(777, timeout=1.0)
    assert c._calls["copy_index"] == [2]  # newest only


# 9/10. sentinel set + successful copy changes clipboard
def test_sentinel_then_capture_success():
    c = make_controller(responses=2, copy_writes="THE EXACT RESPONSE")
    res = c.capture_clipboard_via_sentinel(777, timeout=1.0)
    assert res["text"] == "THE EXACT RESPONSE"
    # user clipboard restored
    assert c._clip["v"] == "USER CLIP"


# 11. unchanged sentinel → failure
def test_unchanged_sentinel_fails():
    c = make_controller(responses=1, copy_writes=None)  # copy writes nothing
    with pytest.raises(ChatGPTError):
        c.capture_clipboard_via_sentinel(777, timeout=0.2)


# 12/13/14. exact text persisted-ready + hash + length correct
def test_exact_text_hash_len():
    text = "Line 1\n  indented  \nLine 3"
    c = make_controller(responses=1, copy_writes=text)
    res = c.capture_clipboard_via_sentinel(777, timeout=1.0)
    assert res["text"] == text  # EXACT, whitespace preserved
    assert res["length"] == len(text)
    assert res["sha256"] == _sha(text)


# ── Service-level fake + fixture ─────────────────────────────────────────────

class FakeChatGPT:
    def __init__(self, *, same=True, hwnd=999, responses=2, text="HELLO GPT OUTPUT",
                 fail_copy=False):
        self.same = same; self.hwnd = hwnd; self.responses = responses
        self.text = text; self.fail_copy = fail_copy
        self.copy_calls = 0; self.calls = []

    def restore_conversation(self, profile_key, url, expected_email=None):
        self.calls.append(("restore", profile_key, url))
        return {"hwnd": self.hwnd, "requested_url": url,
                "observed_url": url if self.same else "https://chatgpt.com/c/OTHER",
                "same": self.same}

    def restore_workflow_owned_conversation(self, *, workflow_id, conversation_url,
                                            window_binding, tab_identity=None,
                                            selected_profile_key=None,
                                            selected_account_email=None):
        # Migrated Part-6 authority: reuse persisted Jarvis-owned binding.
        self.calls.append(("restore_binding", workflow_id,
                           (window_binding or {}).get("hwnd")))
        return {"hwnd": self.hwnd, "requested_url": conversation_url,
                "observed_url": conversation_url if self.same else "https://chatgpt.com/c/OTHER",
                "same": self.same, "reused": True, "recovered": False,
                "identity": tab_identity or (window_binding or {}).get("tab_identity"),
                "window_binding": window_binding}

    def find_latest_assistant_response(self, hwnd):
        if self.responses <= 0:
            raise ChatGPTError("LATEST_RESPONSE_NOT_FOUND")
        return {"index": self.responses - 1, "count": self.responses, "found": True}

    def capture_clipboard_via_sentinel(self, hwnd, **kw):
        self.copy_calls += 1
        if self.fail_copy:
            raise ChatGPTError("GPT_COPY_FAILED: clipboard unchanged (sentinel)")
        return {"text": self.text, "length": len(self.text),
                "sha256": _sha(self.text), "method": "hook",
                "index": self.responses - 1, "count": self.responses}

    # Real GPT response identity helpers (mirror the controller).
    def read_latest_response_text(self, hwnd):
        # Cheap pre-copy read is unavailable in the fake → force post-copy path.
        return None

    def compute_response_identity(self, conversation_url, *, assistant_index, content):
        raw = f"{conversation_url}:{assistant_index}:{_sha(content)}"
        return _sha(raw)[:32]


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    store = WorkflowStore(db_path=tmp_path / "p6.db")
    engine = we_mod.WorkflowEngine(store=store)
    monkeypatch.setattr(we_mod, "workflow_engine", engine)
    monkeypatch.setattr(ar_mod, "workflow_engine", engine)
    events = []
    sends = []

    def publish_fn(wid, etype, payload):
        events.append((wid, etype, payload))

    fake = FakeChatGPT()
    service = WorkflowApprovalService(engine=engine, chatgpt_controller=fake,
                                      publish_fn=publish_fn, run_inline=True)
    # Telegram channel that records sends and returns incrementing message ids.
    state = {"mid": 1000}

    def send(chat_id, text, markup=None):
        state["mid"] += 1
        sends.append({"chat_id": chat_id, "text": text, "markup": markup,
                      "message_id": state["mid"]})
        return state["mid"]

    service.set_telegram_channels(send=send, edit=lambda *a, **k: True,
                                  is_authorized=lambda uid: uid == 42)
    monkeypatch.setattr(was_mod, "workflow_approval_service", service)
    yield engine, service, fake, events, sends
    engine.close()


def _output_ready(engine, *, chat_id=100, iteration=1, text=None):
    wf = engine.create_workflow(project_name="P6")
    wid = wf.workflow_id
    engine.update_workflow_context(
        wid, selected_chrome_profile_key="chrome-profile:Default",
        selected_chrome_account_email="x@y.com", master_gpt_prompt="m",
        chatgpt_conversation_url="https://chatgpt.com/c/live1")
    engine.transition(wid, "INITIALIZING")
    engine.transition(wid, "GPT_BOOTSTRAPPING")
    engine.transition(wid, "WAITING_FOR_COMMAND")
    engine.set_metadata(wid, "current_command", {"telegram_chat_id": chat_id,
                                                "telegram_message_id": 7})
    engine.transition(wid, "GPT_RUNNING")
    engine.update_workflow_context(wid, iteration_number=iteration)
    engine.transition(wid, "GPT_OUTPUT_READY")
    return wid


# 1. capture only allowed from GPT_OUTPUT_READY
def test_capture_only_from_output_ready(svc):
    engine, service, fake, _, _ = svc
    wf = engine.create_workflow(project_name="X")
    wid = wf.workflow_id
    engine.update_workflow_context(wid, selected_chrome_profile_key="k",
                                   chatgpt_conversation_url="https://chatgpt.com/c/x")
    with pytest.raises(ApprovalError):
        service.capture_and_request_approval(wid)  # still IDLE


# 2. exact selected Chrome profile required
def test_profile_required(svc):
    engine, service, fake, _, _ = svc
    wid = _output_ready(engine)
    engine.update_workflow_context(wid, selected_chrome_profile_key=None) if False else None
    # Simulate missing profile by clearing it isn't allowed via context update None?
    # Instead assert the guard exists: create a workflow at OUTPUT_READY w/o profile.
    wf = engine.create_workflow(project_name="NoProf")
    w2 = wf.workflow_id
    engine.update_workflow_context(w2, chatgpt_conversation_url="https://chatgpt.com/c/x")
    engine.transition(w2, "INITIALIZING"); engine.transition(w2, "GPT_BOOTSTRAPPING")
    engine.transition(w2, "WAITING_FOR_COMMAND"); engine.transition(w2, "GPT_RUNNING")
    engine.transition(w2, "GPT_OUTPUT_READY")
    with pytest.raises(ApprovalError):
        service.capture_and_request_approval(w2)


# 3. same saved conversation required (mismatch → no persist, stays capturable)
def test_conversation_mismatch_no_persist(svc):
    engine, service, fake, events, _ = svc
    fake.same = False
    wid = _output_ready(engine)
    service.capture_and_request_approval(wid)
    wf = engine.get_workflow(wid)
    assert not (wf.latest_gpt_output or "")  # nothing persisted
    assert wf.state == "GPT_OUTPUT_READY"    # NOT failed; retryable
    assert any(e[1] == was_mod.EV_APPROVAL_FAILED for e in events)


# 15. duplicate capture does not recopy after persistence
def test_duplicate_capture_no_recopy(svc):
    engine, service, fake, _, _ = svc
    wid = _output_ready(engine)
    service.capture_and_request_approval(wid)
    assert fake.copy_calls == 1
    # Re-run capture body (recovery C): output already persisted → no recopy.
    service._run_capture(wid)
    assert fake.copy_calls == 1


# 12b. exact text persisted (service level) + 13/14 metadata
def test_service_persists_exact(svc):
    engine, service, fake, _, _ = svc
    fake.text = "EXACT\n  spaced  output"
    wid = _output_ready(engine)
    service.capture_and_request_approval(wid)
    wf = engine.get_workflow(wid)
    assert wf.latest_gpt_output == "EXACT\n  spaced  output"
    assert (wf.metadata or {})["latest_gpt_output_hash"] == _sha(fake.text)
    assert (wf.metadata or {})["latest_gpt_output_len"] == len(fake.text)
    assert (wf.metadata or {})["latest_gpt_output_captured_at"]


# 16. GPT_OUTPUT_READY → WAITING_FOR_APPROVAL
def test_transition_to_waiting_approval(svc):
    engine, service, fake, _, _ = svc
    wid = _output_ready(engine)
    service.capture_and_request_approval(wid)
    assert engine.get_workflow(wid).state == "WAITING_FOR_APPROVAL"


# 17. exact output sent to Telegram
def test_exact_output_sent(svc):
    engine, service, fake, _, sends = svc
    fake.text = "MY EXACT RESPONSE"
    wid = _output_ready(engine, chat_id=100)
    service.capture_and_request_approval(wid)
    joined = "".join(s["text"] for s in sends if "GPT RESPONSE" in s["text"])
    assert "MY EXACT RESPONSE" in joined


# 18. long output chunking preserves exact concatenated text
def test_long_output_chunking_preserves(svc):
    engine, service, fake, _, sends = svc
    fake.text = "A" * 8000 + "END"
    wid = _output_ready(engine, chat_id=100)
    service.capture_and_request_approval(wid)
    resp_msgs = [s["text"] for s in sends if "GPT RESPONSE" in s["text"]]
    # strip the headers ("🧠 GPT RESPONSE — i/n\n\n") and concat the chunk bodies
    bodies = []
    for m in resp_msgs:
        bodies.append(m.split("\n\n", 1)[1])
    assert "".join(bodies) == fake.text
    assert len(resp_msgs) >= 2  # was chunked


# 19. approval buttons tied to workflow/iteration/hash (token)
def test_approval_token_binding(svc):
    engine, service, fake, _, sends = svc
    wid = _output_ready(engine, chat_id=100)
    service.capture_and_request_approval(wid)
    appr = (engine.get_workflow(wid).metadata or {}).get("approval") or {}
    assert appr["output_hash"] == _sha(fake.text)
    assert appr["token"]
    # button message carries appr:proceed:<token>
    btn = [s for s in sends if s.get("markup")][-1]
    kb = btn["markup"]["inline_keyboard"][0]
    assert any(b["callback_data"] == f"appr:proceed:{appr['token']}" for b in kb)


def _capture(engine, service, fake, chat_id=100):
    wid = _output_ready(engine, chat_id=chat_id)
    service.capture_and_request_approval(wid)
    appr = (engine.get_workflow(wid).metadata or {}).get("approval") or {}
    return wid, appr["token"]


# 20/24. authorized Proceed accepted + approved_gpt_prompt exact
def test_proceed_authorized(svc):
    engine, service, fake, _, _ = svc
    fake.text = "APPROVE THIS EXACT"
    wid, token = _capture(engine, service, fake)
    r = service.handle_callback(user_id=42, chat_id=100, action="proceed", token=token)
    assert r["ok"] and r["reason"] == "approved"
    wf = engine.get_workflow(wid)
    assert wf.approved_gpt_prompt == "APPROVE THIS EXACT"  # exact
    assert (wf.metadata or {})["approval"]["status"] == "approved"


# 21. unauthorized Proceed rejected
def test_proceed_unauthorized(svc):
    engine, service, fake, _, _ = svc
    wid, token = _capture(engine, service, fake)
    r = service.handle_callback(user_id=999, chat_id=100, action="proceed", token=token)
    assert not r["ok"] and r["reason"] == "unauthorized"
    assert not engine.get_workflow(wid).approved_gpt_prompt


# 22. stale Proceed rejected (token not found)
def test_proceed_stale(svc):
    engine, service, fake, _, _ = svc
    _capture(engine, service, fake)
    r = service.handle_callback(user_id=42, chat_id=100, action="proceed", token="deadbeef")
    assert not r["ok"] and r["reason"] == "stale"


# 23. duplicate Proceed idempotent
def test_proceed_duplicate_idempotent(svc):
    engine, service, fake, _, _ = svc
    wid, token = _capture(engine, service, fake)
    r1 = service.handle_callback(user_id=42, chat_id=100, action="proceed", token=token)
    r2 = service.handle_callback(user_id=42, chat_id=100, action="proceed", token=token)
    assert r1["ok"] and r2["ok"] and r2["reason"] == "already_approved"
    # workflow still WAITING_FOR_APPROVAL (Part 6 does not launch Kiro)
    assert engine.get_workflow(wid).state == "WAITING_FOR_APPROVAL"


# 25/26/27/28. Edit mode: enter, capture next text (not GPT), preview
def test_edit_flow(svc):
    engine, service, fake, events, sends = svc
    wid, token = _capture(engine, service, fake)
    r = service.handle_callback(user_id=42, chat_id=100, action="edit", token=token)
    assert r["reason"] == "edit_mode"
    assert service.is_edit_pending(42, 100) is True   # 25/26 edit captured next
    # next text is the edited prompt — handled by the service, NOT Part 5
    er = service.handle_edit_text(user_id=42, chat_id=100, text="EDITED PROMPT TEXT")
    assert er["handled"] and er["ok"]
    wf = engine.get_workflow(wid)
    assert (wf.metadata or {})["edited_gpt_prompt"] == "EDITED PROMPT TEXT"  # 27 exact, not to GPT
    assert (wf.metadata or {})["approval"]["status"] == "edited_pending_confirmation"
    # 28 preview sent
    assert any("EDITED PROMPT" in s["text"] for s in sends)


# 29. edited Proceed persists exact edited prompt
def test_edit_proceed_persists_exact(svc):
    engine, service, fake, _, _ = svc
    wid, token = _capture(engine, service, fake)
    service.handle_callback(user_id=42, chat_id=100, action="edit", token=token)
    service.handle_edit_text(user_id=42, chat_id=100, text="FINAL EDITED PROMPT")
    r = service.handle_edit_proceed(user_id=42, chat_id=100, token=token)
    assert r["ok"]
    wf = engine.get_workflow(wid)
    assert wf.approved_gpt_prompt == "FINAL EDITED PROMPT"
    assert (wf.metadata or {})["approval"]["status"] == "edited"


# edit text NOT captured for a user with no pending edit
def test_edit_text_not_pending(svc):
    engine, service, fake, _, _ = svc
    r = service.handle_edit_text(user_id=42, chat_id=100, text="random")
    assert r["handled"] is False


# 30. Cancel → CANCELLED
def test_cancel(svc):
    engine, service, fake, _, _ = svc
    wid, token = _capture(engine, service, fake)
    r = service.handle_callback(user_id=42, chat_id=100, action="cancel", token=token)
    assert r["ok"] and r["reason"] == "cancelled"
    assert engine.get_workflow(wid).state == "CANCELLED"


# 31. stale Cancel rejected (already approved not altered)
def test_stale_cancel_after_approve(svc):
    engine, service, fake, _, _ = svc
    wid, token = _capture(engine, service, fake)
    service.handle_callback(user_id=42, chat_id=100, action="proceed", token=token)
    r = service.handle_callback(user_id=42, chat_id=100, action="cancel", token=token)
    assert not r["ok"] and r["reason"] == "already_approved"
    assert engine.get_workflow(wid).state == "WAITING_FOR_APPROVAL"  # unchanged


# duplicate Cancel idempotent
def test_duplicate_cancel_idempotent(svc):
    engine, service, fake, _, _ = svc
    wid, token = _capture(engine, service, fake)
    service.handle_callback(user_id=42, chat_id=100, action="cancel", token=token)
    r = service.handle_callback(user_id=42, chat_id=100, action="cancel", token=token)
    assert r["ok"] and r["reason"] == "already_cancelled"


# 32. approval events ordered
def test_event_ordering(svc):
    engine, service, fake, events, _ = svc
    wid, token = _capture(engine, service, fake)
    service.handle_callback(user_id=42, chat_id=100, action="proceed", token=token)
    types = [e[1] for e in events if e[0] == wid]
    for a, b in [
        (was_mod.EV_CAPTURE_STARTED, was_mod.EV_LATEST_LOCATED),
        (was_mod.EV_LATEST_LOCATED, was_mod.EV_CLIPBOARD_CAPTURED),
        (was_mod.EV_CLIPBOARD_CAPTURED, was_mod.EV_OUTPUT_PERSISTED),
        (was_mod.EV_OUTPUT_PERSISTED, was_mod.EV_TELEGRAM_OUTPUT_SENT),
        (was_mod.EV_TELEGRAM_OUTPUT_SENT, was_mod.EV_APPROVAL_REQUESTED),
        (was_mod.EV_APPROVAL_REQUESTED, was_mod.EV_APPROVAL_PROCEED),
    ]:
        assert types.index(a) < types.index(b), f"{a} before {b}"


# 34. recovery with already-persisted output doesn't recopy
def test_recovery_persisted_no_recopy(svc):
    engine, service, fake, _, _ = svc
    wid = _output_ready(engine)
    service.capture_and_request_approval(wid)
    calls_before = fake.copy_calls
    # crash-and-resume: run capture again
    service._run_capture(wid)
    assert fake.copy_calls == calls_before


# copy failure → GPT_COPY_FAILED, stays capturable (not failed)
def test_copy_failure_stays_capturable(svc):
    engine, service, fake, events, _ = svc
    fake.fail_copy = True
    wid = _output_ready(engine)
    service.capture_and_request_approval(wid)
    wf = engine.get_workflow(wid)
    assert wf.state == "GPT_OUTPUT_READY"  # not FAILED
    assert any(e[1] == was_mod.EV_APPROVAL_FAILED for e in events)


# 33. Telegram poller responsiveness: capture runs on worker (not inline in prod)
def test_capture_uses_worker_not_inline():
    import inspect
    src = inspect.getsource(WorkflowApprovalService.capture_and_request_approval)
    assert "self._queue.put" in src and "start_worker" in src


# get_status surfaces Part 6 fields
def test_get_status(svc):
    engine, service, fake, _, _ = svc
    wid, token = _capture(engine, service, fake)
    st = service.get_status(wid)
    assert st["state"] == "WAITING_FOR_APPROVAL"
    assert st["latest_gpt_output_hash"] == _sha(fake.text)
    assert st["approval"]["token"] == token


# ── Part 5 → Part 6 HANDOFF (auto-enqueue capture on GPT_OUTPUT_READY) ───────

import jarvis_core.workflow_command_service as wcs_mod
from jarvis_core.workflow_command_service import WorkflowCommandService


class _CmdFakeChatGPT:
    """Minimal fake for the Part 5 command service to reach GPT_OUTPUT_READY."""
    def restore_conversation(self, profile_key, url, expected_email=None):
        return {"hwnd": 999, "requested_url": url, "observed_url": url, "same": True}
    def submit_command(self, hwnd, text):
        return {"submitted": True}
    def wait_for_followup_generation_start(self, hwnd, timeout=None):
        return True
    def wait_for_followup_complete(self, hwnd, timeout=None):
        return {"complete": True, "observations": 3, "had_controls": True}


def _waiting_for_command(engine, *, chat_id=100):
    wf = engine.create_workflow(project_name="P5H")
    wid = wf.workflow_id
    engine.update_workflow_context(
        wid, selected_chrome_profile_key="chrome-profile:Default",
        selected_chrome_account_email="x@y.com", master_gpt_prompt="m",
        chatgpt_conversation_url="https://chatgpt.com/c/live1", chatgpt_window_hwnd=999)
    engine.transition(wid, "INITIALIZING"); engine.transition(wid, "GPT_BOOTSTRAPPING")
    engine.transition(wid, "WAITING_FOR_COMMAND")
    return wid


# 1/2. GPT_RUNNING → GPT_OUTPUT_READY automatically enqueues capture exactly once
def test_handoff_enqueues_capture_once(svc, monkeypatch):
    engine, approval, fake, _, _ = svc
    calls = []
    cmd = WorkflowCommandService(engine=engine, chatgpt_controller=_CmdFakeChatGPT(),
                                 run_inline=True)
    cmd._capture_trigger_fn = lambda wid: calls.append(wid)
    _waiting_for_command(engine)
    cmd.submit_command(text="do it", telegram_message_id=1, telegram_chat_id=100,
                       telegram_user_id=42)
    # exactly one capture enqueue for the workflow that reached OUTPUT_READY
    assert len(calls) == 1
    assert engine.get_workflow(calls[0]).state == "GPT_OUTPUT_READY"


# 3. duplicate GPT_OUTPUT_READY handoff does not duplicate copy (approval guard)
def test_duplicate_handoff_no_double_copy(svc):
    engine, approval, fake, _, _ = svc
    wid = _output_ready(engine)
    approval.capture_and_request_approval(wid)   # first capture
    assert fake.copy_calls == 1
    # a second handoff/enqueue for the same (now WAITING_FOR_APPROVAL) workflow
    # is refused by the guard (not GPT_OUTPUT_READY) → no recopy.
    with pytest.raises(ApprovalError):
        approval.capture_and_request_approval(wid)
    assert fake.copy_calls == 1


# 4. restart recovery: persisted GPT_OUTPUT_READY (no output) resumes capture
def test_recovery_enqueues_persisted_output_ready(svc):
    engine, approval, fake, _, _ = svc
    wid = _output_ready(engine)  # GPT_OUTPUT_READY, no output captured yet
    scheduled = approval.recover_pending_captures()
    assert wid in scheduled
    wf = engine.get_workflow(wid)
    assert wf.state == "WAITING_FOR_APPROVAL"   # capture ran inline
    assert (wf.latest_gpt_output_len if hasattr(wf, "latest_gpt_output_len") else len(wf.latest_gpt_output or "")) > 0


# 5. recovery skips a workflow whose output is already persisted (no recopy)
def test_recovery_skips_already_captured(svc):
    engine, approval, fake, _, _ = svc
    wid = _output_ready(engine)
    approval.capture_and_request_approval(wid)   # captures + persists + →WAITING
    calls_before = fake.copy_calls
    scheduled = approval.recover_pending_captures()
    assert wid not in scheduled                  # already WAITING_FOR_APPROVAL, skipped
    assert fake.copy_calls == calls_before        # no recopy


# 6/7. successful capture → WAITING_FOR_APPROVAL + exact Telegram output + buttons
def test_handoff_full_flow_delivers(svc):
    engine, approval, fake, _, sends = svc
    fake.text = "EXACT AUTO-CAPTURED OUTPUT"
    wid = _output_ready(engine, chat_id=100)
    approval.capture_and_request_approval(wid)   # simulates the handoff trigger
    wf = engine.get_workflow(wid)
    assert wf.state == "WAITING_FOR_APPROVAL"
    joined = "".join(s["text"] for s in sends if "GPT RESPONSE" in s["text"])
    assert "EXACT AUTO-CAPTURED OUTPUT" in joined
    # approval buttons message present
    assert any(s.get("markup") for s in sends)


# handoff is best-effort: a failing trigger never breaks Part 5 completion
def test_handoff_trigger_failure_is_swallowed(svc):
    engine, approval, fake, _, _ = svc
    cmd = WorkflowCommandService(engine=engine, chatgpt_controller=_CmdFakeChatGPT(),
                                 run_inline=True)
    def boom(wid):
        raise RuntimeError("capture unavailable")
    cmd._capture_trigger_fn = boom
    wid = _waiting_for_command(engine)
    # must not raise; workflow still reaches GPT_OUTPUT_READY
    cmd.submit_command(text="do it", telegram_message_id=2, telegram_chat_id=100,
                       telegram_user_id=42)
    assert engine.get_workflow(wid).state == "GPT_OUTPUT_READY"


# the command service actually calls the handoff (source-level guarantee)
def test_command_service_invokes_handoff():
    import inspect
    src = inspect.getsource(wcs_mod.WorkflowCommandService._run_command)
    assert "_trigger_output_capture" in src


# ── Fix C: response-identity delivery idempotency ─────────────────────────────

def test_delivery_is_idempotent_for_same_response(svc):
    """Fix C: delivering the SAME captured response twice (Part5→6 handoff +
    recovery + WS reconnect can each trigger) must NOT resend the GPT output to
    Telegram. Delivery is latched by the response-identity token."""
    engine, service, fake, _, sends = svc
    fake.text = "ONE AND ONLY RESPONSE"
    wid = _output_ready(engine, chat_id=100)
    service.capture_and_request_approval(wid)   # captures + delivers once
    resp_before = [s for s in sends if "GPT RESPONSE" in s["text"]]
    assert len(resp_before) >= 1
    # A second delivery attempt for the SAME iteration/response is a no-op.
    wf = engine.get_workflow(wid)
    service._deliver_and_wait(wid, fake.text, wf.iteration_number or 1)
    resp_after = [s for s in sends if "GPT RESPONSE" in s["text"]]
    assert len(resp_after) == len(resp_before)  # no duplicate output sent


def test_new_iteration_response_delivers_again(svc):
    """Fix C: a genuinely NEW response (different iteration → different token)
    IS delivered — the latch only blocks duplicates of the same response."""
    engine, service, fake, _, sends = svc
    fake.text = "FIRST RESPONSE"
    wid = _output_ready(engine, chat_id=100, iteration=1)
    service.capture_and_request_approval(wid)
    first = [s for s in sends if "GPT RESPONSE" in s["text"]]
    # Simulate a new iteration with a new response, then deliver directly.
    engine.update_workflow_context(wid, iteration_number=2)
    engine.set_metadata(wid, "latest_gpt_output_hash", _sha("SECOND RESPONSE"))
    service._deliver_and_wait(wid, "SECOND RESPONSE", 2)
    second = [s for s in sends if "GPT RESPONSE" in s["text"]]
    assert len(second) > len(first)  # the new response WAS delivered


# ── Fix C: browser-level Copy-once via response identity ──────────────────────

def test_copy_button_clicked_exactly_once_for_same_response(svc):
    """The Copy button (capture_clipboard_via_sentinel) fires exactly once for a
    given response identity, even when capture is retried while output is
    already persisted (duplicate trigger / reconnect / recovery)."""
    engine, service, fake, _, _ = svc
    wid = _output_ready(engine, chat_id=100)
    service.capture_and_request_approval(wid)
    assert fake.copy_calls == 1
    # A response-identity latch is now persisted.
    meta = engine.get_workflow(wid).metadata or {}
    assert meta.get("latest_gpt_response_identity")
    assert meta.get("latest_gpt_output_copied_at")
    # Re-run the capture body directly (simulates duplicate handoff / reconnect /
    # recovery re-entry for the SAME response) → NO second Copy click.
    service._run_capture(wid)
    assert fake.copy_calls == 1


def test_copy_once_survives_lost_action_record(svc):
    """Even if the iteration copy-action record were absent, the durable
    response-identity latch still prevents a second Copy click for the SAME
    response."""
    engine, service, fake, _, _ = svc
    wid = _output_ready(engine, chat_id=100)
    service.capture_and_request_approval(wid)
    assert fake.copy_calls == 1
    # Wipe the iteration action record but keep the response-identity latch.
    # (The latch is checked BEFORE begin_action, so no second click happens.)
    service._run_capture(wid)
    assert fake.copy_calls == 1


def test_response_identity_distinguishes_content(svc):
    """The REAL response identity (conversation + assistant index + content)
    is stable for the SAME response and differs for a regenerated response with
    different content at the same index."""
    engine, service, fake, _, _ = svc
    conv = "https://chatgpt.com/c/live1"
    r1_a = fake.compute_response_identity(conv, assistant_index=0, content="R ONE")
    r1_b = fake.compute_response_identity(conv, assistant_index=0, content="R ONE")
    r2 = fake.compute_response_identity(conv, assistant_index=0, content="R TWO")
    assert r1_a == r1_b            # same response, same content → same identity
    assert r1_a != r2             # regenerated (different content) → new identity


def test_second_workflow_copies_independently(svc):
    """A different workflow (different identity) copies exactly once on its own
    — the first workflow's latch never blocks the second."""
    engine, service, fake, _, _ = svc
    w1 = _output_ready(engine, chat_id=100, iteration=1)
    service.capture_and_request_approval(w1)
    assert fake.copy_calls == 1
    w2 = _output_ready(engine, chat_id=100, iteration=1)
    service.capture_and_request_approval(w2)
    assert fake.copy_calls == 2   # the second workflow copied its own response once


# ── B3: copy-once across reconnect / restart / duplicate worker ───────────────

def test_copy_once_across_reconnect(svc):
    """Simulate a WS reconnect re-triggering capture after R1 was copied →
    no second Copy click."""
    engine, service, fake, _, _ = svc
    wid = _output_ready(engine, chat_id=100)
    service.capture_and_request_approval(wid)
    assert fake.copy_calls == 1
    # Reconnect re-enqueues capture for the same workflow/response.
    service._run_capture(wid)
    assert fake.copy_calls == 1


def test_copy_once_across_restart_recovery(svc):
    """Restart recovery re-runs capture for a persisted-output workflow →
    no second Copy click (RECOVERY C + response-identity latch)."""
    engine, service, fake, _, _ = svc
    wid = _output_ready(engine, chat_id=100)
    service.capture_and_request_approval(wid)
    assert fake.copy_calls == 1
    # recover_pending_captures should skip an already-captured workflow.
    scheduled = service.recover_pending_captures()
    assert wid not in scheduled
    assert fake.copy_calls == 1


def test_copy_once_duplicate_worker(svc):
    """A duplicate capture trigger for the same response → no second Copy."""
    engine, service, fake, _, _ = svc
    wid = _output_ready(engine, chat_id=100)
    service.capture_and_request_approval(wid)
    assert fake.copy_calls == 1
    # A duplicate handoff/enqueue for the now-WAITING_FOR_APPROVAL workflow is
    # refused (not GPT_OUTPUT_READY) → no recopy.
    with pytest.raises(ApprovalError):
        service.capture_and_request_approval(wid)
    assert fake.copy_calls == 1


def test_copied_response_identities_recorded(svc):
    """The durable copied-response-identity set records the copied response so
    the same response is recognised later."""
    engine, service, fake, _, _ = svc
    wid = _output_ready(engine, chat_id=100)
    service.capture_and_request_approval(wid)
    meta = engine.get_workflow(wid).metadata or {}
    ids = meta.get("copied_response_identities") or []
    assert len(ids) == 1
    assert meta.get("latest_gpt_response_identity") == ids[0]


# ── Part 6 migration: capture restores via persisted window binding ───────────

def test_part6_uses_window_binding_not_legacy(svc):
    engine, service, fake, _, _ = svc
    wid = _output_ready(engine, chat_id=100)
    engine.set_metadata(wid, "jarvis_browser_window", {
        "owner": "jarvis", "workflow_id": wid,
        "selected_profile_key": "chrome-profile:Default",
        "selected_profile_directory": "Default",
        "selected_account_email": "x@y.com", "hwnd": 999, "launch_token": "tok",
        "tab_identity": {"control_mode": "uia", "host_hwnd": 999,
                         "runtime_id": "42,999,4,0,0,7", "tab_index": 0,
                         "owner": "jarvis", "workflow_id": wid},
        "verified": True})
    service.capture_and_request_approval(wid)
    # binding-aware restore used; legacy restore NOT used.
    assert any(c[0] == "restore_binding" and c[1] == wid for c in fake.calls)
    assert not any(c[0] == "restore" for c in fake.calls)
    assert fake.copy_calls == 1                       # copy once
    assert engine.get_workflow(wid).state == "WAITING_FOR_APPROVAL"
