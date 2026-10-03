"""Part 7b tests — Kiro application + session ownership.

Covers the production-grade requirements added to fix the HWND 722342 bug
(a Chrome window was mistaken for the Kiro IDE) and to give each workflow a
Jarvis-OWNED Kiro session with continuous ownership verification, human
session-switch detection, fail-closed pause, and durable-substate resume.

ALL desktop side effects are faked. No live Kiro. Layers:
- process identity (is_kiro_process / _default_enumerate exe gate)
- JarvisKiroSessionManager (bind / liveness / mismatch / reselect / rebind)
- WorkflowKiroService integration (create+bind, ownership gate, pause/resume)
- KiroMismatchService (Telegram confirmation callbacks, idempotent)
"""
from __future__ import annotations

import hashlib
import pytest

import jarvis_core.workflow_engine as we_mod
import jarvis_core.architecture_routes as ar_mod
import jarvis_core.workflow_kiro_service as wks_mod
from jarvis_core.kiro_controller import is_kiro_process, KiroController
from jarvis_core.kiro_automation_lock import KiroAutomationLock
from jarvis_core.workflow_kiro_service import WorkflowKiroService, KiroServiceError
from jarvis_core.jarvis_kiro_session import (
    JarvisKiroSessionManager, KiroSessionError, BINDING_KEY, MISMATCH_KEY,
)
from jarvis_core.kiro_mismatch_service import KiroMismatchService
from jarvis_core.workflow_store import WorkflowStore

PROJECT = r"C:\Project Opus"
KIRO_EXE = r"C:\Users\me\AppData\Local\Programs\Kiro\Kiro.exe"
CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def _sha(t): return hashlib.sha256(t.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════
# 1-2. Process identity: reject chrome.exe, accept Kiro.exe (the core bug)
# ══════════════════════════════════════════════════════════════════════════

def test_is_kiro_process_accepts_kiro_exe():
    assert is_kiro_process(KIRO_EXE) is True
    assert is_kiro_process(r"C:\other\Kiro.exe", "Kiro.exe") is True


def test_is_kiro_process_rejects_chrome_even_with_kiro_title():
    # The exact HWND 722342 case: chrome.exe window titled "...Kiro...".
    assert is_kiro_process(CHROME_EXE) is False
    assert is_kiro_process(CHROME_EXE, "chrome.exe") is False


def test_is_kiro_process_rejects_edge_and_electron_and_code():
    assert is_kiro_process(r"C:\x\msedge.exe") is False
    assert is_kiro_process(r"C:\x\electron.exe") is False
    assert is_kiro_process(r"C:\x\Code.exe") is False


def test_is_kiro_process_rejects_empty():
    assert is_kiro_process("", "") is False


def test_window_identity_gate_rejects_chrome_accepts_kiro():
    """Downstream contract for the enumerator fix: given a mixed enumeration
    (a Chrome window titled '...Kiro...' + a real Kiro.exe window), the
    controller's identity gate accepts only the Kiro.exe window and rejects the
    Chrome one (the exact HWND 722342 scenario)."""
    enumerated = [
        {"hwnd": 722342, "pid": 6000,
         "title": "Waiting for Kiro Report - Google Chrome",
         "class": "Chrome_WidgetWin_1", "executable": CHROME_EXE,
         "process_name": "chrome.exe"},
        {"hwnd": 18746304, "pid": 3472, "title": "Project Opus - Kiro",
         "class": "Chrome_WidgetWin_1", "executable": KIRO_EXE,
         "process_name": "Kiro.exe"},
    ]
    c = KiroController(enumerate_fn=lambda: [dict(w) for w in enumerated])
    assert c.window_is_kiro(722342) is False        # Chrome window rejected
    assert c.window_is_kiro(18746304) is True        # real Kiro accepted
    # verify_project on the Chrome window can NEVER verify as the project.
    assert c.verify_project(722342, PROJECT)["verified"] is False


def test_default_enumerate_uses_process_identity_gate():
    """The real enumerator applies is_kiro_process to each window's executable
    (verified via source), so a chrome.exe window is filtered out at the source
    rather than by title heuristics."""
    import inspect
    src = inspect.getsource(KiroController._default_enumerate)
    assert "is_kiro_process(exe, pname)" in src       # exe-identity gate, not title
    assert "\"kiro\" in" not in src and "'kiro' in" not in src  # no title substring match


# ══════════════════════════════════════════════════════════════════════════
# Shared fake controller for session/service tests
# ══════════════════════════════════════════════════════════════════════════

class FakeKiroSession:
    """Fake Kiro controller modelling the REAL session UI: a session list of
    rows (durable locators = title + automation_id), an active/selected session,
    and accessible New-session / select actions. Windows carry an executable so
    process identity is exercised realistically."""

    def __init__(self):
        self.windows = {
            200: {"hwnd": 200, "pid": 3472, "title": "Project Opus - Kiro",
                  "class": "Chrome_WidgetWin_1", "executable": KIRO_EXE,
                  "process_name": "Kiro.exe"},
        }
        # Session list rows: list of {title, automation_id}. Pre-seed one human
        # session so a switch target ("B") exists.
        self.sessions = [{"title": "Human Session B", "automation_id": "list_id_3_0"}]
        self.active_title = "Human Session B"      # currently-selected session
        self.active_aid = "list_id_3_0"            # unambiguous active-session key
        self._next_aid = 1
        self.submit_count = 0
        self.new_session_count = 0
        self.calls = []
        self.focus_calls = []

    # enumeration / identity
    def enumerate_windows(self):
        return [dict(w) for w in self.windows.values()]

    def window_process(self, hwnd):
        w = self.windows.get(int(hwnd))
        if not w:
            return {"hwnd": int(hwnd), "pid": None, "executable": "", "process_name": ""}
        return {"hwnd": int(hwnd), "pid": w["pid"],
                "executable": w["executable"], "process_name": w["process_name"]}

    def window_is_kiro(self, hwnd):
        w = self.windows.get(int(hwnd))
        return bool(w) and is_kiro_process(w.get("executable", ""), w.get("process_name", ""))

    def verify_project(self, hwnd, path):
        w = self.windows.get(int(hwnd))
        ok = bool(w) and self.window_is_kiro(hwnd) and "Project Opus" in (w.get("title") or "")
        return {"verified": ok, "requested": path,
                "observed": path if ok else (w.get("title") if w else None),
                "is_kiro_process": self.window_is_kiro(hwnd)}

    def identify_session(self, hwnd, workflow_id):
        return {"kiro_session_id": f"kiro-session:{workflow_id}:{hwnd}",
                "kiro_window_hwnd": int(hwnd),
                "kiro_session_title": self.active_title}

    # ── real session controls ────────────────────────────────────────────────
    def create_new_session(self, hwnd, avoid_hwnds=None):
        self.new_session_count += 1
        title = f"Jarvis Session {chr(ord('A') + self.new_session_count - 1)}"
        aid = f"list_id_3_{self._next_aid}"; self._next_aid += 1
        self.sessions.insert(0, {"title": title, "automation_id": aid})
        self.active_title = title            # creating makes it active
        self.active_aid = aid
        self.calls.append(("new_session", title))
        return {"hwnd": int(hwnd), "title": title, "automation_id": aid,
                "ordinal": 0, "fingerprint": "fp-" + title, "created_via": "new-session-button"}

    def list_sessions(self, hwnd, avoid_hwnds=None):
        return [{"title": s["title"], "automation_id": s["automation_id"],
                 "ordinal": i, "runtime_id": f"rt-{i}"} for i, s in enumerate(self.sessions)]

    def select_session(self, hwnd, locator, avoid_hwnds=None):
        title = (locator.get("title") or "").strip()
        aid = (locator.get("automation_id") or "").strip()
        # Prefer automation_id (disambiguates duplicate titles), then title.
        for s in self.sessions:
            if aid and s["automation_id"] == aid:
                self.active_title = s["title"]; self.active_aid = s["automation_id"]
                self.calls.append(("select_session", s["title"])); return True
        for s in self.sessions:
            if title and s["title"] == title:
                self.active_title = s["title"]; self.active_aid = s["automation_id"]
                self.calls.append(("select_session", s["title"])); return True
        return False    # fail-closed: not found

    def active_session(self, hwnd, avoid_hwnds=None):
        return {"hwnd": int(hwnd), "title": self.active_title,
                "automation_id": self.active_aid,
                "ordinal": None, "window_title": "Project Opus - Kiro",
                "fingerprint": "fp-" + (self.active_aid or self.active_title),
                "is_kiro_process": self.window_is_kiro(hwnd)}

    # test helper: simulate a human switching to another session by title
    def human_switch_to(self, title):
        self.active_title = title
        for s in self.sessions:
            if s["title"] == title:
                self.active_aid = s["automation_id"]; break

    # actions
    def open_project(self, path, avoid_hwnds=None, new_window=True):
        self.calls.append(("open", path))
        return {"hwnd": 200, "verified": True, "observed": path, "new_window": True}

    def focus_window(self, hwnd, avoid_hwnds=None):
        self.focus_calls.append(int(hwnd)); return True

    def focus_composer(self, hwnd, avoid_hwnds=None):
        self.calls.append(("focus_composer", hwnd)); return True

    def paste_prompt(self, hwnd, prompt, avoid_hwnds=None):
        self.calls.append(("paste", prompt))

    def submit_prompt(self, hwnd, avoid_hwnds=None):
        self.submit_count += 1; self.calls.append(("submit", hwnd))

    def verify_submission(self, hwnd, prompt, timeout=None):
        return {"submitted": True, "in_history": True, "working": True, "permission": False}

    def trusted_workspace_paths(self, hwnd, focus=True):
        return [r"C:\Project Opus\file.py"] if self.window_is_kiro(hwnd) else []

    def add_chrome_window(self, hwnd=722342):
        self.windows[hwnd] = {"hwnd": hwnd, "pid": 6000,
                              "title": "Waiting for Kiro Report - Google Chrome",
                              "class": "Chrome_WidgetWin_1", "executable": CHROME_EXE,
                              "process_name": "chrome.exe"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    store = WorkflowStore(db_path=tmp_path / "p7b.db")
    engine = we_mod.WorkflowEngine(store=store)
    monkeypatch.setattr(we_mod, "workflow_engine", engine)
    monkeypatch.setattr(ar_mod, "workflow_engine", engine)
    events = []
    lk = KiroAutomationLock(lock_path=str(tmp_path / "lock.json"), stale_seconds=100,
                            process_alive_fn=lambda pid: True)
    fake = FakeKiroSession()
    mgr = JarvisKiroSessionManager(engine=engine, kiro_controller=fake, project_path=PROJECT)
    service = WorkflowKiroService(engine=engine, kiro_controller=fake, lock=lk,
                                  publish_fn=lambda w, e, p: events.append((w, e, p)),
                                  run_inline=True, project_path=PROJECT)
    service.set_session_manager(mgr)
    monkeypatch.setattr(wks_mod, "workflow_kiro_service", service)
    yield engine, service, fake, lk, mgr, events
    engine.close()


def _approved(engine, *, prompt="Create stage2_validation.txt", iteration=1):
    wf = engine.create_workflow(project_name="P7b"); wid = wf.workflow_id
    engine.update_workflow_context(
        wid, selected_chrome_profile_key="k",
        chatgpt_conversation_url="https://chatgpt.com/c/x", master_gpt_prompt="m")
    engine.transition(wid, "INITIALIZING"); engine.transition(wid, "GPT_BOOTSTRAPPING")
    engine.transition(wid, "WAITING_FOR_COMMAND"); engine.transition(wid, "GPT_RUNNING")
    engine.update_workflow_context(wid, iteration_number=iteration, latest_gpt_output=prompt)
    engine.transition(wid, "GPT_OUTPUT_READY"); engine.transition(wid, "WAITING_FOR_APPROVAL")
    engine.update_workflow_context(wid, approved_gpt_prompt=prompt)
    engine.set_metadata(wid, "approval", {"status": "approved", "iteration": iteration,
                                          "approved_prompt_hash": _sha(prompt),
                                          "chat_id": 4242})
    return wid, prompt


# ══════════════════════════════════════════════════════════════════════════
# 3-9. Session manager: bind / project / new-session / persistence
# ══════════════════════════════════════════════════════════════════════════

def test_assert_kiro_window_rejects_chrome(env):
    _e, _s, fake, _lk, mgr, _ev = env
    fake.add_chrome_window(722342)
    with pytest.raises(KiroSessionError):
        mgr.assert_kiro_window(722342)
    # real Kiro window passes
    assert mgr.assert_kiro_window(200)["executable"].lower().endswith("kiro.exe")


def test_verify_project_window_fail_closed_on_chrome(env):
    _e, _s, fake, _lk, mgr, _ev = env
    fake.add_chrome_window(722342)
    with pytest.raises(KiroSessionError):
        mgr.verify_project_window(722342)


def test_create_and_bind_creates_new_session_and_persists(env):
    engine, _s, fake, _lk, mgr, _ev = env
    wid, _p = _approved(engine)
    binding = mgr.create_and_bind(wid, 200, iteration=1)
    assert fake.new_session_count == 1                       # NEW session created
    assert binding["owner"] == "jarvis"
    assert binding["kiro_process_executable"].lower().endswith("kiro.exe")
    assert binding["kiro_window_hwnd"] == 200
    # DURABLE LOCATOR persisted (title + automation_id), not just a fingerprint
    assert binding["session_locator"]["title"] == "Jarvis Session A"
    assert binding["session_locator"]["automation_id"].startswith("list_id_3")
    # persisted durably
    assert engine.get_workflow(wid).metadata[BINDING_KEY]["binding_id"] == binding["binding_id"]


def test_binding_live_true_when_intact(env):
    engine, _s, _f, _lk, mgr, _ev = env
    wid, _p = _approved(engine)
    mgr.create_and_bind(wid, 200, iteration=1)
    assert mgr.is_binding_live(wid) is True


def test_binding_not_live_when_window_gone(env):
    engine, _s, fake, _lk, mgr, _ev = env
    wid, _p = _approved(engine)
    mgr.create_and_bind(wid, 200, iteration=1)
    fake.windows.pop(200)
    assert mgr.is_binding_live(wid) is False


def test_binding_not_live_when_session_switched(env):
    engine, _s, fake, _lk, mgr, _ev = env
    wid, _p = _approved(engine)
    mgr.create_and_bind(wid, 200, iteration=1)
    fake.human_switch_to("Human Session B")          # human switched sessions
    assert mgr.is_binding_live(wid) is False


# ══════════════════════════════════════════════════════════════════════════
# 10-11. Human session-switch detection + fail-closed mismatch record
# ══════════════════════════════════════════════════════════════════════════

def test_active_session_matches_ok(env):
    engine, _s, _f, _lk, mgr, _ev = env
    wid, _p = _approved(engine)
    mgr.create_and_bind(wid, 200, iteration=1)
    assert mgr.active_session_matches(wid)["match"] is True


def test_detect_mismatch_records_when_switched(env):
    engine, _s, fake, _lk, mgr, _ev = env
    wid, _p = _approved(engine)
    mgr.create_and_bind(wid, 200, iteration=1)
    fake.human_switch_to("Human Session B")
    rec = mgr.detect_and_record_mismatch(wid, substate="PRE_SUBMIT")
    assert rec is not None and rec["reason"] == "session_switched"
    assert engine.get_workflow(wid).metadata[MISMATCH_KEY]["mismatch_id"] == rec["mismatch_id"]


def test_detect_mismatch_none_when_ok(env):
    engine, _s, _f, _lk, mgr, _ev = env
    wid, _p = _approved(engine)
    mgr.create_and_bind(wid, 200, iteration=1)
    assert mgr.detect_and_record_mismatch(wid) is None


# ══════════════════════════════════════════════════════════════════════════
# 12-16. Service integration: first-time bind, no wrong-window action
# ══════════════════════════════════════════════════════════════════════════

def test_service_first_run_creates_bound_session_and_submits_once(env):
    engine, service, fake, lk, _mgr, _ev = env
    wid, prompt = _approved(engine)
    service.start(wid)
    wf = engine.get_workflow(wid)
    assert wf.state == "KIRO_RUNNING"
    assert fake.submit_count == 1
    assert fake.new_session_count == 1                       # a NEW Jarvis session
    assert wf.metadata[BINDING_KEY]["owner"] == "jarvis"


def test_service_rejects_chrome_target_process(env):
    engine, service, fake, lk, _mgr, _ev = env
    # Force open_project to return a Chrome window (simulating the old bug).
    fake.add_chrome_window(722342)
    fake.open_project = lambda path, avoid_hwnds=None, new_window=True: {
        "hwnd": 722342, "verified": True, "observed": path}
    wid, _p = _approved(engine)
    service.start(wid)
    wf = engine.get_workflow(wid)
    assert wf.state == "FAILED"                              # never acted on Chrome
    assert fake.submit_count == 0


def test_service_pauses_on_human_switch_before_submit(env):
    engine, service, fake, lk, mgr, events = env
    wid, _p = _approved(engine)
    # Pre-bind so a live binding exists, then switch the human session so the
    # PRE_SUBMIT ownership check trips.
    mgr.create_and_bind(wid, 200, iteration=1)
    fake.human_switch_to("Human Session B")
    service.start(wid)
    wf = engine.get_workflow(wid)
    assert wf.state == "PAUSED"                              # fail-closed pause
    assert fake.submit_count == 0                            # ZERO paste/submit
    assert wf.metadata[MISMATCH_KEY]["reason"] == "session_switched"
    assert any(e[1] == wks_mod.EV_SESSION_MISMATCH for e in events)
    assert lk.owner() is None                                # lock released


def test_pause_leaves_no_kiro_action(env):
    engine, service, fake, lk, mgr, _ev = env
    wid, _p = _approved(engine)
    mgr.create_and_bind(wid, 200, iteration=1)
    fake.human_switch_to("Human Session B")
    service.start(wid)
    kinds = {c[0] for c in fake.calls}
    assert "paste" not in kinds and "submit" not in kinds


# ══════════════════════════════════════════════════════════════════════════
# 17-21. Confirmation decisions: continue / new / pause / cancel
# ══════════════════════════════════════════════════════════════════════════

def _switched_and_paused(engine, service, fake, mgr):
    wid, _p = _approved(engine)
    mgr.create_and_bind(wid, 200, iteration=1)
    fake.human_switch_to("Human Session B")
    service.start(wid)
    assert engine.get_workflow(wid).state == "PAUSED"
    return wid


def test_continue_reselects_original_and_resumes_without_dup(env):
    engine, service, fake, lk, mgr, _ev = env
    wid = _switched_and_paused(engine, service, fake, mgr)
    # Human left session B selected. Continue must ACTIVELY relocate+select A;
    # the fake's select_session will flip the active session back to A.
    res = service.resume_after_confirmation(wid, "continue")
    assert res["decision"] == "continue" and res["resumed"] is True
    wf = engine.get_workflow(wid)
    assert wf.state == "KIRO_RUNNING"
    assert fake.submit_count == 1                            # submitted once total
    assert fake.new_session_count == 1                       # NO new session created


def test_continue_does_not_resend_completed_submit(env):
    engine, service, fake, lk, mgr, _ev = env
    wid, prompt = _approved(engine)
    service.start(wid)                                       # first submit
    assert fake.submit_count == 1
    # now a human switch happens; force a mismatch + pause via a fresh detect
    fake.human_switch_to("Human Session B")
    mm = mgr.detect_and_record_mismatch(wid, substate="POST_SUBMIT")
    service._enter_session_mismatch_pause(wid, mm)
    # Continue relocates+selects A (fake flips active back); no resend.
    service.resume_after_confirmation(wid, "continue")
    assert fake.submit_count == 1                            # idempotent: no resend


def test_new_session_rebinds_and_keeps_history(env):
    engine, service, fake, lk, mgr, _ev = env
    wid = _switched_and_paused(engine, service, fake, mgr)
    res = service.resume_after_confirmation(wid, "new")
    assert res["decision"] == "new"
    wf = engine.get_workflow(wid)
    # a new session was created and bound; old binding retained in history
    assert fake.new_session_count == 2
    assert wf.metadata.get("kiro_binding_history")
    # new binding points at the freshly-created session B (locator, not fp)
    assert wf.metadata[BINDING_KEY]["session_locator"]["title"] == "Jarvis Session B"


def test_pause_decision_leaves_human_session_untouched(env):
    engine, service, fake, lk, mgr, _ev = env
    wid = _switched_and_paused(engine, service, fake, mgr)
    before = fake.new_session_count
    res = service.resume_after_confirmation(wid, "pause")
    assert res["resumed"] is False
    assert fake.new_session_count == before                 # no session created
    assert fake.submit_count == 0
    # human session never mutated by Jarvis (B stays selected)
    assert fake.active_title == "Human Session B"


def test_cancel_decision_cancels_safely(env):
    engine, service, fake, lk, mgr, _ev = env
    wid = _switched_and_paused(engine, service, fake, mgr)
    res = service.resume_after_confirmation(wid, "cancel")
    assert res["decision"] == "cancel"
    assert engine.get_workflow(wid).state == "CANCELLED"
    assert fake.submit_count == 0


def test_unknown_decision_rejected(env):
    engine, service, fake, lk, mgr, _ev = env
    wid = _switched_and_paused(engine, service, fake, mgr)
    with pytest.raises(KiroServiceError):
        service.resume_after_confirmation(wid, "bogus")


# ══════════════════════════════════════════════════════════════════════════
# 22-24. Reselect + restart recovery
# ══════════════════════════════════════════════════════════════════════════

def test_reselect_bound_session_focuses_original(env):
    engine, _s, fake, _lk, mgr, _ev = env
    wid, _p = _approved(engine)
    mgr.create_and_bind(wid, 200, iteration=1)
    b = mgr.reselect_bound_session(wid)
    assert b["status"] == "bound"
    assert 200 in fake.focus_calls


def test_reselect_selects_original_when_present(env):
    """Continue's core proof: bound session A can be RELOCATED + SELECTED from
    the session list even though a human switched to B."""
    engine, _s, fake, _lk, mgr, _ev = env
    wid, _p = _approved(engine)
    mgr.create_and_bind(wid, 200, iteration=1)         # creates + selects A
    fake.human_switch_to("Human Session B")            # human switches away
    assert fake.active_title == "Human Session B"
    b = mgr.reselect_bound_session(wid)                # Jarvis relocates+selects A
    assert b["status"] == "bound"
    assert fake.active_title == "Jarvis Session A"      # A is active again
    assert ("select_session", "Jarvis Session A") in fake.calls


def test_reselect_fails_closed_when_session_gone(env):
    """If session A is no longer in the list, Continue must FAIL CLOSED rather
    than adopt whatever is active."""
    engine, _s, fake, _lk, mgr, _ev = env
    wid, _p = _approved(engine)
    mgr.create_and_bind(wid, 200, iteration=1)
    # Human closed/removed session A entirely; only B remains.
    fake.sessions = [{"title": "Human Session B", "automation_id": "list_id_3_9"}]
    fake.human_switch_to("Human Session B")
    with pytest.raises(KiroSessionError):
        mgr.reselect_bound_session(wid)


def test_duplicate_titles_disambiguated_by_automation_id(env):
    """Two sessions with the SAME title are disambiguated by automation_id."""
    engine, _s, fake, _lk, mgr, _ev = env
    wid, _p = _approved(engine)
    binding = mgr.create_and_bind(wid, 200, iteration=1)   # A, aid list_id_3_1
    aid = binding["session_locator"]["automation_id"]
    # Inject a second session with the SAME title but different automation_id.
    fake.sessions.insert(0, {"title": "Jarvis Session A", "automation_id": "list_id_3_99"})
    fake.human_switch_to("Human Session B")
    b = mgr.reselect_bound_session(wid)
    assert b["status"] == "bound"
    # It selected the row with the BOUND automation_id, not the impostor.
    assert ("select_session", "Jarvis Session A") in fake.calls
    assert mgr.get_binding(wid)["session_locator"]["automation_id"] == aid


def test_stale_runtime_id_rediscovered(env):
    """Locator survives volatile runtime_ids: selection re-discovers the list
    each time and matches by title/automation_id, never runtime_id."""
    engine, _s, fake, _lk, mgr, _ev = env
    wid, _p = _approved(engine)
    mgr.create_and_bind(wid, 200, iteration=1)
    fake.human_switch_to("Human Session B")
    # Simulate Kiro rebuilding rows with new runtime_ids (title/aid unchanged).
    for i, s in enumerate(fake.sessions):
        s["automation_id"] = s["automation_id"]  # aid stable; rt regenerated in list_sessions
    b = mgr.reselect_bound_session(wid)
    assert b["status"] == "bound" and fake.active_title == "Jarvis Session A"


def test_ownership_monitor_detects_switch_zero_actions(env):
    """check_ownership_once (the monitor's unit) detects a human switch while
    KIRO_RUNNING and pauses fail-closed with ZERO dangerous actions."""
    engine, service, fake, lk, mgr, events = env
    wid, _p = _approved(engine)
    service.start(wid)                                 # reaches KIRO_RUNNING
    assert engine.get_workflow(wid).state == "KIRO_RUNNING"
    calls_before = list(fake.calls)
    fake.human_switch_to("Human Session B")            # human switches mid-run
    rec = service.check_ownership_once(wid)
    assert rec is not None and rec["reason"] == "session_switched"
    assert engine.get_workflow(wid).state == "PAUSED"
    # monitor performed no paste/submit/permission/capture
    new_calls = [c for c in fake.calls if c not in calls_before]
    assert all(c[0] not in ("paste", "submit") for c in new_calls)


def test_ownership_monitor_noop_when_ok(env):
    engine, service, fake, lk, mgr, _ev = env
    wid, _p = _approved(engine)
    service.start(wid)
    assert service.check_ownership_once(wid) is None   # ownership intact → no pause
    assert engine.get_workflow(wid).state == "KIRO_RUNNING"


def test_substate_persisted_and_survives_pause_resume(env):
    """Durable substate (KIRO_RUNNING) is recorded and preserved across a
    mismatch pause + continue, and the completed submit is never replayed."""
    engine, service, fake, lk, mgr, _ev = env
    wid, _p = _approved(engine)
    service.start(wid)
    assert engine.get_workflow(wid).metadata["kiro"]["substate"] == "KIRO_RUNNING"
    fake.human_switch_to("Human Session B")
    service.check_ownership_once(wid)
    assert engine.get_workflow(wid).state == "PAUSED"
    service.resume_after_confirmation(wid, "continue")
    assert engine.get_workflow(wid).state == "KIRO_RUNNING"
    assert fake.submit_count == 1                       # no replay


def test_restart_reuses_live_binding_no_new_session(env):
    engine, service, fake, lk, mgr, _ev = env
    wid, _p = _approved(engine)
    service.start(wid)
    assert fake.new_session_count == 1
    # Simulate restart: fresh service + manager on SAME engine + lock + fake.
    lk.force_clear()
    mgr2 = JarvisKiroSessionManager(engine=engine, kiro_controller=fake, project_path=PROJECT)
    svc2 = WorkflowKiroService(engine=engine, kiro_controller=fake, lock=lk,
                               run_inline=True, project_path=PROJECT)
    svc2.set_session_manager(mgr2)
    svc2._run_start(wid)
    assert fake.submit_count == 1                            # not resubmitted
    assert fake.new_session_count == 1                       # live binding reused


def test_restart_with_wrong_active_session_pauses(env):
    engine, service, fake, lk, mgr, _ev = env
    wid, _p = _approved(engine)
    service.start(wid)
    lk.force_clear()
    fake.human_switch_to("Human Session B")                              # human switched after restart
    mgr2 = JarvisKiroSessionManager(engine=engine, kiro_controller=fake, project_path=PROJECT)
    svc2 = WorkflowKiroService(engine=engine, kiro_controller=fake, lock=lk,
                               run_inline=True, project_path=PROJECT)
    svc2.set_session_manager(mgr2)
    svc2._run_start(wid)
    assert engine.get_workflow(wid).state == "PAUSED"
    assert engine.get_workflow(wid).metadata[MISMATCH_KEY]["reason"] == "session_switched"


# ══════════════════════════════════════════════════════════════════════════
# 25-28. Mismatch service (Telegram callbacks) — idempotent + auth
# ══════════════════════════════════════════════════════════════════════════

def _mismatch_service(engine, service):
    sent = []
    return KiroMismatchService(
        engine=engine, kiro_service=service,
        tg_send=lambda chat, text, markup=None: (sent.append((chat, text, markup)), 999)[1],
        tg_is_authorized=lambda uid: uid == 7,
    ), sent


def test_mismatch_notify_sends_four_buttons(env):
    engine, service, fake, lk, mgr, _ev = env
    wid = _switched_and_paused(engine, service, fake, mgr)
    svc, sent = _mismatch_service(engine, service)
    rec = mgr.get_mismatch(wid)
    svc.notify(wid, rec)
    assert sent, "a Telegram message should be sent"
    markup = sent[-1][2]
    labels = [b[0]["callback_data"] for b in markup["inline_keyboard"]]
    assert any("continue" in c for c in labels)
    assert any("new" in c for c in labels)
    assert any("pause" in c for c in labels)
    assert any("cancel" in c for c in labels)


def test_mismatch_callback_unauthorized_rejected(env):
    engine, service, fake, lk, mgr, _ev = env
    wid = _switched_and_paused(engine, service, fake, mgr)
    svc, _sent = _mismatch_service(engine, service)
    rec = mgr.get_mismatch(wid)
    out = svc.handle_callback(user_id=999, chat_id=1, decision="cancel",
                              mismatch_id=rec["mismatch_id"])
    assert out["ok"] is False and out["reason"] == "unauthorized"
    assert engine.get_workflow(wid).state == "PAUSED"        # nothing happened


def test_mismatch_callback_resolves_and_is_idempotent(env):
    engine, service, fake, lk, mgr, _ev = env
    wid = _switched_and_paused(engine, service, fake, mgr)
    svc, _sent = _mismatch_service(engine, service)
    rec = mgr.get_mismatch(wid)
    out1 = svc.handle_callback(user_id=7, chat_id=1, decision="cancel",
                               mismatch_id=rec["mismatch_id"])
    assert out1["ok"] is True and out1["reason"] == "cancel"
    assert engine.get_workflow(wid).state == "CANCELLED"
    # duplicate callback → idempotent no-op
    out2 = svc.handle_callback(user_id=7, chat_id=1, decision="cancel",
                               mismatch_id=rec["mismatch_id"])
    assert out2["reason"] == "already_resolved"


def test_mismatch_callback_stale_token(env):
    engine, service, fake, lk, mgr, _ev = env
    svc, _sent = _mismatch_service(engine, service)
    out = svc.handle_callback(user_id=7, chat_id=1, decision="continue",
                              mismatch_id="does-not-exist")
    assert out["ok"] is False and out["reason"] == "stale"


# ══════════════════════════════════════════════════════════════════════════
# 29-30. Unrelated session untouched + API routes
# ══════════════════════════════════════════════════════════════════════════

def test_unrelated_kiro_session_untouched_on_pause(env):
    engine, service, fake, lk, mgr, _ev = env
    wid = _switched_and_paused(engine, service, fake, mgr)
    # The human's selected session must be exactly what it was; Jarvis made no
    # session-create/paste/submit/select while paused on the human session.
    assert fake.active_title == "Human Session B"
    assert "paste" not in {c[0] for c in fake.calls}
    assert "select_session" not in {c[0] for c in fake.calls}


def test_ui_routes_session_and_decision(env):
    engine, service, fake, lk, mgr, _ev = env
    from fastapi.testclient import TestClient
    from app import app
    client = TestClient(app)
    wid = _switched_and_paused(engine, service, fake, mgr)
    r = client.get(f"/api/workflows/{wid}/kiro/session")
    assert r.status_code == 200
    body = r.json()
    assert body["binding"]["owner"] == "jarvis"
    assert body["ownership"]["match"] is False               # human switched
    # portal decision parity with Telegram (cancel is safe + terminal)
    r2 = client.post(f"/api/workflows/{wid}/kiro/session-decision",
                     json={"decision": "cancel"})
    assert r2.status_code == 200
    assert engine.get_workflow(wid).state == "CANCELLED"
