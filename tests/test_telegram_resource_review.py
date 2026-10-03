"""Targeted tests for TELEGRAM -> REAL WINDOWS APPS / CHROME TABS CONTROLLER.

Covers the resource-review gate: Feature Development selection, real app/tab
discovery, focus/screenshot/close exact identity, refresh/stale handling,
Continue/Cancel, authorization, and restart safety.

All destructive actions use mocks - no real user resources are touched.
"""
from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from jarvis_core import resource_manager
from jarvis_core.browser_resource_manager import safe_domain_display
from jarvis_core.feature_models import FeatureStatus
from jarvis_core.feature_orchestrator import FeatureOrchestrator
from jarvis_core.feature_store import FeatureStore
from jarvis_core.resource_manager import (
    Resource,
    ResourceRegistry,
    build_resource_detail,
    build_resources_menu,
    _classify_application,
    _classify_tab,
)
from jarvis_core.resource_menus import (
    build_current_running_expanded_apps,
    build_current_running_expanded_chrome,
    build_current_running_menu,
)
from jarvis_core.screenshot_service import looks_sensitive
from jarvis_core.task_manager import TaskManager
from jarvis_core.telegram_service import TelegramService

from tests.test_telegram_integration import FakeAPI, _message, _settings


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def orch(tmp_path):
    store = FeatureStore(root=tmp_path / "features")
    orchestrator = FeatureOrchestrator(store=store, task_manager_instance=TaskManager())
    orchestrator.start_worker = lambda: None  # type: ignore[method-assign]
    orchestrator.set_telegram_channels(
        send=lambda chat_id, text, markup=None: None,
        is_authorized=lambda uid: uid == 123,
    )
    yield orchestrator
    orchestrator.stop_worker(timeout=1)


@pytest.fixture()
def tg(orch):
    api = FakeAPI()
    service = TelegramService(
        settings=_settings(123),
        api=api,
        orchestrator_instance=orch,
    )
    service._start_task_watcher = Mock()
    return service, orch, api


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No real background threads; reset the shared resource singleton.

    The Feature Orchestrator sends its resource-review scan through a daemon
    thread. Left unpatched, scans from one test would race into later tests
    and mutate the module-level ``resource_registry`` singleton. Running the
    scan inline keeps every test deterministic, and resetting the singleton
    guarantees a clean generation-0 registry per test.
    """
    import jarvis_core.feature_orchestrator as fo_mod

    class _InlineThread:
        def __init__(self, target=None, name=None, daemon=None, **_kw):
            self._target = target

        def start(self):
            if self._target:
                self._target()

        def join(self, timeout=None):  # parity with real API
            return None

        def is_alive(self):  # parity with real API
            return False

        def join(self, timeout=None):  # parity with real Thread API
            return None

        def is_alive(self):  # parity with real Thread API
            return False

        def join(self, timeout=None):  # parity with real API
            return None

        def is_alive(self):  # parity with real API
            return False

        def join(self, timeout=None):  # parity with real API
            return None

        def is_alive(self):  # parity with real API (timeout checks)
            return False

    monkeypatch.setattr(fo_mod.threading, "Thread", _InlineThread)

    reg = resource_manager.resource_registry
    reg._generation = 0
    reg._resources = {}
    reg._feature_id = None
    reg._cdp_url = None
    reg._chrome_detected = False
    reg._chrome_controllable = False
    reg._chrome_reason = ""
    reg._chrome_control_mode = ""
    reg._last_scan_at = 0.0
    yield


def _callback(callback_id, user_id, chat_id, data):
    return {
        "update_id": 10,
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}},
            "data": data,
        },
    }


APP_VSCODE = {
    "resource_id": "r1-a100",
    "type": "application",
    "title": "app.py - Jarvis - Visual Studio Code",
    "display_name": "VS Code",
    "pid": 100,
    "window_handle": 100,
    "executable": "Code.exe",
    "close_requires_confirmation": True,
}
TAB_CHATGPT = {
    "resource_id": "r1-t1",
    "type": "browser_tab",
    "title": "ChatGPT",
    "display_name": "Chrome",
    "safe_display_target": "https://chatgpt.com",
    "page_id": "target-1",
}


# ── 1. Mode gate → resource review state ─────────────────────────────────────


def test_feature_development_enters_resource_review(tg):
    service, orch, api = tg
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_message(123, "add a notes export feature"))
        assert service._mode_pending.get((123, 100))
        service.process_update(_callback("cb1", 123, 100, "feature:mode:feature"))

    features = orch.list_features()
    assert len(features) == 1
    # Jarvis pauses at the resource-review stage without auto-advancing:
    assert features[0]["resource_review_completed"] is False
    assert features[0]["status"] == FeatureStatus.resource_review.value
    assert len(orch.list_resource_review_features(telegram_user_id=123)) == 1


def test_normal_command_bypasses_resource_review(tg):
    service, orch, api = tg
    normal = Mock()
    normal.submit.return_value = {
        "task_id": "task-1", "status": "queued", "action": "created",
    }
    with patch.object(service, "_orchestrator", normal):
        with patch.object(service, "_feature_orchestrator", orch):
            service.process_update(_message(123, "add a notes export feature"))
            service.process_update(_callback("cb1", 123, 100, "feature:mode:normal"))

    assert orch.list_features() == [], "Normal Command must not create a feature"
    assert normal.submit.call_args[0][0] == "add a notes export feature"


# ── 2. Real application discovery ────────────────────────────────────────────


def test_application_enumeration_returns_visible_windows():
    from jarvis_core.windows_resource_manager import enumerate_windows

    windows = enumerate_windows()
    assert isinstance(windows, list)
    for w in windows:
        assert w["visible"] is True
        assert w["hwnd"]
        assert w["pid"]
        assert w.get("title")  # blank titles are excluded by the enumerator


# ── 3. Registry behaviour ────────────────────────────────────────────────────


def _fake_windows():
    return [{
        "hwnd": 42, "title": "Calc", "class_name": "C", "pid": 7,
        "process_name": "calc.exe", "executable": "calc.exe",
        "display_name": "Calculator", "focused": False, "visible": True,
        "protected": False,
    }]


_NO_TABS = {"chrome_detected": False, "controllable": False,
            "reason": "", "tabs": [], "cdp_url": None}


def test_scan_ids_stable_within_generation(monkeypatch):
    reg = ResourceRegistry()
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.enumerate_windows", _fake_windows)
    monkeypatch.setattr(
        "jarvis_core.browser_resource_manager.discover_tabs",
        lambda url=None: dict(_NO_TABS))
    scan1 = reg.scan()
    ids1 = sorted(a["resource_id"] for a in scan1["applications"])
    scan_again = reg.scan_result()
    ids2 = sorted(a["resource_id"] for a in scan_again["applications"])
    assert ids1 == ids2 == ["r1-a42"]


def test_refresh_creates_new_generation_and_stales_old_ids(monkeypatch):
    reg = ResourceRegistry()
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.enumerate_windows", _fake_windows)
    monkeypatch.setattr(
        "jarvis_core.browser_resource_manager.discover_tabs",
        lambda url=None: dict(_NO_TABS))
    first = reg.scan()
    old_rid = first["applications"][0]["resource_id"]
    second = reg.scan()  # Refresh bumps generation
    new_rid = second["applications"][0]["resource_id"]
    assert old_rid.startswith("r1-") and new_rid.startswith("r2-")
    assert reg.get(old_rid) is None, "stale id must be rejected"
    assert reg.get(new_rid) is not None


# ── 4. Chrome tab discovery ──────────────────────────────────────────────────


def test_chrome_tabs_enumerated_when_cdp_available(monkeypatch):
    reg = ResourceRegistry()
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.enumerate_windows", lambda: [])
    monkeypatch.setattr(
        "jarvis_core.browser_resource_manager.discover_tabs",
        lambda url=None: {
            "chrome_detected": True, "controllable": True, "reason": "",
            "cdp_url": "http://127.0.0.1:9222",
            "tabs": [
                {"title": "ChatGPT",
                 "safe_display_target": "https://chatgpt.com",
                 "raw_url": "https://chatgpt.com/c/abc", "target_id": "T1",
                 "active": False},
                {"title": "FastAPI",
                 "safe_display_target": "https://fastapi.tiangolo.com",
                 "raw_url": "https://fastapi.tiangolo.com", "target_id": "T2",
                 "active": False},
            ],
        })
    scan = reg.scan()
    titles = [t["title"] for t in scan["tabs"]]
    assert titles == ["ChatGPT", "FastAPI"]
    assert scan["chrome_controllable"] is True
    assert {t["page_id"] for t in scan["tabs"]} == {"T1", "T2"}


def test_chrome_uncontrollable_reported_honestly(monkeypatch):
    """Without CDP *and* without UIA, Jarvis reports honestly - no guesses."""
    from jarvis_core import browser_resource_manager as brm

    monkeypatch.setattr(brm.chrome_processes_running, "__self__", None,
                        raising=False)
    with patch.object(brm, "chrome_processes_running", lambda: True), \
         patch.object(brm, "cdp_reachable", lambda u, **k: False), \
         patch("jarvis_core.uia_browser_manager.enumerate_tabs",
               lambda *a, **k: {"ok": False,
                                "reason": "UIA unavailable (simulated)",
                                "tabs": []}):
        result = brm.discover_tabs("http://127.0.0.1:9222")
    assert result["chrome_detected"] is True
    assert result["controllable"] is False
    assert result["tabs"] == []          # never fabricated
    assert "uia unavailable" in result["reason"].lower()


def test_chrome_uia_fallback_enumerates_without_cdp(monkeypatch):
    """No CDP endpoint -> the real Windows UIA tab strip is used instead."""
    from jarvis_core import browser_resource_manager as brm

    monkeypatch.setattr(brm.chrome_processes_running, "__self__", None,
                        raising=False)
    fake_tabs = [
        {"host_hwnd": 555, "title": "ChatGPT", "runtime_id": [42, 555, 4],
         "tab_index": 0, "selected": False, "control_mode": "uia"},
        {"host_hwnd": 555, "title": "Jarvis localhost", "runtime_id": [42, 555, 9],
         "tab_index": 1, "selected": True, "control_mode": "uia"},
    ]
    with patch.object(brm, "chrome_processes_running", lambda: True), \
         patch.object(brm, "cdp_reachable", lambda u, **k: False), \
         patch("jarvis_core.uia_browser_manager.enumerate_tabs",
               lambda *a, **k: {"ok": True, "reason": "",
                                "tabs": fake_tabs}):
        result = brm.discover_tabs("http://127.0.0.1:9222")
    assert result["controllable"] is True
    assert result["control_mode"] == "uia"
    assert [t["title"] for t in result["tabs"]] == ["ChatGPT", "Jarvis localhost"]
    assert all(t["control_mode"] == "uia" for t in result["tabs"])
    assert {t["host_hwnd"] for t in result["tabs"]} == {555}


def test_safe_domain_display_hides_query_strings():
    display = safe_domain_display("https://mail.google.com/u/0/?authuser=secret")
    assert "secret" not in display
    assert display.startswith("https://mail.google.com")


# ── 5. Focus actions ─────────────────────────────────────────────────────────


def test_focus_exact_application_window(monkeypatch):
    reg = ResourceRegistry()
    calls = []
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.focus_window",
        lambda hwnd: calls.append(hwnd) or (hwnd == 100),
    )
    r = Resource(resource_id="r1-a100", feature_id=None,
                 resource_type="application", title="VS Code",
                 display_name="VS Code", window_handle=100)
    reg._generation = 1
    reg._resources[r.resource_id] = r
    out = reg.focus("r1-a100")
    assert out["status"] == "ok"
    assert calls == [100], "must focus exactly HWND 100"


def test_focus_exact_browser_tab(monkeypatch):
    reg = ResourceRegistry()
    calls = []
    monkeypatch.setattr(
        "jarvis_core.browser_resource_manager.focus_tab",
        lambda cdp, tid: calls.append(tid) or tid == "T9",
    )
    r = Resource(resource_id="r1-t1", feature_id=None,
                 resource_type="browser_tab", title="Docs",
                 display_name="Chrome", page_id="T9")
    reg._generation = 1
    reg._resources[r.resource_id] = r
    reg._cdp_url = "http://127.0.0.1:9222"
    out = reg.focus("r1-t1")
    assert out["status"] == "ok"
    assert calls == ["T9"], "must activate exactly targetId T9"


# ── 6. Screenshots ───────────────────────────────────────────────────────────


def _write_png(path, size=(4, 3)):
    """Write a REAL valid PNG so capture validation passes like in production."""
    import PIL.Image

    PIL.Image.new("RGB", size, (200, 30, 30)).save(path, "PNG")
    return {"file": str(path)}


def test_screenshot_selected_application(monkeypatch, tmp_path):
    reg = ResourceRegistry()
    focused, captured = [], []
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.focus_window",
        lambda hwnd: focused.append(hwnd) or True,
    )

    def fake_capture(hwnd, path):
        captured.append(hwnd)
        return _write_png(path)

    monkeypatch.setattr(
        "jarvis_core.screenshot_service.capture_window", fake_capture,
    )
    monkeypatch.setattr(resource_manager, "SCREENSHOT_DIR", tmp_path)
    r = Resource(resource_id="r1-a5", feature_id=None,
                 resource_type="application", title="Notepad",
                 display_name="Notepad", window_handle=55)
    reg._generation = 1
    reg._resources[r.resource_id] = r
    out = reg.screenshot("r1-a5")
    assert out["status"] == "ok"
    assert focused == [55] and captured == [55]
    assert out["bytes"] > 0 and out["width"] == 4 and out["height"] == 3


def test_screenshot_selected_tab_prefers_cdp(monkeypatch, tmp_path):
    reg = ResourceRegistry()
    shots = []

    def fake_shot(cdp, tid, path, raw=""):
        shots.append(tid)
        return _write_png(path)

    monkeypatch.setattr(
        "jarvis_core.browser_resource_manager.screenshot_tab", fake_shot,
    )
    monkeypatch.setattr(resource_manager, "SCREENSHOT_DIR", tmp_path)
    r = Resource(resource_id="r1-t3", feature_id=None,
                 resource_type="browser_tab", title="Docs",
                 display_name="Chrome", page_id="T3")
    reg._generation = 1
    reg._resources[r.resource_id] = r
    reg._cdp_url = "http://127.0.0.1:9222"
    out = reg.screenshot("r1-t3")
    assert out["status"] == "ok"
    assert shots == ["T3"]
    assert out["bytes"] > 0


def test_screenshot_invalid_capture_rejected(monkeypatch, tmp_path):
    """A zero-byte/invalid 'capture' must be an honest error, never delivered."""
    reg = ResourceRegistry()
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.focus_window",
        lambda hwnd: True,
    )
    monkeypatch.setattr(
        "jarvis_core.screenshot_service.capture_window",
        lambda hwnd, path: open(path, "wb").close() or {"file": path},
    )
    monkeypatch.setattr(resource_manager, "SCREENSHOT_DIR", tmp_path)
    r = Resource(resource_id="r1-a7", feature_id=None,
                 resource_type="application", title="Ghost",
                 display_name="Ghost", window_handle=7)
    reg._generation = 1
    reg._resources[r.resource_id] = r
    out = reg.screenshot("r1-a7")
    assert out["status"] == "error"
    assert "invalid" in out["message"].lower()


def test_sensitive_screenshot_blocked():
    reg = ResourceRegistry()
    r = Resource(resource_id="r1-t9", feature_id=None,
                 resource_type="browser_tab",
                 title="Sign in - .env passwords", display_name="Chrome",
                 page_id="T9",
                 safe_display_target="https://accounts.google.com")
    reg._generation = 1
    reg._resources[r.resource_id] = r
    assert looks_sensitive(r.title, r.safe_display_target) is True
    out = reg.screenshot("r1-t9")
    assert out["status"] == "blocked"
    assert "sensitive" in out["message"].lower()


# ── 7. Close safety & exact identity ─────────────────────────────────────────


def test_close_exact_application_only(monkeypatch):
    reg = ResourceRegistry()
    closed = []
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.close_window",
        lambda hwnd: closed.append(hwnd) or True,
    )
    r_calc = Resource(resource_id="r1-a200", feature_id=None,
                      resource_type="application", title="Calculator",
                      display_name="Calculator", window_handle=200,
                      close_requires_confirmation=False)
    r_code = Resource(resource_id="r1-a100", feature_id=None,
                      resource_type="application", title="VS Code",
                      display_name="VS Code", window_handle=100)
    reg._generation = 1
    reg._resources[r_calc.resource_id] = r_calc
    reg._resources[r_code.resource_id] = r_code

    out = reg.close("r1-a200", confirmed=True)
    assert out["status"] == "ok"
    assert closed == [200], "only Calculator HWND 200 receives WM_CLOSE"
    assert r_code.status == "open"


def test_close_exact_tab_others_remain_open(monkeypatch):
    reg = ResourceRegistry()
    closed = []
    monkeypatch.setattr(
        "jarvis_core.browser_resource_manager.close_tab",
        lambda cdp, tid: closed.append(tid) or True,
    )
    # Verification re-enumerates targets; T2 must be gone, T1 still present.
    monkeypatch.setattr(
        "jarvis_core.browser_resource_manager.discover_tabs",
        lambda url=None, **k: {"tabs": [
            {"target_id": "T1", "title": "ChatGPT", "control_mode": "cdp"},
        ]},
    )
    t1 = Resource(resource_id="r1-t1", feature_id=None,
                  resource_type="browser_tab", title="ChatGPT",
                  display_name="Chrome", page_id="T1")
    t2 = Resource(resource_id="r1-t2", feature_id=None,
                  resource_type="browser_tab", title="Docs",
                  display_name="Chrome", page_id="T2")
    reg._generation = 1
    reg._resources[t1.resource_id] = t1
    reg._resources[t2.resource_id] = t2
    reg._cdp_url = "http://127.0.0.1:9222"

    out = reg.close("r1-t2", confirmed=True)
    assert out["status"] == "ok"
    assert out.get("verified") is True
    assert closed == ["T2"], "exactly one tab closed"
    assert t1.status == "open", "unrelated tab stays open"


def test_no_process_name_wide_kill():
    """close_window must NEVER fall back to killing processes by name."""
    import inspect

    from jarvis_core import windows_resource_manager as wrm

    src = inspect.getsource(wrm.close_window).lower()
    assert "taskkill" not in src
    assert "terminateprocess" not in src
    assert ".kill(" not in src


def test_close_confirmation_for_risky_app(monkeypatch):
    reg = ResourceRegistry()
    closed = []
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.close_window",
        lambda hwnd: closed.append(hwnd) or True,
    )
    r = Resource(resource_id="r1-a100", feature_id=None,
                 resource_type="application", title="VS Code",
                 display_name="VS Code", window_handle=100,
                 close_requires_confirmation=True)
    reg._generation = 1
    reg._resources[r.resource_id] = r

    first = reg.close("r1-a100", confirmed=False)
    assert first["status"] == "needs_confirmation"
    assert closed == [], "nothing closed before confirmation"

    second = reg.close("r1-a100", confirmed=True)
    assert second["status"] == "ok"
    assert closed == [100]


def test_classify_helpers_mark_risky_surfaces():
    assert _classify_application(
        "VS Code", "Code.exe", "x", False, "") == (True, True)
    safe = _classify_application(
        "Calculator", "calc.exe", "y", False, "calc.exe")
    assert safe[0] is True and safe[1] is False
    protected = _classify_application(
        "Jarvis", "python.exe", "jarvis server", True, "")
    assert protected == (False, False), "Jarvis infra must be protected"
    risky_tab = _classify_tab(
        "My Doc", "https://docs.google.com/document/d/1/edit")
    assert risky_tab[1] is True
    plain_tab = _classify_tab("News", "https://example.com/article")
    assert plain_tab[1] is False


# ── 8. Telegram rendering ────────────────────────────────────────────────────


def _fake_scan(apps=None, tabs=None):
    return {
        "generation": 1,
        "feature_id": "fid",
        "applications": apps or [],
        "tabs": tabs or [],
        "chrome_detected": bool(tabs),
        "chrome_controllable": True,
        "chrome_reason": "",
        "scanned_at": "",
    }


def test_resources_menu_lists_apps_and_tabs():
    text, rows = build_resources_menu(
        "fid", _fake_scan(apps=[APP_VSCODE], tabs=[TAB_CHATGPT]),
    )
    assert "CURRENT LAPTOP RESOURCES" in text
    assert "APPLICATIONS" in text and "CHROME TABS" in text
    flat = [b["callback_data"] for row in rows for b in row]
    assert "feature:resrc_show:r1-a100" in flat
    # Tabs open the dedicated TAB DETAILS view - never an app-level view.
    assert "feature:resrc_tab_show:r1-t1" in flat
    # Chrome tabs quick menu + Current Screen action are reachable.
    assert "feature:resrc_tabs:fid" in flat
    assert "feature:resrc_screen:fid" in flat
    assert any("resrc_continue" in cb for cb in flat)
    assert any("resrc_cancel" in cb for cb in flat)


def test_chrome_uncontrollable_message_in_menu():
    text, _rows = build_resources_menu("fid", {
        "generation": 1, "feature_id": "fid", "applications": [],
        "tabs": [], "chrome_detected": True, "chrome_controllable": False,
        "chrome_reason": "no CDP", "scanned_at": "",
    })
    assert "could not be read" in text


def test_resource_detail_buttons():
    r = Resource(
        resource_id="r1-t1", feature_id="fid", resource_type="browser_tab",
        title="ChatGPT", display_name="Chrome",
        safe_display_target="https://chatgpt.com", page_id="T1",
    )
    text, rows = build_resource_detail(r)
    assert "ACTION MENU" in text
    assert "Browser Tab" in text
    labels = [b["text"] for row in rows for b in row]
    assert any("Focus" in lbl for lbl in labels)
    assert any("Screenshot" in lbl for lbl in labels)
    assert any("Edit" in lbl for lbl in labels)
    assert any("Back" in lbl for lbl in labels)
    flat = [b["callback_data"] for row in rows for b in row]
    assert "feature:resrc_focus:r1-t1" in flat
    assert "feature:resrc_shot:r1-t1" in flat
    assert "feature:resrc_edit:r1-t1" in flat
    assert any("resrc_back" in cb for cb in flat if cb)


# ── 9. Telegram flow end-to-end (mocked backends) ────────────────────────────


def _seed_registry(monkeypatch, tg, *, risky=False):
    """Submit a feature via mode gate with a mocked single-app scan.

    The autouse ``_hermetic`` fixture already makes the review scan run
    inline; here we additionally route orchestrator Telegram output into the
    FakeAPI so tests can assert on the delivered menu.
    """
    service, orch, api = tg

    # Route orchestrator Telegram output into the FakeAPI so tests can
    # assert on the resource-review menu the user actually receives.
    orch.set_telegram_channels(
        send=lambda chat_id, text, markup=None: service._send(
            chat_id, text, reply_markup=markup),
        is_authorized=lambda uid: uid == 123,
    )

    reg = resource_manager.resource_registry

    def fake_scan(feature_id=None, cdp_url=None, include_identity=False,
                  read_only=False):
        reg._generation += 1
        gen = reg._generation
        rid = f"r{gen}-a{'100' if risky else '200'}"
        r = Resource(
            resource_id=rid,
            feature_id=feature_id,
            resource_type="application",
            title="VS Code" if risky else "Calculator",
            display_name="VS Code" if risky else "Calculator",
            pid=100 if risky else 200,
            window_handle=100 if risky else 200,
            close_requires_confirmation=risky,
        )
        # Atomic-ish swap: registry state and the returned snapshot are built
        # together so an interleaved stray scan can never split them.
        reg._resources = {rid: r}
        reg._feature_id = feature_id
        reg._chrome_detected = False
        reg._chrome_controllable = False
        reg._chrome_reason = ""
        return {
            "generation": gen,
            "feature_id": feature_id,
            "applications": [r.public()],
            "tabs": [],
            "chrome_detected": False,
            "chrome_controllable": False,
            "chrome_reason": "",
            "scanned_at": "",
        }

    with patch.object(service, "_feature_orchestrator", orch):
        with patch.object(reg, "scan", side_effect=fake_scan):
            service.process_update(_message(123, "add a tiny feature x"))
            service.process_update(_callback("cb1", 123, 100, "feature:mode:feature"))
    return service, orch, api, reg


def test_telegram_feature_shows_real_resource_list(tg, monkeypatch):
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    texts = " ".join(m["text"] for m in api.messages)
    assert "FEATURE RESOURCE REVIEW" in texts
    assert len(reg._resources) == 1
    feature = orch.list_features()[0]
    # Waits at resource_review until user taps Continue:
    assert feature["resource_review_completed"] is False
    assert feature["status"] == FeatureStatus.resource_review.value


def test_telegram_focus_action_targets_exact_window(tg, monkeypatch):
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    rid = next(iter(reg._resources))
    focused = []
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.focus_window",
        lambda hwnd: focused.append(hwnd) or True,
    )
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(
            _callback("cb2", 123, 100, f"feature:resrc_focus:{rid}"))
    assert focused == [200]
    assert any("Focused" in m["text"] for m in api.messages)


def test_telegram_close_safe_app_without_confirmation(tg, monkeypatch):
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    old_rid = next(iter(reg._resources))
    old_hwnd = reg.get(old_rid).window_handle
    closed = []
    scanner = []

    # The post-close flow re-scans; make that fresh scan deterministic: the
    # SAME window is re-registered under a brand-new generation id.
    def _fresh_scan(feature_id=None, cdp_url=None, include_identity=False,
                    read_only=False):
        scanner.append(1)
        reg._generation += 1
        fresh = Resource(
            resource_id=f"r{reg._generation}-a200", feature_id=feature_id,
            resource_type="application", title="Calculator",
            display_name="Calculator", pid=200, window_handle=200,
            close_requires_confirmation=False,
        )
        reg._resources = {fresh.resource_id: fresh}
        return reg.scan_result(include_identity=include_identity)

    monkeypatch.setattr(reg, "scan", _fresh_scan)
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.close_window",
        lambda hwnd: closed.append(hwnd) or True)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(
            _callback("cb4", 123, 100, f"feature:resrc_close:{old_rid}"))

    assert closed == [200], "safe app: graceful WM_CLOSE to the exact HWND"
    assert scanner, "a fresh scan ran after the successful close"
    assert any("Closed" in m["text"] for m in api.messages)
    # The post-close rescan made the OLD generation id stale...
    assert reg.get(old_rid) is None, \
        "old generation id is stale after the post-close rescan"
    # ... while the fresh snapshot re-registers the SAME window.
    fresh = [r for r in reg._resources.values() if r.window_handle == old_hwnd]
    assert len(fresh) == 1, "the same HWND is in the fresh snapshot"
    assert fresh[0].resource_id != old_rid, \
        "the same app now lives under the new generation"
    assert fresh[0].status == "open"


def test_telegram_close_risky_app_asks_confirmation_first(tg, monkeypatch):
    service, orch, api, reg = _seed_registry(monkeypatch, tg, risky=True)
    rid = next(iter(reg._resources))
    closed = []
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.close_window",
        lambda hwnd: closed.append(hwnd) or True)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(
            _callback("c2", 123, 100, f"feature:resrc_close:{rid}"))
    assert closed == [], "risky app: nothing closed before confirmation"
    texts = " ".join(m["text"] for m in api.messages)
    assert "Confirm closing?" in texts
    cbs = [b["callback_data"] for m in api.messages if m.get("reply_markup")
           for row in m["reply_markup"].get("inline_keyboard", [])
           for b in row]
    assert f"feature:resrc_confirm_close:{rid}" in cbs


def test_stale_resource_rejected_after_refresh(tg, monkeypatch):
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    old_rid = next(iter(reg._resources))
    reg.scan(feature_id=reg._feature_id)  # Refresh -> new generation

    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(
            _callback("cb5", 123, 100, f"feature:resrc_focus:{old_rid}"))

    assert any("Resource list changed" in m["text"]
               for m in api.messages), "stale id rejected with refresh warning"
    assert reg.get(old_rid) is None, "old rid is invalid after the refresh"


# ── 10. Continue / Cancel / authorization ────────────────────────────────────


def test_continue_feature_resumes_orchestrator(tg, monkeypatch):
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    fid = orch.list_features()[0]["feature_id"]
    feature = orch.get_feature(fid)
    assert feature["resource_review_completed"] is False
    assert feature["status"] == FeatureStatus.resource_review.value

    enqueued = []
    monkeypatch.setattr(
        orch, "enqueue", lambda action, feature_id: enqueued.append(action))

    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(
            _callback("cb7", 123, 100, f"feature:resrc_continue:{fid}"))

    # Part 9: the ▶️ Continue callback resumes the pipeline by enqueuing
    # RESEARCH exactly once (it no longer stays disabled).
    assert enqueued == ["research"], "Continue enqueues research exactly once"
    assert orch.get_feature(fid)["resource_review_completed"] is True
    assert orch.get_feature(fid)["research_enqueued"] is True


def test_cancel_feature_prevents_progression(tg, monkeypatch):
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    fid = orch.list_features()[0]["feature_id"]
    enqueued = []
    monkeypatch.setattr(
        orch, "enqueue", lambda action, feature_id: enqueued.append(action))

    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(
            _callback("cb8", 123, 100, f"feature:resrc_cancel:{fid}"))

    assert orch.get_feature(fid)["status"] == FeatureStatus.cancelled.value
    assert enqueued == [], "cancelled feature must never enqueue research"
    assert orch.list_resource_review_features(telegram_user_id=123) == []


def test_unauthorized_user_cannot_act_on_resources(tg, monkeypatch):
    service, orch, api = tg
    reg = resource_manager.resource_registry

    def fake_scan(feature_id=None, cdp_url=None, include_identity=False,
                  read_only=False):
        reg._generation += 1
        r = Resource(resource_id=f"r{reg._generation}-a1",
                     feature_id=feature_id, resource_type="application",
                     title="X", display_name="X", window_handle=1)
        reg._resources = {r.resource_id: r}
        return reg.scan_result(feature_id=feature_id)

    with patch.object(service, "_feature_orchestrator", orch):
        with patch.object(reg, "scan", side_effect=fake_scan):
            service.process_update(_message(123, "add secret feature z"))
            service.process_update(_callback("c9", 123, 100, "feature:mode:feature"))

    rid = next(iter(reg._resources))
    before = len(api.messages)
    service.process_update(_callback("c10", 999, 100, f"feature:resrc_focus:{rid}"))
    service.process_update(_callback("c11", 999, 100, f"feature:resrc_shot:{rid}"))
    service.process_update(_callback("c12", 999, 100, f"feature:resrc_close:{rid}"))

    new_texts = " ".join(m["text"] for m in api.messages[before:])
    assert "not authorized" in new_texts.lower()
    assert reg.get(rid) is not None
    assert reg.get(rid).focused is False


# ── 11. Restart does not auto-continue ───────────────────────────────────────


def test_restart_recomputes_resource_review_then_continues(tg, monkeypatch):
    service, orch, api = tg
    fid = orch.submit_feature_request(
        "add restart-safe feature", source="Telegram",
        telegram_user_id=123, telegram_chat_id=100,
    )["feature_id"]
    assert orch.get_feature(fid)["status"] == FeatureStatus.resource_review.value

    # Continue feature:
    orch.continue_after_resource_review(fid, telegram_user_id=123)
    assert orch.get_feature(fid)["resource_review_completed"] is True


def test_resource_scan_failure_still_continues(tg, monkeypatch):
    """A failed scan presents an empty snapshot and waits for user continue/cancel."""
    service, orch, api = tg
    reg = resource_manager.resource_registry

    def boom(feature_id=None, cdp_url=None, **_kw):
        raise RuntimeError("UIA exploded")

    with patch.object(reg, "scan", side_effect=boom):
        fid = orch.submit_feature_request(
            "add failing-scan feature", source="Telegram",
            telegram_user_id=123, telegram_chat_id=100,
        )["feature_id"]

    feature = orch.get_feature(fid)
    assert feature["status"] == FeatureStatus.resource_review.value
    assert len(orch.list_resource_review_features(telegram_user_id=123)) == 1


# ── 12. Normal commands unaffected ───────────────────────────────────────────


def test_normal_commands_completely_unaffected(tg):
    service, orch, api = tg
    normal = Mock()
    normal.submit.return_value = {
        "task_id": "t9", "status": "queued", "action": "created",
    }
    with patch.object(service, "_orchestrator", normal):
        service.process_update(_message(123, "what time is it"))
        service.process_update(_message(123, "/help"))

    assert orch.list_features() == []
    assert service._mode_pending == {}
    assert normal.submit.call_count >= 1


# ── 13. Application Close post-action UI (Part 8C-3C1) ───────────────────────


def _callback_on_panel(callback_id, user_id, chat_id, data, message_id):
    """Callback pressed on the panel message (carries its message_id)."""
    return {
        "update_id": 10,
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}, "message_id": message_id},
            "data": data,
        },
    }


def _seed_two_apps_with_post_close_scan(tg, monkeypatch):
    """Seed Calculator (hwnd 200) + Notepad (hwnd 201); the fresh scan after
    the Close sees ONLY Notepad (Calculator is gone). Returns the pieces."""
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    gen = reg._generation
    rid_closed = f"r{gen}-a200"
    rid_kept = f"r{gen}-a201"
    kept = Resource(
        resource_id=rid_kept, feature_id=reg._feature_id,
        resource_type="application", title="Notepad",
        display_name="Notepad", pid=201, window_handle=201,
    )
    reg._resources[rid_kept] = kept

    scan_calls: list = []

    def post_close_scan(feature_id=None, cdp_url=None,
                        include_identity=False, read_only=False):
        scan_calls.append(1)
        reg._resources = {rid_kept: kept}
        return reg.scan_result(include_identity=include_identity)

    monkeypatch.setattr(reg, "scan", post_close_scan)
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.close_window",
        lambda hwnd: hwnd == 200)
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.is_window_alive",
        lambda hwnd: False)
    monkeypatch.setattr(
        "jarvis_core.resource_manager.time.sleep", lambda _s: None)
    return service, orch, api, reg, rid_closed, rid_kept, scan_calls


def test_app_close_rerenders_applications_expanded_without_closed_app(
        tg, monkeypatch):
    service, orch, api, reg, rid_closed, rid_kept, _scans = (
        _seed_two_apps_with_post_close_scan(tg, monkeypatch))

    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback_on_panel(
            "cbAC1", 123, 100, f"feature:resrc_close:{rid_closed}", 777))

    assert api.edits, "the successful close re-renders the SAME panel"
    edit = api.edits[0]
    kb = [b["callback_data"] for e in api.edits
          for row in (e["reply_markup"]["inline_keyboard"]
                      if e["reply_markup"] else [])
          for b in row]
    assert edit["text"].startswith("\u2705 Closed: Calculator"), \
        "the ✅ Closed banner names the closed app"
    assert "CURRENT RUNNING" in edit["text"], "stays in CURRENT RUNNING"
    assert "Applications (" in edit["text"], "Applications heading rendered"
    assert any("feature:resrc_collapse_apps:" in cb for cb in kb), \
        "Applications stays EXPANDED (Collapse button, not Expand)"
    assert f"feature:resrc_app_actions:{rid_kept}" in kb, \
        "the surviving app is listed with clickable actions"
    assert "Calculator" not in edit["text"].split("\n\n", 1)[1], \
        "closed app disappeared from the rendered list"
    assert not any(cb.endswith(rid_closed) for cb in kb), \
        "no callback targets the closed app"
    assert not any("resrc_hide_app_actions" in cb for cb in kb), \
        "no app action panel remains selected"


def test_app_close_edits_same_message_using_fresh_scan(tg, monkeypatch):
    service, orch, api, reg, rid_closed, rid_kept, scan_calls = (
        _seed_two_apps_with_post_close_scan(tg, monkeypatch))

    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback_on_panel(
            "cbAC2", 123, 100, f"feature:resrc_close:{rid_closed}", 777))

    assert scan_calls, "a fresh scan ran after the successful close"
    assert len(api.edits) == 1, \
        "the SAME panel message is edited, not replaced"
    edit = api.edits[0]
    assert edit["message_id"] == 777, \
        "the Telegram control message_id is preserved"
    assert any("Notepad" in lbl for lbl in _labels(edit)), \
        "rendered from the FRESH post-close scan result"
    assert "Calculator" not in edit["text"].split("\n\n", 1)[1], \
        "pre-close snapshot content is not re-rendered"
    assert not any("Closed:" in m["text"] for m in api.messages), \
        "no new menu message - the outcome lives in the edited panel"


# ── 14. Chrome tab Close post-action UI (Part 8C-3C2) ────────────────────────

_TAB_EMAIL = "reviewer@gmail.com"


def _seed_two_tabs_with_post_close_scan(tg, monkeypatch):
    """Seed ONE Chrome window (hwnd 555, UIA) with Docs + Keep tabs on the
    SAME account; the fresh scan after the Close sees ONLY Keep (Docs is
    gone). Returns the pieces."""
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    gen = reg._generation
    rid_closed = f"r{gen}-t1"
    rid_kept = f"r{gen}-t2"

    def _tab(rid, title, idx):
        res = Resource(
            resource_id=rid, feature_id=reg._feature_id,
            resource_type="browser_tab", title=title, display_name="Chrome",
            control_mode="uia", host_hwnd=555,
            runtime_id=f"42,555,{idx}", tab_index=idx,
        )
        res.account_email = _TAB_EMAIL
        res.identity_label = _TAB_EMAIL
        return res

    kept = _tab(rid_kept, "Keep", 1)
    reg._resources = {rid_closed: _tab(rid_closed, "Docs", 0), rid_kept: kept}
    reg._chrome_detected = True
    reg._chrome_controllable = True
    reg._chrome_control_mode = "uia"

    scan_calls: list = []

    def post_close_scan(feature_id=None, cdp_url=None,
                        include_identity=False, read_only=False):
        scan_calls.append(1)
        reg._resources = {rid_kept: kept}
        return reg.scan_result(include_identity=include_identity)

    monkeypatch.setattr(reg, "scan", post_close_scan)
    monkeypatch.setattr(
        "jarvis_core.uia_browser_manager.close_tab",
        lambda hwnd, rtid, title, idx:
            {"ok": True, "verified": True, "method": "uia_close_button"},
    )
    return service, orch, api, reg, rid_closed, rid_kept, scan_calls


def test_tab_close_rerenders_chrome_expanded_without_closed_tab(
        tg, monkeypatch):
    service, orch, api, reg, rid_closed, rid_kept, _scans = (
        _seed_two_tabs_with_post_close_scan(tg, monkeypatch))

    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback_on_panel(
            "cbTC1", 123, 100, f"feature:resrc_close:{rid_closed}", 777))

    assert api.edits, "the successful close re-renders the SAME panel"
    edit = api.edits[0]
    kb = [b["callback_data"] for e in api.edits
          for row in (e["reply_markup"]["inline_keyboard"]
                      if e["reply_markup"] else [])
          for b in row]
    assert edit["text"].startswith("\u2705 Closed tab: Docs"), \
        "the ✅ Closed banner names the closed tab"
    assert "CURRENT RUNNING" in edit["text"], "stays in CURRENT RUNNING"
    assert "Chrome (" in edit["text"], "Chrome heading rendered"
    assert any("feature:resrc_collapse_chrome:" in cb for cb in kb), \
        "Chrome stays EXPANDED (Collapse button, not Expand)"
    assert f"feature:resrc_tab_actions:{rid_kept}" in kb, \
        "the surviving tab is still a clickable entry"
    assert "Docs" not in edit["text"].split("\n\n", 1)[1], \
        "closed tab disappeared from the rendered list"
    assert not any(rid_closed in cb for cb in kb), \
        "no callback targets the closed tab"
    assert not any("resrc_hide_tab_actions" in cb for cb in kb), \
        "no tab action panel remains selected"


def test_tab_close_edits_same_message_using_fresh_scan(tg, monkeypatch):
    service, orch, api, reg, rid_closed, rid_kept, scan_calls = (
        _seed_two_tabs_with_post_close_scan(tg, monkeypatch))

    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback_on_panel(
            "cbTC2", 123, 100, f"feature:resrc_close:{rid_closed}", 777))

    assert scan_calls, "a fresh scan ran after the successful close"
    assert len(api.edits) == 1, \
        "the SAME panel message is edited, not replaced"
    edit = api.edits[0]
    assert edit["message_id"] == 777, \
        "the Telegram control message_id is preserved"
    assert any("Keep" in lbl for lbl in _labels(edit)), \
        "rendered from the FRESH post-close scan result"
    assert "Docs" not in edit["text"].split("\n\n", 1)[1], \
        "pre-close snapshot content is not re-rendered"
    assert not any("Closed tab:" in m["text"] for m in api.messages), \
        "no new menu message - the outcome lives in the edited panel"


# ── 15. Application Edit placeholder UI (Part 8C-3D1) ────────────────────────


def _seed_two_apps_for_edit(tg, monkeypatch):
    """Seed Calculator (a200, from the feature scan) + Notepad (a201); no
    close/rescan stub - Edit re-renders the CURRENT RUNNING snapshot."""
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    gen = reg._generation
    rid_sel = f"r{gen}-a200"
    rid_other = f"r{gen}-a201"
    reg._resources[rid_other] = Resource(
        resource_id=rid_other, feature_id=reg._feature_id,
        resource_type="application", title="Notepad",
        display_name="Notepad", pid=201, window_handle=201,
    )
    return service, orch, api, reg, rid_sel, rid_other


def test_app_edit_stays_on_selected_app_with_placeholder_notice(
        tg, monkeypatch):
    service, orch, api, reg, rid_sel, rid_other = (
        _seed_two_apps_for_edit(tg, monkeypatch))

    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback_on_panel(
            "cbAE1", 123, 100, f"feature:resrc_edit:{rid_sel}", 777))

    assert api.edits, "the Edit action re-renders the SAME panel"
    edit = api.edits[0]
    kb = [b["callback_data"] for e in api.edits
          for row in (e["reply_markup"]["inline_keyboard"]
                      if e["reply_markup"] else [])
          for b in row]
    assert edit["text"].startswith(
        "\u270f\ufe0f Edit controls will be implemented in a later phase."), \
        "the placeholder notice is prepended on top"
    assert "CURRENT RUNNING" in edit["text"], "stays in CURRENT RUNNING"
    assert "Applications (" in edit["text"], "Applications heading rendered"
    assert any("feature:resrc_collapse_apps:" in cb for cb in kb), \
        "Applications stays EXPANDED (Collapse button, not Expand)"
    assert f"feature:resrc_hide_app_actions:{reg._feature_id}" in kb, \
        "the SAME app stays selected with its action panel"
    assert f"feature:resrc_edit:{rid_sel}" in kb, \
        "the selected app's inline action buttons remain visible"
    assert f"feature:resrc_app_actions:{rid_other}" in kb, \
        "the other app is still listed with clickable actions"


def test_app_edit_preserves_message_and_action_buttons(tg, monkeypatch):
    service, orch, api, reg, rid_sel, _rid_other = (
        _seed_two_apps_for_edit(tg, monkeypatch))

    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback_on_panel(
            "cbAE2", 123, 100, f"feature:resrc_edit:{rid_sel}", 777))

    assert len(api.edits) == 1, \
        "the SAME panel message is edited, not replaced"
    edit = api.edits[0]
    assert edit["message_id"] == 777, \
        "the Telegram control message_id is preserved"
    kb = [b["callback_data"] for row in
          (edit["reply_markup"]["inline_keyboard"]
           if edit["reply_markup"] else [])
          for b in row]
    assert f"feature:resrc_focus:{rid_sel}" in kb, "Focus button remains"
    assert f"feature:resrc_shot:{rid_sel}" in kb, "Screenshot button remains"
    assert f"feature:resrc_close:{rid_sel}" in kb, "Close button remains"
    assert f"feature:resrc_edit:{rid_sel}" in kb, "Edit button remains"
    assert any("Calculator" in lbl for lbl in _labels(edit)), \
        "the selected app is rendered in the panel body"
    assert not any("Edit controls will be implemented" in m["text"]
                   for m in api.messages), \
        "no new notice message - the outcome lives in the edited panel"


# ── 16. Chrome tab Edit placeholder UI (Part 8C-3D2) ─────────────────────────


def _seed_two_tabs_for_edit(tg, monkeypatch):
    """Seed ONE Chrome window (hwnd 555, UIA) with Docs + Keep tabs on the
    SAME account; no close/rescan stub - Edit re-renders the CURRENT RUNNING
    snapshot with the SAME tab still selected."""
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    gen = reg._generation
    rid_sel = f"r{gen}-t1"
    rid_other = f"r{gen}-t2"

    def _tab(rid, title, idx):
        res = Resource(
            resource_id=rid, feature_id=reg._feature_id,
            resource_type="browser_tab", title=title, display_name="Chrome",
            control_mode="uia", host_hwnd=555,
            runtime_id=f"42,555,{idx}", tab_index=idx,
        )
        res.account_email = _TAB_EMAIL
        res.identity_label = _TAB_EMAIL
        return res

    reg._resources = {rid_sel: _tab(rid_sel, "Docs", 0),
                      rid_other: _tab(rid_other, "Keep", 1)}
    reg._chrome_detected = True
    reg._chrome_controllable = True
    reg._chrome_control_mode = "uia"
    return service, orch, api, reg, rid_sel, rid_other


def test_tab_edit_stays_on_selected_tab_with_placeholder_notice(
        tg, monkeypatch):
    service, orch, api, reg, rid_sel, rid_other = (
        _seed_two_tabs_for_edit(tg, monkeypatch))

    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback_on_panel(
            "cbTE1", 123, 100, f"feature:resrc_edit:{rid_sel}", 777))

    assert api.edits, "the Edit action re-renders the SAME panel"
    edit = api.edits[0]
    kb = [b["callback_data"] for e in api.edits
          for row in (e["reply_markup"]["inline_keyboard"]
                      if e["reply_markup"] else [])
          for b in row]
    assert edit["text"].startswith(
        "\u270f\ufe0f Edit controls will be implemented in a later phase."), \
        "the placeholder notice is prepended on top"
    assert "CURRENT RUNNING" in edit["text"], "stays in CURRENT RUNNING"
    assert "Chrome (" in edit["text"], "Chrome heading rendered"
    assert any("feature:resrc_collapse_chrome:" in cb for cb in kb), \
        "Chrome stays EXPANDED (Collapse button, not Expand)"
    assert f"feature:resrc_hide_tab_actions:{reg._feature_id}" in kb, \
        "the SAME tab stays selected with its action panel"
    assert f"feature:resrc_edit:{rid_sel}" in kb, \
        "the selected tab's inline action buttons remain visible"
    assert f"feature:resrc_tab_actions:{rid_other}" in kb, \
        "the other tab is still a clickable entry"


def test_tab_edit_preserves_message_and_action_buttons(tg, monkeypatch):
    service, orch, api, reg, rid_sel, _rid_other = (
        _seed_two_tabs_for_edit(tg, monkeypatch))

    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback_on_panel(
            "cbTE2", 123, 100, f"feature:resrc_edit:{rid_sel}", 777))

    assert len(api.edits) == 1, \
        "the SAME panel message is edited, not replaced"
    edit = api.edits[0]
    assert edit["message_id"] == 777, \
        "the Telegram control message_id is preserved"
    kb = [b["callback_data"] for row in
          (edit["reply_markup"]["inline_keyboard"]
           if edit["reply_markup"] else [])
          for b in row]
    assert f"feature:resrc_focus:{rid_sel}" in kb, "Focus button remains"
    assert f"feature:resrc_shot:{rid_sel}" in kb, "Screenshot button remains"
    assert f"feature:resrc_close:{rid_sel}" in kb, "Close button remains"
    assert f"feature:resrc_edit:{rid_sel}" in kb, "Edit button remains"
    assert any("Docs" in lbl for lbl in _labels(edit)), \
        "the selected tab is rendered in the panel body"
    assert not any("Edit controls will be implemented" in m["text"]
                   for m in api.messages), \
        "no new notice message - the outcome lives in the edited panel"


# ── 15. Back → HOME + context-preserving Refresh (Part 8C-3E) ────────────────
# Back decision PROOF: every Current Running panel (collapsed AND expanded)
# renders Back → Resource Review HOME (resrc_home) - never a rescan, never the
# legacy resrc_back row. Refresh is CONTEXT-PRESERVING: collapsed stays
# collapsed, Applications expanded stays Applications expanded, Chrome expanded
# stays Chrome expanded, and a selected app/tab survives the ONE rescan by its
# STABLE identity (exact window_handle for apps; CDP page_id / UIA
# host_hwnd + runtime_id for tabs) with the selection following the NEW
# generation-stamped resource id - or the panel renders with NO selection.

_WORLD_SCAN = {
    "applications": [
        {"resource_id": "r1-a200", "type": "application",
         "title": "Calculator", "display_name": "Calculator",
         "window_handle": 200, "pid": 200},
        {"resource_id": "r1-a201", "type": "application",
         "title": "Notepad", "display_name": "Notepad",
         "window_handle": 201, "pid": 201},
    ],
    "tabs": [
        {"resource_id": "r1-t0", "type": "browser_tab", "title": "Gmail",
         "display_name": "Chrome", "control_mode": "cdp",
         "page_id": "tgt-gmail-1", "host_hwnd": 555, "tab_index": 0},
        {"resource_id": "r1-t1", "type": "browser_tab", "title": "Docs",
         "display_name": "Chrome", "control_mode": "uia",
         "host_hwnd": 555, "runtime_id": "42,7,1", "tab_index": 1},
    ],
    "chrome_detected": True,
}


def _datas(edit) -> list:
    return [b["callback_data"]
            for row in (edit["reply_markup"]["inline_keyboard"]
                        if edit["reply_markup"] else [])
            for b in row]


def _labels(edit) -> list:
    return [b["text"]
            for row in (edit["reply_markup"]["inline_keyboard"]
                        if edit["reply_markup"] else [])
            for b in row]

def _refresh_world(reg, monkeypatch, *, notepad_alive=True, gmail_alive=True):
    """Two-generation world for context-preserving Refresh tests.

    Generation A (placed in the registry NOW): Calculator (hwnd 200), Notepad
    (hwnd 201), CDP tab 'Gmail' (page target ``tgt-gmail-1``, host 555) and
    UIA tab 'Docs' (host 555, runtime ``42,7,1``). The NEXT scan - the ONE
    rescan a Refresh runs - yields generation B: the SAME stable identities
    under NEW generation-stamped ids, with the CDP tab RENAMED (titles are
    not identity) and the UIA tab MOVED to another position (tab index is not
    identity). ``notepad_alive`` / ``gmail_alive`` simulate the window / tab
    being closed on the laptop before the Refresh. Returns the list recording
    how many scans actually ran.
    """
    fid = reg._feature_id

    def build(gen, *, notepad=True, gmail=True, rename=False, moved=False):
        res = {}
        res[f"r{gen}-a200"] = Resource(
            resource_id=f"r{gen}-a200", feature_id=fid,
            resource_type="application", title="Calculator",
            display_name="Calculator", pid=200, window_handle=200)
        if notepad:
            res[f"r{gen}-a201"] = Resource(
                resource_id=f"r{gen}-a201", feature_id=fid,
                resource_type="application", title="Notepad",
                display_name="Notepad", pid=201, window_handle=201)
        if gmail:
            res[f"r{gen}-t0"] = Resource(
                resource_id=f"r{gen}-t0", feature_id=fid,
                resource_type="browser_tab",
                title="Gmail - Inbox (renamed)" if rename else "Gmail",
                display_name="Chrome",
                safe_display_target="https://mail.google.com",
                control_mode="cdp", page_id="tgt-gmail-1", host_hwnd=555,
                tab_index=0)
        docs_index = 2 if moved else 1
        res[f"r{gen}-t{docs_index}"] = Resource(
            resource_id=f"r{gen}-t{docs_index}", feature_id=fid,
            resource_type="browser_tab", title="Docs", display_name="Chrome",
            control_mode="uia", host_hwnd=555, runtime_id="42,7,1",
            tab_index=docs_index)
        return res

    reg._resources = build(1)
    reg._chrome_detected = True
    reg._chrome_controllable = True

    scans: list = []

    def fake_scan(feature_id=None, cdp_url=None, include_identity=False,
                  read_only=False):
        scans.append(1)
        reg._generation += 1
        fresh = build(reg._generation, notepad=notepad_alive,
                      gmail=gmail_alive, rename=True, moved=True)
        reg._resources = fresh
        return {
            "generation": reg._generation, "feature_id": fid,
            "applications": [r.public(include_identity)
                             for r in fresh.values()
                             if r.resource_type == "application"],
            "tabs": [r.public(include_identity) for r in fresh.values()
                     if r.resource_type == "browser_tab"],
            "chrome_detected": True, "chrome_controllable": True,
            "chrome_reason": "", "chrome_control_mode": "cdp",
            "scanned_at": "",
        }

    monkeypatch.setattr(reg, "scan", fake_scan)
    return scans

# ── 15a. Builder-level Back / Refresh rows ───────────────────────────────────


def test_back_rows_target_home_on_every_current_running_panel():
    """Back decision PROOF: collapsed + both expanded panels render Back →
    Resource Review HOME (resrc_home); the legacy resrc_back row is gone."""
    panels = [
        build_current_running_menu("f1", _WORLD_SCAN),
        build_current_running_expanded_apps("f1", _WORLD_SCAN,
                                            app_id="r1-a201"),
        build_current_running_expanded_chrome("f1", _WORLD_SCAN,
                                              tab_id="r1-t0"),
    ]
    for _text, rows in panels:
        datas = [b["callback_data"] for row in rows for b in row]
        assert any(d.startswith("feature:resrc_home:") for d in datas), \
            "Back targets Resource Review HOME"
        assert not any("resrc_back" in d for d in datas), \
            "the legacy resrc_back row must not come back"


def test_collapsed_refresh_still_targets_run():
    """8C-1 contract intact: the collapsed panel's Refresh stays
    resrc_refresh_run (only the EXPANDED panels carry selection context)."""
    _text, rows = build_current_running_menu("f1", _WORLD_SCAN)
    datas = [b["callback_data"] for row in rows for b in row]
    assert any(d.startswith("feature:resrc_refresh_run:") for d in datas)
    assert not any("resrc_refresh_apps" in d for d in datas)
    assert not any("resrc_refresh_chrome" in d for d in datas)


def test_expanded_refresh_rows_carry_selection_context():
    """Refresh on an expanded panel carries the SELECTED resource's id so the
    refresh can re-match it; with no selection it carries the feature id."""
    _t1, rows1 = build_current_running_expanded_apps(
        "f1", _WORLD_SCAN, app_id="r1-a201")
    datas1 = [b["callback_data"] for row in rows1 for b in row]
    assert "feature:resrc_refresh_apps:r1-a201" in datas1
    _t2, rows2 = build_current_running_expanded_apps("f1", _WORLD_SCAN)
    datas2 = [b["callback_data"] for row in rows2 for b in row]
    assert "feature:resrc_refresh_apps:f1" in datas2
    _t3, rows3 = build_current_running_expanded_chrome(
        "f1", _WORLD_SCAN, tab_id="r1-t0")
    datas3 = [b["callback_data"] for row in rows3 for b in row]
    assert "feature:resrc_refresh_chrome:r1-t0" in datas3
    _t4, rows4 = build_current_running_expanded_chrome("f1", _WORLD_SCAN)
    datas4 = [b["callback_data"] for row in rows4 for b in row]
    assert "feature:resrc_refresh_chrome:f1" in datas4

# ── 15b. Refresh of the collapsed Current Running panel ──────────────────────


def test_refresh_run_collapsed_rerenders_collapsed_exactly_once(tg, monkeypatch):
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    scans = _refresh_world(reg, monkeypatch)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback_on_panel(
            "cbRF1", 123, 100, "feature:resrc_refresh_run:", 777))

    assert scans == [1], "refresh re-scans exactly ONCE"
    assert len(api.edits) == 1, "the SAME message is edited"
    edit = api.edits[0]
    datas = _datas(edit)
    assert edit["message_id"] == 777
    assert "CURRENT RUNNING" in edit["text"]
    assert "FEATURE RESOURCE REVIEW" not in edit["text"], \
        "refresh_run does NOT fall through to HOME"
    assert any(d.startswith("feature:resrc_expand_apps:") for d in datas)
    assert any(d.startswith("feature:resrc_expand_chrome:") for d in datas)
    assert any(d.startswith("feature:resrc_refresh_run:") for d in datas)
    assert any(d.startswith("feature:resrc_home:") for d in datas)
    assert "Notepad" not in edit["text"], "collapsed list stays collapsed"
    assert "Gmail" not in edit["text"], "collapsed list stays collapsed"

# ── 15c. Refresh with Applications expanded ──────────────────────────────────


def test_refresh_apps_expanded_keeps_selection_on_new_id(tg, monkeypatch):
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    scans = _refresh_world(reg, monkeypatch)
    with patch.object(service, "_feature_orchestrator", orch):
        # Open Notepad's action panel: Applications expanded, Notepad selected.
        service.process_update(_callback_on_panel(
            "cbSA1", 123, 100, "feature:resrc_app_actions:r1-a201", 777))
        datas1 = _datas(api.edits[-1])
        assert "feature:resrc_app_actions:r1-a201" in datas1
        assert "feature:resrc_refresh_apps:r1-a201" in datas1, \
            "the panel's Refresh row carries the selected app's id"
        service.process_update(_callback_on_panel(
            "cbRF2", 123, 100, "feature:resrc_refresh_apps:r1-a201", 777))

    assert scans == [1], "exactly ONE rescan for the context refresh"
    assert len(api.edits) == 2, "still the SAME message"
    edit = api.edits[-1]
    datas = _datas(edit)
    assert edit["message_id"] == 777
    assert "Applications (" in edit["text"], "Applications stays EXPANDED"
    assert any("feature:resrc_collapse_apps:" in d for d in datas)
    assert any("Notepad" in lbl for lbl in _labels(edit)), "same window (hwnd 201) still listed"
    assert "Gmail" not in edit["text"], "Chrome stays collapsed"
    assert "feature:resrc_app_actions:r2-a201" in datas, \
        "selection re-matched by HWND onto the NEW generation-stamped id"
    assert any(d.startswith("feature:resrc_hide_app_actions")
               for d in datas), \
        "the Notepad actions panel is open for the NEW id"
    assert "feature:resrc_app_actions:r1-a201" not in datas, "old id gone"
    assert "feature:resrc_refresh_apps:r2-a201" in datas, \
        "the Refresh row now carries the NEW selected id"


def test_refresh_apps_expanded_drops_selection_when_window_closed(
        tg, monkeypatch):
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    scans = _refresh_world(reg, monkeypatch, notepad_alive=False)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback_on_panel(
            "cbSA2", 123, 100, "feature:resrc_app_actions:r1-a201", 777))
        service.process_update(_callback_on_panel(
            "cbRF3", 123, 100, "feature:resrc_refresh_apps:r1-a201", 777))

    assert scans == [1]
    assert len(api.edits) == 2
    edit = api.edits[-1]
    datas = _datas(edit)
    assert edit["message_id"] == 777
    assert "Applications (" in edit["text"], "Applications stays EXPANDED"
    assert any("Calculator" in lbl for lbl in _labels(edit)), "still-open applications are kept"
    assert "Notepad" not in edit["text"], "closed window disappears"
    assert not any(d.startswith("feature:resrc_hide_app_actions")
                   for d in datas), \
        "selection is DROPPED when the HWND no longer exists (no actions panel)"
    assert not any(d.startswith("feature:resrc_close:") for d in datas), \
        "no Close action is rendered without a selection"
    assert f"feature:resrc_refresh_apps:{reg._feature_id}" in datas, \
        "the Refresh row falls back to the feature context"


def test_refresh_apps_scan_failure_keeps_context_and_warns(tg, monkeypatch):
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    scans = _refresh_world(reg, monkeypatch)

    def boom(*_a, **_kw):
        scans.append(1)
        raise RuntimeError("laptop unreachable")

    monkeypatch.setattr(reg, "scan", boom)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback_on_panel(
            "cbSA3", 123, 100, "feature:resrc_app_actions:r1-a201", 777))
        service.process_update(_callback_on_panel(
            "cbRF4", 123, 100, "feature:resrc_refresh_apps:r1-a201", 777))

    assert scans == [1], "the failed rescan still ran exactly once"
    assert len(api.edits) == 2, "no crash - the SAME message is edited"
    edit = api.edits[-1]
    datas = _datas(edit)
    assert "Resource refresh failed" in edit["text"], "visible warning"
    assert "Applications (" in edit["text"], "expanded context is kept"
    assert any("Notepad" in lbl for lbl in _labels(edit)), "last known snapshot is still rendered"
    assert "feature:resrc_app_actions:r1-a201" in datas, \
        "selection survives on the last known generation id"
    assert "feature:resrc_refresh_apps:r1-a201" in datas, \
        "Refresh stays available for a retry"

# ── 15d. Refresh with Chrome expanded ────────────────────────────────────────


def test_refresh_chrome_expanded_keeps_cdp_tab_selection_on_new_id(
        tg, monkeypatch):
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    scans = _refresh_world(reg, monkeypatch)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback_on_panel(
            "cbST1", 123, 100, "feature:resrc_tab_actions:r1-t0", 777))
        datas1 = _datas(api.edits[-1])
        assert "feature:resrc_tab_actions:r1-t0" in datas1
        assert "feature:resrc_refresh_chrome:r1-t0" in datas1, \
            "the panel's Refresh row carries the selected tab's id"
        service.process_update(_callback_on_panel(
            "cbRF5", 123, 100, "feature:resrc_refresh_chrome:r1-t0", 777))

    assert scans == [1], "exactly ONE rescan for the context refresh"
    assert len(api.edits) == 2, "still the SAME message"
    edit = api.edits[-1]
    datas = _datas(edit)
    assert edit["message_id"] == 777
    assert "Chrome (" in edit["text"], "Chrome stays EXPANDED"
    assert any("Gmail - Inbox (renamed)" in lbl for lbl in _labels(edit)), \
        "re-matched by CDP page_id even though the tab was RENAMED"
    assert "Calculator" not in edit["text"], "Applications stay collapsed"
    assert "feature:resrc_tab_actions:r2-t0" in datas, \
        "selection re-matched by page_id onto the NEW generation-stamped id"
    assert any(d.startswith("feature:resrc_hide_tab_actions")
               for d in datas), \
        "the Gmail actions panel is open for the NEW id"
    assert "feature:resrc_tab_actions:r1-t0" not in datas, "old id gone"
    assert "feature:resrc_refresh_chrome:r2-t0" in datas, \
        "the Refresh row now carries the NEW selected id"
    assert any("feature:resrc_collapse_chrome:" in d for d in datas)


def test_refresh_chrome_expanded_uia_tab_matches_host_and_runtime(
        tg, monkeypatch):
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    scans = _refresh_world(reg, monkeypatch)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback_on_panel(
            "cbST2", 123, 100, "feature:resrc_tab_actions:r1-t1", 777))
        service.process_update(_callback_on_panel(
            "cbRF6", 123, 100, "feature:resrc_refresh_chrome:r1-t1", 777))

    assert scans == [1]
    assert len(api.edits) == 2
    edit = api.edits[-1]
    datas = _datas(edit)
    assert edit["message_id"] == 777
    assert "Chrome (" in edit["text"], "Chrome stays EXPANDED"
    assert any("Docs" in lbl for lbl in _labels(edit)), "same UIA tab (host 555, runtime 42,7,1)"
    assert "feature:resrc_tab_actions:r2-t2" in datas, \
        "re-matched by host_hwnd + runtime_id even though the tab MOVED " \
        "to another position (r1-t1 -> r2-t2)"
    assert "feature:resrc_tab_actions:r1-t1" not in datas, "old id gone"
    assert "feature:resrc_refresh_chrome:r2-t2" in datas

def test_refresh_chrome_expanded_drops_selection_when_target_closed(
        tg, monkeypatch):
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    scans = _refresh_world(reg, monkeypatch, gmail_alive=False)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback_on_panel(
            "cbST3", 123, 100, "feature:resrc_tab_actions:r1-t0", 777))
        service.process_update(_callback_on_panel(
            "cbRF7", 123, 100, "feature:resrc_refresh_chrome:r1-t0", 777))

    assert scans == [1]
    assert len(api.edits) == 2
    edit = api.edits[-1]
    datas = _datas(edit)
    assert edit["message_id"] == 777
    assert "Chrome (" in edit["text"], "Chrome stays EXPANDED"
    assert "Gmail" not in edit["text"], "closed target disappears"
    assert any("Docs" in lbl for lbl in _labels(edit)), "still-open tabs are kept"
    assert not any(d.startswith("feature:resrc_hide_tab_actions")
                   for d in datas), \
        "selection is DROPPED when the exact tab identity no longer exists"
    assert not any(d.startswith("feature:resrc_focus:") for d in datas), \
        "no Focus action is rendered without a selection"
    assert f"feature:resrc_refresh_chrome:{reg._feature_id}" in datas, \
        "the Refresh row falls back to the feature context"


def test_refresh_chrome_scan_failure_keeps_context_and_warns(tg, monkeypatch):
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    scans = _refresh_world(reg, monkeypatch)

    def boom(*_a, **_kw):
        scans.append(1)
        raise RuntimeError("laptop unreachable")

    monkeypatch.setattr(reg, "scan", boom)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback_on_panel(
            "cbST4", 123, 100, "feature:resrc_tab_actions:r1-t0", 777))
        service.process_update(_callback_on_panel(
            "cbRF8", 123, 100, "feature:resrc_refresh_chrome:r1-t0", 777))

    assert scans == [1], "the failed rescan still ran exactly once"
    assert len(api.edits) == 2, "no crash - the SAME message is edited"
    edit = api.edits[-1]
    datas = _datas(edit)
    assert "Resource refresh failed" in edit["text"], "visible warning"
    assert "Chrome (" in edit["text"], "expanded context is kept"
    assert any("Gmail" in lbl for lbl in _labels(edit)), "last known snapshot is still rendered"
    assert "feature:resrc_tab_actions:r1-t0" in datas, \
        "selection survives on the last known generation id"
    assert "feature:resrc_refresh_chrome:r1-t0" in datas, \
        "Refresh stays available for a retry"

# ── 15e. Back from expanded panels → HOME (NO rescan) ────────────────────────


def test_back_from_expanded_apps_goes_home_without_rescan(tg, monkeypatch):
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    scans = _refresh_world(reg, monkeypatch)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback_on_panel(
            "cbBA1", 123, 100, "feature:resrc_app_actions:r1-a201", 777))
        service.process_update(_callback_on_panel(
            "cbBA2", 123, 100, "feature:resrc_home:", 777))

    assert scans == [], "Back is context-only: NO rescan on the way home"
    edit = api.edits[-1]
    datas = _datas(edit)
    assert edit["message_id"] == 777, "the SAME message is edited"
    assert "FEATURE RESOURCE REVIEW" in edit["text"], \
        "Back lands on Resource Review HOME"
    assert not any("resrc_back" in d for d in datas), "no legacy Back row"
    assert not any("resrc_app_actions" in d for d in datas), \
        "no app list on HOME - it never dumps resource rows"


def test_back_from_expanded_chrome_goes_home_without_rescan(tg, monkeypatch):
    service, orch, api, reg = _seed_registry(monkeypatch, tg)
    scans = _refresh_world(reg, monkeypatch)
    with patch.object(service, "_feature_orchestrator", orch):
        service.process_update(_callback_on_panel(
            "cbBB1", 123, 100, "feature:resrc_tab_actions:r1-t0", 777))
        service.process_update(_callback_on_panel(
            "cbBB2", 123, 100, "feature:resrc_home:", 777))

    assert scans == [], "Back is context-only: NO rescan on the way home"
    edit = api.edits[-1]
    datas = _datas(edit)
    assert edit["message_id"] == 777
    assert "FEATURE RESOURCE REVIEW" in edit["text"], \
        "Back lands on Resource Review HOME"
    assert not any("resrc_back" in d for d in datas)
    assert not any("resrc_tab_actions" in d for d in datas)






# ── PART 9. Continue Feature resumes the pipeline (research enqueued once) ────
#
# Part 8 locked: RESOURCE_REVIEW → final scan/persist → resource_review_complete
# was a HARD STOP. Part 9 adds the missing bridge: the explicit ▶️ Continue must
# resume the existing pipeline by enqueuing RESEARCH EXACTLY ONCE, with the
# persisted ``research_enqueued`` flag as the idempotency source of truth.


from jarvis_core.feature_orchestrator import ACTION_RESEARCH


def _record_research_enqueues(orch, monkeypatch):
    """Replace enqueue with a recorder that only tracks RESEARCH actions."""
    calls: list = []
    monkeypatch.setattr(
        orch, "enqueue",
        lambda action, feature_id: calls.append((action, feature_id)))
    return calls


def _research_count(calls, fid):
    return sum(1 for a, f in calls if a == ACTION_RESEARCH and f == fid)


def test_part9_normal_continue_enqueues_research_once(orch, monkeypatch):
    """A. Normal Continue => research queued exactly once, state advances past
    the review, snapshot/ownership/request preserved, and the persisted
    ``research_enqueued`` flag is set."""
    res = orch.submit_feature_request(
        "add part9 normal feature", source="Telegram",
        telegram_user_id=123, telegram_chat_id=100,
    )
    fid = res["feature_id"]
    assert orch.get_feature(fid)["status"] == FeatureStatus.resource_review.value

    before = orch.get_feature(fid)
    calls = _record_research_enqueues(orch, monkeypatch)

    out = orch.continue_after_resource_review(fid, telegram_user_id=123)

    assert out["status"] == "ok"
    assert out["action"] == "feature_continued"
    assert out["phase"] == "research"
    assert out["research_enqueued"] is True
    assert _research_count(calls, fid) == 1, "research queued exactly once"

    feat = orch.get_feature(fid)
    assert feat["resource_review_completed"] is True
    assert feat["research_enqueued"] is True
    # Preserved identity / ownership / request.
    assert feat["feature_id"] == before["feature_id"]
    assert feat["original_request"] == before["original_request"]


def test_part9_double_continue_click_enqueues_research_once(orch, monkeypatch):
    """B. Double Continue click => still exactly one research enqueue."""
    fid = orch.submit_feature_request(
        "add part9 double-click feature", source="Telegram",
        telegram_user_id=123, telegram_chat_id=100,
    )["feature_id"]

    calls = _record_research_enqueues(orch, monkeypatch)

    orch.continue_after_resource_review(fid, telegram_user_id=123)
    # Second physical tap on the same button.
    orch.continue_after_resource_review(fid, telegram_user_id=123)

    assert _research_count(calls, fid) == 1, "double click still enqueues once"
    assert orch.get_feature(fid)["research_enqueued"] is True


def test_part9_duplicate_callback_enqueues_research_once(orch, monkeypatch):
    """C. Duplicate Telegram callback delivery => still exactly one enqueue.

    A retried/duplicated callback re-invokes continue_after_resource_review
    with identical params; the persisted flag guarantees single enqueue."""
    fid = orch.submit_feature_request(
        "add part9 dup-callback feature", source="Telegram",
        telegram_user_id=123, telegram_chat_id=100,
    )["feature_id"]

    calls = _record_research_enqueues(orch, monkeypatch)

    # Same callback delivered three times.
    for _ in range(3):
        orch.continue_after_resource_review(fid, telegram_user_id=123)

    assert _research_count(calls, fid) == 1, "duplicate callbacks enqueue once"


def test_part9_restart_after_continue_no_duplicate_enqueue(orch, tmp_path, monkeypatch):
    """D. Restart/recovery after a successful Continue => no duplicate enqueue.

    Simulate the crash window: Continue persisted research_enqueued=True and
    state=resource_review_complete but the research stage had not yet set
    ANALYZING. A fresh orchestrator over the SAME store must resume research
    exactly once (never twice)."""
    store = orch._store
    fid = orch.submit_feature_request(
        "add part9 restart feature", source="Telegram",
        telegram_user_id=123, telegram_chat_id=100,
    )["feature_id"]

    calls = _record_research_enqueues(orch, monkeypatch)
    orch.continue_after_resource_review(fid, telegram_user_id=123)
    assert _research_count(calls, fid) == 1

    feat = store.get(fid)
    assert feat.status == FeatureStatus.resource_review_complete.value
    assert feat.research_enqueued is True

    # Fresh orchestrator over the same persisted store == process restart.
    new_orch = FeatureOrchestrator(
        store=store, task_manager_instance=TaskManager())
    new_orch.start_worker = lambda: None  # type: ignore[method-assign]
    new_orch.set_telegram_channels(
        send=lambda chat_id, text, markup=None: None,
        is_authorized=lambda uid: uid == 123,
    )
    resume_calls = _record_research_enqueues(new_orch, monkeypatch)
    try:
        new_orch._restore_pending()
        # A duplicate Continue after restart must also be a no-op.
        new_orch.continue_after_resource_review(fid, telegram_user_id=123)
    finally:
        new_orch.stop_worker(timeout=1)

    # Recovery resumes research once; the duplicate Continue adds nothing.
    resumed = [a for a, f in getattr(new_orch, "_resume_actions", [])
               if a == ACTION_RESEARCH and f == fid]
    total = len(resumed) + _research_count(resume_calls, fid)
    assert total == 1, "restart recovery resumes research exactly once"


def test_part9_scan_failure_review_still_continues(tg, monkeypatch):
    """E. A feature whose resource scan failed still honours ▶️ Continue: it
    progresses and enqueues research once (Resource Review contract)."""
    service, orch, api = tg

    def boom(feature_id=None, cdp_url=None, **_kw):
        raise RuntimeError("UIA exploded")

    with patch.object(resource_manager.resource_registry, "scan",
                      side_effect=boom):
        fid = orch.submit_feature_request(
            "add part9 scan-fail feature", source="Telegram",
            telegram_user_id=123, telegram_chat_id=100,
        )["feature_id"]

    assert orch.get_feature(fid)["status"] == FeatureStatus.resource_review.value

    calls = _record_research_enqueues(orch, monkeypatch)
    # The final scan on Continue also fails, but Continue must still progress.
    with patch.object(resource_manager.resource_registry, "scan",
                      side_effect=boom):
        out = orch.continue_after_resource_review(fid, telegram_user_id=123)

    assert out["status"] == "ok"
    assert _research_count(calls, fid) == 1, "scan-fail Continue still enqueues once"
    assert orch.get_feature(fid)["research_enqueued"] is True


def test_part9_unauthorized_user_cannot_continue(orch, monkeypatch):
    """F. An unauthorized user must NOT continue another user's feature: no
    state change, no research enqueue, flag stays False."""
    fid = orch.submit_feature_request(
        "add part9 owned feature", source="Telegram",
        telegram_user_id=123, telegram_chat_id=100,
    )["feature_id"]

    calls = _record_research_enqueues(orch, monkeypatch)
    out = orch.continue_after_resource_review(fid, telegram_user_id=999)

    assert out.get("status") != "ok" or out.get("action") != "feature_continued"
    assert _research_count(calls, fid) == 0, "unauthorized user enqueues nothing"
    feat = orch.get_feature(fid)
    assert feat["research_enqueued"] is False
    assert feat["status"] == FeatureStatus.resource_review.value
