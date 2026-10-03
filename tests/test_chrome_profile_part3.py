"""Part 3 tests — dedicated Chrome Profile Controller + APIs.

All OS interaction is injected via the controller's ``*_fn`` hooks, so NO real
Chrome process is launched and no real window is focused. API tests repoint the
workflow_engine singleton at a temp DB and inject a fake controller.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import jarvis_core.workflow_engine as we_mod
import jarvis_core.architecture_routes as ar_mod
import jarvis_core.chrome_profile_controller as cpc_mod
from jarvis_core.chrome_profile_controller import (
    ChromeProfileController,
    ChromeExecutableNotFound,
    ProfileNotFound,
    ProfileVerificationError,
    profile_key_for,
    _extract_profile_directory,
)
from jarvis_core.workflow_store import WorkflowStore
from app import app


# ── Fake OS fixtures ─────────────────────────────────────────────────────────

FAKE_METADATA = {
    "profiles": [
        {"profile_dir": "Default", "name": "Codetrust", "gaia_name": "Codetrust",
         "user_name": "codetrust2026@gmail.com"},
        {"profile_dir": "Profile 4", "name": "Bhargav", "gaia_name": "Bhargav",
         "user_name": "bhargav@gmail.com"},
        {"profile_dir": "Profile 7", "name": "Thrilok", "gaia_name": "Thrilok",
         "user_name": "thrilok@gmail.com"},
        {"profile_dir": "Profile 9", "name": "xyz", "gaia_name": "", "user_name": ""},
    ],
    "sources": ["C:/fake/User Data"],
}


def make_controller(*, open_windows=None, pid_profile=None, launch_record=None,
                    focus_result=True, exe="C:/fake/chrome.exe", on_disk=None,
                    window_identity=None):
    """Build a controller with fully-injected OS behaviour."""
    open_windows = open_windows if open_windows is not None else []
    pid_profile = pid_profile if pid_profile is not None else {}
    window_identity = window_identity if window_identity is not None else {}

    def metadata_fn():
        return FAKE_METADATA

    def host_windows_fn():
        return list(open_windows)

    def process_profile_fn():
        return dict(pid_profile)

    def focus_fn(hwnd):
        return focus_result

    def launch_fn(exe_path, profile_dir):
        if launch_record is not None:
            launch_record.append({"exe": exe_path, "profile_dir": profile_dir})
        # Simulate the launched window appearing, owned by profile_dir.
        pid = 9000 + len(open_windows)
        open_windows.append({"hwnd": 5000 + len(open_windows), "title": "New Tab", "pid": pid})
        pid_profile[pid] = profile_dir
        return pid

    def exe_resolver_fn():
        if exe is None:
            raise ChromeExecutableNotFound("no chrome")
        return exe

    def window_identity_fn(hwnd):
        # Map hwnd -> profile_dir via injected UIA-evidence dict (None = unproven).
        return window_identity.get(int(hwnd))

    c = ChromeProfileController(
        user_data_dir="C:/fake/User Data",
        metadata_fn=metadata_fn,
        host_windows_fn=host_windows_fn,
        focus_fn=focus_fn,
        launch_fn=launch_fn,
        process_profile_fn=process_profile_fn,
        exe_resolver_fn=exe_resolver_fn,
        window_identity_fn=window_identity_fn,
    )
    # Neutralise on-disk scan + Preferences reads (pure-metadata tests).
    c._on_disk_profile_dirs = lambda: (on_disk or [])
    c._email_from_preferences = lambda pdir: ""
    return c


# ── Controller tests ─────────────────────────────────────────────────────────

# 1. discover real-shaped profile metadata
def test_discover_profile_shape():
    c = make_controller()
    profs = c.discover_profiles()
    assert len(profs) == 4
    p = profs[0]
    for key in ("profile_key", "profile_directory", "profile_name", "account_email",
                "display_name", "currently_open", "window_count", "verified_identity",
                "identity_source"):
        assert key in p


# 2. stable key based on profile directory
def test_stable_key_from_directory():
    assert profile_key_for("Profile 7") == "chrome-profile:Profile 7"
    c = make_controller()
    p7 = c.get_profile("chrome-profile:Profile 7")
    assert p7 is not None and p7["profile_directory"] == "Profile 7"


# 3. no fabricated account email
def test_no_fabricated_email():
    c = make_controller()
    xyz = c.get_profile("chrome-profile:Profile 9")
    assert xyz["account_email"] is None
    assert xyz["verified_identity"] is False
    # display falls back to profile name, never a guessed gmail
    assert "@" not in (xyz["display_name"] or "")


# 4. multiple profiles remain distinct
def test_profiles_distinct():
    c = make_controller()
    keys = [p["profile_key"] for p in c.discover_profiles()]
    assert len(keys) == len(set(keys)) == 4


# 8. Test Open already-open profile focuses existing window (no launch)
def test_test_open_focuses_existing_window():
    launches = []
    win = {"hwnd": 111, "title": "Gmail", "pid": 700}
    c = make_controller(open_windows=[win], pid_profile={700: "Profile 7"},
                        launch_record=launches)
    res = c.open_or_focus("chrome-profile:Profile 7")
    assert res["action"] == "focus"
    assert res["hwnd"] == 111
    assert res["verified"] is True
    assert launches == []  # 10. no duplicate launch


# 9. Test Open closed profile launches correct profile directory
def test_test_open_launches_closed_profile():
    launches = []
    c = make_controller(open_windows=[], pid_profile={}, launch_record=launches)
    res = c.open_or_focus("chrome-profile:Profile 4")
    assert res["action"] == "launch"
    assert res["verified"] is True
    assert len(launches) == 1
    assert launches[0]["profile_dir"] == "Profile 4"  # correct --profile-directory


# 11 & 12. verification succeeds only for requested profile / mismatch fails
def test_verification_match_and_mismatch():
    win = {"hwnd": 222, "title": "x", "pid": 800}
    c = make_controller(open_windows=[win], pid_profile={800: "Profile 7"})
    assert c.verify_profile_open("chrome-profile:Profile 7", 222)["verified"] is True
    assert c.verify_profile_open("chrome-profile:Profile 4", 222)["verified"] is False
    assert c.verify_profile_open("chrome-profile:Profile 4", 222)["observed"] == "chrome-profile:Profile 7"


# 13. PID-only match is insufficient — a window with no --profile-directory arg
# is NOT accepted as proof of a specific profile.
def test_pid_only_insufficient():
    # Window pid 900 has NO entry in pid_profile (no --profile-directory).
    win = {"hwnd": 333, "title": "x", "pid": 900}
    c = make_controller(open_windows=[win], pid_profile={})
    assert c.observed_profile_for_window(333) is None
    # It is attributed to inferred Default, and focus must refuse to claim it.
    with pytest.raises(ProfileVerificationError):
        c.focus_profile("chrome-profile:Default")


# 14. no cross-profile HWND leakage
def test_no_cross_profile_leakage():
    wins = [
        {"hwnd": 401, "title": "a", "pid": 41},
        {"hwnd": 407, "title": "b", "pid": 47},
    ]
    c = make_controller(open_windows=wins, pid_profile={41: "Profile 4", 47: "Profile 7"})
    p4 = [w["hwnd"] for w in c.get_profile_windows("chrome-profile:Profile 4")]
    p7 = [w["hwnd"] for w in c.get_profile_windows("chrome-profile:Profile 7")]
    assert p4 == [401]
    assert p7 == [407]
    assert set(p4).isdisjoint(p7)


def _block_winreg(monkeypatch):
    """Force the winreg App Paths lookup to miss so resolution falls through
    to the environment-variable install locations deterministically."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "winreg":
            raise ImportError("no winreg (test)")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)


# 15. Chrome executable resolution
def test_exe_resolution(monkeypatch, tmp_path):
    _block_winreg(monkeypatch)
    exe = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    resolved = ChromeProfileController.resolve_chrome_executable()
    assert resolved.endswith("chrome.exe")
    assert str(tmp_path) in resolved


def test_exe_resolution_missing(monkeypatch):
    monkeypatch.delenv("PROGRAMFILES", raising=False)
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    # Also force winreg to raise (simulate no registry entry).
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "winreg":
            raise ImportError("no winreg")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ChromeExecutableNotFound):
        ChromeProfileController.resolve_chrome_executable()


# 16. launch timeout handled (window never appears)
def test_launch_timeout(monkeypatch):
    c = make_controller(open_windows=[], pid_profile={})
    # Launch that does NOT register a window → verification can't prove it.
    c._launch_fn = lambda exe, pdir: 1234  # returns pid, no window appears
    monkeypatch.setattr(cpc_mod, "LAUNCH_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(cpc_mod, "LAUNCH_POLL_INTERVAL", 0.05)
    with pytest.raises(ProfileVerificationError):
        c.launch_profile("chrome-profile:Profile 4")


# 17. focus failure handled honestly (still verifies ownership, flags failure)
def test_focus_failure_honest():
    win = {"hwnd": 555, "title": "x", "pid": 55}
    c = make_controller(open_windows=[win], pid_profile={55: "Profile 7"},
                        focus_result=False)
    res = c.focus_profile("chrome-profile:Profile 7")
    assert res["focused"] is False
    assert res["focus_failed"] is True
    assert res["verified"] is True  # ownership still proven, just not foregrounded


# 21. invalid profile rejected
def test_invalid_profile_rejected():
    c = make_controller()
    with pytest.raises(ProfileNotFound):
        c.open_or_focus("chrome-profile:Does Not Exist")


# 22. missing/deleted profile handled (in metadata but dir gone → still keyed)
def test_missing_profile_handled():
    c = make_controller()
    assert c.get_profile("chrome-profile:Profile 999") is None


# 23. account email may be unknown safely
def test_unknown_email_safe():
    c = make_controller()
    xyz = c.get_profile("chrome-profile:Profile 9")
    assert xyz["account_email"] is None
    assert xyz["display_name"]  # some safe label, not empty


# Real-world multiplex case: one browser PID hosts windows from several
# profiles (no --profile-directory on the PID). Per-window UIA evidence proves
# ownership; focus/verify succeed for the correct window.
def test_uia_window_identity_multiplexed():
    wins = [
        {"hwnd": 600, "title": "about:blank", "pid": 17452},  # master PID, no arg
        {"hwnd": 601, "title": "about:blank", "pid": 17452},
    ]
    # PID map is EMPTY (matches reality: master process has no profile arg).
    c = make_controller(
        open_windows=wins, pid_profile={},
        window_identity={600: "Profile 7", 601: "Profile 4"},
    )
    # Each window is proven to its own profile (no cross leakage).
    assert c.observed_profile_for_window(600) == "Profile 7"
    assert c.observed_profile_for_window(601) == "Profile 4"
    assert c.verify_profile_open("chrome-profile:Profile 7", 600)["verified"] is True
    assert c.verify_profile_open("chrome-profile:Profile 4", 600)["verified"] is False
    # Focus uses the strongly-proven window (not inferred) and verifies.
    res7 = c.focus_profile("chrome-profile:Profile 7")
    assert res7["verified"] is True and res7["hwnd"] == 600
    res4 = c.focus_profile("chrome-profile:Profile 4")
    assert res4["verified"] is True and res4["hwnd"] == 601


# Extra: cmdline parser handles = and space forms
def test_extract_profile_directory():
    assert _extract_profile_directory(["chrome", "--profile-directory=Profile 7"]) == "Profile 7"
    assert _extract_profile_directory(["chrome", "--profile-directory", "Profile 4"]) == "Profile 4"
    assert _extract_profile_directory(["chrome", "--other"]) is None


# ── API tests (repoint engine + inject fake controller) ──────────────────────

@pytest.fixture()
def api(tmp_path, monkeypatch):
    store = WorkflowStore(db_path=tmp_path / "chrome_api.db")
    engine = we_mod.WorkflowEngine(store=store)
    monkeypatch.setattr(we_mod, "workflow_engine", engine)
    monkeypatch.setattr(ar_mod, "workflow_engine", engine)

    # Inject a fake controller singleton used by the routes' lazy imports.
    win = {"hwnd": 111, "title": "Gmail", "pid": 700}
    controller = make_controller(open_windows=[win], pid_profile={700: "Profile 7"})
    monkeypatch.setattr(cpc_mod, "chrome_profile_controller", controller)

    client = TestClient(app)
    try:
        yield client, engine, controller
    finally:
        engine.close()


def _new_wf(client):
    return client.post("/api/workflows", json={"project_name": "Chrome"}).json()["workflow"]["workflow_id"]


# 5. profile selection persists to workflow
def test_api_select_persists(api):
    client, engine, _ = api
    wid = _new_wf(client)
    r = client.post(f"/api/workflows/{wid}/chrome-profile",
                    json={"profile_key": "chrome-profile:Profile 7"})
    assert r.status_code == 200
    wf = engine.get_workflow(wid)
    assert wf.selected_chrome_profile_key == "chrome-profile:Profile 7"
    assert wf.selected_chrome_account_email == "thrilok@gmail.com"
    # Workflow state must NOT advance (still IDLE) — this is configuration.
    assert wf.state == "IDLE"


# 6. selection survives engine/service restart
def test_api_select_survives_restart(api, tmp_path):
    client, engine, _ = api
    wid = _new_wf(client)
    client.post(f"/api/workflows/{wid}/chrome-profile",
                json={"profile_key": "chrome-profile:Profile 4"})
    from jarvis_core.workflow_engine import WorkflowEngine
    eng2 = WorkflowEngine(store=WorkflowStore(db_path=engine._store.db_path))
    try:
        wf = eng2.get_workflow(wid)
        assert wf.selected_chrome_profile_key == "chrome-profile:Profile 4"
        assert wf.selected_chrome_account_email == "bhargav@gmail.com"
    finally:
        eng2.close()


# 7. workflow A/B selections isolated
def test_api_ab_isolation(api):
    client, engine, _ = api
    a = _new_wf(client)
    b = _new_wf(client)
    client.post(f"/api/workflows/{a}/chrome-profile", json={"profile_key": "chrome-profile:Default"})
    client.post(f"/api/workflows/{b}/chrome-profile", json={"profile_key": "chrome-profile:Profile 7"})
    assert engine.get_workflow(a).selected_chrome_profile_key == "chrome-profile:Default"
    assert engine.get_workflow(b).selected_chrome_profile_key == "chrome-profile:Profile 7"


# 18. duplicate Test Open action prevented
def test_api_duplicate_test_open_prevented(api):
    client, engine, _ = api
    wid = _new_wf(client)
    client.post(f"/api/workflows/{wid}/chrome-profile", json={"profile_key": "chrome-profile:Profile 7"})
    body = {"profile_key": "chrome-profile:Profile 7", "request_id": "req-1"}
    r1 = client.post(f"/api/workflows/{wid}/chrome-profile/test-open", json=body)
    r2 = client.post(f"/api/workflows/{wid}/chrome-profile/test-open", json=body)
    assert r1.status_code == 200 and r1.json()["result"]["verified"] is True
    assert r2.status_code == 200 and r2.json().get("duplicate") is True


# 19. events emitted correctly
def test_api_events_emitted(api):
    client, engine, _ = api
    wid = _new_wf(client)
    client.post(f"/api/workflows/{wid}/chrome-profile", json={"profile_key": "chrome-profile:Profile 7"})
    with client.websocket_connect(f"/ws/workflows/{wid}") as ws:
        ws.receive_json()  # snapshot
        client.post(f"/api/workflows/{wid}/chrome-profile/test-open",
                    json={"profile_key": "chrome-profile:Profile 7", "request_id": "ev-1"})
        seen = set()
        for _ in range(8):
            m = ws.receive_json()
            if m.get("type", "").startswith("chrome_profile"):
                seen.add(m["type"])
            if {"chrome_profile_opening", "chrome_profile_verified"} <= seen:
                break
        assert "chrome_profile_opening" in seen
        assert "chrome_profile_verified" in seen


# 20. status endpoint accurate
def test_api_status_accurate(api):
    client, engine, _ = api
    wid = _new_wf(client)
    # No selection yet
    s0 = client.get(f"/api/workflows/{wid}/chrome-profile/status").json()
    assert s0["selected_profile_key"] is None
    # Select Profile 7 (which is "open" in the fake) and check status
    client.post(f"/api/workflows/{wid}/chrome-profile", json={"profile_key": "chrome-profile:Profile 7"})
    s1 = client.get(f"/api/workflows/{wid}/chrome-profile/status").json()
    assert s1["selected_profile_key"] == "chrome-profile:Profile 7"
    assert s1["currently_open"] is True
    assert s1["window_count"] == 1
    # After a verified test-open, verified flag is true
    client.post(f"/api/workflows/{wid}/chrome-profile/test-open",
                json={"profile_key": "chrome-profile:Profile 7", "request_id": "st-1"})
    s2 = client.get(f"/api/workflows/{wid}/chrome-profile/status").json()
    assert s2["verified"] is True


# API: invalid profile rejected at the route layer
def test_api_invalid_profile_rejected(api):
    client, engine, _ = api
    wid = _new_wf(client)
    r = client.post(f"/api/workflows/{wid}/chrome-profile", json={"profile_key": "chrome-profile:Nope"})
    assert r.status_code == 400


# API: test-open verification failure surfaces as 409 + fail_action
def test_api_test_open_verification_failure(api, monkeypatch):
    client, engine, controller = api
    wid = _new_wf(client)
    client.post(f"/api/workflows/{wid}/chrome-profile", json={"profile_key": "chrome-profile:Profile 7"})
    # Make open_or_focus raise a verification error.
    def boom(key):
        raise ProfileVerificationError("observed different profile")
    controller.open_or_focus = boom
    r = client.post(f"/api/workflows/{wid}/chrome-profile/test-open",
                    json={"profile_key": "chrome-profile:Profile 7", "request_id": "fail-1"})
    assert r.status_code == 409
    # The action must be recorded FAILED.
    acts = [a for a in engine.list_actions(wid) if a.action_type == "chrome_test_open"]
    assert acts and acts[-1].status == "FAILED"


# GET /api/chrome/profiles works with the injected controller
def test_api_list_profiles(api):
    client, engine, _ = api
    r = client.get("/api/chrome/profiles")
    assert r.status_code == 200
    keys = [p["profile_key"] for p in r.json()["profiles"]]
    assert "chrome-profile:Profile 7" in keys


# ── Selected Chrome profile is ABSOLUTE (Part A hardening) ───────────────────

def _win(hwnd, pid, title="x"):
    return {"hwnd": hwnd, "pid": pid, "title": title}


# 1. selected Default + Default observed = PASS (dir + email both match)
def test_absolute_default_matches_pass():
    c = make_controller(open_windows=[_win(100, 500)],
                        window_identity={100: "Default"})
    r = c.open_or_focus("chrome-profile:Default",
                        expected_email="codetrust2026@gmail.com")
    assert r["verified"] is True and r["hwnd"] == 100
    assert r["observed"] == "chrome-profile:Default"


# 2. selected Default + only a Profile 37 window observed = FAIL (no fallback)
def test_absolute_default_but_only_other_profile_open_launches_exact():
    launches = []
    # Only a Profile 37 window is open; selecting Default must NOT use it.
    c = make_controller(open_windows=[_win(200, 600)],
                        window_identity={200: "Profile 37"},
                        launch_record=launches)
    # Profile 37 isn't a metadata profile here, so selecting Default launches
    # the EXACT Default directory instead of ever touching window 200.
    r = c.open_or_focus("chrome-profile:Default",
                        expected_email="codetrust2026@gmail.com")
    assert r["action"] == "launch"
    assert launches and launches[0]["profile_dir"] == "Default"  # exact dir
    assert r["hwnd"] != 200  # never the other profile's window


# 3. selected Profile 7 + Default observed = FAIL → launches exact Profile 7
def test_absolute_profile7_not_default():
    launches = []
    c = make_controller(open_windows=[_win(100, 500)],
                        window_identity={100: "Default"},
                        launch_record=launches)
    r = c.open_or_focus("chrome-profile:Profile 7",
                        expected_email="thrilok@gmail.com")
    assert launches and launches[0]["profile_dir"] == "Profile 7"
    assert r["verified"] is True and r["observed"] == "chrome-profile:Profile 7"


# 4/5. selected profile not open → exact profile launched + verified before use
def test_absolute_launch_when_closed_and_verified():
    launches = []
    c = make_controller(open_windows=[], window_identity={},
                        launch_record=launches)
    r = c.open_or_focus("chrome-profile:Profile 7",
                        expected_email="thrilok@gmail.com")
    assert r["action"] == "launch"
    assert launches[0]["profile_dir"] == "Profile 7"
    assert r["verified"] is True


# 6. one Chrome PID owning multiple profiles → per-window identity decides
def test_absolute_same_pid_multiple_profiles():
    # Two windows, SAME pid, different UIA identities.
    c = make_controller(open_windows=[_win(100, 700), _win(101, 700)],
                        window_identity={100: "Default", 101: "Profile 7"})
    r = c.open_or_focus("chrome-profile:Profile 7", expected_email="thrilok@gmail.com")
    assert r["hwnd"] == 101 and r["observed"] == "chrome-profile:Profile 7"


# 7. wrong HWND rejected: verify_profile_open on a Default window for Profile 7
def test_absolute_wrong_hwnd_rejected():
    c = make_controller(open_windows=[_win(100, 500)], window_identity={100: "Default"})
    v = c.verify_profile_open("chrome-profile:Profile 7", 100,
                              expected_email="thrilok@gmail.com", require_email=True)
    assert v["verified"] is False


# 8. wrong email rejected (directory matches but email differs)
def test_absolute_wrong_email_rejected():
    # window is Default (dir), but inject an email hook that reports a DIFFERENT
    # account → email cross-check must fail closed.
    c = ChromeProfileController(
        user_data_dir="C:/fake/User Data",
        metadata_fn=lambda: FAKE_METADATA,
        host_windows_fn=lambda: [_win(100, 500)],
        focus_fn=lambda h: True,
        launch_fn=lambda e, d: 1,
        process_profile_fn=lambda: {},
        exe_resolver_fn=lambda: "C:/fake/chrome.exe",
        window_identity_fn=lambda h: "Default",
        window_email_fn=lambda h: "someoneelse@gmail.com",
    )
    c._on_disk_profile_dirs = lambda: []
    c._email_from_preferences = lambda pdir: ""
    v = c.verify_profile_open("chrome-profile:Default", 100,
                              expected_email="codetrust2026@gmail.com", require_email=True)
    assert v["verified"] is False and v["reason"] == "EMAIL_MISMATCH"


# 9. ambiguous / unidentified windows rejected (inferred never accepted)
def test_absolute_unidentified_window_rejected():
    launches = []
    # A window with NO identity evidence (inferred Default) must NOT be used.
    c = make_controller(open_windows=[_win(100, 500)], window_identity={},
                        launch_record=launches)
    r = c.open_or_focus("chrome-profile:Default",
                        expected_email="codetrust2026@gmail.com")
    # Falls through to launching the exact Default (never the inferred window).
    assert r["action"] == "launch"


# 10. no trusted identity at all → fail closed (launch also cannot verify)
def test_absolute_fail_closed_no_identity():
    # Launches produce windows with NO identity → verification never succeeds.
    def launch_fn(exe, pdir):
        return 1  # launched but the new window carries no identity
    c = ChromeProfileController(
        user_data_dir="C:/fake/User Data",
        metadata_fn=lambda: FAKE_METADATA,
        host_windows_fn=lambda: [],
        focus_fn=lambda h: True,
        launch_fn=launch_fn,
        process_profile_fn=lambda: {},
        exe_resolver_fn=lambda: "C:/fake/chrome.exe",
        window_identity_fn=lambda h: None,
    )
    c._on_disk_profile_dirs = lambda: []
    c._email_from_preferences = lambda pdir: ""
    import jarvis_core.chrome_profile_controller as m
    old = m.LAUNCH_WAIT_SECONDS
    m.LAUNCH_WAIT_SECONDS = 0.1
    try:
        with pytest.raises(ProfileVerificationError):
            c.open_or_focus("chrome-profile:Default",
                            expected_email="codetrust2026@gmail.com")
    finally:
        m.LAUNCH_WAIT_SECONDS = old


# 12. no fallback profile: verifying a specific profile never returns another
def test_absolute_no_fallback_profile():
    c = make_controller(open_windows=[_win(100, 500), _win(101, 501)],
                        window_identity={100: "Default", 101: "Profile 4"})
    # request Profile 4 → must return the Profile 4 window, never Default.
    r = c.open_or_focus("chrome-profile:Profile 4", expected_email="bhargav@gmail.com")
    assert r["hwnd"] == 101 and r["observed"] == "chrome-profile:Profile 4"


# email cross-check unproven → fail closed when required
def test_absolute_email_unproven_fails_closed():
    c = ChromeProfileController(
        user_data_dir="C:/fake/User Data",
        metadata_fn=lambda: FAKE_METADATA,
        host_windows_fn=lambda: [_win(100, 500)],
        focus_fn=lambda h: True,
        launch_fn=lambda e, d: 1,
        process_profile_fn=lambda: {},
        exe_resolver_fn=lambda: "C:/fake/chrome.exe",
        window_identity_fn=lambda h: "Default",
        window_email_fn=lambda h: None,  # cannot prove email
    )
    c._on_disk_profile_dirs = lambda: []
    c._email_from_preferences = lambda pdir: ""
    v = c.verify_profile_open("chrome-profile:Default", 100,
                              expected_email="codetrust2026@gmail.com", require_email=True)
    assert v["verified"] is False and v["reason"] == "EMAIL_UNPROVEN"


# ── Fix A: selected profile absolute vs frontend/foreground/unidentified ─────

# An UNIDENTIFIED window (no PID arg, no UIA chip) must NEVER be attributed to
# Default nor used as the selected profile's window.
def test_absolute_unidentified_never_default():
    # window 900 has no pid_profile entry and window_identity returns None.
    c = make_controller(open_windows=[_win(900, 800)], window_identity={})
    wins = c.get_profile_windows("chrome-profile:Default")
    # the unidentified window is NOT in Default's candidate list
    assert all(w["hwnd"] != 900 for w in wins)
    # resolve returns nothing (no proven Default window)
    assert c.resolve_profile_window("chrome-profile:Default") is None


# The Jarvis-frontend / foreground window (a DIFFERENT profile) must not be
# used when a different profile is selected — selection is authoritative.
def test_absolute_frontend_window_ignored_when_other_profile_selected():
    launches = []
    # Frontend runs in Profile 4 (hwnd 900); workflow selects Default.
    c = make_controller(open_windows=[_win(900, 800)],
                        window_identity={900: "Profile 4"},
                        launch_record=launches)
    r = c.open_or_focus("chrome-profile:Default",
                        expected_email="codetrust2026@gmail.com")
    # Default not open → launched exact Default; the Profile 4 frontend window
    # (900) is never used.
    assert r["action"] == "launch"
    assert launches and launches[0]["profile_dir"] == "Default"
    assert r["hwnd"] != 900


# Even if the frontend window IS the selected profile, a separate verified
# window is still required (foreground does not auto-become the target); here
# the only Default window verifies by dir+email and is used deterministically,
# NOT because it is foreground.
def test_absolute_selected_profile_used_by_identity_not_foreground():
    # Two Default windows; selection must pick a verified one by identity.
    c = make_controller(open_windows=[_win(100, 500), _win(101, 500)],
                        window_identity={100: "Default", 101: "Default"})
    r = c.open_or_focus("chrome-profile:Default",
                        expected_email="codetrust2026@gmail.com")
    assert r["verified"] is True and r["observed"] == "chrome-profile:Default"
    assert r["hwnd"] in (100, 101)


# resolve_profile_window never returns an inferred/unidentified fallback.
def test_resolve_never_returns_inferred():
    c = make_controller(open_windows=[_win(900, 800)], window_identity={})
    assert c.resolve_profile_window("chrome-profile:Default") is None
