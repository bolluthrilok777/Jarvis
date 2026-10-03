"""Tests for the workflow-binding-aware restore (Part 5/6 later-stage migration).

A LIVE persisted JarvisBrowserWindowBinding is reused directly (no legacy
profile discovery); a dead/stale binding triggers hardened launch-token
recovery. Reproduces the exact live PROFILE_MISMATCH scenario and proves the
legacy profile verifier never runs on the workflow path.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from jarvis_core.chatgpt_browser_controller import ChatGPTBrowserController

CONV = "https://chatgpt.com/c/6a9ed3e7-371c-83e8-bd57-444bc28709b0"
RID = "42,10029300,4,0,0,314092"
HWND = 10029300


def _binding(hwnd=HWND, rid=RID, workflow_id="wfX"):
    return {
        "owner": "jarvis", "workflow_id": workflow_id,
        "selected_profile_key": "chrome-profile:Profile 3",
        "selected_profile_directory": "Profile 3",
        "selected_account_email": "codetrust2025@gmail.com",
        "hwnd": hwnd, "launch_token": "tok",
        "tab_identity": {"control_mode": "uia", "host_hwnd": hwnd,
                         "runtime_id": rid, "tab_index": 0,
                         "owner": "jarvis", "workflow_id": workflow_id},
    }


class _LegacyTripwire:
    """Records if any legacy profile method is called (must never happen on the
    workflow path for a live binding)."""
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name in ("ensure_selected_profile", "open_or_focus",
                    "verify_profile_open", "launch_profile", "discover_profiles"):
            def _tripped(*a, **k):
                self.calls.append(name)
                raise AssertionError(f"legacy profile method called: {name}")
            return _tripped
        raise AttributeError(name)


def _controller(*, uia_tabs, host_hwnds, jarvis_window=None):
    """Real ChatGPTBrowserController with only the tab-layer + host-window hooks
    injected. focus_verify_fn is a tripwire that FAILS if the legacy profile
    path is ever used."""
    tw = _LegacyTripwire()

    def _focus_verify(pk, expected_email=None):
        tw.calls.append("ensure_selected_profile")
        raise AssertionError("legacy ensure_selected_profile/open_or_focus used")

    ctrl = ChatGPTBrowserController(
        focus_verify_fn=_focus_verify,
        discover_tabs_fn=lambda: {"tabs": list(uia_tabs), "cdp_url": "",
                                  "controllable": True},
        focus_tab_uia_fn=lambda h, r, t, i: True,
        read_tab_url_uia_fn=lambda h, r, *, title="", tab_index=-1: (
            {"ok": True, "url": _url_for(uia_tabs, h, r), "code": "", "reason": ""}
            if _url_for(uia_tabs, h, r) else
            {"ok": False, "url": "", "code": "TAB_URL_UNAVAILABLE", "reason": ""}),
        jarvis_window_controller=jarvis_window,
    )
    ctrl.ensure_chatgpt_ready = lambda hwnd, allow_login_page=False: "READY"  # type: ignore
    ctrl._require_foreground = lambda hwnd: None  # type: ignore
    ctrl.capture_conversation_url = lambda hwnd: CONV  # type: ignore
    ctrl._tripwire = tw
    # Patch chrome_host_windows used by the live-binding check.
    ctrl._host_hwnds = host_hwnds
    return ctrl, tw


def _url_for(tabs, hwnd, rid):
    r = list(rid)
    for t in tabs:
        if t.get("host_hwnd") == hwnd and list(t.get("runtime_id") or []) == r:
            return t.get("raw_url") or ""
    return ""


def _uia_tab(hwnd, rid, url, idx=0):
    return {"control_mode": "uia", "host_hwnd": hwnd, "runtime_id": list(rid),
            "tab_index": idx, "title": "ChatGPT", "raw_url": url}


def _rid_list(s):
    return [int(x) for x in s.split(",")]


# ── LIVE binding reuse (the exact fix) ────────────────────────────────────────

def test_live_binding_reused_no_legacy_profile_calls():
    tabs = [_uia_tab(HWND, _rid_list(RID), CONV)]
    ctrl, tw = _controller(uia_tabs=tabs, host_hwnds={HWND})
    with patch("jarvis_core.uia_browser_manager.chrome_host_windows",
               return_value=[{"hwnd": HWND, "pid": 5, "title": "x"}]):
        res = ctrl.restore_workflow_owned_conversation(
            workflow_id="wfX", conversation_url=CONV,
            window_binding=_binding(), tab_identity=_binding()["tab_identity"])
    assert res["reused"] is True
    assert res["recovered"] is False
    assert res["same"] is True
    assert res["hwnd"] == HWND
    assert res["identity"]["runtime_id"] == RID
    # NO legacy profile method was called.
    assert tw.calls == []


def test_live_binding_exact_failure_scenario_no_profile_mismatch():
    """Reproduce the real live failure inputs (Profile 3 / codetrust2025 /
    HWND 10029300 / runtime 42,10029300,4,0,0,314092). Legacy verification MUST
    NOT run and PROFILE_MISMATCH cannot occur."""
    tabs = [_uia_tab(HWND, _rid_list(RID), CONV)]
    ctrl, tw = _controller(uia_tabs=tabs, host_hwnds={HWND})
    with patch("jarvis_core.uia_browser_manager.chrome_host_windows",
               return_value=[{"hwnd": HWND, "pid": 5}]):
        res = ctrl.restore_workflow_owned_conversation(
            workflow_id="wfX", conversation_url=CONV, window_binding=_binding())
    assert res["reused"] is True and res["same"] is True
    assert tw.calls == []            # no ProfileVerificationError path at all


def test_live_binding_same_hwnd_runtime_conversation():
    tabs = [_uia_tab(HWND, _rid_list(RID), CONV)]
    ctrl, tw = _controller(uia_tabs=tabs, host_hwnds={HWND})
    with patch("jarvis_core.uia_browser_manager.chrome_host_windows",
               return_value=[{"hwnd": HWND, "pid": 5}]):
        res = ctrl.restore_workflow_owned_conversation(
            workflow_id="wfX", conversation_url=CONV, window_binding=_binding())
    assert res["hwnd"] == HWND
    assert res["identity"]["runtime_id"] == RID
    assert res["observed_url"] == CONV


# ── RECOVERY (dead/stale binding) ─────────────────────────────────────────────

class _RecoveryJarvisWindow:
    """Fake jarvis_window_controller.recreate_for_conversation."""
    def __init__(self):
        self.calls = []

    def recreate_for_conversation(self, *, workflow_id, selected_profile_key,
                                  selected_account_email, conversation_url,
                                  selected_profile_directory=None):
        self.calls.append((workflow_id, selected_profile_directory, conversation_url))
        new_hwnd = 55501
        return {"owner": "jarvis", "workflow_id": workflow_id, "hwnd": new_hwnd,
                "launch_token": "newtok",
                "selected_profile_directory": selected_profile_directory,
                "selected_account_email": selected_account_email,
                "conversation_url": conversation_url,
                "tab_identity": {"control_mode": "uia", "host_hwnd": new_hwnd,
                                 "runtime_id": "42,55501,4,0,0,7", "tab_index": 0,
                                 "owner": "jarvis", "workflow_id": workflow_id},
                "verified": True, "verification_method":
                    "local_state_profile_mapping_plus_launch_token_provenance"}


def test_dead_binding_hwnd_gone_triggers_token_recovery():
    # Bound HWND is NOT among live host windows → recovery.
    jw = _RecoveryJarvisWindow()
    tabs = []
    ctrl, tw = _controller(uia_tabs=tabs, host_hwnds=set(), jarvis_window=jw)
    ctrl.capture_conversation_url = lambda hwnd: CONV  # type: ignore
    with patch("jarvis_core.uia_browser_manager.chrome_host_windows",
               return_value=[{"hwnd": 999, "pid": 5}]):   # bound HWND absent
        res = ctrl.restore_workflow_owned_conversation(
            workflow_id="wfX", conversation_url=CONV, window_binding=_binding())
    assert res["recovered"] is True and res["reused"] is False
    assert res["hwnd"] == 55501
    assert len(jw.calls) == 1                     # token recovery ran once
    assert jw.calls[0][1] == "Profile 3"          # same selected directory
    assert tw.calls == []                          # never legacy verifier


def test_stale_runtime_id_triggers_recovery():
    # Bound HWND live, but the persisted runtime_id no longer matches any tab.
    jw = _RecoveryJarvisWindow()
    tabs = [_uia_tab(HWND, [9, 9, 9], CONV)]       # different runtime id
    ctrl, tw = _controller(uia_tabs=tabs, host_hwnds={HWND}, jarvis_window=jw)
    ctrl.capture_conversation_url = lambda hwnd: CONV  # type: ignore
    with patch("jarvis_core.uia_browser_manager.chrome_host_windows",
               return_value=[{"hwnd": HWND, "pid": 5}]):
        res = ctrl.restore_workflow_owned_conversation(
            workflow_id="wfX", conversation_url=CONV, window_binding=_binding())
    assert res["recovered"] is True
    assert len(jw.calls) == 1
    assert tw.calls == []


def test_wrong_conversation_on_owned_tab_triggers_recovery():
    # Owned tab exists but shows a DIFFERENT conversation → recover (don't act).
    jw = _RecoveryJarvisWindow()
    other = "https://chatgpt.com/c/DIFFERENT"
    tabs = [_uia_tab(HWND, _rid_list(RID), other)]
    ctrl, tw = _controller(uia_tabs=tabs, host_hwnds={HWND}, jarvis_window=jw)
    ctrl.capture_conversation_url = lambda hwnd: CONV  # type: ignore
    with patch("jarvis_core.uia_browser_manager.chrome_host_windows",
               return_value=[{"hwnd": HWND, "pid": 5}]):
        res = ctrl.restore_workflow_owned_conversation(
            workflow_id="wfX", conversation_url=CONV, window_binding=_binding())
    assert res["recovered"] is True
    assert len(jw.calls) == 1


def test_cross_workflow_binding_not_reused():
    # Binding belongs to a DIFFERENT workflow → not reused; recovery instead.
    jw = _RecoveryJarvisWindow()
    tabs = [_uia_tab(HWND, _rid_list(RID), CONV)]
    ctrl, tw = _controller(uia_tabs=tabs, host_hwnds={HWND}, jarvis_window=jw)
    ctrl.capture_conversation_url = lambda hwnd: CONV  # type: ignore
    b = _binding(workflow_id="OTHER")
    with patch("jarvis_core.uia_browser_manager.chrome_host_windows",
               return_value=[{"hwnd": HWND, "pid": 5}]):
        res = ctrl.restore_workflow_owned_conversation(
            workflow_id="wfX", conversation_url=CONV, window_binding=b)
    assert res["recovered"] is True     # cross-workflow → recover, never adopt
    assert tw.calls == []
