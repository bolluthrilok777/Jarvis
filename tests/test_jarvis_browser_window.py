"""Tests for the hardened launch-provenance Jarvis-owned window architecture.

Identity = trusted Local State email→directory mapping + unique launch token +
new-HWND correlation + exact marker-tab URL proof. No chrome://version, avatar,
title, PID, or page-content signals. All OS interactions injected.
"""
from __future__ import annotations

import pytest

import jarvis_core.jarvis_browser_window as jbw
from jarvis_core.jarvis_browser_window import (
    JarvisBrowserWindowController, MARKER_BASE, VERIFICATION_METHOD,
    ProfileMappingInvalidError, NoNewWindowError,
    LaunchTokenNotFoundError, AmbiguousLaunchTokenError,
    directories_for_email,
)

# Three profiles sharing display name "Code"; unique emails.
META = {"profiles": [
    {"profile_dir": "Default", "name": "Code", "user_name": "codetrust2026@gmail.com"},
    {"profile_dir": "Profile 3", "name": "Code", "user_name": "codetrust2025@gmail.com"},
    {"profile_dir": "Profile 37", "name": "Code", "user_name": "codetrust2027@gmail.com"},
    {"profile_dir": "Profile 4", "name": "Bhargav", "user_name": "bhargavraghuvanshi5@gmail.com"},
]}
# A metadata variant with an AMBIGUOUS email (same email on two dirs).
META_AMBIG = {"profiles": [
    {"profile_dir": "Default", "name": "Code", "user_name": "dup@gmail.com"},
    {"profile_dir": "Profile 9", "name": "Code9", "user_name": "dup@gmail.com"},
]}


class Sim:
    """Simulates Chrome: existing windows + a launch that creates new windows,
    each carrying (or not) the marker token in a tab URL."""

    def __init__(self, existing=(1001,), new_specs=None, token="TOK"):
        # new_specs: list of dicts {hwnd, has_token(bool), extra_tabs(list of url)}
        self.hwnds = list(existing)
        self.new_specs = new_specs if new_specs is not None else [
            {"hwnd": 2001, "has_token": True}]
        self.fixed_token = token
        self.launches = []
        self.navigations = []
        self.closed = []
        self._tab_urls = {}         # hwnd -> [ {runtime_id,tab_index,title,url} ]
        # pre-existing user windows have unrelated tabs
        for h in existing:
            self._tab_urls[h] = [{"runtime_id": [1, h, 1], "tab_index": 0,
                                  "title": "user", "url": "https://mail.google.com/"}]

    def host_windows(self):
        return [{"hwnd": h, "pid": 5, "title": "x"} for h in self.hwnds]

    def token(self):
        return self.fixed_token

    def launch(self, exe, profile_dir, marker_url):
        self.launches.append((profile_dir, marker_url))
        for spec in self.new_specs:
            h = spec["hwnd"]
            self.hwnds.append(h)
            tabs = []
            if spec.get("has_token", False):
                tabs.append({"runtime_id": [42, h, 4, 0, 0, 900], "tab_index": 0,
                             "title": "Jarvis Browser Binding", "url": marker_url})
            for i, u in enumerate(spec.get("extra_tabs", [])):
                tabs.append({"runtime_id": [42, h, 4, 0, 0, 950 + i],
                             "tab_index": len(tabs), "title": "x", "url": u})
            if not tabs:
                tabs.append({"runtime_id": [42, h, 4, 0, 0, 999], "tab_index": 0,
                             "title": "blank", "url": "about:blank"})
            self._tab_urls[h] = tabs
        return 12345

    def window_tab_urls(self, hwnd):
        return list(self._tab_urls.get(int(hwnd), []))

    def navigate(self, hwnd, url):
        self.navigations.append((hwnd, url))

    def close_window(self, hwnd):
        self.closed.append(int(hwnd))
        self.hwnds = [h for h in self.hwnds if h != int(hwnd)]
        return True


def _ctrl(sim, meta=META):
    return JarvisBrowserWindowController(
        host_windows_fn=sim.host_windows,
        launch_new_window_fn=sim.launch,
        exe_resolver_fn=lambda: "C:\\chrome.exe",
        window_tab_urls_fn=sim.window_tab_urls,
        navigate_fn=sim.navigate,
        metadata_fn=lambda: meta,
        close_window_fn=sim.close_window,
        token_fn=sim.token,
    )


# ── mapping ───────────────────────────────────────────────────────────────────

def test_mapping_exact_one():
    assert directories_for_email("codetrust2026@gmail.com", META) == ["Default"]


def test_mapping_missing_email_fails():
    sim = Sim(); c = _ctrl(sim)
    with pytest.raises(ProfileMappingInvalidError):
        c.open_verified_window(workflow_id="w", selected_profile_key="k",
                               selected_account_email="nope@gmail.com")


def test_mapping_ambiguous_email_fails():
    sim = Sim(); c = _ctrl(sim, meta=META_AMBIG)
    with pytest.raises(ProfileMappingInvalidError):
        c.open_verified_window(workflow_id="w", selected_profile_key="k",
                               selected_account_email="dup@gmail.com")


def test_mapping_directory_mismatch_fails():
    sim = Sim(); c = _ctrl(sim)
    with pytest.raises(ProfileMappingInvalidError):
        c.open_verified_window(workflow_id="w", selected_profile_key="k",
                               selected_account_email="codetrust2026@gmail.com",
                               selected_profile_directory="Profile 37")  # wrong


# ── launch token correlation ─────────────────────────────────────────────────

def test_one_new_window_with_token_pass():
    sim = Sim(existing=(1001,), new_specs=[{"hwnd": 2001, "has_token": True}])
    c = _ctrl(sim)
    b = c.open_verified_window(workflow_id="wfA", selected_profile_key="chrome-profile:Default",
                               selected_account_email="codetrust2026@gmail.com")
    assert b["verified"] is True
    assert b["hwnd"] == 2001
    assert b["selected_profile_directory"] == "Default"
    assert b["launch_token"] == "TOK"
    assert b["marker_url"] == f"{MARKER_BASE}/TOK"
    assert b["verification_method"] == VERIFICATION_METHOD
    assert b["owner"] == "jarvis" and b["workflow_id"] == "wfA"
    assert sim.launches == [("Default", f"{MARKER_BASE}/TOK")]
    # tab identity captured from the marker tab
    assert b["tab_identity"]["runtime_id"] == "42,2001,4,0,0,900"


def test_several_new_windows_only_one_has_token():
    sim = Sim(existing=(1001,), new_specs=[
        {"hwnd": 2001, "has_token": False, "extra_tabs": ["https://youtube.com"]},
        {"hwnd": 2002, "has_token": True},   # ours
        {"hwnd": 2003, "has_token": False},
    ])
    c = _ctrl(sim)
    b = c.open_verified_window(workflow_id="wfA", selected_profile_key="k",
                               selected_account_email="codetrust2026@gmail.com")
    assert b["hwnd"] == 2002                # token-bearing window chosen


def test_no_token_bearing_window_fails():
    sim = Sim(existing=(1001,), new_specs=[{"hwnd": 2001, "has_token": False}])
    c = _ctrl(sim)
    import jarvis_core.jarvis_browser_window as m
    # speed up the poll loop
    m.NEW_WINDOW_WAIT_SECONDS = 0.6
    m.NEW_WINDOW_POLL_INTERVAL = 0.15
    with pytest.raises(LaunchTokenNotFoundError):
        c.open_verified_window(workflow_id="wfA", selected_profile_key="k",
                               selected_account_email="codetrust2026@gmail.com")


def test_no_new_window_fails(monkeypatch):
    sim = Sim(existing=(1001,), new_specs=[])   # launch creates nothing
    c = _ctrl(sim)
    monkeypatch.setattr(jbw, "NEW_WINDOW_WAIT_SECONDS", 0.6)
    monkeypatch.setattr(jbw, "NEW_WINDOW_POLL_INTERVAL", 0.15)
    with pytest.raises(NoNewWindowError):
        c.open_verified_window(workflow_id="wfA", selected_profile_key="k",
                               selected_account_email="codetrust2026@gmail.com")


def test_multiple_token_bearing_windows_fail():
    sim = Sim(existing=(1001,), new_specs=[
        {"hwnd": 2001, "has_token": True},
        {"hwnd": 2002, "has_token": True},   # duplicate token → ambiguous
    ])
    c = _ctrl(sim)
    with pytest.raises(AmbiguousLaunchTokenError):
        c.open_verified_window(workflow_id="wfA", selected_profile_key="k",
                               selected_account_email="codetrust2026@gmail.com")


def test_old_user_window_with_different_token_ignored():
    # A pre-existing user window carries a DIFFERENT browser-bind token; only
    # the NEW window with OUR token is chosen.
    sim = Sim(existing=(1001,))
    sim._tab_urls[1001] = [{"runtime_id": [1, 1001, 1], "tab_index": 0, "title": "old",
                            "url": f"{MARKER_BASE}/OTHERTOKEN"}]
    c = _ctrl(sim)
    b = c.open_verified_window(workflow_id="wfA", selected_profile_key="k",
                               selected_account_email="codetrust2026@gmail.com")
    assert b["hwnd"] == 2001            # the NEW token window, not the old one


def test_token_is_workflow_specific():
    # Two workflows get distinct tokens (real token_fn); bindings don't collide.
    c = JarvisBrowserWindowController(
        host_windows_fn=lambda: [], launch_new_window_fn=lambda *a: 1,
        exe_resolver_fn=lambda: "x", window_tab_urls_fn=lambda h: [],
        navigate_fn=lambda *a: None, metadata_fn=lambda: META,
        close_window_fn=lambda h: True)
    t1 = c._token_fn(); t2 = c._token_fn()
    assert t1 != t2 and len(t1) >= 32


# ── open vs closed / never adopt ──────────────────────────────────────────────

def test_already_open_profile_still_launches_new_window():
    # An existing Default window (1001) is present but NOT adopted; a new
    # token-bearing window (2001) is launched and used.
    sim = Sim(existing=(1001,), new_specs=[{"hwnd": 2001, "has_token": True}])
    c = _ctrl(sim)
    b = c.open_verified_window(workflow_id="wfA", selected_profile_key="k",
                               selected_account_email="codetrust2026@gmail.com")
    assert b["hwnd"] == 2001
    assert 1001 not in [b["hwnd"]]
    assert len(sim.launches) == 1


def test_frontend_emails_in_page_are_irrelevant():
    # A new NON-token window whose page lists all account emails must be ignored;
    # only the token window is chosen.
    sim = Sim(existing=(1001,), new_specs=[
        {"hwnd": 2001, "has_token": False,
         "extra_tabs": ["http://127.0.0.1:8000/ui"]},   # the frontend, no token
        {"hwnd": 2002, "has_token": True},
    ])
    c = _ctrl(sim)
    b = c.open_verified_window(workflow_id="wfA", selected_profile_key="k",
                               selected_account_email="codetrust2026@gmail.com")
    assert b["hwnd"] == 2002


# ── recovery ──────────────────────────────────────────────────────────────────

def test_is_binding_live_requires_workflow_hwnd_and_tab():
    sim = Sim(existing=(1001,), new_specs=[{"hwnd": 2001, "has_token": True}])
    c = _ctrl(sim)
    b = c.open_verified_window(workflow_id="wfA", selected_profile_key="k",
                               selected_account_email="codetrust2026@gmail.com")
    assert c.is_binding_live(b, workflow_id="wfA") is True
    assert c.is_binding_live(b, workflow_id="wfB") is False   # other workflow
    # window gone → not live
    sim.hwnds = [h for h in sim.hwnds if h != 2001]
    assert c.is_binding_live(b, workflow_id="wfA") is False


def test_recreate_for_conversation_new_window_and_navigates():
    sim = Sim(existing=(1001,), new_specs=[{"hwnd": 2001, "has_token": True}])
    c = _ctrl(sim)
    conv = "https://chatgpt.com/c/live1"
    b = c.recreate_for_conversation(
        workflow_id="wfA", selected_profile_key="k",
        selected_account_email="codetrust2026@gmail.com", conversation_url=conv)
    assert b["hwnd"] == 2001
    assert b["conversation_url"] == conv
    assert (2001, conv) in sim.navigations   # navigated the marker tab to conv


def test_stale_hwnd_never_adopted_recovery_launches_fresh():
    # A stale binding points at a vanished hwnd; recovery LAUNCHES a new window
    # (never adopts an existing arbitrary window).
    sim = Sim(existing=(1001,), new_specs=[{"hwnd": 3003, "has_token": True}])
    c = _ctrl(sim)
    stale = {"owner": "jarvis", "workflow_id": "wfA", "hwnd": 999999,
             "tab_identity": {"runtime_id": "gone"}}
    assert c.is_binding_live(stale, workflow_id="wfA") is False
    b = c.recreate_for_conversation(
        workflow_id="wfA", selected_profile_key="k",
        selected_account_email="codetrust2026@gmail.com",
        conversation_url="https://chatgpt.com/c/x")
    assert b["hwnd"] == 3003 and b["verified"] is True
