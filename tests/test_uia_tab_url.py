"""UIA exact-tab URL read + URL-based tab binding tests (Chrome 152, no CDP).

Covers the safe read_tab_url fail-closed behavior (mocked pywinauto) and the
controller-level URL binding: conversation match by read-back URL, new-tab URL
verification, reuse/stale/ambiguous handling, personal-tab safety, and Jarvis
frontend identification by URL (never title).
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

import jarvis_core.uia_browser_manager as u
from jarvis_core.chatgpt_browser_controller import (
    ChatGPTBrowserController,
    TabIdentityUnavailableError,
)

CONV_A = "https://chatgpt.com/c/aaaa-1111"
CONV_B = "https://chatgpt.com/c/bbbb-2222"
USER_CONV = "https://chatgpt.com/c/user-9999"
GMAIL = "https://mail.google.com/mail/u/0"
JARVIS = "http://127.0.0.1:8000/ui"


# ── read_tab_url (real function, mocked UIA) ──────────────────────────────────

def _fake_tabitem(runtime_id, selected=False):
    el = MagicMock()
    el.element_info.runtime_id = list(runtime_id)
    el.is_selected.return_value = selected
    def _select():
        el.is_selected.return_value = True
    el.select.side_effect = _select
    return el


def _patch_window(tabitems):
    win = MagicMock()
    win.descendants.return_value = tabitems
    return win


# 1. exact runtime_id URL read succeeds + 6. clipboard restored
def test_read_tab_url_exact_success_and_clipboard_restored():
    tab = _fake_tabitem([42, 700, 4, 0, 0, 111])
    win = _patch_window([tab])
    clip = {"v": "USER CLIP"}
    with patch.object(u, "chrome_host_windows", return_value=[{"hwnd": 700}]), \
         patch.object(u, "_attach", return_value=win), \
         patch("jarvis_core.windows_resource_manager.foreground_window", return_value=700), \
         patch.dict("sys.modules", {"pyperclip": MagicMock()}) as mods:
        pc = mods["pyperclip"]
        seq = ["USER CLIP", "https://chatgpt.com/c/aaaa-1111"]
        pc.paste.side_effect = lambda: seq.pop(0) if len(seq) > 1 else seq[0]
        pc.copy.side_effect = lambda v: clip.update(v=v)
        res = u.read_tab_url(700, [42, 700, 4, 0, 0, 111])
    assert res["ok"] is True
    assert res["url"] == "https://chatgpt.com/c/aaaa-1111"
    # clipboard restored to the user's original value
    assert clip["v"] == "USER CLIP"


# 2/3. wrong/stale runtime_id rejected (TAB_IDENTITY_STALE)
def test_read_tab_url_stale_runtime_id():
    tab = _fake_tabitem([42, 700, 4, 0, 0, 111])
    win = _patch_window([tab])
    with patch.object(u, "chrome_host_windows", return_value=[{"hwnd": 700}]), \
         patch.object(u, "_attach", return_value=win):
        res = u.read_tab_url(700, [9, 9, 9])   # not present
    assert res["ok"] is False
    assert res["code"] == "TAB_IDENTITY_STALE"


def test_read_tab_url_rejects_stale_clipboard_text():
    """SAFETY: if the address-bar copy leaves the sentinel/previous clipboard
    (no real URL copied), or the clipboard holds free text, it must fail closed
    — never treat stale clipboard content as a URL."""
    tab = _fake_tabitem([42, 700, 4, 0, 0, 111])
    win = _patch_window([tab])
    with patch.object(u, "chrome_host_windows", return_value=[{"hwnd": 700}]), \
         patch.object(u, "_attach", return_value=win), \
         patch("jarvis_core.windows_resource_manager.foreground_window", return_value=700), \
         patch.dict("sys.modules", {"pyperclip": MagicMock()}) as mods:
        pc = mods["pyperclip"]
        # paste() called: (1) save prev clip, (2) read after copy → returns a
        # big blob of free text (stale clipboard), NOT a URL.
        blob = "CORRECTION - DO NOT RESTART CHROME\nline2\nline3"
        seq = ["USER CLIP", blob]
        pc.paste.side_effect = lambda: seq.pop(0) if len(seq) > 1 else seq[-1]
        pc.copy.side_effect = lambda v: None
        res = u.read_tab_url(700, [42, 700, 4, 0, 0, 111])
    assert res["ok"] is False
    assert res["code"] == "TAB_URL_UNAVAILABLE"


def test_looks_like_url_helper():
    assert u._looks_like_url("https://chatgpt.com/c/abc") is True
    assert u._looks_like_url("about:blank") is True
    assert u._looks_like_url("not a url with spaces") is False
    assert u._looks_like_url("multi\nline\ntext") is False
    assert u._looks_like_url("") is False


# 4. wrong HWND rejected
def test_read_tab_url_wrong_hwnd():
    with patch.object(u, "chrome_host_windows", return_value=[{"hwnd": 700}]):
        res = u.read_tab_url(999999, [1, 2, 3])
    assert res["ok"] is False
    assert res["code"] == "TAB_IDENTITY_STALE"


# URL unavailable when address bar cannot be read (foreground not taken)
def test_read_tab_url_unavailable_when_not_foreground():
    tab = _fake_tabitem([42, 700, 4, 0, 0, 111])
    win = _patch_window([tab])
    with patch.object(u, "chrome_host_windows", return_value=[{"hwnd": 700}]), \
         patch.object(u, "_attach", return_value=win), \
         patch("jarvis_core.windows_resource_manager.foreground_window", return_value=12345):
        res = u.read_tab_url(700, [42, 700, 4, 0, 0, 111])
    assert res["ok"] is False
    assert res["code"] == "TAB_URL_UNAVAILABLE"


# ── Controller URL binding (fake tab layer) ───────────────────────────────────

def _uia_tab(host, rid, url, idx=0, title="ChatGPT"):
    return {"control_mode": "uia", "host_hwnd": host, "runtime_id": list(rid),
            "tab_index": idx, "title": title, "raw_url": url}


class _Layer:
    def __init__(self, tabs):
        self.tabs = list(tabs)
        self.closed = []
        self.created = []

    def discover(self):
        # UIA discovery does NOT expose URL — strip raw_url to force reader use.
        stripped = []
        for t in self.tabs:
            s = dict(t)
            s.pop("raw_url", None)
            stripped.append(s)
        return {"tabs": stripped, "cdp_url": "", "controllable": True}

    def read_uia_url(self, host, rid, *, title="", tab_index=-1):
        r = list(rid)
        for t in self.tabs:
            if t.get("host_hwnd") == host and list(t.get("runtime_id") or []) == r:
                url = t.get("raw_url") or ""
                if url and url != "about:blank":
                    return {"ok": True, "url": url, "code": "", "reason": ""}
                return {"ok": False, "url": "", "code": "TAB_URL_UNAVAILABLE", "reason": ""}
        return {"ok": False, "url": "", "code": "TAB_IDENTITY_STALE", "reason": ""}

    def focus_uia(self, host, rid, title, idx):
        return True

    def close_uia(self, host, rid, title, idx):
        self.closed.append((host, tuple(rid)))
        r = list(rid)
        self.tabs = [t for t in self.tabs
                     if not (t.get("host_hwnd") == host
                             and list(t.get("runtime_id") or []) == r)]
        return {"ok": True, "verified": True}

    def create_uia(self, host):
        rid = [90 + len(self.tabs)]
        idx = len(self.tabs)
        self.tabs.append(_uia_tab(host, rid, "about:blank", idx, "New Tab"))
        self.created.append(tuple(rid))
        return {"ok": True, "host_hwnd": host, "runtime_id": rid,
                "tab_index": idx, "title": "New Tab", "control_mode": "uia"}


def _ctrl(layer, navigated=None):
    nav = navigated if navigated is not None else []
    c = ChatGPTBrowserController(
        focus_verify_fn=lambda pk, expected_email=None: {"hwnd": 700, "verified": True},
        discover_tabs_fn=layer.discover,
        focus_tab_uia_fn=layer.focus_uia,
        close_tab_uia_fn=layer.close_uia,
        create_tab_uia_fn=layer.create_uia,
        read_tab_url_uia_fn=layer.read_uia_url,
    )
    def _nav(hwnd, url):
        nav.append(url)
        uia = [t for t in layer.tabs
               if t.get("control_mode") == "uia" and t.get("host_hwnd") == hwnd]
        if uia:
            uia[-1]["raw_url"] = url
    c._navigate = _nav                                        # type: ignore
    c.ensure_chatgpt_ready = lambda hwnd, allow_login_page=False: "READY"  # type: ignore
    c._require_foreground = lambda hwnd: None                 # type: ignore
    c.capture_conversation_url = lambda hwnd: (nav[-1] if nav else CONV_A)  # type: ignore
    return c, nav


# 7/8. new tab runtime_id differs + URL verified after navigation
def test_uia_new_tab_distinct_id_and_url_verified():
    layer = _Layer([_uia_tab(700, [1], GMAIL, 0, "Gmail")])
    c, nav = _ctrl(layer)
    created = c.create_owned_tab(700, workflow_id="wfA", conversation_url=CONV_A)
    ident = created["identity"]
    assert ident["control_mode"] == "uia"
    assert ident["runtime_id"] == "91"          # brand-new runtime id (distinct)
    assert ident["url"] == CONV_A               # verified after navigation
    assert nav == [CONV_A]


# 9/10. Gmail + personal ChatGPT tabs unchanged by a create
def test_uia_personal_tabs_unchanged():
    layer = _Layer([_uia_tab(700, [1], GMAIL, 0, "Gmail"),
                    _uia_tab(700, [2], USER_CONV, 1, "ChatGPT")])
    c, nav = _ctrl(layer)
    c.create_owned_tab(700, workflow_id="wfA", conversation_url=CONV_A)
    assert layer.closed == []
    assert any(t["runtime_id"] == [1] for t in layer.tabs)   # gmail intact
    assert any(t["runtime_id"] == [2] for t in layer.tabs)   # personal chat intact


# 11. persisted runtime_id + correct URL reused
def test_uia_persisted_correct_url_reused():
    layer = _Layer([_uia_tab(700, [5], CONV_A, 0, "ChatGPT")])
    c, nav = _ctrl(layer)
    ident = {"owner": "jarvis", "workflow_id": "wfA", "conversation_url": CONV_A,
             "control_mode": "uia", "host_hwnd": 700, "runtime_id": "5"}
    res = c.resolve_owned_tab(700, workflow_id="wfA", conversation_url=CONV_A,
                              identity=ident)
    assert res["state"] == "reused"


# 12. persisted runtime_id + WRONG url → not reused (treated as not-matching)
def test_uia_persisted_wrong_url_not_reused():
    # The persisted tab now shows a DIFFERENT conversation → it is not a match,
    # and no other tab has CONV_A → missing (caller must create).
    layer = _Layer([_uia_tab(700, [5], CONV_B, 0, "ChatGPT")])
    c, nav = _ctrl(layer)
    ident = {"owner": "jarvis", "workflow_id": "wfA", "conversation_url": CONV_A,
             "control_mode": "uia", "host_hwnd": 700, "runtime_id": "5"}
    res = c.resolve_owned_tab(700, workflow_id="wfA", conversation_url=CONV_A,
                              identity=ident)
    assert res["state"] == "missing"


# 13. stale runtime_id + conversation found on another tab → replaced
def test_uia_stale_runtime_conversation_found():
    layer = _Layer([_uia_tab(700, [8], CONV_A, 0, "ChatGPT")])  # different id
    c, nav = _ctrl(layer)
    stale = {"owner": "jarvis", "workflow_id": "wfA", "conversation_url": CONV_A,
             "control_mode": "uia", "host_hwnd": 700, "runtime_id": "999"}
    res = c.resolve_owned_tab(700, workflow_id="wfA", conversation_url=CONV_A,
                              identity=stale)
    assert res["state"] == "replaced"
    assert res["identity"]["runtime_id"] == "8"


# 14. stale runtime_id + NO conversation tab → missing → create one
def test_uia_stale_runtime_no_conversation_missing():
    layer = _Layer([_uia_tab(700, [1], GMAIL, 0, "Gmail")])
    c, nav = _ctrl(layer)
    stale = {"owner": "jarvis", "workflow_id": "wfA", "conversation_url": CONV_A,
             "control_mode": "uia", "host_hwnd": 700, "runtime_id": "999"}
    res = c.resolve_owned_tab(700, workflow_id="wfA", conversation_url=CONV_A,
                              identity=stale)
    assert res["state"] == "missing"


# 15. ambiguous same-URL tabs → only PROVEN Jarvis-owned closed (manual safe)
def test_uia_ambiguous_manual_copies_not_closed():
    layer = _Layer([_uia_tab(700, [3], CONV_A, 0, "ChatGPT"),
                    _uia_tab(700, [4], CONV_A, 1, "ChatGPT")])
    c, nav = _ctrl(layer)
    # Neither is Jarvis-owned → none closed (both could be manual copies).
    res = c.resolve_owned_tab(700, workflow_id="wfA", conversation_url=CONV_A,
                              identity=None)
    assert res["state"] == "replaced"
    assert res["closed"] == 0
    assert layer.closed == []


# 16. Jarvis frontend identified by URL, not title
def test_identify_jarvis_frontend_by_url():
    layer = _Layer([
        _uia_tab(700, [1], "https://chatgpt.com/c/x", 0, "TeleAutomation"),
        _uia_tab(900, [2], JARVIS, 0, "Jarvis"),   # the frontend, other window
    ])
    c, nav = _ctrl(layer)
    fe = c.identify_jarvis_frontend()
    assert fe is not None
    assert fe["host_hwnd"] == 900
    assert fe["url"].startswith("http://127.0.0.1:8000")


# 17. wrong frontend profile does not affect the workflow automation target
def test_frontend_profile_does_not_change_workflow_target():
    # Frontend in window 900; workflow conversation tab in window 700. Resolving
    # the workflow tab must key on 700, never on the frontend window.
    layer = _Layer([
        _uia_tab(900, [2], JARVIS, 0, "Jarvis"),
        _uia_tab(700, [5], CONV_A, 0, "ChatGPT"),
    ])
    c, nav = _ctrl(layer)
    ident = {"owner": "jarvis", "workflow_id": "wfA", "conversation_url": CONV_A,
             "control_mode": "uia", "host_hwnd": 700, "runtime_id": "5"}
    res = c.resolve_owned_tab(700, workflow_id="wfA", conversation_url=CONV_A,
                              identity=ident)
    assert res["state"] == "reused"
    assert res["identity"]["host_hwnd"] == 700


# ── open_chatgpt_in_new_tab: never touch active/frontend tab ──────────────────

def test_open_new_tab_creates_dedicated_tab_not_active():
    """Bootstrap open creates ONE NEW tab in the verified window and opens
    ChatGPT there — the currently-active tab (e.g. the Jarvis frontend) is not
    navigated or overwritten."""
    frontend = _uia_tab(700, [1], JARVIS, 0, "Jarvis AI Command Center")
    layer = _Layer([frontend])
    c, nav = _ctrl(layer)
    # ChatGPT-root readiness check passes.
    c.ensure_chatgpt_ready = lambda hwnd, allow_login_page=False: "READY"
    res = c.open_chatgpt_in_new_tab(700, workflow_id="wfA")
    ident = res["identity"]
    # A brand-new runtime id was created (not the frontend's [1]).
    assert ident["runtime_id"] != "1"
    assert ident["created_by"] == "jarvis"
    # The Jarvis frontend tab still exists and its URL is unchanged.
    fe = [t for t in layer.tabs if t["runtime_id"] == [1]][0]
    assert fe["raw_url"] == JARVIS
    assert layer.closed == []


def test_open_new_tab_navigates_only_the_new_tab_to_chatgpt():
    frontend = _uia_tab(700, [1], JARVIS, 0, "Jarvis")
    gmail = _uia_tab(700, [2], GMAIL, 1, "Gmail")
    layer = _Layer([frontend, gmail])
    c, nav = _ctrl(layer)
    c.ensure_chatgpt_ready = lambda hwnd, allow_login_page=False: "READY"
    res = c.open_chatgpt_in_new_tab(700, workflow_id="wfA")
    # The new tab's URL is chatgpt.com (fresh chat); frontend + gmail untouched.
    assert "chatgpt.com" in (res["identity"].get("url") or "")
    assert [t for t in layer.tabs if t["runtime_id"] == [1]][0]["raw_url"] == JARVIS
    assert [t for t in layer.tabs if t["runtime_id"] == [2]][0]["raw_url"] == GMAIL


def test_open_new_tab_wrong_profile_fails_before_any_tab():
    """If the selected profile cannot be verified, no tab is created."""
    layer = _Layer([_uia_tab(700, [1], JARVIS, 0, "Jarvis")])
    c, nav = _ctrl(layer)
    def _boom(pk, expected_email=None):
        from jarvis_core.chatgpt_browser_controller import ProfileOwnershipError
        raise ProfileOwnershipError("PROFILE_MISMATCH")
    c.ensure_selected_profile = _boom
    with pytest.raises(Exception):
        c.open_chatgpt_in_new_tab(700, workflow_id="wfA")
    assert layer.created == []            # no tab created on failure
    assert nav == []                       # nothing navigated
