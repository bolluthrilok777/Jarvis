"""Discovery source-of-truth regression tests (proven from live scan).

Root causes proven live and fixed here:
1. msedgewebview2.exe (WebView2 runtime host) was listed as a separate
   "application" and duplicated WhatsApp -> now filtered out in
   windows_resource_manager.enumerate_windows.
2. "Whatsapp.Root" ugly display name -> mapped to "WhatsApp".
3. Chrome per-window identity groups correctly by proven account_email
   (never merges different profiles; aggregates same profile across windows).
4. Chrome account-token resolution survives a generation drift (refresh must
   not report a still-open account as "no longer open").
"""
from __future__ import annotations

from jarvis_core.resource_menus import (
    chrome_profiles,
    resolve_chrome_account,
    resolve_chrome_account_by_key,
)


# ── 1/2. Application discovery: WebView2 helper filtered, WhatsApp named ──────


def test_webview2_helper_is_filtered_out(monkeypatch):
    import jarvis_core.windows_resource_manager as wrm

    windows = [
        # (hwnd, title, class, pid, process_name, executable)
        (111, "WhatsApp", "WinUIDesktopWin32WindowClass", 10,
         "WhatsApp.Root.exe",
         r"C:\Program Files\WindowsApps\WhatsAppDesktop\WhatsApp.Root.exe"),
        (222, "(2) WhatsApp", "Chrome_WidgetWin_1", 20,
         "msedgewebview2.exe",
         r"C:\Program Files (x86)\Microsoft\EdgeWebView\msedgewebview2.exe"),
        (333, "Agent Focus", "Chrome_WidgetWin_1", 30,
         "Kiro.exe", r"C:\Users\x\Kiro\Kiro.exe"),
    ]
    _install_fake_enum(monkeypatch, wrm, windows)

    names = [w["display_name"] for w in wrm.enumerate_windows()]
    procs = [w["process_name"].lower() for w in wrm.enumerate_windows()]
    assert "msedgewebview2.exe" not in procs, "WebView2 helper must be excluded"
    # WhatsApp appears exactly once (host only), named cleanly.
    assert names.count("WhatsApp") == 1
    assert "Kiro" in names
    assert not any("Msedgewebview2" in n or "Webview2" in n for n in names)


def test_whatsapp_and_kiro_friendly_names():
    import jarvis_core.windows_resource_manager as wrm
    assert wrm._friendly_name("WhatsApp.Root.exe", "WhatsApp", "X") == "WhatsApp"
    assert wrm._friendly_name("Kiro.exe", "Agent Focus", "X") == "Kiro"


def _install_fake_enum(monkeypatch, wrm, windows):
    """Drive enumerate_windows against a fake Win32 layer so the real helper
    filter / friendly-name logic runs on synthetic windows."""
    class _FakeUser32:
        def IsWindowVisible(self, h): return True
        def GetWindow(self, h, flag): return 0  # no owner
        def GetForegroundWindow(self): return 0
        def GetWindowThreadProcessId(self, h, pid_ptr):
            pid_ptr._obj.value = _PID_BY_HWND.get(int(h), 0)
            return 0

        def EnumWindows(self, proc, lparam):
            for hwnd, *_ in windows:
                proc(hwnd, 0)
            return True

    _PID_BY_HWND = {w[0]: w[3] for w in windows}
    _META = {w[0]: w for w in windows}

    monkeypatch.setattr(wrm, "user32", _FakeUser32())
    monkeypatch.setattr(wrm, "_window_text", lambda h: _META[int(h)][1])
    monkeypatch.setattr(wrm, "_window_class", lambda h: _META[int(h)][2])
    monkeypatch.setattr(wrm, "_is_cloaked", lambda h: False)
    monkeypatch.setattr(wrm, "_is_protected", lambda t: False)
    monkeypatch.setattr(
        wrm, "_process_info",
        lambda pid: {
            "process_name": next(w[4] for w in windows if w[3] == pid),
            "executable": next(w[5] for w in windows if w[3] == pid),
        })


# ── 3. Chrome per-window identity grouping (never merge different profiles) ──


def _tab(rid, title, email, hwnd):
    return {"resource_id": rid, "title": title, "account_email": email,
            "host_hwnd": hwnd, "tab_index": 0}


def test_different_profiles_never_merge_same_profile_aggregates():
    scan = {
        "generation": 1,
        "chrome_detected": True,
        "applications": [],
        "tabs": [
            _tab("r1-t1", "Tele1", "codetrust2026@gmail.com", 111),
            _tab("r1-t2", "Tele2", "codetrust2026@gmail.com", 111),
            _tab("r1-t3", "Jarvis", "codetrust2025@gmail.com", 222),
            _tab("r1-t4", "Naukri", "bhargavraghuvanshi5@gmail.com", 333),
            # Same account (codetrust2026) across a SECOND window -> aggregate.
            _tab("r1-t5", "Tele3", "codetrust2026@gmail.com", 444),
        ],
    }
    groups = {p["label"]: p["count"] for p in chrome_profiles(scan)}
    assert groups == {
        "codetrust2026@gmail.com": 3,   # 111 + 444 aggregated
        "codetrust2025@gmail.com": 1,
        "bhargavraghuvanshi5@gmail.com": 1,
    }
    total = len(scan["tabs"])
    assert sum(g for g in groups.values()) == total == 5


def test_chrome_total_equals_unique_tabs_and_sum():
    scan = {
        "generation": 1, "chrome_detected": True, "applications": [],
        "tabs": [
            _tab("r1-t1", "A", "a@gmail.com", 111),
            _tab("r1-t2", "B", "b@gmail.com", 222),
            _tab("r1-t3", "C", "", 333),  # unknown identity
        ],
    }
    profs = chrome_profiles(scan)
    assert sum(p["count"] for p in profs) == len(scan["tabs"])
    assert any(p["is_unknown"] and p["count"] == 1 for p in profs)


# ── 4. Refresh token drift: still-open account is not reported gone ──────────


def test_account_token_resolves_after_generation_drift():
    scan_g1 = {
        "generation": 1, "chrome_detected": True, "applications": [],
        "tabs": [
            _tab("r1-t1", "A", "a@gmail.com", 111),
            _tab("r1-t2", "B", "b@gmail.com", 222),
        ],
    }
    profs1 = chrome_profiles(scan_g1)  # mints g1 tokens + records stable keys
    tok = profs1[0]["token"]           # g1-p1 -> a@gmail.com (alphabetical)

    # A later generation (same accounts still open) issues NEW tokens (g2-*).
    scan_g2 = {
        "generation": 2, "chrome_detected": True, "applications": [],
        "tabs": [
            _tab("r2-t1", "A", "a@gmail.com", 111),
            _tab("r2-t2", "B", "b@gmail.com", 222),
        ],
    }
    # The OLD token resolves via its STABLE identity key (registry), not order.
    matched = resolve_chrome_account(scan_g2, tok)
    assert matched is not None
    assert matched["label"] == "a@gmail.com"


def test_reordered_accounts_do_not_redirect_stale_token():
    """Requirement #9: a stale token must NEVER map to a different profile just
    because the account list order changed."""
    scan_g1 = {
        "generation": 1, "chrome_detected": True, "applications": [],
        "tabs": [
            _tab("r1-t1", "X", "aaa@gmail.com", 111),
            _tab("r1-t2", "Y", "bbb@gmail.com", 222),
        ],
    }
    profs1 = chrome_profiles(scan_g1)
    tok_aaa = next(p["token"] for p in profs1 if p["label"] == "aaa@gmail.com")

    # New generation where aaa is CLOSED and a new account 'ccc' opened, so the
    # ordinal position that aaa held is now a DIFFERENT profile.
    scan_g2 = {
        "generation": 2, "chrome_detected": True, "applications": [],
        "tabs": [
            _tab("r2-t1", "Z", "bbb@gmail.com", 222),
            _tab("r2-t2", "W", "ccc@gmail.com", 333),
        ],
    }
    # aaa genuinely disappeared -> None (return to account list), and it must
    # NEVER resolve to bbb/ccc that now hold that ordinal position.
    matched = resolve_chrome_account(scan_g2, tok_aaa)
    assert matched is None, "stale token must not redirect to another profile"


def test_removed_profile_returns_none():
    scan_g1 = {
        "generation": 1, "chrome_detected": True, "applications": [],
        "tabs": [_tab("r1-t1", "A", "gone@gmail.com", 111)],
    }
    tok = chrome_profiles(scan_g1)[0]["token"]
    scan_g2 = {
        "generation": 2, "chrome_detected": True, "applications": [],
        "tabs": [_tab("r2-t1", "B", "other@gmail.com", 222)],
    }
    assert resolve_chrome_account(scan_g2, tok) is None


# ── Chrome tab discovery reliability (retry + last-known-good + title clean) ──


def test_memory_usage_suffix_stripped_from_title():
    import jarvis_core.resource_manager as rm
    assert rm._clean_tab_title("TeleAutomation - Memory usage - 55.0 MB") == "TeleAutomation"
    assert rm._clean_tab_title("Docs \u2014 Memory usage \u2014 330 MB") == "Docs"
    # Identity-bearing / normal titles are untouched.
    assert rm._clean_tab_title("Gmail - Inbox") == "Gmail - Inbox"
    assert rm._clean_tab_title("Memory Bank App") == "Memory Bank App"


def test_browser_windows_excluded_from_applications_but_enumerated():
    """chrome.exe stays in enumerate_windows (source for tab discovery) but is
    filtered OUT of the Applications list in ResourceRegistry.scan."""
    import jarvis_core.windows_resource_manager as wrm
    import jarvis_core.resource_manager as rm

    fake_windows = [
        {"hwnd": 111, "title": "Naukri - Google Chrome", "class_name": "Chrome_WidgetWin_1",
         "pid": 10, "process_name": "chrome.exe",
         "executable": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
         "display_name": "Chrome", "visible": True, "focused": False, "protected": False},
        {"hwnd": 222, "title": "WhatsApp", "class_name": "WinUIDesktopWin32WindowClass",
         "pid": 20, "process_name": "WhatsApp.Root.exe",
         "executable": r"C:\WindowsApps\WhatsApp.Root.exe",
         "display_name": "WhatsApp", "visible": True, "focused": False, "protected": False},
        {"hwnd": 333, "title": "Agent Focus", "class_name": "Chrome_WidgetWin_1",
         "pid": 30, "process_name": "Kiro.exe", "executable": r"C:\Kiro\Kiro.exe",
         "display_name": "Kiro", "visible": True, "focused": False, "protected": False},
    ]
    import jarvis_core.browser_resource_manager as brm

    monkeypatch = _Patcher()
    try:
        monkeypatch.setattr(wrm, "enumerate_windows", lambda: list(fake_windows))
        # No Chrome tabs discoverable in this unit context.
        monkeypatch.setattr(brm, "discover_tabs",
                            lambda cdp_url=None: {"chrome_detected": False,
                                                  "controllable": False,
                                                  "tabs": [], "cdp_url": None})
        monkeypatch.setattr(
            __import__("jarvis_core.uia_browser_manager", fromlist=["x"]),
            "chrome_host_windows", lambda: [])
        reg = rm.ResourceRegistry()
        snap = reg.scan(include_identity=True, read_only=True)
        apps = snap.get("applications") or []
        names = [x.get("display_name") for x in apps]
        assert "Chrome" not in names, "browser window must NOT be an Application"
        assert "WhatsApp" in names and "Kiro" in names
        # But enumerate_windows itself still contains the chrome window
        # (source for chrome_host_windows).
        assert any(w["process_name"] == "chrome.exe" for w in wrm.enumerate_windows())
    finally:
        monkeypatch.undo()


def test_last_known_good_preserved_on_transient_zero(monkeypatch):
    """Chrome windows open + fresh discovery returns 0 -> previous tabs kept."""
    import jarvis_core.resource_manager as rm
    import jarvis_core.browser_resource_manager as brm
    import jarvis_core.uia_browser_manager as uia
    import jarvis_core.windows_resource_manager as wrm

    monkeypatch.setattr(wrm, "enumerate_windows", lambda: [])

    good = {"chrome_detected": True, "controllable": True, "control_mode": "uia",
            "cdp_url": None,
            "tabs": [
                {"title": "A", "host_hwnd": 111, "tab_index": 0,
                 "runtime_id": [1], "control_mode": "uia"},
                {"title": "B", "host_hwnd": 111, "tab_index": 1,
                 "runtime_id": [2], "control_mode": "uia"},
            ]}
    empty = {"chrome_detected": True, "controllable": False, "control_mode": "",
             "cdp_url": None, "tabs": []}

    reg = rm.ResourceRegistry()
    # First scan: good tabs, host windows present.
    monkeypatch.setattr(brm, "discover_tabs", lambda cdp_url=None: good)
    monkeypatch.setattr(uia, "chrome_host_windows", lambda: [{"hwnd": 111}])
    s1 = reg.scan(include_identity=True, read_only=True)
    assert len(s1.get("tabs") or []) == 2

    # Second scan: transient failure (0 tabs) BUT Chrome window still open.
    monkeypatch.setattr(brm, "discover_tabs", lambda cdp_url=None: empty)
    s2 = reg.scan(include_identity=True, read_only=True)
    assert len(s2.get("tabs") or []) == 2, "last-known-good tabs preserved"

    # Third scan: Chrome genuinely closed (no host windows) -> real zero.
    monkeypatch.setattr(uia, "chrome_host_windows", lambda: [])
    s3 = reg.scan(include_identity=True, read_only=True)
    assert len(s3.get("tabs") or []) == 0, "no stale preservation once Chrome closed"


class _Patcher:
    """Minimal monkeypatch usable inside a plain function (no pytest fixture)."""
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
