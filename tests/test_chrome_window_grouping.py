"""Targeted tests for Chrome window/profile identification and grouping.

Covers the layered account-identity ladder, per-window tab grouping by exact
parent HWND, same-account multi-window handling, the All Apps & Tabs overview,
pagination, stale-button rejection, privacy (no tokens/credentials), and the
new ``resrc_wmenu`` / ``resrc_wtabs`` / ``resrc_allapps`` / ``resrc_home``
Telegram dispatch arms. Destructive actions are mocked - nothing real is
touched, and no credentials/tokens are ever exposed.
"""
from __future__ import annotations

import json

import pytest

from jarvis_core import chrome_profile_identity as cpi
from jarvis_core.resource_menus import (
    build_all_apps_menu,
    build_chrome_accounts_menu,
    build_resource_review_home,
    build_window_tabs_menu,
)

EMAIL_1 = "user1@gmail.com"
EMAIL_2 = "user2@gmail.com"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _tab(rid, hwnd, title, idx=0, email="", profile="", source="",
         selected=False):
    identity_label = email or (f"Chrome \u2014 {profile}" if profile else "")
    return {
        "resource_id": rid,
        "type": "browser_tab",
        "title": title,
        "display_name": "Chrome",
        "host_hwnd": hwnd,
        "tab_index": idx,
        "selected": selected,
        "account_email": email,
        "profile_label": profile,
        "identity_label": identity_label,
        "identity_source": source,
    }


def _app(rid, title="VS Code", name="VS Code", exe="Code.exe", hwnd=100):
    return {
        "resource_id": rid,
        "type": "application",
        "title": title,
        "display_name": name,
        "executable": exe,
        "window_handle": hwnd,
    }


def _scan(apps=None, tabs=None, chrome_detected=True, controllable=True):
    return {
        "applications": apps or [],
        "tabs": tabs or [],
        "chrome_detected": chrome_detected,
        "chrome_controllable": controllable,
    }


def _buttons(rows):
    return [b["callback_data"] for row in rows for b in row]


# ── 1. Chrome window -> profile/account email identification ─────────────────


def test_identify_window_proven_email_from_uia_plus_metadata(monkeypatch):
    """A UIA chip matching a known signed-in email wins (identity_source=uia)."""
    meta = {"profiles": [{
        "profile_dir": "Default",
        "name": "Work",
        "gaia_name": "Work",
        "user_name": EMAIL_1,
    }], "sources": ["C:/fake/User Data"]}

    def _fake_probe(hwnd):
        assert hwnd == 500
        return [EMAIL_1, "Work", "Chrome"]

    monkeypatch.setattr(cpi, "_probe_window_strings", _fake_probe)
    result = cpi.identify_window(500, metadata=meta)
    assert result["email"] == EMAIL_1
    assert result["profile_label"] == "Work"
    assert result["identity_source"] == "uia"
    assert result["confidence"] == "high"


def test_identify_window_fallback_when_email_unavailable(monkeypatch):
    """No proven email -> caller must show an honest profile/window label."""
    meta = {"profiles": [{
        "profile_dir": "Default", "name": "Work", "gaia_name": "",
        "user_name": "",
    }], "sources": ["C:/fake/User Data"]}

    def _fake_probe(hwnd):
        return ["Work"]  # a profile-name chip only, no email.

    monkeypatch.setattr(cpi, "_probe_window_strings", _fake_probe)
    result = cpi.identify_window(500, metadata=meta)
    assert result["email"] == ""
    assert result["profile_label"] == "Work"
    label = cpi.display_label_for(result, window_number=1)
    assert label == "Chrome \u2014 Work"


def test_identify_window_nothing_proven_uses_window_ordinal(monkeypatch):
    monkeypatch.setattr(cpi, "_probe_window_strings", lambda hwnd: [])
    meta = {"profiles": [], "sources": []}
    result = cpi.identify_window(500, metadata=meta)
    assert result["email"] == ""
    assert result["profile_label"] == ""
    assert cpi.display_label_for(result, window_number=2) == \
        "Chrome \u2014 Window 2"


def test_local_state_parse_keeps_only_safe_fields(tmp_path):
    """Secondary metadata layer: only 4 display keys survive; secrets never
    read/retained even if present in the raw JSON."""
    base = tmp_path / "User Data"
    base.mkdir()
    (base / "Local State").write_text(json.dumps({
        "account_tracker_accounts": [{"email": "naughty@example.com"}],
        "profile": {
            "info_cache": {
                "Default": {
                    "name": "Work",
                    "user_name": EMAIL_1,
                    "gaia_name": "Work",
                    "gaia_id": "123456789",
                    "last_used": "2020-01-01",
                },
            },
        },
    }), encoding="utf-8")
    meta = cpi.chrome_profile_metadata(user_data_dirs=[str(base)])
    assert meta["sources"] == [str(base)]
    profiles = meta["profiles"]
    assert len(profiles) == 1
    profile = profiles[0]
    assert profile["user_name"] == EMAIL_1
    assert profile["name"] == "Work"
    assert set(profile.keys()) == {
        "profile_dir", "name", "gaia_name", "user_name",
    }
    assert "gaia_id" not in profile
    assert "last_used" not in profile


def test_single_profile_attribution_by_elimination(monkeypatch):
    """Exactly one signed-in profile in running Chrome => inferred label."""
    meta = {"profiles": [{
        "profile_dir": "Default",
        "name": "Work",
        "gaia_name": "Work",
        "user_name": EMAIL_1,
    }], "sources": ["C:/fake/User Data"]}
    monkeypatch.setattr(cpi, "_probe_window_strings", lambda hwnd: [])
    result = cpi.identify_window(500, metadata=meta)
    assert result["email"] == EMAIL_1
    assert result["identity_source"] == "meta-single"
    assert result["confidence"] == "inferred-single-profile"


def test_same_account_two_windows_not_merged():
    """Display label disambiguates with --- Window N; each HWND stays distinct."""
    identity = {"email": EMAIL_1, "profile_label": "Work",
                "identity_source": "meta-single"}
    label1 = cpi.display_label_for(identity, window_number=1,
                                   duplicate_of_same_account=True)
    label2 = cpi.display_label_for(identity, window_number=2,
                                   duplicate_of_same_account=True)
    assert label1 == f"{EMAIL_1} \u2014 Window 1"
    assert label2 == f"{EMAIL_1} \u2014 Window 2"


# ── 2/3/4. Building the per-window menus ────────────────────────────────────


def test_accounts_menu_two_windows_different_accounts():
    tabs = [
        _tab("r1-t1", 200, "ChatGPT", email=EMAIL_1, profile="Work"),
        _tab("r1-t2", 200, "Gmail", idx=1, email=EMAIL_1, profile="Work"),
        _tab("r2-t1", 300, "YouTube", email=EMAIL_2, profile="Personal"),
    ]
    text, rows = build_chrome_accounts_menu(None, _scan(tabs=tabs))
    assert EMAIL_1 in text
    assert EMAIL_2 in text
    datas = _buttons(rows)
    assert "feature:resrc_wtabs:200@0" in datas
    assert "feature:resrc_wtabs:300@0" in datas
    assert not any("resrc_show" in d for d in datas)


def test_accounts_menu_windows_without_email_fallback():
    tabs = [_tab("r1-t1", 200, "Docs", profile="Work")]
    text, rows = build_chrome_accounts_menu(None, _scan(tabs=tabs))
    assert "Chrome \u2014 Work" in text


def test_window_tabs_grouped_by_exact_parent_hwnd():
    """A window view shows ONLY tabs whose host_hwnd matches; other windows'
    tabs are excluded entirely."""
    tabs = [
        _tab("r1-t1", 200, "ChatGPT", idx=0, email=EMAIL_1, profile="Work"),
        _tab("r1-t2", 200, "Gmail", idx=1, email=EMAIL_1, profile="Work"),
        _tab("r2-t1", 300, "YouTube", idx=0, email=EMAIL_2,
             profile="Personal"),
        _tab("r2-t2", 300, "Calendar", idx=1, email=EMAIL_2,
             profile="Personal"),
    ]
    text, rows = build_window_tabs_menu(None, _scan(tabs=tabs), 200, 0)
    assert text is not None
    assert "ChatGPT" in text
    assert "Gmail" in text
    assert "YouTube" not in text
    assert "Calendar" not in text
    assert "Account" not in text
    assert EMAIL_1 in text          # header: "🌐 user1@gmail.com"
    datas = _buttons(rows)
    assert "feature:resrc_tab_show:r1-t1" in datas
    assert "feature:resrc_tab_show:r1-t2" in datas
    assert not any("r2-" in d for d in datas)


def test_window_tabs_stale_window_rejected():
    """A button for a window no longer in the current scan returns None."""
    tabs = [_tab("r1-t1", 200, "ChatGPT", email=EMAIL_1)]
    result = build_window_tabs_menu(None, _scan(tabs=tabs), 999, 0)
    assert result is None


def test_account_identity_never_changes_tab_identity():
    """Whether or not an email is known, the tab buttons keep the exact same
    resource_id-based callback (Focus/Screenshot/Close still target the tab)."""
    with_email = _tab("r1-t1", 200, "Docs", email=EMAIL_1, profile="Work")
    no_email = _tab("r1-t1", 200, "Docs", profile="Work")
    _t, rows1 = build_window_tabs_menu(None, _scan(tabs=[with_email]), 200, 0)
    _t, rows2 = build_window_tabs_menu(None, _scan(tabs=[no_email]), 200, 0)
    b1 = [b for b in _buttons(rows1) if "resrc_tab_show" in b]
    b2 = [b for b in _buttons(rows2) if "resrc_tab_show" in b]
    assert b1 == ["feature:resrc_tab_show:r1-t1"]
    assert b1 == b2


def test_refresh_updates_grouping():
    """A re-scan with a moved/closed tab reflects the new parent grouping."""
    first = _scan(tabs=[_tab("r1-t1", 200, "Docs", email=EMAIL_1)])
    _text, rows = build_chrome_accounts_menu(None, first)
    assert "feature:resrc_wtabs:200@0" in _buttons(rows)
    second = _scan(tabs=[_tab("r1-t1", 300, "Docs", email=EMAIL_2)])
    text2, rows2 = build_chrome_accounts_menu(None, second)
    assert "feature:resrc_wtabs:300@0" in _buttons(rows2)
    assert not any("200" in d for d in _buttons(rows2))
    assert EMAIL_2 in text2


# ── 6. All Apps & Tabs overview ──────────────────────────────────────────────


def test_all_apps_menu_shows_applications_and_grouped_chrome():
    apps = [
        _app("r1-a1", title="app.py - Jarvis - Visual Studio Code",
             name="VS Code", exe="Code.exe", hwnd=100),
        _app("r1-a2", name="Calculator", exe="Calc.exe", hwnd=110),
    ]
    tabs = [
        _tab("r1-t1", 200, "ChatGPT", idx=0, email=EMAIL_1, profile="Work"),
        _tab("r1-t2", 200, "Gmail", idx=1, email=EMAIL_1, profile="Work"),
        _tab("r2-t1", 300, "YouTube", idx=0, email=EMAIL_2,
             profile="Personal"),
    ]
    text, rows = build_all_apps_menu(None, _scan(apps=apps, tabs=tabs), 0)
    assert "ALL APPS & TABS" in text
    assert "APPLICATIONS" in text
    assert "CHROME" in text
    assert "VS Code" in text
    assert "Calculator" in text
    assert EMAIL_1 in text
    assert EMAIL_2 in text
    datas = _buttons(rows)
    assert "feature:resrc_show:r1-a1" in datas
    assert "feature:resrc_wtabs:200@0" in datas
    assert "feature:resrc_tab_show:r1-t1" in datas
    assert "feature:resrc_tab_show:r2-t1" in datas


def test_all_apps_pagination_no_silent_truncation():
    apps = [_app(f"r1-a{i}", name=f"App {i}", title="", hwnd=i)
            for i in range(20)]
    tabs = [_tab(f"r1-t{i}", 901, f"Tab {i}", idx=i, email=EMAIL_1)
            for i in range(25)]
    # 20 apps + 2 headers + 1 window + 25 tabs = 48 entries > 30/page.
    _text0, rows0 = build_all_apps_menu(None, _scan(apps=apps, tabs=tabs), 0)
    datas0 = _buttons(rows0)
    assert any("resrc_allapps:1" in d for d in datas0)   # Next present
    assert not any("resrc_allapps:-1" in d for d in datas0)
    _text1, rows1 = build_all_apps_menu(None, _scan(apps=apps, tabs=tabs), 1)
    datas1 = _buttons(rows1)
    assert "Tab 24" in _text1
    assert any("resrc_allapps:0" in d for d in datas1)   # Previous present
    assert not any("resrc_allapps:2" in d for d in datas1)


def test_window_tabs_pagination_previous_next():
    tabs = [_tab(f"r1-t{i}", 500, f"Tab {i}", idx=i, email=EMAIL_1)
            for i in range(25)]
    text0, rows0 = build_window_tabs_menu(None, _scan(tabs=tabs), 500, 0)
    datas0 = _buttons(rows0)
    assert EMAIL_1 in text0         # header: "🌐 user1@gmail.com"
    assert "Tab 19" in text0
    assert "Tab 20" not in text0
    assert any("resrc_wtabs:500@1" in d for d in datas0)
    text1, rows1 = build_window_tabs_menu(None, _scan(tabs=tabs), 500, 1)
    datas1 = _buttons(rows1)
    assert "Tab 20" in text1
    assert "Tab 24" in text1
    assert any("resrc_wtabs:500@0" in d for d in datas1)
    assert not any("resrc_wtabs:500@2" in d for d in datas1)


# ── 7. Home screen ───────────────────────────────────────────────────────────


def test_resource_review_home_button_first():
    apps = [_app("r1-a1", name="A", hwnd=1), _app("r2-a2", name="B", hwnd=2)]
    tabs = [_tab("r1-t1", 200, "A", email=EMAIL_1),
            _tab("r1-t2", 200, "B", email=EMAIL_1),
            _tab("r2-t1", 300, "C", email=EMAIL_2)]
    text, rows = build_resource_review_home("f1", _scan(apps=apps, tabs=tabs))
    assert "FEATURE RESOURCE REVIEW" in text
    datas = _buttons(rows)
    # Button-first home: exactly the primary navigation, no resource rows.
    assert "feature:resrc_run:f1" in datas
    assert "feature:resrc_screen:f1" not in datas
    assert "feature:resrc_refresh:f1" not in datas
    assert "feature:resrc_continue:f1" in datas
    assert "feature:resrc_cancel:f1" in datas
    assert not any("resrc_show" in d for d in datas)
    assert not any("resrc_tab_show" in d for d in datas)
    assert not any("resrc_wtabs" in d for d in datas)


# ── 9/10. Privacy + existing behavior ────────────────────────────────────────


def test_no_credentials_or_tokens_present_in_any_menu():
    apps = [_app("r1-a1", name="VS Code", hwnd=100)]
    tabs = [_tab("r1-t1", 200, "Docs", email=EMAIL_1, profile="Work",
                 source="uia")]
    scans = [
        build_resource_review_home(None, _scan(apps=apps, tabs=tabs)),
        build_chrome_accounts_menu(None, _scan(tabs=tabs)),
        build_window_tabs_menu(None, _scan(tabs=tabs), 200, 0),
        build_all_apps_menu(None, _scan(apps=apps, tabs=tabs), 0),
    ]
    for text, rows in scans:
        blob = (text + " " + " ".join(_buttons(rows))).lower()
        assert "token" not in blob
        assert "cookie" not in blob
        assert "gaia_id" not in blob
        assert "oauth" not in blob
        assert "?auth" not in blob
        assert "password" not in blob


def test_existing_tab_actions_unchanged_callback_shape():
    """Clicking a grouped tab still opens the SAME resrc_tab_show detail."""
    tabs = [_tab("r1-t1", 200, "Docs", email=EMAIL_1, profile="Work")]
    _text, rows = build_window_tabs_menu(None, _scan(tabs=tabs), 200, 0)
    assert "feature:resrc_tab_show:r1-t1" in _buttons(rows)


# ── Telegram dispatch arms (auth-gated router) ───────────────────────────────


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


@pytest.fixture()
def orch(tmp_path):
    from jarvis_core.feature_orchestrator import FeatureOrchestrator
    from jarvis_core.feature_store import FeatureStore
    from jarvis_core.task_manager import TaskManager

    store = FeatureStore(root=tmp_path / "features")
    orchestrator = FeatureOrchestrator(
        store=store, task_manager_instance=TaskManager())
    orchestrator.start_worker = lambda: None  # type: ignore[method-assign]
    orchestrator.set_telegram_channels(
        send=lambda chat_id, text, markup=None: None,
        is_authorized=lambda uid: uid == 123,
    )
    yield orchestrator
    orchestrator.stop_worker(timeout=1)


@pytest.fixture()
def tg(orch):
    from unittest.mock import Mock

    from jarvis_core.telegram_service import TelegramService
    from tests.test_telegram_integration import FakeAPI, _settings

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
    """No real background threads; reset the shared resource singleton."""
    import jarvis_core.feature_orchestrator as fo_mod
    from jarvis_core import resource_manager as rm

    class _InlineThread:
        def __init__(self, target=None, name=None, daemon=None, **_kw):
            self._target = target

        def start(self):
            if self._target:
                self._target()

    monkeypatch.setattr(fo_mod.threading, "Thread", _InlineThread)
    reg = rm.resource_registry
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


def _pattern_scan():
    apps = [_app("r1-a1")]
    tabs = [
        _tab("r1-t1", 200, "ChatGPT", idx=0, email=EMAIL_1, profile="Work"),
        _tab("r2-t1", 300, "YouTube", idx=0, email=EMAIL_2,
             profile="Personal"),
    ]
    return _scan(apps=apps, tabs=tabs)


def test_resrc_wmenu_dispatch_uses_identity_scan(tg, monkeypatch):
    service, _orch, api = tg
    from jarvis_core import resource_manager as rm
    monkeypatch.setattr(
        rm.resource_registry, "scan_result",
        lambda include_identity=False: _pattern_scan())
    service.process_update(_callback("cb1", 123, 100, "feature:resrc_wmenu:"))
    texts = " ".join(m["text"] for m in api.messages)
    assert EMAIL_1 in texts
    assert EMAIL_2 in texts
    flat = [b["callback_data"] for m in api.messages
            for row in m.get("reply_markup", {}).get("inline_keyboard", [])
            for b in row]
    assert "feature:resrc_wtabs:200@0" in flat


def test_resrc_wtabs_dispatch_filters_to_exact_window(tg, monkeypatch):
    service, _orch, api = tg
    from jarvis_core import resource_manager as rm
    monkeypatch.setattr(
        rm.resource_registry, "scan_result",
        lambda include_identity=False: _pattern_scan())
    service.process_update(
        _callback("cb2", 123, 100, "feature:resrc_wtabs:200@0"))
    texts = " ".join(m["text"] for m in api.messages)
    assert "ChatGPT" in texts
    assert "YouTube" not in texts
    assert EMAIL_1 in texts


def test_resrc_wtabs_stale_window_rejected(tg, monkeypatch):
    service, _orch, api = tg
    from jarvis_core import resource_manager as rm
    monkeypatch.setattr(
        rm.resource_registry, "scan_result",
        lambda include_identity=False: _pattern_scan())
    service.process_update(
        _callback("cb3", 123, 100, "feature:resrc_wtabs:999@0"))
    texts = " ".join(m["text"] for m in api.messages)
    assert "no longer in the current scan" in texts


def test_resrc_allapps_dispatch_overview(tg, monkeypatch):
    service, _orch, api = tg
    from jarvis_core import resource_manager as rm
    monkeypatch.setattr(
        rm.resource_registry, "scan_result",
        lambda include_identity=False: _pattern_scan())
    service.process_update(_callback("cb4", 123, 100, "feature:resrc_allapps:0"))
    texts = " ".join(m["text"] for m in api.messages)
    assert "ALL APPS & TABS" in texts
    assert "APPLICATIONS" in texts
    assert EMAIL_1 in texts


def test_resrc_home_dispatch_simple_review(tg, monkeypatch):
    service, _orch, api = tg
    from jarvis_core import resource_manager as rm
    monkeypatch.setattr(
        rm.resource_registry, "scan_result",
        lambda include_identity=False: _pattern_scan())
    service.process_update(_callback("cb5", 123, 100, "feature:resrc_home:fid7"))
    texts = " ".join(m["text"] for m in api.messages)
    assert "FEATURE RESOURCE REVIEW" in texts
    flat = [b["callback_data"] for m in api.messages
            for row in m.get("reply_markup", {}).get("inline_keyboard", [])
            for b in row]
    assert "feature:resrc_run:fid7" in flat
    assert "feature:resrc_continue:fid7" in flat
    assert "feature:resrc_cancel:fid7" in flat


def test_unauthorized_user_cannot_open_grouped_views(tg):
    service, _orch, api = tg
    service.process_update(_callback("cb1", 999, 100, "feature:resrc_wmenu:"))
    service.process_update(_callback("cb2", 999, 100, "feature:resrc_allapps:0"))
    service.process_update(_callback("cb3", 999, 100, "feature:resrc_wtabs:200@0"))
    texts = " ".join(m["text"] for m in api.messages)
    assert "not authorized" in texts.lower()
    assert EMAIL_1 not in texts


def test_resrc_tab_show_still_opens_detail_with_account(tg):
    """Tab detail keeps the proven Account/Profile line and untouched buttons."""
    service, _orch, api = tg
    from jarvis_core import resource_manager as rm
    from jarvis_core.resource_manager import Resource

    reg = rm.resource_registry
    reg._generation = 1
    res = Resource(
        resource_id="r1-t1", feature_id=None, resource_type="browser_tab",
        title="ChatGPT", display_name="Chrome", host_hwnd=200,
        tab_index=0, account_email=EMAIL_1, profile_label="Work",
        identity_label=EMAIL_1, identity_source="meta-single",
    )
    reg._resources = {"r1-t1": res}
    service.process_update(_callback("cb9", 123, 100, "feature:resrc_tab_show:r1-t1"))
    texts = " ".join(m["text"] for m in api.messages)
    assert EMAIL_1 in texts
    flat = [b["callback_data"] for m in api.messages
            for row in m.get("reply_markup", {}).get("inline_keyboard", [])
            for b in row]
    assert "feature:resrc_focus:r1-t1" in flat
    assert "feature:resrc_shot:r1-t1" in flat
    assert "feature:resrc_close:r1-t1" in flat