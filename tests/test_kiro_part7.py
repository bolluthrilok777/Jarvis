"""Part 7 tests — Kiro controller + exact project session.

ALL desktop side effects are mocked. Three layers:
- KiroController with injected discover/launch/enumerate/window_strings/focus/
  keys/clipboard/click hooks — exact-project verification, dedicated-window
  selection, composer paste (exact whitespace), submit-once, positive submission
  verification, permission detection (observe-only).
- KiroAutomationLock — exclusivity, non-owner release, stale reclaim.
- WorkflowKiroService with run_inline=True + fake controller + real engine temp
  DB + temp lock — approval validation, transitions, idempotency/recovery,
  wrong-window protection, permission boundary, event order.
"""
from __future__ import annotations

import hashlib
import os
import pytest

import jarvis_core.workflow_engine as we_mod
import jarvis_core.architecture_routes as ar_mod
import jarvis_core.workflow_kiro_service as wks_mod
from jarvis_core.kiro_controller import (
    KiroController, normalize_path, KiroProjectMismatchError,
    KiroForegroundBlockedError, KiroSubmissionError, KiroError,
)
from jarvis_core.kiro_automation_lock import KiroAutomationLock
from jarvis_core.workflow_kiro_service import WorkflowKiroService, KiroServiceError
from jarvis_core.workflow_store import WorkflowStore


PROJECT = r"C:\Project Opus"
def _sha(t): return hashlib.sha256(t.encode("utf-8")).hexdigest()


# ── Controller fakes ─────────────────────────────────────────────────────────

def make_controller(*, windows=None, strings_by_hwnd=None, running=True,
                    fg=None, launch_adds=None):
    """windows: list of {hwnd,pid,title,class}. strings_by_hwnd: dict hwnd->list.
    launch_adds: window dict appended to enumerate() after launch (dedicated)."""
    state = {"windows": list(windows or []), "fg": fg, "keys": [], "clip": {"v": "USER"},
             "clicks": [], "launched": []}
    strings_by_hwnd = strings_by_hwnd or {}

    def discover_fn():
        return {"exe": r"C:\K\Kiro.exe", "cli": r"C:\K\bin\kiro.cmd",
                "pids": [111]} if running else {"exe": r"C:\K\Kiro.exe", "cli": None, "pids": []}

    def launch_fn(cli, path, new_window):
        state["launched"].append((cli, path, new_window))
        if launch_adds is not None:
            state["windows"].append(dict(launch_adds))

    def enumerate_fn():
        return [dict(w) for w in state["windows"]]

    def window_strings_fn(hwnd):
        return list(strings_by_hwnd.get(int(hwnd), []))

    def foreground_fn():
        return state["fg"]

    def focus_fn(hwnd):
        state["fg"] = int(hwnd)  # simulate successful focus
        return True

    def send_keys_fn(chord):
        state["keys"].append(chord)

    def clipboard_get_fn():
        return state["clip"]["v"]

    def clipboard_set_fn(t):
        state["clip"]["v"] = t

    def click_point_fn(x, y):
        state["clicks"].append((x, y))

    def window_rect_fn(hwnd):
        return {"x": 0, "y": 0, "width": 1000, "height": 900}

    c = KiroController(
        discover_fn=discover_fn, launch_fn=launch_fn, enumerate_fn=enumerate_fn,
        window_strings_fn=window_strings_fn, foreground_fn=foreground_fn,
        focus_fn=focus_fn, send_keys_fn=send_keys_fn,
        clipboard_get_fn=clipboard_get_fn, clipboard_set_fn=clipboard_set_fn,
        click_point_fn=click_point_fn, window_rect_fn=window_rect_fn)
    c._state = state
    return c


# 8. Kiro executable discovery
def test_discovery():
    c = make_controller()
    d = c.discover_kiro()
    assert d and d["exe"].lower().endswith("kiro.exe")


# 9. Kiro already running detected
def test_already_running_windows():
    c = make_controller(windows=[{"hwnd": 5, "pid": 1, "title": "x - Kiro", "class": "Chrome_WidgetWin_1"}])
    assert len(c.enumerate_windows()) == 1


# 11. exact Project Opus verification (path evidence)
def test_verify_project_path_evidence():
    c = make_controller(strings_by_hwnd={9: [r"C:\Project Opus"]})
    v = c.verify_project(9, PROJECT)
    assert v["verified"] is True


# 11b. verification via title folder token
def test_verify_project_title_token():
    c = make_controller(windows=[{"hwnd": 9, "pid": 1, "title": "app.py - Project Opus - Kiro", "class": "Chrome_WidgetWin_1"}],
                        strings_by_hwnd={9: []})
    v = c.verify_project(9, PROJECT)
    assert v["verified"] is True


# 12. project mismatch rejected
def test_verify_project_mismatch():
    c = make_controller(strings_by_hwnd={9: [r"C:\Other Project"]})
    v = c.verify_project(9, PROJECT)
    assert v["verified"] is False


# 18. path normalization
def test_path_normalization():
    assert normalize_path("C:/Project Opus/") == normalize_path(r"c:\PROJECT OPUS")


# 10. launch when closed → open_project launches + returns dedicated new window
def test_open_project_launches_dedicated():
    c = make_controller(windows=[{"hwnd": 100, "pid": 1, "title": "dev - Kiro", "class": "Chrome_WidgetWin_1"}],
                        strings_by_hwnd={200: [r"C:\Project Opus"]},
                        launch_adds={"hwnd": 200, "pid": 2, "title": "Project Opus - Kiro", "class": "Chrome_WidgetWin_1"})
    res = c.open_project(PROJECT, avoid_hwnds=[100], new_window=True)
    assert res["hwnd"] == 200 and res["verified"] is True
    assert c._state["launched"] and c._state["launched"][0][2] is True  # new_window


# 13. wrong window never receives prompt (avoid_hwnds refuses focus/type)
def test_avoid_window_refused():
    c = make_controller(windows=[{"hwnd": 100, "pid": 1, "title": "dev", "class": "x"}])
    with pytest.raises(KiroForegroundBlockedError):
        c.paste_prompt(100, "hi", avoid_hwnds=[100])


# 16/17/18. composer focus + exact paste (whitespace preserved) + submit once
def test_paste_exact_and_submit_once():
    c = make_controller(windows=[{"hwnd": 9, "pid": 2, "title": "Project Opus - Kiro", "class": "x"}],
                        fg=9)
    prompt = "Line1\n  keep  spaces\nLine3"
    c.focus_composer(9)
    c.paste_prompt(9, prompt)
    assert c._state["clip"]["v"] == "USER"  # restored after paste
    # the exact text was placed on the clipboard during paste
    c.submit_prompt(9)
    assert c._state["keys"].count("ctrl+v") == 1
    assert c._state["keys"].count("enter") == 1


def test_paste_records_exact_text():
    captured = {}
    c = make_controller(windows=[{"hwnd": 9, "pid": 2, "title": "t", "class": "x"}], fg=9)
    orig = c._clipboard_set_fn
    def spy(t):
        captured.setdefault("seen", []).append(t)
        orig(t)
    c._clipboard_set_fn = spy
    c.paste_prompt(9, "EXACT\t whitespace\n")
    assert "EXACT\t whitespace\n" in captured["seen"]


# 21. submission positively verified (in history)
def test_verify_submission_in_history():
    c = make_controller(windows=[{"hwnd": 9, "pid": 2, "title": "t", "class": "x"}], fg=9,
                        strings_by_hwnd={9: ["some ui", "part 7 kiro live test received please", "Stop"]})
    r = c.verify_submission(9, "part 7 kiro live test received", timeout=1.0)
    assert r["submitted"] is True


# 21b. submission not verified → error (Enter alone insufficient)
def test_verify_submission_fails_without_evidence():
    c = make_controller(windows=[{"hwnd": 9, "pid": 2, "title": "t", "class": "x"}], fg=9,
                        strings_by_hwnd={9: ["idle editor"]})
    with pytest.raises(KiroSubmissionError):
        c.verify_submission(9, "totally different prompt text here", timeout=0.2)


# 25. permission prompt detected (observe only)
def test_permission_detected():
    c = make_controller(strings_by_hwnd={9: ["Allow this command to run?"]})
    assert c.detect_permission_prompt(9) is True


# empty prompt rejected
def test_paste_empty_rejected():
    c = make_controller(windows=[{"hwnd": 9, "pid": 2, "title": "t", "class": "x"}], fg=9)
    with pytest.raises(KiroError):
        c.paste_prompt(9, "   ")


# ── Lock tests ───────────────────────────────────────────────────────────────

def _lock(tmp_path, alive=True):
    st = {"v": alive}
    lk = KiroAutomationLock(lock_path=str(tmp_path / "k.json"), stale_seconds=100,
                            process_alive_fn=lambda pid: st["v"])
    lk._alive_state = st
    return lk


# 4. exclusive lock acquired
def test_lock_acquire(tmp_path):
    lk = _lock(tmp_path)
    assert lk.acquire("A", pid=1) is True
    assert lk.owner() == "A"


# 5. second workflow cannot acquire active lock
def test_lock_exclusive(tmp_path):
    lk = _lock(tmp_path)
    lk.acquire("A", pid=1)
    assert lk.acquire("B", pid=2) is False


# 6. lock released
def test_lock_release(tmp_path):
    lk = _lock(tmp_path)
    lk.acquire("A", pid=1)
    assert lk.release("B") is False  # non-owner cannot release
    assert lk.release("A") is True
    assert lk.acquire("B", pid=2) is True


# 7. stale lock recovery (owner process gone)
def test_lock_stale_reclaim(tmp_path):
    lk = _lock(tmp_path, alive=True)
    lk.acquire("A", pid=999)
    lk._alive_state["v"] = False  # A's process died
    assert lk.is_stale() is True
    assert lk.acquire("B", pid=2) is True


# ── Service-level fake + fixture ─────────────────────────────────────────────

class FakeKiro:
    def __init__(self, *, verified=True, permission=False, open_hwnd=200,
                 mismatch=False):
        self.verified = verified; self.permission = permission
        self.open_hwnd = open_hwnd; self.mismatch = mismatch
        self.submit_count = 0; self.calls = []

    def open_project(self, path, avoid_hwnds=None, new_window=True):
        self.calls.append(("open", path, tuple(sorted(avoid_hwnds or []))))
        # Honour the avoid set like the real controller: the only window
        # (open_hwnd) being avoided means no dedicated non-protected window
        # can be found → mismatch error.
        if avoid_hwnds and int(self.open_hwnd) in {int(x) for x in avoid_hwnds}:
            raise KiroProjectMismatchError(
                f"No dedicated Kiro window for {path!r} (only window is protected)")
        if self.mismatch:
            # returns a window that will fail verify
            return {"hwnd": self.open_hwnd, "verified": False, "observed": r"C:\Other"}
        return {"hwnd": self.open_hwnd, "verified": True, "observed": path, "new_window": True}

    def verify_project(self, hwnd, path):
        ok = self.verified and not self.mismatch
        return {"verified": ok, "requested": path, "observed": path if ok else r"C:\Other"}

    def identify_session(self, hwnd, workflow_id):
        return {"kiro_session_id": f"kiro-session:{workflow_id}:{hwnd}",
                "kiro_window_hwnd": int(hwnd), "kiro_session_title": "Project Opus - Kiro"}

    def focus_composer(self, hwnd, avoid_hwnds=None):
        self.calls.append(("focus_composer", hwnd)); return True

    def paste_prompt(self, hwnd, prompt, avoid_hwnds=None):
        self.calls.append(("paste", prompt))

    def submit_prompt(self, hwnd, avoid_hwnds=None):
        self.submit_count += 1; self.calls.append(("submit", hwnd))

    def verify_submission(self, hwnd, prompt, timeout=None):
        self.calls.append(("verify_sub", hwnd))
        return {"submitted": True, "in_history": True, "working": not self.permission,
                "permission": self.permission}

    def focus_window(self, hwnd, avoid_hwnds=None):
        # Honour the avoid set: refuse to focus a protected window.
        if avoid_hwnds and int(hwnd) in {int(x) for x in avoid_hwnds}:
            return False
        return True

    def enumerate_windows(self):
        # Default: a single Project Opus window (handoff target = open_hwnd).
        # Tests may override self.windows to model multiple windows.
        if getattr(self, "windows", None) is not None:
            return [dict(w) for w in self.windows]
        return [{"hwnd": self.open_hwnd, "pid": 1,
                 "title": "Project Opus - Kiro", "class": "Chrome_WidgetWin_1"}]

    def trusted_workspace_paths(self, hwnd, focus=True):
        # Trusted IDE workspace/file paths per window. Tests set
        # self.trusted_by_hwnd = {hwnd: [paths...]}. Default: the open_hwnd
        # exposes a descendant file path under C:\Project Opus.
        tb = getattr(self, "trusted_by_hwnd", None)
        if tb is not None:
            return list(tb.get(int(hwnd), []))
        if int(hwnd) == int(self.open_hwnd):
            return [r"C:\Project Opus\jarvis_core\file.py"]
        return []

    # ── Kiro application + session ownership surface (Part 7b) ────────────────
    def window_is_kiro(self, hwnd):
        return int(hwnd) not in {int(x) for x in getattr(self, "non_kiro_hwnds", set())}

    def window_process(self, hwnd):
        exe = (r"C:\Users\me\AppData\Local\Programs\Kiro\Kiro.exe"
               if self.window_is_kiro(hwnd) else r"C:\Program Files\Google\Chrome\chrome.exe")
        return {"hwnd": int(hwnd), "pid": 111, "executable": exe,
                "process_name": os.path.basename(exe)}

    def create_new_session(self, hwnd, avoid_hwnds=None):
        self.calls.append(("new_session", hwnd))
        title = getattr(self, "session_title", "Jarvis Session A")
        self._active_title = title
        return {"hwnd": int(hwnd), "title": title, "automation_id": "list_id_3_0",
                "ordinal": 0, "fingerprint": "fp-A", "created_via": "new-session-button"}

    def active_session(self, hwnd, avoid_hwnds=None):
        title = getattr(self, "_active_title", getattr(self, "session_title", "Jarvis Session A"))
        return {"hwnd": int(hwnd), "title": title, "automation_id": "list_id_3_0",
                "ordinal": None, "window_title": "Project Opus - Kiro",
                "fingerprint": "fp-A", "is_kiro_process": self.window_is_kiro(hwnd)}

    def list_sessions(self, hwnd, avoid_hwnds=None):
        return [{"title": getattr(self, "_active_title", "Jarvis Session A"),
                 "automation_id": "list_id_3_0", "ordinal": 0, "runtime_id": ""}]

    def select_session(self, hwnd, locator, avoid_hwnds=None):
        self.calls.append(("select_session", locator.get("title")))
        self._active_title = locator.get("title")
        return True


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    store = WorkflowStore(db_path=tmp_path / "p7.db")
    engine = we_mod.WorkflowEngine(store=store)
    monkeypatch.setattr(we_mod, "workflow_engine", engine)
    monkeypatch.setattr(ar_mod, "workflow_engine", engine)
    events = []
    lk = KiroAutomationLock(lock_path=str(tmp_path / "lock.json"), stale_seconds=100,
                            process_alive_fn=lambda pid: True)
    fake = FakeKiro()
    service = WorkflowKiroService(engine=engine, kiro_controller=fake, lock=lk,
                                  publish_fn=lambda w, e, p: events.append((w, e, p)),
                                  run_inline=True)
    monkeypatch.setattr(wks_mod, "workflow_kiro_service", service)
    yield engine, service, fake, lk, events
    engine.close()


def _approved(engine, *, prompt="Inspect Project Opus and reply PART 7 OK", iteration=1):
    wf = engine.create_workflow(project_name="P7")
    wid = wf.workflow_id
    engine.update_workflow_context(
        wid, selected_chrome_profile_key="k", chatgpt_conversation_url="https://chatgpt.com/c/x",
        master_gpt_prompt="m")
    engine.transition(wid, "INITIALIZING"); engine.transition(wid, "GPT_BOOTSTRAPPING")
    engine.transition(wid, "WAITING_FOR_COMMAND"); engine.transition(wid, "GPT_RUNNING")
    engine.update_workflow_context(wid, iteration_number=iteration, latest_gpt_output=prompt)
    engine.transition(wid, "GPT_OUTPUT_READY")
    engine.transition(wid, "WAITING_FOR_APPROVAL")
    engine.update_workflow_context(wid, approved_gpt_prompt=prompt)
    engine.set_metadata(wid, "approval", {"status": "approved", "iteration": iteration,
                                          "approved_prompt_hash": _sha(prompt)})
    return wid, prompt


# 1. unapproved workflow rejected
def test_unapproved_rejected(svc):
    engine, service, fake, lk, _ = svc
    wf = engine.create_workflow(project_name="X"); wid = wf.workflow_id
    with pytest.raises(KiroServiceError):
        service.start(wid)


# 2. approved prompt missing rejected
def test_approved_prompt_missing(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved(engine)
    engine.update_workflow_context(wid, approved_gpt_prompt="")
    with pytest.raises(KiroServiceError):
        service.start(wid)


# 3. approved hash mismatch rejected
def test_hash_mismatch_rejected(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved(engine)
    engine.set_metadata(wid, "approval", {"status": "approved", "iteration": 1,
                                          "approved_prompt_hash": "deadbeef"})
    with pytest.raises(KiroServiceError):
        service.start(wid)


# 4/5. lock acquired; second workflow blocked while active
def test_lock_acquired_and_blocks_second(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved(engine)
    service.start(wid)  # inline: runs, reaches KIRO_RUNNING, keeps lock
    assert lk.owner() == wid
    # second workflow cannot acquire
    wid2, _ = _approved(engine)
    service.start(wid2)
    assert engine.get_workflow(wid2).state != "KIRO_RUNNING"  # blocked by lock


# 23/24. WAITING_FOR_APPROVAL → KIRO_STARTING → KIRO_RUNNING
def test_success_reaches_running(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved(engine)
    service.start(wid)
    assert engine.get_workflow(wid).state == "KIRO_RUNNING"
    assert fake.submit_count == 1


# 13/14. avoid the developer window (protected hwnds passed to controller)
def test_protected_windows_passed(svc):
    engine, service, fake, lk, _ = svc
    service.set_protected_windows([3802614])
    wid, _p = _approved(engine)
    service.start(wid)
    opens = [c for c in fake.calls if c[0] == "open"]
    assert opens and 3802614 in opens[0][2]


# 12. project mismatch → not RUNNING, fails
def test_project_mismatch_fails(svc):
    engine, service, fake, lk, _ = svc
    fake.mismatch = True
    wid, _p = _approved(engine)
    service.start(wid)
    wf = engine.get_workflow(wid)
    assert wf.state == "FAILED"
    assert fake.submit_count == 0  # never submitted into a mismatched project


# 17. exact prompt submitted (paste receives exact approved prompt)
def test_exact_prompt_submitted(svc):
    engine, service, fake, lk, _ = svc
    wid, prompt = _approved(engine, prompt="EXACT APPROVED  PROMPT\nline2")
    service.start(wid)
    pastes = [c[1] for c in fake.calls if c[0] == "paste"]
    assert pastes == [prompt]  # exact, once


# 19/20. submit once; duplicate action does not resubmit (recovery D)
def test_idempotent_no_resubmit(svc):
    engine, service, fake, lk, _ = svc
    wid, prompt = _approved(engine)
    service.start(wid)
    assert fake.submit_count == 1
    # simulate re-run (crash recovery): action already completed → no resubmit
    lk.force_clear()  # allow re-acquire
    service._run_start(wid)
    assert fake.submit_count == 1


# 22. submission failure not marked KIRO_RUNNING
def test_submission_failure_not_running(svc):
    engine, service, fake, lk, _ = svc
    def boom(hwnd, prompt, timeout=None):
        from jarvis_core.kiro_controller import KiroSubmissionError
        raise KiroSubmissionError("no evidence")
    fake.verify_submission = boom
    wid, _p = _approved(engine)
    service.start(wid)
    assert engine.get_workflow(wid).state == "FAILED"


# 25. permission detected but NOT auto-allowed (no permission decision made)
def test_permission_detected_not_decided(svc):
    engine, service, fake, lk, events = svc
    fake.permission = True
    wid, _p = _approved(engine)
    service.start(wid)
    wf = engine.get_workflow(wid)
    assert wf.state == "KIRO_RUNNING"  # Part 7 does NOT transition to WAITING_PERMISSION
    assert (wf.metadata or {})["kiro"]["status"] == "waiting_permission"
    assert any(e[1] == wks_mod.EV_WAITING_PERMISSION for e in events)


# 26/27. no monitoring / no result copy (controller has no such calls invoked)
def test_no_monitoring_or_result_copy(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved(engine)
    service.start(wid)
    # fake only ever saw open/verify/session/new_session/focus/paste/submit/
    # verify_sub — never monitoring or result copy.
    kinds = {c[0] for c in fake.calls}
    assert kinds <= {"open", "focus_composer", "paste", "submit", "verify_sub",
                     "new_session"}


# 28. realtime event order
def test_event_order(svc):
    engine, service, fake, lk, events = svc
    wid, _p = _approved(engine)
    service.start(wid)
    types = [e[1] for e in events if e[0] == wid]
    for a, b in [
        (wks_mod.EV_LOCK_ACQUIRED, wks_mod.EV_OPENING),
        (wks_mod.EV_OPENING, wks_mod.EV_WINDOW_FOUND),
        (wks_mod.EV_PROJECT_VERIFIED, wks_mod.EV_SESSION_READY),
        (wks_mod.EV_SESSION_READY, wks_mod.EV_PROMPT_SUBMITTING),
        (wks_mod.EV_PROMPT_SUBMITTED, wks_mod.EV_RUNNING),
    ]:
        assert types.index(a) < types.index(b), f"{a} before {b}"


# persisted identity
def test_persists_identity(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved(engine)
    service.start(wid)
    wf = engine.get_workflow(wid)
    assert wf.kiro_session_id and wf.kiro_project_path == PROJECT
    assert (wf.metadata or {})["kiro"]["window_hwnd"] == 200


# 6b. lock released on failure
def test_lock_released_on_failure(svc):
    engine, service, fake, lk, _ = svc
    fake.mismatch = True
    wid, _p = _approved(engine)
    service.start(wid)
    assert lk.owner() is None  # released after mismatch failure


# 30. restart recovery does not blindly resubmit (action completed persists)
def test_restart_recovery_no_resubmit(svc):
    engine, service, fake, lk, _ = svc
    wid, prompt = _approved(engine)
    service.start(wid)
    assert fake.submit_count == 1
    # fresh service instance (simulate restart), same engine + lock
    lk.force_clear()
    fake2 = FakeKiro()
    svc2 = WorkflowKiroService(engine=engine, kiro_controller=fake2, lock=lk, run_inline=True)
    svc2._run_start(wid)
    assert fake2.submit_count == 0  # completed action → no resubmit


# get_status surfaces Part 7 fields
def test_get_status(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved(engine)
    service.start(wid)
    st = service.get_status(wid)
    assert st["state"] == "KIRO_RUNNING"
    assert st["kiro_session_id"]
    assert st["kiro_window_hwnd"] == 200


# 29. worker not inline in production (uses queue)
def test_worker_used_in_prod():
    import inspect
    src = inspect.getsource(WorkflowKiroService.start)
    assert "self._queue.put" in src and "start_worker" in src


# ── Part 7 UI wiring: Start Kiro control ─────────────────────────────────────

from fastapi.testclient import TestClient as _TestClient
from app import app as _app

_ui_client = _TestClient(_app)


def _ui_html():
    r = _ui_client.get("/ui")
    assert r.status_code == 200
    return r.text


# 1. Start Kiro control exists and is gated on approved WAITING_FOR_APPROVAL
def test_ui_start_kiro_present_and_gated():
    h = _ui_html()
    assert "\u25b6 Start Kiro" in h or "Start Kiro" in h
    # enable condition present: WAITING_FOR_APPROVAL + approved/edited + prompt
    assert "canStartKiro" in h
    assert 'st==="WAITING_FOR_APPROVAL"' in h
    assert 'aprStatus==="approved"' in h and 'aprStatus==="edited"' in h
    assert "(w.approved_gpt_prompt||\"\").trim()" in h


# 2. disabled when not approved (button emits 'disabled' unless canStartKiro)
def test_ui_start_kiro_disabled_when_not_approved():
    h = _ui_html()
    # the Start Kiro button uses the canStartKiro flag to toggle 'disabled '
    assert "(canStartKiro?''" in h and "disabled " in h


# 3. clicking invokes the correct endpoint exactly once, via the backend API
def test_ui_start_kiro_invokes_endpoint():
    h = _ui_html()
    assert 'AW.kiroStart()' in h
    assert '"/api/workflows/"+encodeURIComponent(wfId)+"/kiro/start"' in h


# 4. NO frontend prompt-submission logic (delegates to backend service)
def test_ui_no_frontend_prompt_logic():
    import re
    h = _ui_html()
    # extract the kiroStart function body and ensure it only POSTs to /kiro/start
    m = re.search(r"async function kiroStart\(\)\{.*?\n  \}", h, re.S)
    assert m, "kiroStart function not found"
    body = m.group(0)
    assert "/kiro/start" in body
    # must not paste/submit/composer/clipboard in the browser
    for forbidden in ("paste", "composer", "clipboard", "submit_prompt", "ctrl+v"):
        assert forbidden not in body.lower()


# 5. Open/Focus + Verify remain disabled before a Kiro session exists
def test_ui_focus_verify_gated_on_window():
    h = _ui_html()
    assert "const canFocus = !!k.window_hwnd;" in h
    # both focus + verify buttons keyed off canFocus
    assert "(canFocus?''" in h


# 6. backend /kiro/start still guards on approval (service authoritative)
def test_backend_start_still_guards(svc):
    engine, service, fake, lk, _ = svc
    # unapproved workflow → start rejected by the service (not the UI)
    wf = engine.create_workflow(project_name="Guard"); wid = wf.workflow_id
    with pytest.raises(KiroServiceError):
        service.start(wid)


# ── Manual exclusive handoff (session-safety without inference) ───────────────

PROJECT_OPUS = r"C:\Project Opus"


def _approved_protected(engine, service, fake, *, hwnd=200,
                        prompt="Inspect Project Opus and reply PART 7 OK"):
    """Approved workflow whose ONLY Project Opus window (hwnd) is registered as a
    protected developer window — so automation requires a manual handoff."""
    fake.open_hwnd = hwnd
    wid, p = _approved(engine, prompt=prompt)
    service.set_protected_windows([hwnd])
    return wid, p


# 1. Start Kiro rejected without a manual handoff (protected window blocked)
def test_start_rejected_without_handoff(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved_protected(engine, service, fake, hwnd=200)
    service.start(wid)  # inline
    wf = engine.get_workflow(wid)
    # No handoff → the only window is protected → open_project avoids it and
    # (single-window fake) cannot find a non-protected verifying window →
    # NOT running, and NOTHING was submitted.
    assert wf.state != "KIRO_RUNNING"
    assert fake.submit_count == 0


# 8. valid handoff allows the existing (protected) Project Opus hwnd
def test_valid_handoff_allows_protected_hwnd(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved_protected(engine, service, fake, hwnd=200)
    rec = service.grant_handoff(wid, target_hwnd=200, project_path=PROJECT_OPUS)
    assert rec["confirmed"] is True and rec["target_hwnd"] == 200
    service.start(wid)
    wf = engine.get_workflow(wid)
    assert wf.state == "KIRO_RUNNING"
    assert fake.submit_count == 1
    # it acted on the protected hwnd 200 (paste/submit happened)
    assert ("submit", 200) in fake.calls


# 2. handoff must target an OPEN project window — a different hwnd is refused
#    at grant time (rejected before any state change), so nothing is submitted.
def test_handoff_wrong_hwnd_rejected(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved_protected(engine, service, fake, hwnd=200)
    # grant handoff for a DIFFERENT hwnd than the actual project window
    with pytest.raises(KiroServiceError):
        service.grant_handoff(wid, target_hwnd=999, project_path=PROJECT_OPUS)
    # no handoff persisted → start refuses the protected window → no submit
    service.start(wid)
    assert fake.submit_count == 0


# 3/5. handoff/grant must target exact Project Opus (mismatch rejected)
def test_handoff_project_mismatch_rejected(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved_protected(engine, service, fake, hwnd=200)
    with pytest.raises(KiroServiceError):
        service.grant_handoff(wid, target_hwnd=200, project_path=r"C:\Jarvis")


# 4. different hwnd rejected at start when project window verify fails
def test_handoff_window_verify_mismatch(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved_protected(engine, service, fake, hwnd=200)
    service.grant_handoff(wid, target_hwnd=200, project_path=PROJECT_OPUS)
    fake.mismatch = True  # window now fails project verification
    service.start(wid)
    wf = engine.get_workflow(wid)
    assert wf.state == "FAILED"
    assert fake.submit_count == 0


# 6. lock still required even with a handoff
def test_handoff_still_requires_lock(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved_protected(engine, service, fake, hwnd=200)
    service.grant_handoff(wid, target_hwnd=200, project_path=PROJECT_OPUS)
    # another owner holds the lock
    lk.acquire("someone-else", pid=1)
    service.start(wid)
    wf = engine.get_workflow(wid)
    # lock held → refused before submission (stays WAITING_FOR_APPROVAL, which
    # legally cannot go to FAILED); the safety guarantee is: nothing submitted.
    assert wf.state != "KIRO_RUNNING"
    assert fake.submit_count == 0


# 7. approved prompt hash still checked (handoff does not bypass it)
def test_handoff_prompt_hash_checked(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved_protected(engine, service, fake, hwnd=200)
    # corrupt the recorded approval hash so validation fails
    engine.set_metadata(wid, "approval", {"status": "approved", "iteration": 1,
                                          "approved_prompt_hash": "deadbeef"})
    with pytest.raises(KiroServiceError):
        service.grant_handoff(wid, target_hwnd=200, project_path=PROJECT_OPUS)


# 9. handoff consumed after successful submission
def test_handoff_consumed_after_success(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved_protected(engine, service, fake, hwnd=200)
    service.grant_handoff(wid, target_hwnd=200, project_path=PROJECT_OPUS)
    service.start(wid)
    ho = (engine.get_workflow(wid).metadata or {}).get("kiro_manual_handoff") or {}
    assert ho.get("consumed") is True and ho.get("confirmed") is False


# 10. retry cannot reuse a consumed handoff
def test_retry_cannot_reuse_consumed_handoff(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved_protected(engine, service, fake, hwnd=200)
    service.grant_handoff(wid, target_hwnd=200, project_path=PROJECT_OPUS)
    service.start(wid)
    assert fake.submit_count == 1
    # simulate a second attempt: clear lock + reset action, keep consumed handoff.
    # The workflow is now KIRO_RUNNING; _valid_handoff must refuse the consumed
    # grant, so re-running start does not act on the protected window again.
    lk.force_clear()
    fake.calls.clear()
    # a fresh run of the body must not paste/submit into the protected hwnd
    service._run_start(wid)
    assert ("submit", 200) not in fake.calls


# 11. failed pre-submit validation does NOT consume the handoff
def test_failed_presubmit_does_not_consume(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved_protected(engine, service, fake, hwnd=200)
    service.grant_handoff(wid, target_hwnd=200, project_path=PROJECT_OPUS)
    fake.mismatch = True  # pre-paste re-verify fails → raises before submit
    service.start(wid)
    ho = (engine.get_workflow(wid).metadata or {}).get("kiro_manual_handoff") or {}
    assert ho.get("consumed") in (False, None)   # still available for retry
    assert ho.get("confirmed") is True
    assert fake.submit_count == 0


# ── Handoff target resolution (trusted evidence + path containment) ──────────

from jarvis_core.kiro_controller import path_within as _path_within


# 1. exact project ROOT evidence accepted
def test_resolve_exact_root_accepted():
    assert _path_within(r"C:\Project Opus", r"C:\Project Opus") is True


# 2. trusted DESCENDANT file path accepted
def test_resolve_descendant_accepted():
    assert _path_within(r"C:\Project Opus\jarvis_core\file.py", r"C:\Project Opus") is True


# 3. C:\Project Opus2 rejected (not naive startswith)
def test_resolve_opus2_rejected():
    assert _path_within(r"C:\Project Opus2\x.py", r"C:\Project Opus") is False


# 4. unrelated project rejected
def test_resolve_unrelated_rejected():
    assert _path_within(r"C:\Jarvis\a.py", r"C:\Project Opus") is False


# 5. chat text containing the path is IGNORED (only trusted paths counted)
def test_resolve_ignores_chat_text(svc):
    engine, service, fake, lk, _ = svc
    fake.open_hwnd = 200
    fake.windows = [{"hwnd": 200, "pid": 1, "title": "Agent Focus", "class": "Chrome_WidgetWin_1"}]
    # trusted evidence is EMPTY (chat text is not returned by trusted_workspace_paths)
    fake.trusted_by_hwnd = {200: []}
    # verify_project also returns non-verifying (no trusted path)
    fake.mismatch = True
    assert service._project_window_candidates() == []


# 6. zero candidates → HANDOFF_NO_TARGET_WINDOW
def test_grant_zero_candidates(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved(engine)
    fake.windows = []  # no Kiro windows at all
    with pytest.raises(KiroServiceError) as ei:
        service.grant_handoff(wid, project_path=PROJECT_OPUS)
    assert "HANDOFF_NO_TARGET_WINDOW" in str(ei.value)


# 7. multiple matching windows → HANDOFF_AMBIGUOUS_TARGET
def test_grant_ambiguous(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved(engine)
    fake.windows = [
        {"hwnd": 200, "pid": 1, "title": "Agent Focus", "class": "Chrome_WidgetWin_1"},
        {"hwnd": 300, "pid": 1, "title": "Project Opus - Kiro", "class": "Chrome_WidgetWin_1"},
    ]
    fake.trusted_by_hwnd = {200: [r"C:\Project Opus\a.py"],
                            300: [r"C:\Project Opus\jarvis_core\b.py"]}
    with pytest.raises(KiroServiceError) as ei:
        service.grant_handoff(wid, project_path=PROJECT_OPUS)
    assert "HANDOFF_AMBIGUOUS_TARGET" in str(ei.value)


# 8. exactly one valid window → handoff confirmed
def test_grant_single_confirmed(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved(engine)
    fake.open_hwnd = 200
    fake.windows = [{"hwnd": 200, "pid": 1, "title": "Agent Focus", "class": "Chrome_WidgetWin_1"}]
    fake.trusted_by_hwnd = {200: [r"C:\Project Opus\jarvis_core\file.py"]}
    rec = service.grant_handoff(wid, project_path=PROJECT_OPUS)
    assert rec["confirmed"] is True


# 9. persisted handoff stores the RESOLVED current hwnd
def test_grant_persists_resolved_hwnd(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved(engine)
    fake.open_hwnd = 200
    fake.windows = [{"hwnd": 200, "pid": 1, "title": "Agent Focus", "class": "Chrome_WidgetWin_1"}]
    fake.trusted_by_hwnd = {200: [r"C:\Project Opus\jarvis_core\file.py"]}
    service.grant_handoff(wid, project_path=PROJECT_OPUS)
    ho = (engine.get_workflow(wid).metadata or {}).get("kiro_manual_handoff") or {}
    assert ho["target_hwnd"] == 200 and ho["project_path"].lower().endswith("project opus")


# explicit target that is NOT a project window is rejected
def test_grant_explicit_nonproject_rejected(svc):
    engine, service, fake, lk, _ = svc
    wid, _p = _approved(engine)
    fake.open_hwnd = 200
    fake.windows = [{"hwnd": 200, "pid": 1, "title": "Agent Focus", "class": "Chrome_WidgetWin_1"}]
    fake.trusted_by_hwnd = {200: [r"C:\Project Opus\a.py"]}
    with pytest.raises(KiroServiceError):
        service.grant_handoff(wid, target_hwnd=999, project_path=PROJECT_OPUS)  # 999 not a project window
