"""Targeted tests: CHROME APPLICATION WINDOW vs INDIVIDUAL CHROME TABS.

Regression coverage for the real physical-test failure where Chrome appeared
as ONE resource and a supposed "tab" close took down the whole Chrome window.

Guarantees locked in here (everything destructive is mocked - the real
browser is never touched by automated tests):

* layered discovery: CDP targetIds first, Windows UIA tab-strip fallback
* every tab carries a full identity tuple (hwnd + runtime-id + index + title)
* exact focus / exact screenshot / exact close for ONE tab
* ``close_application_window`` and ``close_browser_tab`` are separate
  backends that REJECT each other's resource type
* a tab close never posts WM_CLOSE to Chrome and never kills processes;
  Chrome itself stays open and exactly one tab disappears
"""
from __future__ import annotations

import inspect
import types
from unittest.mock import Mock

import PIL.Image
import pytest

from jarvis_core import (
    browser_resource_manager,
    resource_manager,
    uia_browser_manager,
)
from jarvis_core.resource_manager import Resource, ResourceRegistry
from jarvis_core.uia_browser_manager import UIATabError


# ── Fakes for the pywinauto UIA surface ──────────────────────────────────────


class FakeCloseButton:
    def __init__(self):
        self.invoked = False

    def window_text(self):
        return "Close"

    def invoke(self):
        self.invoked = True


class FakeTabItem:
    def __init__(self, title, runtime_id, children=None, selected=False):
        self._title = title
        self._runtime_id = runtime_id
        self._children = children or []
        self._selected = selected

    @property
    def element_info(self):
        return types.SimpleNamespace(runtime_id=list(self._runtime_id))

    def window_text(self):
        return self._title

    def children(self, control_type=None):
        return list(self._children)

    def select(self):
        self._selected = True

    def is_selected(self):
        return self._selected


class FakeChromeWindow:
    def __init__(self, items):
        self._items = items
        self.typed = None

    def descendants(self, control_type=None):
        return list(self._items)

    def set_focus(self):
        return True

    def type_keys(self, keys):
        self.typed = keys


def _png(path, size=(5, 5)):
    PIL.Image.new("RGB", size, (30, 30, 220)).save(path, "PNG")
    return {"file": str(path), "width": size[0], "height": size[1]}


# ── Discovery ────────────────────────────────────────────────────────────────


def test_cdp_discovery_enumerates_page_targets_with_target_ids(monkeypatch):
    targets = [
        {"type": "page", "title": "ChatGPT",
         "url": "https://chatgpt.com/c/abc?token=SECRET", "id": "T1"},
        {"type": "page", "title": "Docs",
         "url": "https://docs.example.com/x", "id": "T2"},
        {"type": "service_worker", "title": "sw", "url": "x", "id": "S9"},
    ]
    response = Mock()
    response.json.return_value = targets
    monkeypatch.setattr(browser_resource_manager, "cdp_reachable",
                        lambda url, **k: True)
    monkeypatch.setattr(browser_resource_manager.requests, "get",
                        lambda *a, **k: response)

    result = browser_resource_manager.discover_tabs("http://127.0.0.1:9222")

    assert result["controllable"] is True
    assert result["control_mode"] == "cdp"
    ids = {t["target_id"] for t in result["tabs"]}
    assert ids == {"T1", "T2"}, "only page targets become controllable tabs"
    chatgpt = next(t for t in result["tabs"] if t["target_id"] == "T1")
    assert "SECRET" not in chatgpt["safe_display_target"], \
        "query strings are never exposed"


def test_uia_fallback_used_when_cdp_unreachable(monkeypatch):
    monkeypatch.setattr(browser_resource_manager,
                        "chrome_processes_running", lambda: True)
    monkeypatch.setattr(browser_resource_manager, "cdp_reachable",
                        lambda url, **k: False)
    monkeypatch.setattr(
        "jarvis_core.uia_browser_manager.enumerate_tabs",
        lambda *a, **k: {"ok": True, "reason": "", "tabs": [
            {"host_hwnd": 555, "title": "ChatGPT",
             "runtime_id": [42, 555, 4], "tab_index": 0,
             "selected": False, "control_mode": "uia"},
            {"host_hwnd": 555, "title": "Jarvis localhost",
             "runtime_id": [42, 555, 9], "tab_index": 1,
             "selected": True, "control_mode": "uia"},
            {"host_hwnd": 555, "title": "Google",
             "runtime_id": [42, 555, 2], "tab_index": 2,
             "selected": False, "control_mode": "uia"},
            {"host_hwnd": 555, "title": "YouTube",
             "runtime_id": [42, 555, 7], "tab_index": 3,
             "selected": False, "control_mode": "uia"},
        ]},
    )

    result = browser_resource_manager.discover_tabs()

    assert result["controllable"] is True
    assert result["control_mode"] == "uia"
    titles = [t["title"] for t in result["tabs"]]
    assert titles == ["ChatGPT", "Jarvis localhost", "Google", "YouTube"]


# ── Registry: identity storage ───────────────────────────────────────────────


def _seed_uia_registry(registry, tabs):
    registry._generation = 1
    registry._feature_id = None
    registry._cdp_url = None
    registry._chrome_detected = True
    registry._chrome_controllable = True
    registry._chrome_control_mode = "uia"
    resources = {}
    for index, (rid, title, rtid, idx) in enumerate(tabs):
        resources[rid] = Resource(
            resource_id=rid, feature_id=None, resource_type="browser_tab",
            title=title, display_name="Chrome",
            control_mode="uia", host_hwnd=555,
            runtime_id=",".join(str(x) for x in rtid), tab_index=idx,
        )
    registry._resources = resources


def test_multiple_tabs_keep_distinct_identities():
    reg = ResourceRegistry()
    _seed_uia_registry(reg, [
        ("r1-t1", "ChatGPT", [42, 555, 4], 0),
        ("r1-t2", "Jarvis localhost", [42, 555, 9], 1),
        ("r1-t3", "YouTube", [42, 555, 7], 2),
    ])
    pubs = {rid: reg.get(rid).public() for rid in ("r1-t1", "r1-t2", "r1-t3")}
    runtime_ids = {p["runtime_id"] for p in pubs.values()}
    assert len(runtime_ids) == 3, "title alone is never the identity"
    assert all(p["control_mode"] == "uia" for p in pubs.values())
    assert all(p["host_hwnd"] == 555 for p in pubs.values())
    assert [pubs["r1-t1"]["tab_index"], pubs["r1-t3"]["tab_index"]] == [0, 2]


# ── Exact tab actions through the registry ───────────────────────────────────


@pytest.fixture()
def uia_reg():
    reg = ResourceRegistry()
    _seed_uia_registry(reg, [
        ("r1-t1", "ChatGPT", [42, 555, 4], 0),
        ("r1-t2", "Docs", [42, 555, 8], 1),
    ])
    yield reg
    reg._generation = 0
    reg._resources = {}


def test_focus_tab_uses_exact_uia_identity(uia_reg, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "jarvis_core.uia_browser_manager.focus_tab",
        lambda hwnd, rtid, title, idx:
            calls.append((hwnd, list(rtid), title, idx)) or True,
    )
    out = uia_reg.focus("r1-t2")
    assert out["status"] == "ok"
    assert calls == [(555, [42, 555, 8], "Docs", 1)]


def test_focus_tab_honest_error_when_strip_changed(uia_reg, monkeypatch):
    def _boom(hwnd, rtid, title, idx):
        raise UIATabError("Could not safely identify that tab")

    monkeypatch.setattr("jarvis_core.uia_browser_manager.focus_tab", _boom)
    out = uia_reg.focus("r1-t1")
    assert out["status"] == "error"
    assert "safely identify" in out["message"]


def test_screenshot_tab_selects_exact_tab_then_captures_host(
    uia_reg, monkeypatch, tmp_path
):
    monkeypatch.setattr(resource_manager, "SCREENSHOT_DIR", tmp_path)
    focus_calls, capture_calls = [], []

    monkeypatch.setattr(
        "jarvis_core.uia_browser_manager.focus_tab",
        lambda hwnd, rtid, title, idx:
            focus_calls.append((hwnd, list(rtid), title, idx)) or True,
    )

    def _capture(hwnd, path):
        capture_calls.append(hwnd)
        return _png(path)

    monkeypatch.setattr(
        "jarvis_core.screenshot_service.capture_window", _capture,
    )

    out = uia_reg.screenshot("r1-t1")
    assert out["status"] == "ok"
    assert focus_calls == [(555, [42, 555, 4], "ChatGPT", 0)]
    assert capture_calls == [555], \
        "capture targets the EXACT Chrome window hosting the selected tab"
    assert out["bytes"] > 0


# ── Backend separation guards ────────────────────────────────────────────────


def test_application_close_rejects_browser_tab_id(uia_reg):
    out = uia_reg.close_application_window("r1-t1")
    assert out["status"] == "rejected"
    assert "tab" in out["message"].lower()


def test_tab_close_rejects_application_resource(monkeypatch):
    reg = ResourceRegistry()
    reg._generation = 1
    app = Resource(resource_id="r1-a5", feature_id=None,
                   resource_type="application", title="Calc",
                   display_name="Calculator", window_handle=77)
    reg._resources = {app.resource_id: app}
    out = reg.close_browser_tab("r1-a5")
    assert out["status"] == "rejected"



# ── UIA close_tab mechanics (fake pywinauto surface) ─────────────────────────


def _patch_uia_env(monkeypatch, windows):
    """Patch host discovery/attach + time so the real manager logic runs."""
    monkeypatch.setattr(uia_browser_manager, "chrome_host_windows",
                        lambda: [{"hwnd": 555, "title": "x", "pid": 1}])
    state = {"attach_calls": 0}

    def _attach(_hwnd):
        state["attach_calls"] += 1
        return windows[min(state["attach_calls"] - 1, len(windows) - 1)]

    monkeypatch.setattr(uia_browser_manager, "_attach", _attach)
    monkeypatch.setattr(uia_browser_manager.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.is_window_alive",
        lambda hwnd: hwnd == 555,
    )
    return state


def test_uia_close_prefers_per_tab_close_button_and_verifies(monkeypatch):
    button = FakeCloseButton()
    docs = FakeTabItem("Docs", [42, 555, 8], children=[button])
    keep = FakeTabItem("Keep", [42, 555, 9])
    before = FakeChromeWindow([keep, docs])
    after = FakeChromeWindow([keep])          # exactly one tab gone
    _patch_uia_env(monkeypatch, [before, after, after, after])

    outcome = uia_browser_manager.close_tab(555, [42, 555, 8], "Docs", 1)

    assert button.invoked is True, "the tab's OWN Close button is preferred"
    assert outcome["ok"] is True
    assert outcome["verified"] is True
    assert outcome["method"] == "uia_close_button"


def test_uia_close_ctrl_w_fallback_only_after_verified_selection(monkeypatch):
    docs = FakeTabItem("Docs", [42, 555, 8])   # no Close-button child
    keep = FakeTabItem("Keep", [42, 555, 9])
    before = FakeChromeWindow([keep, docs])
    after = FakeChromeWindow([keep])
    _patch_uia_env(monkeypatch, [before, after, after, after])
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.foreground_window",
        lambda: 555,
    )

    outcome = uia_browser_manager.close_tab(555, [42, 555, 8], "Docs", 1)

    assert before.typed == "^w"
    assert outcome["verified"] is True


def test_uia_close_refuses_when_tab_cannot_be_identified(monkeypatch):
    keep = FakeTabItem("Keep", [42, 555, 9])
    window = FakeChromeWindow([keep])
    _patch_uia_env(monkeypatch, [window])

    outcome = uia_browser_manager.close_tab(555, [42, 555, 8], "Docs", 1)

    assert outcome["ok"] is False
    assert "safely identify" in outcome["reason"]
    assert window.typed is None, "no keys are sent when identity is unsure"


def test_uia_close_unverified_when_count_does_not_drop(monkeypatch):
    button = FakeCloseButton()
    docs = FakeTabItem("Docs", [42, 555, 8], children=[button])
    keep = FakeTabItem("Keep", [42, 555, 9])
    window = FakeChromeWindow([keep, docs])   # count never drops
    _patch_uia_env(monkeypatch, [window])

    outcome = uia_browser_manager.close_tab(555, [42, 555, 8], "Docs", 1)

    assert outcome["ok"] is False
    assert outcome["verified"] is False
    assert "still present" in outcome["reason"]


# ── Registry-level close: separation + Chrome stays alive ────────────────────


def test_registry_tab_close_never_touches_window_or_processes(
    uia_reg, monkeypatch
):
    wm_close_calls, kill_calls = [], []
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.close_window",
        lambda hwnd: wm_close_calls.append(hwnd) or True,
    )
    monkeypatch.setattr(
        uia_browser_manager,
        "close_tab",
        lambda hwnd, rtid, title, idx:
            {"ok": True, "verified": True, "method": "uia_close_button"},
    )

    out = uia_reg.close_browser_tab("r1-t2")

    assert out["status"] == "ok" and out["verified"] is True
    assert out["message"].startswith("\u2705 Closed tab:")
    assert wm_close_calls == [], "WM_CLOSE must never be used for a TAB"
    source = inspect.getsource(uia_browser_manager.close_tab).lower()
    assert "wm_close" not in source and "taskkill" not in source
    assert "terminateprocess" not in source and ".kill(" not in source


def test_registry_dispatches_by_type_and_reports_unverified(uia_reg,
                                                             monkeypatch):
    app_close, uia_close = [], []
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.close_window",
        lambda hwnd: app_close.append(hwnd) or True,
    )
    monkeypatch.setattr(
        uia_browser_manager, "close_tab",
        lambda hwnd, rtid, title, idx:
            uia_close.append((hwnd, title)) or
            {"ok": True, "verified": False, "method": "ctrl_w_active_tab"},
    )

    out = uia_reg.close_browser_tab("r1-t1")
    assert out["status"] == "ok" and out["verified"] is False
    assert "\u26a0\ufe0f" in out["message"], \
        "unverified closure is reported honestly, never claimed as success"

    dispatched = uia_reg.close("r1-t2")     # legacy dispatcher routes by type
    assert uia_close and app_close == []


def test_stale_tab_resource_is_rejected(uia_reg):
    uia_reg._generation = 2                  # refresh bumped the generation
    out = uia_reg.close_browser_tab("r1-t1")
    assert out["status"] == "stale"
    assert "Refresh" in out["message"]


# ── Telegram: Close Tab vs Close Application routing ─────────────────────────


def _cb(cb_id, user_id, data, chat_id=100):
    return {
        "update_id": 10,
        "callback_query": {
            "id": cb_id,
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}},
            "data": data,
        },
    }


def _fake_scan(feature_id=None, cdp_url=None, **_kw):
    return {
        "generation": 1, "feature_id": feature_id,
        "applications": [], "tabs": [],
        "chrome_detected": True, "chrome_controllable": True,
        "chrome_reason": "", "chrome_control_mode": "uia",
        "scanned_at": "",
    }


def test_telegram_close_tab_routes_only_to_tab_backend(monkeypatch):
    from jarvis_core.telegram_service import TelegramService
    from tests.test_telegram_integration import FakeAPI, _settings

    reg = resource_manager.resource_registry
    _seed_uia_registry(reg, [("r1-t1", "ChatGPT", [42, 555, 4], 0),
                             ("r1-t2", "Docs", [42, 555, 8], 1)])
    reg._feature_id = None
    monkeypatch.setattr(reg, "scan", _fake_scan)
    wm_close_calls = []
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.close_window",
        lambda hwnd: wm_close_calls.append(hwnd) or True,
    )
    uia_close_calls = []
    monkeypatch.setattr(
        uia_browser_manager, "close_tab",
        lambda hwnd, rtid, title, idx:
            uia_close_calls.append((hwnd, list(rtid), title, idx)) or
            {"ok": True, "verified": True, "method": "uia_close_button"},
    )

    api = FakeAPI()
    service = TelegramService(settings=_settings(123), api=api)
    service._start_task_watcher = Mock()

    from unittest.mock import patch as _patch

    try:
        with _patch.object(service, "_feature_orchestrator", Mock()):
            service.process_update(_cb("cbT1", 123, "feature:resrc_close:r1-t2"))

        assert uia_close_calls == [(555, [42, 555, 8], "Docs", 1)], \
            "the TAB backend handled the request"
        assert wm_close_calls == [], \
            "an application-window close must be unreachable for a tab id"
        assert reg.get("r1-t2") is not None and reg.get("r1-t2").status == "closed"
        texts = [m["text"] for m in api.messages]
        assert any("Closed tab: Docs" in t for t in texts)
    finally:
        reg._generation = 0
        reg._resources = {}


def test_telegram_close_application_uses_window_backend_only(monkeypatch):
    from jarvis_core.telegram_service import TelegramService
    from tests.test_telegram_integration import FakeAPI, _settings

    reg = resource_manager.resource_registry
    reg._generation = 1
    reg._feature_id = None
    app = Resource(resource_id="r1-a3", feature_id=None,
                   resource_type="application", title="Calculator",
                   display_name="Calculator", window_handle=303,
                   close_requires_confirmation=False)
    reg._resources = {app.resource_id: app}
    tab_backend = Mock(return_value={"status": "ok"})
    monkeypatch.setattr(reg, "close_browser_tab", tab_backend)
    monkeypatch.setattr(
        "jarvis_core.windows_resource_manager.close_window",
        lambda hwnd: True,
    )
    try:
        api = FakeAPI()
        service = TelegramService(settings=_settings(123), api=api)
        service._start_task_watcher = Mock()

        from unittest.mock import patch as _patch

        with _patch.object(service, "_feature_orchestrator", Mock()):
            service.process_update(
                _cb("cbA1", 123, "feature:resrc_close:r1-a3"))

        assert tab_backend.call_count == 0, \
            "an application close must never reach the tab backend"
        assert any("Closed" in m["text"] for m in api.messages)
    finally:
        reg._generation = 0
        reg._resources = {}


def test_telegram_risky_tab_requires_confirmation_then_closes_exactly(
    monkeypatch,
):
    from jarvis_core.telegram_service import TelegramService
    from tests.test_telegram_integration import FakeAPI, _settings

    reg = resource_manager.resource_registry
    _seed_uia_registry(reg, [("r1-t1", "ChatGPT", [42, 555, 4], 0),
                             ("r1-t2", "Docs", [42, 555, 8], 1)])
    reg._feature_id = None
    monkeypatch.setattr(reg, "scan", _fake_scan)
    docs = reg.get("r1-t2")
    docs.close_requires_confirmation = True   # e.g. an editor tab
    closed = []
    monkeypatch.setattr(
        uia_browser_manager, "close_tab",
        lambda hwnd, rtid, title, idx: closed.append(title) or
        {"ok": True, "verified": True, "method": "uia_close_button"},
    )

    api = FakeAPI()
    service = TelegramService(settings=_settings(123), api=api)
    service._start_task_watcher = Mock()

    from unittest.mock import patch as _patch

    try:
        with _patch.object(service, "_feature_orchestrator", Mock()):
            service.process_update(
                _cb("cbR1", 123, "feature:resrc_close:r1-t2"))
        assert closed == [], "nothing closes before explicit confirmation"
        confirm_rows = api.messages[-1]["reply_markup"]["inline_keyboard"][0]
        datas = [b["callback_data"] for b in confirm_rows]
        assert f"feature:resrc_confirm_close:r1-t2" in datas
        assert "feature:resrc_tab_show:r1-t2" in datas, \
            "Cancel restores the SAME tab detail in place"

        with _patch.object(service, "_feature_orchestrator", Mock()):
            service.process_update(
                _cb("cbR2", 123, f"feature:resrc_confirm_close:r1-t2"))
        assert closed == ["Docs"], "confirmation closes exactly that one tab"
    finally:
        reg._generation = 0
        reg._resources = {}


def test_telegram_stale_tab_shows_refresh_hint(uia_reg):
    from jarvis_core.telegram_service import TelegramService
    from tests.test_telegram_integration import FakeAPI, _settings

    uia_reg._generation = 9                  # any press is now stale
    api = FakeAPI()
    service = TelegramService(settings=_settings(123), api=api)
    service._start_task_watcher = Mock()

    from unittest.mock import patch as _patch

    with _patch.object(service, "_feature_orchestrator", Mock()):
        service.process_update(_cb("cbS1", 123, "feature:resrc_shot:r1-t1"))
        service.process_update(_cb("cbS2", 123, "feature:resrc_close:r1-t1"))

    texts = [m["text"] for m in api.messages]
    assert sum("no longer in the current scan" in t for t in texts) == 2

