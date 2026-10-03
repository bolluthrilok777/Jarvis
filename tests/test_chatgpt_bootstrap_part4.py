"""Part 4 tests — ChatGPT bootstrap + persistent conversation.

ALL browser side effects are mocked. Two layers are tested:

- ChatGPTBrowserController with injected send-keys/clipboard/window-strings so
  navigation, login/ready detection, generation start/complete (positive
  evidence, no fixed sleep) and URL capture/validation are exercised without a
  real browser.
- WorkflowBootstrapService driven with run_inline=True and a fake ChatGPT
  controller + fake profile controller, plus the real WorkflowEngine over a
  temp DB, to prove transitions, idempotency, persistence and events.
"""
from __future__ import annotations

import time
import pytest
from fastapi.testclient import TestClient

import jarvis_core.workflow_engine as we_mod
import jarvis_core.architecture_routes as ar_mod
import jarvis_core.chatgpt_browser_controller as cbc_mod
import jarvis_core.workflow_bootstrap_service as wbs_mod
from jarvis_core.chatgpt_browser_controller import (
    ChatGPTBrowserController,
    LoginRequiredError,
    ChatGPTNotReadyError,
    GenerationTimeoutError,
    ConversationUrlError,
    ProfileOwnershipError,
)
from jarvis_core.workflow_bootstrap_service import (
    WorkflowBootstrapService,
    BootstrapError,
)
from jarvis_core.workflow_store import WorkflowStore
from app import app


# ── Controller-level fakes ───────────────────────────────────────────────────

def make_controller(*, strings_sequence=None, verified=True, hwnd=555,
                    observed="chrome-profile:Profile 7",
                    clipboard_url="https://chatgpt.com/c/abc123"):
    """Build a ChatGPTBrowserController with injected OS behaviour.

    strings_sequence: list of UIA-string lists returned on successive
    window_strings_fn calls (last one repeats). Drives state detection.
    """
    calls = {"keys": [], "typed": [], "clip_set": [], "clip_get": 0, "strings": 0}
    seq = list(strings_sequence or [["Message ChatGPT"]])
    clip = {"value": ""}

    def focus_verify_fn(profile_key):
        return {"verified": verified, "hwnd": hwnd, "requested": profile_key,
                "observed": observed}

    def window_strings_fn(h):
        calls["strings"] += 1
        idx = min(calls["strings"] - 1, len(seq) - 1)
        return seq[idx]

    def send_keys_fn(chord):
        calls["keys"].append(chord)
        # Emulate Ctrl+C after Ctrl+L placing the URL on the clipboard.
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
        return hwnd  # always the selected window (foreground guard passes)

    def click_point_fn(x, y):
        calls.setdefault("clicks", []).append((x, y))

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
    """Shrink poll intervals so timing-based tests run fast (logic unchanged)."""
    monkeypatch.setattr(cbc_mod, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(cbc_mod, "NAV_SETTLE_SECONDS", 0.0)


# 3/4. profile verification required + wrong profile fails
def test_profile_ownership_required():
    c = make_controller(verified=False)
    with pytest.raises(ProfileOwnershipError):
        c.ensure_selected_profile("chrome-profile:Profile 7")


def test_open_chatgpt_focuses_verified_profile():
    c = make_controller()
    r = c.open_chatgpt("chrome-profile:Profile 7")
    assert r["hwnd"] == 555
    assert "ctrl+l" in c._calls["keys"]
    assert any("chatgpt.com" in t for t in c._calls["typed"])
    assert "enter" in c._calls["keys"]


# 5. login-required detection — a login/auth URL is proof of login required.
def test_login_required_detection_by_url():
    c = make_controller(clipboard_url="https://chatgpt.com/auth/login",
                        strings_sequence=[["Log in", "Sign up", "Welcome back"]])
    with pytest.raises(LoginRequiredError):
        c.ensure_chatgpt_ready(555)


# login detection by accessibility when URL is not chatgpt at all
def test_login_required_detection_by_page():
    c = make_controller(clipboard_url="about:blank",
                        strings_sequence=[["Log in", "Sign up", "Welcome back"]])
    with pytest.raises(LoginRequiredError):
        c.ensure_chatgpt_ready(555)


# 6. ready detection — a chatgpt.com conversation URL proves readiness.
def test_ready_detection_by_url():
    c = make_controller(clipboard_url="https://chatgpt.com/",
                        strings_sequence=[["Message ChatGPT", "New chat"]])
    assert c.ensure_chatgpt_ready(555) == "READY"


def test_ready_timeout():
    # Never lands on chatgpt.com and no ready evidence → timeout.
    c = make_controller(clipboard_url="https://example.com/",
                        strings_sequence=[["loading..."]])
    import jarvis_core.chatgpt_browser_controller as m
    old = m.READY_TIMEOUT_SECONDS
    m.READY_TIMEOUT_SECONDS = 0.05
    try:
        with pytest.raises(ChatGPTNotReadyError):
            c.ensure_chatgpt_ready(555)
    finally:
        m.READY_TIMEOUT_SECONDS = old


# 7. new-chat activation — navigates to the ChatGPT ROOT (collision-free new
# chat), NOT a Chrome-colliding keyboard shortcut.
def test_new_chat_activation():
    c = make_controller(strings_sequence=[["Message ChatGPT"]],
                        clipboard_url="https://chatgpt.com/")
    c.start_new_chat(555)
    # Navigated to the root URL (fresh new chat), no Chrome-bookmark shortcut.
    assert any(t == "https://chatgpt.com/" for t in c._calls["typed"])
    assert "ctrl+shift+o" not in c._calls["keys"]


# 8/9. exact master requirement pasted + submitted once
def test_submit_prompt_pastes_exact_and_submits_once():
    c = make_controller()
    c.submit_prompt(555, "EXACT MASTER REQUIREMENT")
    assert "EXACT MASTER REQUIREMENT" in c._calls["clip_set"]  # exact text set
    assert c._calls["keys"].count("ctrl+v") == 1
    assert c._calls["keys"].count("enter") == 1  # submitted exactly once


def test_submit_prompt_empty_rejected():
    c = make_controller()
    with pytest.raises(Exception):
        c.submit_prompt(555, "   ")


# 11. generation start detected
def test_generation_start_detected():
    c = make_controller(strings_sequence=[["thinking"], ["Stop generating"]])
    assert c.detect_generation_started(555, timeout=1.0) is True


# 12/13. completion detected positively + not via fixed sleep
def test_generation_completion_positive_and_stable(monkeypatch):
    # Sequence: generating, generating, then completed controls appear and stay.
    seq = [
        ["Stop generating"],
        ["Stop generating"],
        ["Copy", "Regenerate"],   # obs 1
        ["Copy", "Regenerate"],   # obs 2
        ["Copy", "Regenerate"],   # obs 3 -> stable
    ]
    c = make_controller(strings_sequence=seq)
    res = c.wait_for_generation_complete(555, timeout=5.0)
    assert res["complete"] is True
    assert res["observations"] >= cbc_mod.COMPLETION_STABLE_OBSERVATIONS


def test_completion_requires_stability_not_single_frame():
    # A single completed frame surrounded by generating must NOT count as done.
    seq = [
        ["Copy", "Regenerate"],   # transient controls
        ["Stop generating"],      # back to generating -> resets stability
        ["Stop generating"],
    ]
    c = make_controller(strings_sequence=seq)
    with pytest.raises(GenerationTimeoutError):
        c.wait_for_generation_complete(555, timeout=0.1)


# 13. fixed sleep is NOT the completion signal (source + behavioral guarantee)
def test_no_fixed_sleep_as_completion_logic():
    import inspect
    src = inspect.getsource(ChatGPTBrowserController.wait_for_generation_complete)
    # Must require positive stable evidence, not a blind sleep-then-return.
    assert "COMPLETION_STABLE_OBSERVATIONS" in src
    assert "has_controls" in src and "generating" in src
    # No long fixed sleeps (e.g. sleep(30)/sleep(60)) as the completion path.
    assert "sleep(30)" not in src and "sleep(60)" not in src
    # The only sleep is the bounded poll interval.
    assert "POLL_INTERVAL" in src


# 14. completion timeout handled
def test_completion_timeout():
    c = make_controller(strings_sequence=[["Stop generating"]])
    with pytest.raises(GenerationTimeoutError):
        c.wait_for_generation_complete(555, timeout=0.05)


# 15. actual conversation URL captured
def test_capture_conversation_url():
    c = make_controller(clipboard_url="https://chatgpt.com/c/deadbeef")
    url = c.capture_conversation_url(555)
    assert url == "https://chatgpt.com/c/deadbeef"
    assert "ctrl+l" in c._calls["keys"] and "ctrl+c" in c._calls["keys"]


# 16. invalid/non-chatgpt URL rejected
def test_invalid_url_rejected():
    c = make_controller(clipboard_url="https://example.com/foo")
    with pytest.raises(ConversationUrlError):
        c.capture_conversation_url(555)


def test_login_url_rejected():
    c = make_controller(clipboard_url="https://chatgpt.com/auth/login")
    with pytest.raises(ConversationUrlError):
        c.capture_conversation_url(555)


# clipboard is restored after paste/capture
def test_clipboard_restored():
    c = make_controller(clipboard_url="https://chatgpt.com/c/x")
    # seed a prior clipboard value
    c._clipboard_set_fn("USER CLIPBOARD")
    c.submit_prompt(555, "prompt text")
    # last clip_set should restore the prior value
    assert c._calls["clip_set"][-1] == "USER CLIPBOARD"


# 19/20. Open Conversation uses saved URL and stays in selected profile
def test_restore_conversation_uses_saved_url_same_profile():
    c = make_controller(
        strings_sequence=[["Message ChatGPT"]],
        clipboard_url="https://chatgpt.com/c/keepme",
    )
    res = c.restore_conversation("chrome-profile:Profile 7", "https://chatgpt.com/c/keepme")
    assert res["same"] is True
    assert res["observed_url"].endswith("/c/keepme")
    # navigation typed the saved conversation URL (stays in the profile window)
    assert any("chatgpt.com/c/keepme" in t for t in c._calls["typed"])


# ── Service-level fakes + fixture ────────────────────────────────────────────

class FakeChatGPT:
    """Records the bootstrap call sequence; never touches a browser."""
    def __init__(self, *, login=False, verify_ok=True, url="https://chatgpt.com/c/live1"):
        self.login = login
        self.verify_ok = verify_ok
        self.url = url
        self.submit_count = 0
        self.calls = []

    def open_chatgpt(self, profile_key, expected_email=None, navigate=True):
        self.calls.append(("open", profile_key, navigate))
        if not self.verify_ok:
            raise ProfileOwnershipError("wrong profile")
        return {"hwnd": 999}

    def open_chatgpt_in_new_tab(self, profile_key, *, workflow_id, expected_email=None):
        # Verify the profile, create ONE new dedicated tab, open ChatGPT in it.
        self.calls.append(("open_new_tab", profile_key, workflow_id))
        if not self.verify_ok:
            raise ProfileOwnershipError("wrong profile")
        self.calls.append(("new_chat", 999))
        # Mirror the real controller: opening ChatGPT confirms readiness (login
        # page raises), so the login-required path still fails before prompt.
        self.ensure_chatgpt_ready(999)
        return {"hwnd": 999, "identity": {
            "owner": "jarvis", "workflow_id": workflow_id, "control_mode": "uia",
            "host_hwnd": 999, "runtime_id": "1,2,3", "conversation_url": self.url,
            "created_by": "jarvis"}}

    def ensure_selected_profile(self, profile_key, expected_email=None):
        if not self.verify_ok:
            raise ProfileOwnershipError("wrong profile")
        return 999

    def ensure_chatgpt_ready(self, hwnd, allow_login_page=False):
        self.calls.append(("ready", hwnd))
        if self.login:
            raise LoginRequiredError("CHATGPT_LOGIN_REQUIRED")
        return "READY"

    def start_new_chat(self, hwnd):
        self.calls.append(("new_chat", hwnd))
        # Mirror the real controller: start_new_chat navigates to root and then
        # confirms ChatGPT is ready (raises LoginRequiredError on a login page).
        self.ensure_chatgpt_ready(hwnd)

    def submit_prompt(self, hwnd, prompt):
        self.submit_count += 1
        self.calls.append(("submit", prompt))
        return {"submitted": True}

    def detect_generation_started(self, hwnd, timeout=None):
        self.calls.append(("gen_start", hwnd))
        return True

    def wait_for_generation_complete(self, hwnd, timeout=None):
        self.calls.append(("gen_complete", hwnd))
        return {"complete": True}

    def capture_conversation_url(self, hwnd):
        self.calls.append(("capture", hwnd))
        return self.url

    def restore_conversation(self, profile_key, url, expected_email=None):
        self.calls.append(("restore", profile_key, url))
        return {"hwnd": 999, "requested_url": url, "observed_url": url, "same": True}


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    store = WorkflowStore(db_path=tmp_path / "p4.db")
    engine = we_mod.WorkflowEngine(store=store)
    monkeypatch.setattr(we_mod, "workflow_engine", engine)
    monkeypatch.setattr(ar_mod, "workflow_engine", engine)
    events = []

    def publish_fn(wid, etype, payload):
        events.append((wid, etype, payload))

    fake = FakeChatGPT()
    service = WorkflowBootstrapService(
        engine=engine, chatgpt_controller=fake, publish_fn=publish_fn,
        run_inline=True,
    )
    monkeypatch.setattr(wbs_mod, "workflow_bootstrap_service", service)
    yield engine, service, fake, events
    engine.close()


def _mk_workflow(engine, *, profile="chrome-profile:Profile 7", prompt="Do the thing"):
    wf = engine.create_workflow(project_name="P4")
    if profile is not None:
        engine.update_workflow_context(wf.workflow_id, selected_chrome_profile_key=profile,
                                        selected_chrome_account_email="x@y.com")
    if prompt is not None:
        engine.update_workflow_context(wf.workflow_id, master_gpt_prompt=prompt)
    return wf.workflow_id


# 1. no selected Chrome profile → start rejected
def test_start_rejected_no_profile(svc):
    engine, service, fake, _ = svc
    wid = _mk_workflow(engine, profile=None)
    with pytest.raises(BootstrapError):
        service.start(wid)


# 2. empty Master Requirement → rejected
def test_start_rejected_no_prompt(svc):
    engine, service, fake, _ = svc
    wid = _mk_workflow(engine, prompt=None)
    with pytest.raises(BootstrapError):
        service.start(wid)


# 21. workflow reaches WAITING_FOR_COMMAND only after bootstrap success
def test_bootstrap_success_reaches_waiting(svc):
    engine, service, fake, _ = svc
    wid = _mk_workflow(engine)
    service.start(wid)
    wf = engine.get_workflow(wid)
    assert wf.state == "WAITING_FOR_COMMAND"
    assert wf.chatgpt_conversation_url == "https://chatgpt.com/c/live1"  # 15/17 persisted
    assert wf.chatgpt_window_hwnd == 999  # 18 persisted
    assert fake.submit_count == 1  # 9 submitted once


# 4. wrong profile → bootstrap fails
def test_wrong_profile_fails(svc):
    engine, service, fake, _ = svc
    fake.verify_ok = False
    wid = _mk_workflow(engine)
    service.start(wid)
    wf = engine.get_workflow(wid)
    assert wf.state == "FAILED"


# 5. login-required → FAILED with code
def test_login_required_fails(svc):
    engine, service, fake, _ = svc
    fake.login = True
    wid = _mk_workflow(engine)
    service.start(wid)
    wf = engine.get_workflow(wid)
    assert wf.state == "FAILED"
    combined = ((wf.last_error_code or "") + " " + (wf.last_error or "")).lower()
    assert "login" in combined


# 10/24. duplicate start / restart after completed prompt does not resend
def test_duplicate_start_does_not_resend(svc):
    engine, service, fake, _ = svc
    wid = _mk_workflow(engine)
    service.start(wid)
    assert fake.submit_count == 1
    # Workflow is now WAITING_FOR_COMMAND; a second start is rejected (not IDLE).
    with pytest.raises(BootstrapError):
        service.start(wid)
    assert fake.submit_count == 1


def test_restart_after_completed_prompt_no_resend(svc):
    """Simulate a crash where the master-prompt action already COMPLETED but the
    workflow is still mid-bootstrap. Re-running the bootstrap MUST NOT resend."""
    engine, service, fake, _ = svc
    wf = engine.create_workflow(project_name="Recover")
    wid = wf.workflow_id
    engine.update_workflow_context(wid, selected_chrome_profile_key="chrome-profile:Profile 7",
                                   selected_chrome_account_email="x@y.com",
                                   master_gpt_prompt="Do the thing")
    # Drive to GPT_BOOTSTRAPPING and mark BOTH actions completed (as a prior run
    # would have) BEFORE re-running the bootstrap body.
    engine.transition(wid, "INITIALIZING")
    engine.transition(wid, "GPT_BOOTSTRAPPING")
    for act in (f"chatgpt-bootstrap-open:{wid}", f"chatgpt-master-prompt:{wid}"):
        engine.begin_action(wid, act, action_type=(
            "chatgpt_open" if "open" in act else "chatgpt_master_prompt"))
        engine.complete_action(wid, act, result_ref="prior-run")
    # Re-run the bootstrap body (recovery). begin_action returns started=False,
    # so submit_prompt is NEVER called.
    service._run_bootstrap(wid)
    assert fake.submit_count == 0  # NOT resent
    assert engine.get_workflow(wid).state == "WAITING_FOR_COMMAND"


# 22. failure transitions to FAILED
def test_failure_transitions_failed(svc):
    engine, service, fake, _ = svc

    def boom(hwnd, timeout=None):
        raise GenerationTimeoutError("no completion")
    fake.wait_for_generation_complete = boom
    wid = _mk_workflow(engine)
    service.start(wid)
    assert engine.get_workflow(wid).state == "FAILED"


# 23. realtime events ordered
def test_events_ordered(svc):
    engine, service, fake, events = svc
    wid = _mk_workflow(engine)
    service.start(wid)
    types = [e[1] for e in events if e[0] == wid]
    # Key milestones appear in order.
    def idx(t): return types.index(t)
    assert idx("workflow_start_requested") < idx("chatgpt_opening")
    assert idx("chatgpt_opening") < idx("chatgpt_ready")
    assert idx("chatgpt_ready") < idx("chatgpt_new_chat")
    assert idx("chatgpt_new_chat") < idx("chatgpt_master_prompt_submitted")
    assert idx("chatgpt_master_prompt_submitted") < idx("chatgpt_generation_complete")
    assert idx("chatgpt_generation_complete") < idx("chatgpt_conversation_saved")
    assert idx("chatgpt_conversation_saved") < idx("chatgpt_bootstrap_complete")


# 25. restart selection/conversation persists
def test_restart_persistence(svc, tmp_path):
    engine, service, fake, _ = svc
    wid = _mk_workflow(engine)
    service.start(wid)
    from jarvis_core.workflow_engine import WorkflowEngine
    eng2 = WorkflowEngine(store=WorkflowStore(db_path=engine._store.db_path))
    try:
        wf = eng2.get_workflow(wid)
        assert wf.state == "WAITING_FOR_COMMAND"
        assert wf.selected_chrome_profile_key == "chrome-profile:Profile 7"
        assert wf.chatgpt_conversation_url == "https://chatgpt.com/c/live1"
    finally:
        eng2.close()


# ── API-level tests ──────────────────────────────────────────────────────────

@pytest.fixture()
def api(svc):
    engine, service, fake, events = svc
    client = TestClient(app)
    return client, engine, service, fake


def _api_mk(client, engine):
    wid = engine.create_workflow(project_name="API").workflow_id
    engine.update_workflow_context(wid, selected_chrome_profile_key="chrome-profile:Profile 7",
                                   selected_chrome_account_email="x@y.com",
                                   master_gpt_prompt="Master req")
    return wid


def test_api_start_success(api):
    client, engine, service, fake = api
    wid = _api_mk(client, engine)
    r = client.post(f"/api/workflows/{wid}/start", json={})
    assert r.status_code == 200
    # run_inline means bootstrap already completed by the time start returns.
    assert engine.get_workflow(wid).state == "WAITING_FOR_COMMAND"


def test_api_start_rejects_no_profile(api):
    client, engine, service, fake = api
    wid = engine.create_workflow(project_name="NP").workflow_id
    engine.update_workflow_context(wid, master_gpt_prompt="x")
    r = client.post(f"/api/workflows/{wid}/start", json={})
    assert r.status_code == 400


def test_api_chatgpt_status(api):
    client, engine, service, fake = api
    wid = _api_mk(client, engine)
    client.post(f"/api/workflows/{wid}/start", json={})
    s = client.get(f"/api/workflows/{wid}/chatgpt/status").json()
    assert s["status"] == "saved"
    assert s["conversation_url"] == "https://chatgpt.com/c/live1"


def test_api_open_conversation_uses_saved_url(api):
    client, engine, service, fake = api
    wid = _api_mk(client, engine)
    client.post(f"/api/workflows/{wid}/start", json={})
    r = client.post(f"/api/workflows/{wid}/chatgpt/open-conversation", json={})
    assert r.status_code == 200
    assert r.json()["result"]["same"] is True
    # restore was called with the saved URL in the selected profile
    assert ("restore", "chrome-profile:Profile 7", "https://chatgpt.com/c/live1") in fake.calls


def test_api_open_conversation_no_conversation(api):
    client, engine, service, fake = api
    wid = _api_mk(client, engine)  # not started → no saved URL
    r = client.post(f"/api/workflows/{wid}/chatgpt/open-conversation", json={})
    assert r.status_code == 400


# ── WAITING_FOR_COMMAND Telegram readiness notification (Part B) ─────────────

def _notify_recorder():
    sent = []
    def notify(text, markup=None):
        sent.append(text)
        return 700 + len(sent)  # fake message id
    return sent, notify


# 15. WAITING_FOR_COMMAND sends the readiness notification exactly once
def test_waiting_notification_sent_once(svc):
    engine, service, fake, _ = svc
    sent, notify = _notify_recorder()
    service.set_telegram_channels(notify=notify)
    wid = _mk_workflow(engine)
    service.start(wid)
    assert engine.get_workflow(wid).state == "WAITING_FOR_COMMAND"
    assert len(sent) == 1
    assert "GPT is ready" in sent[0]
    meta = (engine.get_workflow(wid).metadata or {}).get("telegram_waiting_notification") or {}
    assert meta.get("sent") is True and meta.get("message_id") == 701


# 16. notification only after durable WAITING_FOR_COMMAND (state persisted first)
def test_waiting_notification_only_after_state(svc):
    engine, service, fake, _ = svc
    captured_state = {}
    def notify(text, markup=None):
        captured_state["state"] = engine.get_workflow(wid).state
        return 701
    service.set_telegram_channels(notify=notify)
    wid = _mk_workflow(engine)
    service.start(wid)
    assert captured_state["state"] == "WAITING_FOR_COMMAND"


# 17. duplicate call does not duplicate the notification (idempotent action)
def test_waiting_notification_duplicate_no_resend(svc):
    engine, service, fake, _ = svc
    sent, notify = _notify_recorder()
    service.set_telegram_channels(notify=notify)
    wid = _mk_workflow(engine)
    service.start(wid)
    assert len(sent) == 1
    # direct re-invocation must NOT resend
    service.notify_awaiting_command(wid)
    assert len(sent) == 1


# 18. restart recovery sends a MISSING notification once
def test_waiting_notification_recovery_sends_missing(svc):
    engine, service, fake, _ = svc
    # First reach WAITING_FOR_COMMAND with NO notify channel → not sent.
    wid = _mk_workflow(engine)
    service.start(wid)
    meta = (engine.get_workflow(wid).metadata or {}).get("telegram_waiting_notification") or {}
    assert not meta.get("sent")
    # Now wire a channel + run recovery → sends once.
    sent, notify = _notify_recorder()
    service.set_telegram_channels(notify=notify)
    recovered = service.recover_awaiting_command_notifications()
    assert wid in recovered and len(sent) == 1


# 19. already-sent notification is NOT resent by recovery
def test_waiting_notification_recovery_skips_sent(svc):
    engine, service, fake, _ = svc
    sent, notify = _notify_recorder()
    service.set_telegram_channels(notify=notify)
    wid = _mk_workflow(engine)
    service.start(wid)
    assert len(sent) == 1
    recovered = service.recover_awaiting_command_notifications()
    assert wid not in recovered and len(sent) == 1


# 22. send failure is recorded safely (sent=False) and does not break bootstrap
def test_waiting_notification_send_failure_recorded(svc):
    engine, service, fake, _ = svc
    def notify(text, markup=None):
        return None  # failure / no chat resolvable
    service.set_telegram_channels(notify=notify)
    wid = _mk_workflow(engine)
    service.start(wid)
    # bootstrap still reached WAITING_FOR_COMMAND
    assert engine.get_workflow(wid).state == "WAITING_FOR_COMMAND"
    meta = (engine.get_workflow(wid).metadata or {}).get("telegram_waiting_notification") or {}
    assert meta.get("sent") is False


# 23. retry works after a send failure (recovery re-sends once channel works)
def test_waiting_notification_retry_after_failure(svc):
    engine, service, fake, _ = svc
    state = {"ok": False}
    sent = []
    def notify(text, markup=None):
        if not state["ok"]:
            return None
        sent.append(text); return 701
    service.set_telegram_channels(notify=notify)
    wid = _mk_workflow(engine)
    service.start(wid)  # fails to send
    assert len(sent) == 0
    state["ok"] = True
    recovered = service.recover_awaiting_command_notifications()
    assert wid in recovered and len(sent) == 1


# notification not sent for a non-WAITING workflow (guard)
def test_waiting_notification_guard_state(svc):
    engine, service, fake, _ = svc
    sent, notify = _notify_recorder()
    service.set_telegram_channels(notify=notify)
    wf = engine.create_workflow(project_name="guard")
    # IDLE workflow → no notification
    assert service.notify_awaiting_command(wf.workflow_id) is False
    assert len(sent) == 0


# ── Fix B: single Jarvis-owned tab + same conversation ────────────────────────

def test_bootstrap_opens_one_new_tab_single_navigation(svc):
    """Bootstrap opens the workflow in ONE new dedicated tab (never the active
    tab): exactly one open_chatgpt_in_new_tab call and exactly one fresh chat
    navigation. The legacy active-tab open_chatgpt is NOT used."""
    engine, service, fake, _ = svc
    wid = _mk_workflow(engine)
    service.start(wid)
    open_new_tab = [c for c in fake.calls if c[0] == "open_new_tab"]
    new_chats = [c for c in fake.calls if c[0] == "new_chat"]
    legacy_open = [c for c in fake.calls if c[0] == "open"]
    assert len(open_new_tab) == 1                 # one dedicated new tab
    assert open_new_tab[0][1] == "chrome-profile:Profile 7"
    assert len(new_chats) == 1                    # one fresh-chat navigation
    assert legacy_open == []                      # active-tab open never used


def test_bootstrap_persists_jarvis_tab_binding(svc):
    """Fix B: a Jarvis-owned tab binding is persisted (profile identity + hwnd +
    conversation URL) so later stages reuse THIS one conversation."""
    engine, service, fake, _ = svc
    wid = _mk_workflow(engine)
    service.start(wid)
    wf = engine.get_workflow(wid)
    binding = (wf.metadata or {}).get("chatgpt_tab_binding")
    assert binding is not None
    assert binding["owner"] == "jarvis"
    assert binding["profile_key"] == "chrome-profile:Profile 7"
    assert binding["conversation_url"] == "https://chatgpt.com/c/live1"
    assert binding["window_hwnd"] == 999
    assert binding.get("created_at") and binding.get("verified_at")


def test_open_conversation_reuses_same_conversation_url(svc):
    """Fix B: reopening the conversation navigates the SAME saved /c/<id> URL,
    never a new chat (no start_new_chat outside bootstrap)."""
    engine, service, fake, _ = svc
    wid = _mk_workflow(engine)
    service.start(wid)
    # Clear recorded calls, then reopen the saved conversation.
    fake.calls.clear()
    service.open_conversation(wid)
    restores = [c for c in fake.calls if c[0] == "restore"]
    new_chats = [c for c in fake.calls if c[0] == "new_chat"]
    assert restores == [("restore", "chrome-profile:Profile 7",
                         "https://chatgpt.com/c/live1")]
    # reopening NEVER creates a new chat
    assert new_chats == []


# ── Fix B: bootstrap persists a REAL tab identity + exact navigation count ────

class TabAwareFakeChatGPT(FakeChatGPT):
    """Fake that also implements establish_owned_tab (real tab identity path)."""

    def __init__(self, *, tab_identity=None, closed=0, **kw):
        super().__init__(**kw)
        self._tab_identity = tab_identity or {
            "owner": "jarvis", "workflow_id": None,
            "conversation_url": "https://chatgpt.com/c/live1",
            "control_mode": "cdp", "target_id": "T-LIVE"}
        self._closed = closed

    def establish_owned_tab(self, hwnd, *, workflow_id, conversation_url):
        self.calls.append(("establish_owned_tab", workflow_id, conversation_url))
        ident = dict(self._tab_identity)
        ident["workflow_id"] = workflow_id
        ident["conversation_url"] = conversation_url
        return {"identity": ident, "count": 1, "closed": self._closed, "reason": ""}


@pytest.fixture()
def tab_svc(tmp_path, monkeypatch):
    store = WorkflowStore(db_path=tmp_path / "p4tab.db")
    engine = we_mod.WorkflowEngine(store=store)
    monkeypatch.setattr(we_mod, "workflow_engine", engine)
    monkeypatch.setattr(ar_mod, "workflow_engine", engine)
    events = []
    fake = TabAwareFakeChatGPT()
    service = WorkflowBootstrapService(
        engine=engine, chatgpt_controller=fake,
        publish_fn=lambda w, e, p: events.append((w, e, p)), run_inline=True)
    monkeypatch.setattr(wbs_mod, "workflow_bootstrap_service", service)
    yield engine, service, fake, events
    engine.close()


def test_bootstrap_persists_real_cdp_tab_id(tab_svc):
    engine, service, fake, _ = tab_svc
    wid = _mk_workflow(engine)
    service.start(wid)
    wf = engine.get_workflow(wid)
    # chatgpt_tab_id is now a REAL identifier (the CDP target id), not null.
    assert wf.chatgpt_tab_id == "T-LIVE"
    binding = (wf.metadata or {}).get("chatgpt_tab_binding") or {}
    ident = binding.get("tab_identity") or {}
    assert ident.get("control_mode") == "cdp"
    assert ident.get("target_id") == "T-LIVE"
    assert ident.get("workflow_id") == wid          # workflow-scoped
    # establish_owned_tab was invoked exactly once.
    assert [c for c in fake.calls if c[0] == "establish_owned_tab"] == [
        ("establish_owned_tab", wid, "https://chatgpt.com/c/live1")]


def test_bootstrap_navigation_exactly_once(tab_svc):
    """NEW workflow: ONE new dedicated tab, ONE fresh-chat navigation, ONE
    Master Requirement submit — no active-tab open, no repeated navigation."""
    engine, service, fake, _ = tab_svc
    wid = _mk_workflow(engine)
    service.start(wid)
    open_new_tab = [c for c in fake.calls if c[0] == "open_new_tab"]
    new_chats = [c for c in fake.calls if c[0] == "new_chat"]
    submits = [c for c in fake.calls if c[0] == "submit"]
    legacy_open = [c for c in fake.calls if c[0] == "open"]
    assert len(open_new_tab) == 1                       # one dedicated new tab
    assert len(new_chats) == 1                          # single fresh-chat nav
    assert len(submits) == 1                            # master prompt once
    assert legacy_open == []                            # active-tab open unused
    assert fake.submit_count == 1


def test_bootstrap_reports_duplicate_tabs_closed(tab_svc, monkeypatch):
    engine, service, fake, events = tab_svc
    fake._closed = 2   # pretend two duplicate Jarvis tabs were cleaned
    wid = _mk_workflow(engine)
    service.start(wid)
    closed_events = [p for (w, e, p) in events
                     if w == wid and p.get("duplicate_tabs_closed")]
    assert closed_events and closed_events[0]["duplicate_tabs_closed"] == 2


def test_bootstrap_fails_closed_when_no_tab_identity(tab_svc):
    """B1: if a REAL tab identity cannot be obtained, the bootstrap FAILS —
    it must not reach WAITING_FOR_COMMAND with window/URL-only ownership."""
    from jarvis_core.chatgpt_browser_controller import TabIdentityUnavailableError
    engine, service, fake, _ = tab_svc

    def _raise(hwnd, *, workflow_id, conversation_url):
        raise TabIdentityUnavailableError("TAB_IDENTITY_UNAVAILABLE")
    fake.establish_owned_tab = _raise
    wid = _mk_workflow(engine)
    service.start(wid)
    wf = engine.get_workflow(wid)
    assert wf.state == "FAILED"
    assert wf.state != "WAITING_FOR_COMMAND"


# ── Dedicated Jarvis-owned window bootstrap (authoritative Part-7 open) ────────

class DedicatedWindowFakeChatGPT(FakeChatGPT):
    """Fake implementing the dedicated-window open (chrome://version-verified)."""

    def open_chatgpt_in_dedicated_window(self, profile_key, *, workflow_id,
                                         selected_profile_directory,
                                         expected_email=None):
        self.calls.append(("open_dedicated", profile_key, workflow_id,
                           selected_profile_directory))
        if not self.verify_ok:
            raise ProfileOwnershipError("wrong profile")
        self.calls.append(("new_chat", 999))
        self.ensure_chatgpt_ready(999)
        binding = {"owner": "jarvis", "workflow_id": workflow_id,
                   "selected_profile_directory": selected_profile_directory,
                   "observed_profile_directory": selected_profile_directory,
                   "observed_account_email": expected_email,
                   "hwnd": 999, "observed_profile_path":
                   f"C:/UD/{selected_profile_directory}", "verified": True}
        return {"hwnd": 999, "window_binding": binding, "identity": {
            "owner": "jarvis", "workflow_id": workflow_id, "control_mode": "uia",
            "host_hwnd": 999, "runtime_id": "7,7,7", "conversation_url": self.url,
            "created_by": "jarvis"}}


@pytest.fixture()
def ded_svc(tmp_path, monkeypatch):
    store = WorkflowStore(db_path=tmp_path / "p4ded.db")
    engine = we_mod.WorkflowEngine(store=store)
    monkeypatch.setattr(we_mod, "workflow_engine", engine)
    monkeypatch.setattr(ar_mod, "workflow_engine", engine)
    fake = DedicatedWindowFakeChatGPT()
    service = WorkflowBootstrapService(
        engine=engine, chatgpt_controller=fake,
        publish_fn=lambda w, e, p: None, run_inline=True)
    monkeypatch.setattr(wbs_mod, "workflow_bootstrap_service", service)
    yield engine, service, fake
    engine.close()


def test_bootstrap_uses_dedicated_window_and_persists_binding(ded_svc):
    engine, service, fake = ded_svc
    wid = _mk_workflow(engine)          # selected chrome-profile:Profile 7
    service.start(wid)
    wf = engine.get_workflow(wid)
    assert wf.state == "WAITING_FOR_COMMAND"
    # The dedicated-window open was used (not the active-tab or new-tab path).
    ded = [c for c in fake.calls if c[0] == "open_dedicated"]
    assert len(ded) == 1
    assert ded[0][3] == "Profile 7"     # selected directory passed through
    # The verified Jarvis window binding is persisted on the workflow.
    binding = (wf.metadata or {}).get("jarvis_browser_window") or {}
    assert binding.get("owner") == "jarvis"
    assert binding.get("verified") is True
    assert binding.get("observed_profile_directory") == "Profile 7"
    # No legacy active-tab open was used.
    assert [c for c in fake.calls if c[0] == "open"] == []
    assert fake.submit_count == 1
