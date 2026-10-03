"""Fix B/C tab-lifecycle tests — REAL Jarvis-owned ChatGPT tab identity,
canonical/missing/stale recovery, duplicate cleanup, cross-workflow isolation,
same-conversation-after-replacement, and browser-level Copy-once.

These are pure unit tests of the tab-ownership logic in
``ChatGPTBrowserController`` and the response-identity Copy-once latch in the
approval service. All browser side effects are injected — no real Chrome.
"""
from __future__ import annotations

import pytest

from jarvis_core.chatgpt_browser_controller import (
    ChatGPTBrowserController,
    TabIdentityUnavailableError,
)


CONV_A = "https://chatgpt.com/c/aaaa-1111"
CONV_B = "https://chatgpt.com/c/bbbb-2222"
USER_CONV = "https://chatgpt.com/c/user-9999"


def _cdp_tab(target_id, url, **kw):
    d = {"control_mode": "cdp", "target_id": target_id, "raw_url": url}
    d.update(kw)
    return d


def _uia_tab(host_hwnd, runtime_id, url, tab_index=0, title="ChatGPT", **kw):
    d = {"control_mode": "uia", "host_hwnd": host_hwnd,
         "runtime_id": list(runtime_id), "raw_url": url, "tab_index": tab_index,
         "title": title}
    d.update(kw)
    return d


class FakeTabLayer:
    """Records focus/close and serves a scripted discover_tabs result."""

    def __init__(self, tabs, *, cdp_url="http://127.0.0.1:9222", controllable=True):
        self.tabs = list(tabs)
        self.cdp_url = cdp_url
        self.controllable = controllable
        self.focused = []
        self.closed = []

    def discover(self):
        return {"tabs": list(self.tabs), "cdp_url": self.cdp_url,
                "controllable": self.controllable, "chrome_detected": True}

    def focus_cdp(self, cdp_url, target_id):
        self.focused.append(("cdp", target_id))
        return True

    def close_cdp(self, cdp_url, target_id):
        self.closed.append(("cdp", target_id))
        self.tabs = [t for t in self.tabs if (t.get("target_id") != target_id)]
        return True

    def focus_uia(self, host_hwnd, runtime_id, title, tab_index):
        self.focused.append(("uia", host_hwnd, tuple(runtime_id)))
        return True

    def close_uia(self, host_hwnd, runtime_id, title, tab_index):
        self.closed.append(("uia", host_hwnd, tuple(runtime_id)))
        rid = list(runtime_id)
        self.tabs = [t for t in self.tabs
                     if not (t.get("host_hwnd") == host_hwnd
                             and list(t.get("runtime_id") or []) == rid)]
        return {"ok": True, "verified": True}

    def create_cdp(self, cdp_url, url):
        # Simulate Chrome creating a brand-new tab with a fresh target id.
        tid = f"T-NEW-{len(self.tabs)}"
        tab = _cdp_tab(tid, url)
        self.tabs.append(tab)
        self.created = getattr(self, "created", [])
        self.created.append(tid)
        return {"target_id": tid, "raw_url": url, "control_mode": "cdp"}

    def create_uia(self, host_hwnd):
        rid = [50 + len(self.tabs)]
        idx = len(self.tabs)
        # New UIA tab starts blank; the controller navigates it and then reads
        # the URL back — model that by storing a pending target the reader
        # returns once navigation has occurred (see nav stub in _controller).
        tab = _uia_tab(host_hwnd, rid, "about:blank", tab_index=idx, title="New Tab")
        self.tabs.append(tab)
        self.created = getattr(self, "created", [])
        self.created.append(tuple(rid))
        return {"ok": True, "host_hwnd": host_hwnd, "runtime_id": rid,
                "tab_index": idx, "title": "New Tab", "control_mode": "uia"}

    def read_uia_url(self, host_hwnd, runtime_id, *, title="", tab_index=-1):
        # Return the raw_url the layer holds for the exact runtime_id tab.
        rid = list(runtime_id)
        for t in self.tabs:
            if (t.get("control_mode") == "uia"
                    and t.get("host_hwnd") == host_hwnd
                    and list(t.get("runtime_id") or []) == rid):
                url = t.get("raw_url") or ""
                if url and url != "about:blank":
                    return {"ok": True, "url": url, "code": "", "reason": ""}
                return {"ok": False, "url": "", "code": "TAB_URL_UNAVAILABLE",
                        "reason": "blank"}
        return {"ok": False, "url": "", "code": "TAB_IDENTITY_STALE",
                "reason": "gone"}


def _controller(layer, *, navigated=None):
    """Controller wired to the fake tab layer. Profile verify + navigation +
    URL capture are stubbed so no real browser is touched."""
    navigated = navigated if navigated is not None else []

    ctrl = ChatGPTBrowserController(
        focus_verify_fn=lambda pk, expected_email=None: {"hwnd": 555, "verified": True},
        discover_tabs_fn=layer.discover,
        focus_tab_cdp_fn=layer.focus_cdp,
        close_tab_cdp_fn=layer.close_cdp,
        focus_tab_uia_fn=layer.focus_uia,
        close_tab_uia_fn=layer.close_uia,
        create_tab_cdp_fn=layer.create_cdp,
        create_tab_uia_fn=layer.create_uia,
        read_tab_url_uia_fn=layer.read_uia_url,
    )
    # Stub navigation + readiness + URL capture (return the conversation we were
    # told to navigate to, mirroring a real address bar after navigation).
    def _nav(hwnd, url):
        navigated.append(url)
        # Mirror real navigation: the currently-active/last-created UIA tab in
        # this window now points at url (so read-back sees the real URL).
        uia = [t for t in layer.tabs
               if t.get("control_mode") == "uia" and t.get("host_hwnd") == hwnd]
        if uia:
            uia[-1]["raw_url"] = url
    ctrl._navigate = _nav  # type: ignore
    ctrl.ensure_chatgpt_ready = lambda hwnd, allow_login_page=False: "READY"  # type: ignore
    ctrl._require_foreground = lambda hwnd: None  # type: ignore
    ctrl._nav_log = navigated
    return ctrl


# 1. establish → exactly one real tab identity per workflow
def test_establish_owned_tab_records_real_cdp_identity():
    layer = FakeTabLayer([_cdp_tab("T-A", CONV_A), _cdp_tab("T-user", USER_CONV)])
    ctrl = _controller(layer)
    res = ctrl.establish_owned_tab(555, workflow_id="wfA", conversation_url=CONV_A)
    ident = res["identity"]
    assert ident is not None
    assert ident["control_mode"] == "cdp"
    assert ident["target_id"] == "T-A"
    assert ident["workflow_id"] == "wfA"
    assert ident["conversation_url"] == CONV_A
    assert res["closed"] == 0            # user tab untouched
    assert layer.closed == []


# 2. unrelated personal ChatGPT tabs are never touched
def test_personal_tabs_untouched():
    layer = FakeTabLayer([
        _cdp_tab("T-user1", USER_CONV),
        _cdp_tab("T-user2", "https://chatgpt.com/c/other-3333"),
        _cdp_tab("T-A", CONV_A),
    ])
    ctrl = _controller(layer)
    ctrl.establish_owned_tab(555, workflow_id="wfA", conversation_url=CONV_A)
    assert layer.closed == []            # nothing closed


# 3. duplicate cleanup only closes PROVEN Jarvis-owned duplicates
def test_manual_same_conversation_copy_not_closed():
    """Two tabs on the SAME conversation, neither created by Jarvis (e.g. the
    user manually opened a second copy) → NOTHING is closed. Same URL alone is
    NOT ownership."""
    layer = FakeTabLayer([
        _cdp_tab("T-A1", CONV_A),
        _cdp_tab("T-A2", CONV_A),       # manual copy — must NOT be closed
        _cdp_tab("T-user", USER_CONV),
    ])
    ctrl = _controller(layer)
    res = ctrl.establish_owned_tab(555, workflow_id="wfA", conversation_url=CONV_A)
    assert res["closed"] == 0            # neither is Jarvis-owned → none closed
    assert layer.closed == []
    assert res["identity"]["target_id"] == "T-A1"   # canonical still chosen


def test_only_proven_jarvis_duplicate_closed():
    """When Jarvis created BOTH same-conversation tabs (positive ownership), the
    duplicate is closed and exactly one canonical remains."""
    layer = FakeTabLayer([_cdp_tab("T-user", USER_CONV)])
    ctrl = _controller(layer)
    # Jarvis creates two tabs for the SAME workflow/conversation (register both).
    c1 = ctrl.create_owned_tab(555, workflow_id="wfA", conversation_url=CONV_A)
    c2 = ctrl.create_owned_tab(555, workflow_id="wfA", conversation_url=CONV_A)
    assert c1["identity"]["target_id"] != c2["identity"]["target_id"]
    # Now resolve: both are Jarvis-owned duplicates → one closed, one canonical.
    res = ctrl.resolve_owned_tab(555, workflow_id="wfA", conversation_url=CONV_A,
                                 identity=c1["identity"])
    assert res["state"] == "replaced"
    assert res["closed"] == 1
    assert len(layer.closed) == 1


# 4. canonical persisted tab still present → reused (no navigation churn)
def test_resolve_reuses_canonical_tab():
    layer = FakeTabLayer([_cdp_tab("T-A", CONV_A)])
    ctrl = _controller(layer)
    identity = {"owner": "jarvis", "workflow_id": "wfA", "conversation_url": CONV_A,
                "control_mode": "cdp", "target_id": "T-A"}
    res = ctrl.resolve_owned_tab(555, workflow_id="wfA", conversation_url=CONV_A,
                                 identity=identity)
    assert res["state"] == "reused"
    assert res["identity"]["target_id"] == "T-A"


# 5. persisted tab missing → caller must create exactly one replacement
def test_resolve_missing_tab():
    layer = FakeTabLayer([_cdp_tab("T-user", USER_CONV)])  # our conv absent
    ctrl = _controller(layer)
    identity = {"owner": "jarvis", "workflow_id": "wfA", "conversation_url": CONV_A,
                "control_mode": "cdp", "target_id": "T-GONE"}
    res = ctrl.resolve_owned_tab(555, workflow_id="wfA", conversation_url=CONV_A,
                                 identity=identity)
    assert res["state"] == "missing"


# 6. stale/recycled identity but same-conversation tab exists → replaced
def test_resolve_stale_identity_replaced():
    layer = FakeTabLayer([_cdp_tab("T-A-NEW", CONV_A)])  # new target id
    ctrl = _controller(layer)
    stale = {"owner": "jarvis", "workflow_id": "wfA", "conversation_url": CONV_A,
             "control_mode": "cdp", "target_id": "T-A-OLD"}
    res = ctrl.resolve_owned_tab(555, workflow_id="wfA", conversation_url=CONV_A,
                                 identity=stale)
    assert res["state"] == "replaced"
    assert res["identity"]["target_id"] == "T-A-NEW"


# 7. another workflow's identity is never adopted
def test_resolve_cross_workflow_rejected():
    layer = FakeTabLayer([_cdp_tab("T-B", CONV_B)])
    ctrl = _controller(layer)
    other = {"owner": "jarvis", "workflow_id": "wfB", "conversation_url": CONV_B,
             "control_mode": "cdp", "target_id": "T-B"}
    # wfA tries to resolve using wfB's identity → rejected
    res = ctrl.resolve_owned_tab(555, workflow_id="wfA", conversation_url=CONV_A,
                                 identity=other)
    assert res["state"] == "cross_workflow_rejected"
    assert res["identity"] is None


# 7b. workflow A tab != workflow B tab (distinct conversations → distinct ids)
def test_distinct_workflows_distinct_tabs():
    layer = FakeTabLayer([_cdp_tab("T-A", CONV_A), _cdp_tab("T-B", CONV_B)])
    ctrl = _controller(layer)
    a = ctrl.establish_owned_tab(555, workflow_id="wfA", conversation_url=CONV_A)
    b = ctrl.establish_owned_tab(555, workflow_id="wfB", conversation_url=CONV_B)
    assert a["identity"]["target_id"] != b["identity"]["target_id"]
    assert a["identity"]["workflow_id"] == "wfA"
    assert b["identity"]["workflow_id"] == "wfB"


# 8. missing canonical tab → CREATE a real new tab on the SAME conversation
def test_restore_owned_missing_creates_new_real_tab_same_conversation():
    layer = FakeTabLayer([
        _cdp_tab("T-gmail", "https://mail.google.com/"),
        _cdp_tab("T-userchat", USER_CONV),   # user's personal ChatGPT
    ])
    ctrl = _controller(layer)
    ctrl.capture_conversation_url = lambda hwnd: CONV_A  # type: ignore
    stale = {"owner": "jarvis", "workflow_id": "wfA", "conversation_url": CONV_A,
             "control_mode": "cdp", "target_id": "T-GONE"}
    res = ctrl.restore_owned_conversation(
        "chrome-profile:Default", CONV_A, workflow_id="wfA", identity=stale)
    assert res["tab_state"] == "missing"
    assert res["same"] is True                        # SAME conversation
    # A NEW real tab was created (CDP create returned a fresh target id).
    assert getattr(layer, "created", [])              # create_tab was used
    assert res["identity"]["target_id"] in layer.created
    assert res["identity"]["created_by"] == "jarvis"
    # Personal tabs untouched.
    assert layer.closed == []
    assert any(t.get("target_id") == "T-gmail" for t in layer.tabs)
    assert any(t.get("target_id") == "T-userchat" for t in layer.tabs)


# 8b. restore reuses existing owned tab without creating a new conversation
def test_restore_owned_reuse_no_new_conversation():
    layer = FakeTabLayer([_cdp_tab("T-A", CONV_A)])
    nav = []
    ctrl = _controller(layer, navigated=nav)
    ctrl.capture_conversation_url = lambda hwnd: CONV_A  # type: ignore
    identity = {"owner": "jarvis", "workflow_id": "wfA", "conversation_url": CONV_A,
                "control_mode": "cdp", "target_id": "T-A"}
    res = ctrl.restore_owned_conversation(
        "chrome-profile:Default", CONV_A, workflow_id="wfA", identity=identity)
    assert res["tab_state"] == "reused"
    assert res["same"] is True
    # focused the exact owned tab; navigation still targets the SAME conv only
    assert ("cdp", "T-A") in layer.focused
    assert nav == [CONV_A]


# B1: no CDP/UIA identity anywhere → FAIL CLOSED (no window+URL fallback)
def test_establish_fails_closed_when_no_identity():
    # The conversation tab exists but has NO controllable identity fields.
    bad = {"control_mode": "", "raw_url": CONV_A}     # no target_id, no runtime_id
    layer = FakeTabLayer([bad])
    ctrl = _controller(layer)
    with pytest.raises(TabIdentityUnavailableError):
        ctrl.establish_owned_tab(555, workflow_id="wfA", conversation_url=CONV_A)


def test_create_owned_tab_fails_closed_when_uncreatable():
    # No CDP endpoint (cdp_url empty) and UIA create returns not-ok → fail closed.
    layer = FakeTabLayer([], cdp_url="")
    ctrl = _controller(layer)
    ctrl._create_tab_uia_fn = lambda hwnd: {"ok": False, "reason": "no strip"}
    with pytest.raises(TabIdentityUnavailableError):
        ctrl.create_owned_tab(555, workflow_id="wfA", conversation_url=CONV_A)


# UIA duplicate cleanup uses exact host_hwnd + runtime_id (Jarvis-created dups)
def test_uia_duplicate_cleanup_exact_identity():
    layer = FakeTabLayer([_uia_tab(700, [9, 9], USER_CONV, tab_index=0)], cdp_url="")
    ctrl = _controller(layer)
    # Jarvis creates two UIA tabs for the SAME conversation (both owned).
    ctrl.capture_conversation_url = lambda hwnd: CONV_A  # type: ignore
    c1 = ctrl.create_owned_tab(700, workflow_id="wfA", conversation_url=CONV_A)
    c2 = ctrl.create_owned_tab(700, workflow_id="wfA", conversation_url=CONV_A)
    # Point both created UIA tabs at CONV_A so they are same-conversation dups.
    for t in layer.tabs:
        if t.get("control_mode") == "uia" and t.get("raw_url") == "about:blank":
            t["raw_url"] = CONV_A
    res = ctrl.resolve_owned_tab(700, workflow_id="wfA", conversation_url=CONV_A,
                                 identity=c1["identity"])
    assert res["state"] == "replaced"
    assert res["closed"] == 1               # exactly one Jarvis-owned dup closed
    assert len(layer.closed) == 1
    # The user's tab (different URL) is never closed.
    assert all(c[0] == "uia" for c in layer.closed)


# ── B3: REAL GPT response identity (content-based) ────────────────────────────

def test_response_identity_same_content_same_index():
    layer = FakeTabLayer([_cdp_tab("T-A", CONV_A)])
    ctrl = _controller(layer)
    a = ctrl.compute_response_identity(CONV_A, assistant_index=0, content="R ONE")
    b = ctrl.compute_response_identity(CONV_A, assistant_index=0, content="R ONE")
    assert a == b            # same response observed twice → SAME identity


def test_response_identity_regeneration_same_iteration_differs():
    """R1 then a REGENERATED R2 at the same assistant index (same iteration,
    same conversation) → DIFFERENT identity (content differs)."""
    layer = FakeTabLayer([_cdp_tab("T-A", CONV_A)])
    ctrl = _controller(layer)
    r1 = ctrl.compute_response_identity(CONV_A, assistant_index=0, content="R ONE")
    r2 = ctrl.compute_response_identity(CONV_A, assistant_index=0, content="R TWO regenerated")
    assert r1 != r2


def test_response_identity_ignores_edge_whitespace_only():
    """Trailing whitespace / surrounding blank lines don't change identity, but
    real content changes do."""
    layer = FakeTabLayer([_cdp_tab("T-A", CONV_A)])
    ctrl = _controller(layer)
    a = ctrl.compute_response_identity(CONV_A, assistant_index=1, content="hello world")
    b = ctrl.compute_response_identity(CONV_A, assistant_index=1, content="hello world   \n\n")
    c = ctrl.compute_response_identity(CONV_A, assistant_index=1, content="hello  world")
    assert a == b            # only edge whitespace differs → same
    assert a != c            # internal content differs → different


def test_response_identity_different_conversation_differs():
    layer = FakeTabLayer([_cdp_tab("T-A", CONV_A)])
    ctrl = _controller(layer)
    a = ctrl.compute_response_identity(CONV_A, assistant_index=0, content="same")
    b = ctrl.compute_response_identity(CONV_B, assistant_index=0, content="same")
    assert a != b
