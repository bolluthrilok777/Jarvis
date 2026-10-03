"""8C-3F: Applications / Chrome heading EXPAND-COLLAPSE dispatch.

The four heading callbacks rendered by the CURRENT RUNNING menus —
``resrc_expand_apps``, ``resrc_collapse_apps``, ``resrc_expand_chrome`` and
``resrc_collapse_chrome`` — must be dispatched by the resource review handler:

* the SAME Telegram message is edited in place (same ``message_id``);
* NO rescan runs — the CURRENT ResourceRegistry snapshot only;
* no extra message is sent (in particular no "Not available" fallback);
* Applications expanded keeps Chrome collapsed and vice versa.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from jarvis_core import resource_manager
from jarvis_core.feature_orchestrator import FeatureOrchestrator
from jarvis_core.feature_store import FeatureStore
from jarvis_core.resource_manager import Resource
from jarvis_core.task_manager import TaskManager
from jarvis_core.telegram_service import TelegramService

from tests.test_telegram_integration import FakeAPI, _settings

FID = "f-exp"
PANEL_MESSAGE_ID = 777


@pytest.fixture()
def orch(tmp_path):
    store = FeatureStore(root=tmp_path / "features")
    orchestrator = FeatureOrchestrator(store=store,
                                       task_manager_instance=TaskManager())
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
    return service, api


def _reset_registry():
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


@pytest.fixture(autouse=True)
def _hermetic_registry():
    """Guarantee a clean generation-0 registry per test, reset after too."""
    _reset_registry()
    yield
    _reset_registry()


@pytest.fixture()
def world(monkeypatch):
    """A CURRENT registry snapshot: 2 apps + 1 Chrome window (2 tabs).

    ``ResourceRegistry.scan`` is instrumented so every test can PROVE that
    expand / collapse NEVER rescans — the panel is re-rendered from the
    in-memory snapshot only.
    """
    reg = resource_manager.resource_registry
    reg._generation = 1
    reg._feature_id = FID
    reg._chrome_detected = True
    reg._chrome_controllable = True
    reg._chrome_control_mode = "cdp"
    reg._chrome_reason = ""
    reg._resources = {
        "r1-a200": Resource(
            resource_id="r1-a200", feature_id=FID,
            resource_type="application", title="Calculator",
            display_name="Calculator", pid=200, window_handle=200),
        "r1-a201": Resource(
            resource_id="r1-a201", feature_id=FID,
            resource_type="application", title="Notepad",
            display_name="Notepad", pid=201, window_handle=201),
        "r1-t0": Resource(
            resource_id="r1-t0", feature_id=FID,
            resource_type="browser_tab", title="Gmail",
            display_name="Chrome",
            safe_display_target="https://mail.google.com",
            control_mode="cdp", page_id="tgt-gmail-1", host_hwnd=555,
            tab_index=0),
        "r1-t1": Resource(
            resource_id="r1-t1", feature_id=FID,
            resource_type="browser_tab", title="Docs",
            display_name="Chrome", control_mode="uia", host_hwnd=555,
            runtime_id="42,7,1", tab_index=1),
    }

    scans: list = []

    def _record_scan(*args, **kwargs):
        scans.append(1)
        return reg.scan_result(include_identity=True)

    monkeypatch.setattr(reg, "scan", _record_scan)
    return scans


def _callback_on_panel(callback_id, data):
    """Callback pressed on the CURRENT RUNNING panel message (message_id)."""
    return {
        "update_id": 10,
        "callback_query": {
            "id": callback_id,
            "from": {"id": 123},
            "message": {"chat": {"id": 100},
                        "message_id": PANEL_MESSAGE_ID},
            "data": data,
        },
    }


def _tap(service, callback_id, action):
    """Press one Applications / Chrome heading button on the panel."""
    service.process_update(_callback_on_panel(
        callback_id, f"feature:{action}:{FID}"))


def _datas(edit) -> list:
    return [b["callback_data"]
            for row in (edit["reply_markup"]["inline_keyboard"]
                        if edit["reply_markup"] else [])
            for b in row]


# ── 1. Expand Applications ───────────────────────────────────────────────────


def test_8c3f_expand_apps_heading(tg, world):
    service, api = tg
    _tap(service, "cbEA", "resrc_expand_apps")

    assert len(api.edits) == 1, "the SAME panel message is edited in place"
    edit = api.edits[0]
    assert edit["message_id"] == PANEL_MESSAGE_ID, "same message_id kept"
    text, datas = edit["text"], _datas(edit)
    assert "CURRENT RUNNING" in text
    assert "Applications (2)" in text
    assert "feature:resrc_app_actions:r1-a200" in datas and \
        "feature:resrc_app_actions:r1-a201" in datas, \
        "Applications EXPANDED (both apps listed as clickable buttons)"
    assert "Gmail" not in text and "Docs" not in text, \
        "Chrome stays COLLAPSED (no tab names in the body)"
    assert "feature:resrc_collapse_apps:" in datas, \
        "Applications heading is now the Collapse button"
    assert "resrc_expand_apps" not in datas, \
        "Applications no longer offers Expand while expanded"
    assert "feature:resrc_expand_chrome:" in datas, \
        "Chrome offers Expand"
    assert "Not available" not in text, "no Not-available text"
    assert api.messages == [], \
        "no extra message (the old bug sent a Not-available fallback)"
    assert world == [], "expand NEVER rescans - current snapshot only"


# ── 2. Collapse Applications ─────────────────────────────────────────────────


def test_8c3f_collapse_apps_heading(tg, world):
    service, api = tg
    _tap(service, "cbCA", "resrc_collapse_apps")

    assert len(api.edits) == 1, "the SAME panel message is edited in place"
    edit = api.edits[0]
    assert edit["message_id"] == PANEL_MESSAGE_ID, "same message_id kept"
    text, datas = edit["text"], _datas(edit)
    assert "CURRENT RUNNING" in text, "collapsed CURRENT RUNNING panel"
    assert "Applications (2)" in text, "Applications heading (collapsed)"
    assert "Calculator" not in text and "Notepad" not in text, \
        "app list hidden again"
    assert "Gmail" not in text and "Docs" not in text, "Chrome stays collapsed"
    assert "feature:resrc_expand_apps:" in datas, \
        "Applications offers Expand again"
    assert "feature:resrc_expand_chrome:" in datas, \
        "Chrome still offers Expand"
    assert "resrc_collapse" not in " ".join(datas), \
        "nothing offers Collapse on the fully collapsed panel"
    assert "Not available" not in text, "no Not-available text"
    assert api.messages == [], \
        "no extra message (the old bug sent a Not-available fallback)"
    assert world == [], "collapse NEVER rescans - current snapshot only"

# ── 3. Expand Chrome ─────────────────────────────────────────────────────────


def test_8c3f_expand_chrome_heading(tg, world):
    service, api = tg
    _tap(service, "cbEC", "resrc_expand_chrome")

    # New account-grouped UX: tapping Chrome opens the ACCOUNT LIST (not the
    # flat tab list). These 2 tabs have no resolvable identity, so they form a
    # single "Other / Unknown" account group.
    assert len(api.edits) == 1, "the SAME panel message is edited in place"
    edit = api.edits[0]
    assert edit["message_id"] == PANEL_MESSAGE_ID, "same message_id kept"
    text, datas = edit["text"], _datas(edit)
    assert "CHROME" in text
    account_rows = [d for d in datas if "feature:resrc_chrome_account:" in d]
    assert len(account_rows) == 1, "one Other/Unknown account group for 2 tabs"
    # Tabs are NOT shown directly at the account-list level.
    assert not any("resrc_tab_actions" in d for d in datas)
    assert "Calculator" not in text and "Notepad" not in text, \
        "Applications stay COLLAPSED (no app list)"
    assert "feature:resrc_refresh_chrome_accounts:" in datas, "Refresh present"
    assert api.messages == [], "no extra message"
    assert world == [], "expand NEVER rescans - current snapshot only"

    # Drilling into the account then shows exactly those 2 tabs as buttons.
    service.process_update(_callback_on_panel("cbEC2", account_rows[0]))
    datas2 = _datas(api.edits[-1])
    assert "feature:resrc_tab_actions:r1-t0" in datas2
    assert "feature:resrc_tab_actions:r1-t1" in datas2


# ── 4. Collapse Chrome ───────────────────────────────────────────────────────


def test_8c3f_collapse_chrome_heading(tg, world):
    service, api = tg
    _tap(service, "cbCC", "resrc_collapse_chrome")

    assert len(api.edits) == 1, "the SAME panel message is edited in place"
    edit = api.edits[0]
    assert edit["message_id"] == PANEL_MESSAGE_ID, "same message_id kept"
    text, datas = edit["text"], _datas(edit)
    assert "CURRENT RUNNING" in text, "collapsed CURRENT RUNNING panel"
    assert "Chrome (2)" in text, "Chrome heading (collapsed)"
    assert "Gmail" not in text and "Docs" not in text, "tab list hidden again"
    assert "Calculator" not in text and "Notepad" not in text, \
        "Applications stay collapsed"
    assert "feature:resrc_expand_chrome:" in datas, \
        "Chrome offers Expand again"
    assert "feature:resrc_expand_apps:" in datas, \
        "Applications still offers Expand"
    assert "resrc_collapse" not in " ".join(datas), \
        "nothing offers Collapse on the fully collapsed panel"
    assert "Not available" not in text, "no Not-available text"
    assert api.messages == [], \
        "no extra message (the old bug sent a Not-available fallback)"
    assert world == [], "collapse NEVER rescans - current snapshot only"

