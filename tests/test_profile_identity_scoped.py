"""Browser-chrome-only profile identity tests (Chrome 152, shared master PID).

Proves page/document content can NEVER spoof Chrome profile identity: identity
comes only from the toolbar/avatar (Document subtree pruned) or the Chrome-owned
profile menu popup. Includes the mandatory page-content-spoof regression.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import jarvis_core.chrome_profile_identity as cid


# Trusted Local State metadata (subset of the real machine).
META = {
    "sources": ["C:/UserData"],
    "profiles": [
        {"profile_dir": "Default", "name": "Codetrust", "gaia_name": "Codetrust",
         "user_name": "codetrust2026@gmail.com"},
        {"profile_dir": "Profile 37", "name": "Code", "gaia_name": "Code",
         "user_name": "codetrust2027@gmail.com"},
        {"profile_dir": "Profile 3", "name": "Code25", "gaia_name": "Code25",
         "user_name": "codetrust2025@gmail.com"},
        {"profile_dir": "Profile 7", "name": "Thrilok", "gaia_name": "Thrilok",
         "user_name": "bolluthrilok777@gmail.com"},
    ],
}

ALL_EMAILS = [p["user_name"] for p in META["profiles"]]


# ── _match_identity_from_strings (pure) ───────────────────────────────────────

def test_match_exact_email():
    m = cid._match_identity_from_strings(["codetrust2027@gmail.com"], META)
    assert m["profile_dir"] == "Profile 37"
    assert m["account_email"] == "codetrust2027@gmail.com"


def test_match_email_substring_in_avatar_tooltip():
    m = cid._match_identity_from_strings(
        ["Your Profile (codetrust2026@gmail.com)"], META)
    assert m["profile_dir"] == "Default"


def test_match_none_when_no_known_identity():
    assert cid._match_identity_from_strings(["random text", "New Tab"], META) == {}


# ── Fake UIA tree to test Document pruning ────────────────────────────────────

class _El:
    def __init__(self, ctype, text="", children=None, automation_id=""):
        self._ctype = ctype
        self._text = text
        self._children = children or []
        self._aid = automation_id
        self.element_info = MagicMock()
        self.element_info.control_type = ctype
        self.element_info.automation_id = automation_id

    def window_text(self):
        return self._text

    def children(self):
        return self._children


def _chrome_window_tree(avatar_email, page_emails):
    """A Chrome window: toolbar with the avatar button (real profile) + a web
    Document containing page_emails (which must be IGNORED)."""
    document = _El("Document", "web page", children=[
        _El("Text", e) for e in page_emails
    ])
    toolbar = _El("ToolBar", "", children=[
        _El("Button", f"Your Profile ({avatar_email})", automation_id="avatar"),
    ])
    pane = _El("Pane", "", children=[toolbar, document])
    return _El("Window", "Jarvis AI Command Center - Google Chrome",
               children=[pane])


# MANDATORY regression: page content has EVERY email, avatar is Profile 37.
def test_page_content_cannot_spoof_profile_identity():
    tree = _chrome_window_tree("codetrust2027@gmail.com", ALL_EMAILS)
    with patch("jarvis_core.chrome_profile_identity.Desktop", create=True) as _D, \
         patch.object(cid, "_probe_browser_chrome_strings",
                      wraps=cid._probe_browser_chrome_strings):
        # Drive the real scoped walk against our fake tree.
        with patch("pywinauto.Desktop") as PW:
            win = MagicMock()
            win.window.return_value = tree
            PW.return_value = win
            out = cid.probe_browser_profile_identity(9635246, metadata=META)
    assert out["verified"] is True
    assert out["profile_directory"] == "Profile 37"        # avatar wins
    assert out["account_email"] == "codetrust2027@gmail.com"
    assert out["evidence_source"] == "toolbar_profile_button_email"


def test_scoped_walk_excludes_document_emails():
    tree = _chrome_window_tree("codetrust2027@gmail.com", ALL_EMAILS)
    with patch("pywinauto.Desktop") as PW:
        win = MagicMock()
        win.window.return_value = tree
        PW.return_value = win
        strings = cid._probe_browser_chrome_strings(9635246)
    # The avatar string is present; NONE of the page emails leak in.
    assert any("codetrust2027@gmail.com" in s for s in strings)
    for e in ("codetrust2026@gmail.com", "codetrust2025@gmail.com",
              "bolluthrilok777@gmail.com"):
        assert not any(e in s for s in strings), f"page email leaked: {e}"


def test_unproven_when_no_chrome_evidence():
    # Toolbar has no known identity and the page-menu fallback finds nothing.
    tree = _El("Window", "Some Window - Google Chrome", children=[
        _El("Pane", "", children=[
            _El("ToolBar", "", children=[_El("Button", "Reload")]),
            _El("Document", "", children=[_El("Text", "codetrust2026@gmail.com")]),
        ])
    ])
    with patch("pywinauto.Desktop") as PW:
        win = MagicMock()
        win.window.return_value = tree
        win.descendants.return_value = []      # no avatar button for the menu
        PW.return_value = win
        out = cid.probe_browser_profile_identity(123, metadata=META)
    assert out["verified"] is False
    assert out["evidence_source"] == "PROFILE_IDENTITY_UNPROVEN"


def test_wrong_avatar_email_yields_that_profile_not_requested():
    # If the avatar shows bolluthrilok (Profile 7), identity is Profile 7 — the
    # caller's verify step then rejects it against a requested Default/2026.
    tree = _chrome_window_tree("bolluthrilok777@gmail.com", ALL_EMAILS)
    with patch("pywinauto.Desktop") as PW:
        win = MagicMock()
        win.window.return_value = tree
        PW.return_value = win
        out = cid.probe_browser_profile_identity(12914012, metadata=META)
    assert out["profile_directory"] == "Profile 7"
    assert out["account_email"] == "bolluthrilok777@gmail.com"


# ── Controller verify_profile_open uses browser-chrome identity only ──────────

from jarvis_core.chrome_profile_controller import ChromeProfileController


def _controller():
    # Real controller with metadata stubbed; host windows + probe are patched
    # per test. No window_identity_fn injection → exercises the REAL scoped path.
    c = ChromeProfileController.__new__(ChromeProfileController)
    # Minimal init of the fields verify_profile_open touches.
    c._metadata_fn = lambda: META
    c._host_windows_fn = lambda: [{"hwnd": 9635246, "pid": 28976},
                                  {"hwnd": 12914012, "pid": 28976}]
    c._process_profile_fn = lambda: {}      # shared master PID: NO --profile-directory
    c._window_identity_fn = c._default_window_identity
    c._window_email_fn = c._default_window_email
    c._profiles_cache = None
    return c


def _patch_probe(mapping):
    """Patch probe_browser_profile_identity to return a fixed identity per hwnd
    (simulating browser-chrome attestation), ignoring any page content."""
    def _fake(hwnd, metadata=None):
        m = mapping.get(int(hwnd))
        if not m:
            return {"hwnd": hwnd, "profile_directory": None, "account_email": None,
                    "profile_name": None, "evidence_source": "PROFILE_IDENTITY_UNPROVEN",
                    "verified": False}
        return {"hwnd": hwnd, "profile_directory": m[0], "account_email": m[1],
                "profile_name": "x", "evidence_source": "toolbar_profile_button",
                "verified": True}
    return patch.object(cid, "probe_browser_profile_identity", _fake)


def test_verify_rejects_window_whose_chrome_identity_is_another_profile():
    c = _controller()
    # HWND 9635246 is really Profile 37 by browser chrome (even though its PAGE
    # shows codetrust2026). verify for Default/2026 must FAIL.
    with _patch_probe({9635246: ("Profile 37", "codetrust2027@gmail.com")}):
        v = c.verify_profile_open("chrome-profile:Default", 9635246,
                                  expected_email="codetrust2026@gmail.com",
                                  require_email=True)
    assert v["verified"] is False
    assert v["reason"] in ("DIRECTORY_MISMATCH", "EMAIL_MISMATCH")


def test_verify_accepts_only_true_chrome_profile():
    c = _controller()
    with _patch_probe({12914012: ("Default", "codetrust2026@gmail.com")}):
        v = c.verify_profile_open("chrome-profile:Default", 12914012,
                                  expected_email="codetrust2026@gmail.com",
                                  require_email=True)
    assert v["verified"] is True
    assert v["observed"] == "chrome-profile:Default"


def test_verify_fail_closed_when_identity_unproven():
    c = _controller()
    with _patch_probe({}):     # no browser-chrome evidence for any hwnd
        v = c.verify_profile_open("chrome-profile:Default", 9635246,
                                  expected_email="codetrust2026@gmail.com",
                                  require_email=True)
    assert v["verified"] is False


# ── Email disambiguation (shared display name "Code") ─────────────────────────

# Metadata where THREE profiles share the display name "Code".
META_SHARED = {
    "sources": ["C:/UserData"],
    "profiles": [
        {"profile_dir": "Default", "name": "Code", "gaia_name": "Code",
         "user_name": "codetrust2026@gmail.com"},
        {"profile_dir": "Profile 3", "name": "Code", "gaia_name": "Code",
         "user_name": "codetrust2025@gmail.com"},
        {"profile_dir": "Profile 37", "name": "Code", "gaia_name": "Code",
         "user_name": "codetrust2027@gmail.com"},
    ],
}
SHARED_EMAILS = [p["user_name"] for p in META_SHARED["profiles"]]


def test_ambiguous_display_name_rejected_no_first_match():
    # Only the shared name "Code" is present (no email) → AMBIGUOUS, never a pick.
    m = cid._match_identity_from_strings(
        ["Waiting for Kiro Report - Google Chrome - Code"], META_SHARED)
    assert m.get("ambiguous") is True
    assert "profile_dir" not in m


def test_names_shared_by_multiple_detects_code():
    shared = cid._names_shared_by_multiple(META_SHARED)
    assert "code" in shared


def test_directory_for_email_maps_uniquely():
    assert cid._directory_for_email("codetrust2026@gmail.com", META_SHARED) == "Default"
    assert cid._directory_for_email("codetrust2027@gmail.com", META_SHARED) == "Profile 37"
    assert cid._directory_for_email("unknown@gmail.com", META_SHARED) is None


def _win_with_avatar_email(avatar_email, page_emails, title_name="Code"):
    document = _El("Document", "web page",
                   children=[_El("Text", e) for e in page_emails])
    toolbar = _El("ToolBar", "", children=[
        _El("Button", f"Your Profile ({avatar_email})", automation_id="avatar")])
    pane = _El("Pane", "", children=[toolbar, document])
    return _El("Window", f"App - Google Chrome - {title_name}", children=[pane])


def _probe(hwnd, tree, meta):
    with patch("pywinauto.Desktop") as PW:
        win = MagicMock()
        win.window.return_value = tree
        win.descendants.return_value = []
        PW.return_value = win
        return cid.probe_browser_profile_identity(hwnd, metadata=meta)


def test_email_disambiguates_2026():
    # Shared name "Code" but avatar exposes exact 2026 → Default.
    tree = _win_with_avatar_email("codetrust2026@gmail.com", SHARED_EMAILS)
    out = _probe(1, tree, META_SHARED)
    assert out["verified"] is True
    assert out["profile_directory"] == "Default"
    assert out["account_email"] == "codetrust2026@gmail.com"
    assert out["evidence_source"] == "toolbar_profile_button_email"


def test_email_disambiguates_2027():
    tree = _win_with_avatar_email("codetrust2027@gmail.com", SHARED_EMAILS)
    out = _probe(2, tree, META_SHARED)
    assert out["profile_directory"] == "Profile 37"
    assert out["account_email"] == "codetrust2027@gmail.com"


def test_email_disambiguates_2025():
    tree = _win_with_avatar_email("codetrust2025@gmail.com", SHARED_EMAILS)
    out = _probe(3, tree, META_SHARED)
    assert out["profile_directory"] == "Profile 3"
    assert out["account_email"] == "codetrust2025@gmail.com"


def test_wrong_page_email_ignored_avatar_wins():
    # Avatar = 2027; page lists 2026 (and all others). Must stay Profile 37.
    tree = _win_with_avatar_email("codetrust2027@gmail.com",
                                  ["codetrust2026@gmail.com"] + SHARED_EMAILS)
    out = _probe(4, tree, META_SHARED)
    assert out["profile_directory"] == "Profile 37"
    assert out["account_email"] == "codetrust2027@gmail.com"


def test_avatar_email_absent_shared_name_fails_closed():
    # No email anywhere in browser chrome, only the shared name "Code" on frame,
    # page has emails (ignored), menu unavailable → UNPROVEN.
    document = _El("Document", "", children=[_El("Text", e) for e in SHARED_EMAILS])
    toolbar = _El("ToolBar", "", children=[_El("Button", "Reload")])
    tree = _El("Window", "App - Google Chrome - Code",
               children=[_El("Pane", "", children=[toolbar, document])])
    out = _probe(5, tree, META_SHARED)
    assert out["verified"] is False
    assert out["evidence_source"] == "PROFILE_IDENTITY_UNPROVEN"


def test_chatgpt_message_emails_cannot_spoof():
    # A ChatGPT page (Document) full of emails; avatar is 2027.
    tree = _win_with_avatar_email(
        "codetrust2027@gmail.com",
        ["codetrust2026@gmail.com", "bolluthrilok777@gmail.com",
         "codetrust2025@gmail.com"])
    out = _probe(6, tree, META_SHARED)
    assert out["profile_directory"] == "Profile 37"
