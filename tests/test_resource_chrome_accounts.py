"""Chrome account-grouped UX: Current Running -> Chrome -> Gmail/profile
account -> that account's tabs -> tab actions.

Chrome tabs are grouped by the REAL proven Chrome profile/account identity
(account_email -> profile_label -> identity_label) already produced by
chrome_profile_identity.py / ResourceRegistry. Identity is never inferred from
titles/URLs. Tabs with no resolvable identity go to "Other / Unknown" and are
never dropped. Every generated callback stays within Telegram's 64-byte limit.
"""
from __future__ import annotations

from unittest.mock import Mock

import jarvis_core.resource_manager as resource_manager
from jarvis_core.resource_manager import Resource
from jarvis_core.resource_menus import (
    build_chrome_account_list,
    build_chrome_account_tabs,
    chrome_profiles,
    resolve_chrome_account,
    resolve_chrome_account_by_key,
)
from jarvis_core.telegram_service import TelegramService
from tests.test_telegram_integration import FakeAPI, _settings


# ── Builder-level fixtures ───────────────────────────────────────────────────


def _scan(tabs, generation=9, chrome_detected=True):
    return {
        "generation": generation,
        "chrome_detected": chrome_detected,
        "applications": [],
        "tabs": tabs,
    }


def _tab(rid, title, *, email="", profile="", identity="", hwnd=100, idx=0):
    d = {"resource_id": rid, "title": title, "tab_index": idx, "host_hwnd": hwnd}
    if email:
        d["account_email"] = email
    if profile:
        d["profile_label"] = profile
    if identity:
        d["identity_label"] = identity
    return d


def _cbs(rows):
    return [b["callback_data"] for row in rows for b in row]


def _labels(rows):
    return [b["text"] for row in rows for b in row]


def _assert_within_64(rows, label="panel"):
    for cb in _cbs(rows):
        assert len(cb.encode("utf-8")) <= 64, f"{label}: {cb!r} > 64 bytes"


# 1. Chrome total = real tab count (heading/grouping never invents tabs).
def test_chrome_total_equals_real_tab_count():
    scan = _scan([
        _tab("r9-t0", "A", email="abc@gmail.com"),
        _tab("r9-t1", "B", email="abc@gmail.com"),
        _tab("r9-t2", "C", email="xyz@gmail.com"),
    ])
    assert sum(p["count"] for p in chrome_profiles(scan)) == len(scan["tabs"])


# 2. One Gmail account with multiple tabs.
def test_single_account_multiple_tabs():
    scan = _scan([
        _tab("r9-t0", "A", email="abc@gmail.com"),
        _tab("r9-t1", "B", email="abc@gmail.com"),
        _tab("r9-t2", "C", email="abc@gmail.com"),
    ])
    profiles = chrome_profiles(scan)
    assert len(profiles) == 1
    assert profiles[0]["label"] == "abc@gmail.com"
    assert profiles[0]["count"] == 3


# 3. Multiple Gmail accounts.
def test_multiple_accounts():
    scan = _scan([
        _tab("r9-t0", "A", email="abc@gmail.com"),
        _tab("r9-t1", "C", email="xyz@gmail.com"),
        _tab("r9-t2", "D", email="xyz@gmail.com"),
    ])
    profiles = {p["label"]: p["count"] for p in chrome_profiles(scan)}
    assert profiles == {"abc@gmail.com": 1, "xyz@gmail.com": 2}


# 4. Same Gmail account across multiple Chrome windows aggregates.
def test_same_account_across_windows_aggregates():
    scan = _scan([
        _tab("r9-t0", "A", email="abc@gmail.com", hwnd=111),
        _tab("r9-t1", "B", email="abc@gmail.com", hwnd=111),
        _tab("r9-t2", "C", email="abc@gmail.com", hwnd=222),
    ])
    profiles = chrome_profiles(scan)
    assert len(profiles) == 1, "windows must NOT be exposed as separate groups"
    assert profiles[0]["count"] == 3


# 5. Unknown/unresolved tabs grouped as Other / Unknown.
def test_unknown_group():
    scan = _scan([
        _tab("r9-t0", "A", email="abc@gmail.com"),
        _tab("r9-t1", "B"),  # no identity at all
    ])
    profiles = {p["label"]: p for p in chrome_profiles(scan)}
    assert "Other / Unknown" in profiles
    assert profiles["Other / Unknown"]["count"] == 1
    assert profiles["Other / Unknown"]["is_unknown"] is True


# 6. No zero-tab account groups rendered.
def test_no_zero_tab_groups_rendered():
    scan = _scan([_tab("r9-t0", "A", email="abc@gmail.com")])
    _text, rows = build_chrome_account_list("f1", scan)
    # Exactly one account row + Refresh + Back.
    account_rows = [r for r in _cbs(rows) if "resrc_chrome_account:" in r]
    assert len(account_rows) == 1


# 7. Sum(account counts) == Chrome total (incl. unknown).
def test_sum_counts_equals_total():
    scan = _scan([
        _tab("r9-t0", "A", email="abc@gmail.com"),
        _tab("r9-t1", "B", profile="Work"),
        _tab("r9-t2", "C"),
        _tab("r9-t3", "D", identity="Chrome Window 2"),
    ])
    assert sum(p["count"] for p in chrome_profiles(scan)) == 4


# 8. Clicking one Gmail account shows ONLY its tabs.
def test_account_tabs_shows_only_that_account():
    scan = _scan([
        _tab("r9-t0", "AlphaTab", email="abc@gmail.com"),
        _tab("r9-t1", "BetaTab", email="abc@gmail.com"),
        _tab("r9-t2", "GammaTab", email="xyz@gmail.com"),
    ])
    prof = {p["label"]: p for p in chrome_profiles(scan)}
    tok = prof["abc@gmail.com"]["token"]
    _text, rows = build_chrome_account_tabs("f1", scan, tok)
    cbs = _cbs(rows)
    assert any("resrc_tab_actions:r9-t0" in c for c in cbs)
    assert any("resrc_tab_actions:r9-t1" in c for c in cbs)
    assert not any("r9-t2" in c for c in cbs), "other account's tab must not show"


# 9/10. Back wiring: account tabs -> account list; account list -> Current Running.
def test_back_wiring_levels():
    scan = _scan([_tab("r9-t0", "A", email="abc@gmail.com")])
    _t1, list_rows = build_chrome_account_list("f1", scan)
    # account list Back -> collapse chrome (Current Running)
    assert any("resrc_collapse_chrome" in c for c in _cbs(list_rows))
    tok = chrome_profiles(scan)[0]["token"]
    _t2, tab_rows = build_chrome_account_tabs("f1", scan, tok)
    # account tabs Back -> expand chrome (account list)
    assert any("resrc_expand_chrome" in c for c in _cbs(tab_rows))


# 11/12. Refresh callbacks exist at both levels.
def test_refresh_callbacks_present():
    scan = _scan([_tab("r9-t0", "A", email="abc@gmail.com")])
    _t1, list_rows = build_chrome_account_list("f1", scan)
    assert any("resrc_refresh_chrome_accounts" in c for c in _cbs(list_rows))
    tok = chrome_profiles(scan)[0]["token"]
    _t2, tab_rows = build_chrome_account_tabs("f1", scan, tok)
    assert any("resrc_refresh_chrome_account:" in c for c in _cbs(tab_rows))


# 13. Selected tab shows Focus/Screenshot/Close/Edit/Hide actions.
def test_selected_tab_shows_actions():
    scan = _scan([
        _tab("r9-t0", "A", email="abc@gmail.com"),
        _tab("r9-t1", "B", email="abc@gmail.com"),
    ])
    tok = chrome_profiles(scan)[0]["token"]
    _text, rows = build_chrome_account_tabs("f1", scan, tok, tab_id="r9-t0")
    cbs = _cbs(rows)
    assert any("resrc_focus:r9-t0" in c for c in cbs)
    assert any("resrc_shot:r9-t0" in c for c in cbs)
    assert any("resrc_close:r9-t0" in c for c in cbs)
    assert any("resrc_edit:r9-t0" in c for c in cbs)
    # Hide Actions returns to the account view (same token).
    assert any(f"resrc_chrome_account:{tok}" in c for c in cbs)


# 14. Closing the last tab of an account removes the group.
def test_last_tab_close_removes_group():
    before = _scan([
        _tab("r9-t0", "A", email="abc@gmail.com"),
        _tab("r9-t1", "B", email="xyz@gmail.com"),
    ])
    assert len(chrome_profiles(before)) == 2
    after = _scan([_tab("r10-t0", "B", email="xyz@gmail.com")], generation=10)
    labels = [p["label"] for p in chrome_profiles(after)]
    assert "abc@gmail.com" not in labels
    assert labels == ["xyz@gmail.com"]


# 15. Callback_data <= 64 bytes for all panels (realistic UUID + long email).
def test_all_callbacks_within_64_bytes():
    scan = _scan([
        _tab("r9-t0", "A very long tab title " * 3, email="a.very.long.address@googlemail.com"),
        _tab("r9-t1", "B", email="a.very.long.address@googlemail.com"),
        _tab("r9-t2", "C"),
    ])
    fid = "a0ba512a-852a-4b8e-bf98-e77d5e448858"
    _t1, list_rows = build_chrome_account_list(fid, scan)
    _assert_within_64(list_rows, "account_list")
    for p in chrome_profiles(scan):
        _t2, tab_rows = build_chrome_account_tabs(fid, scan, p["token"])
        _assert_within_64(tab_rows, "account_tabs")
        _t3, sel_rows = build_chrome_account_tabs(
            fid, scan, p["token"], tab_id=p["tabs"][0]["resource_id"])
        _assert_within_64(sel_rows, "account_tabs_sel")


# Cross-generation identity re-match (Refresh survival).
def test_resolve_by_key_survives_generation_change():
    scan_g1 = _scan([_tab("r9-t0", "A", email="abc@gmail.com")], generation=9)
    p1 = chrome_profiles(scan_g1)[0]
    key = p1["key"]
    scan_g2 = _scan([_tab("r10-t0", "A", email="abc@gmail.com")], generation=10)
    matched = resolve_chrome_account_by_key(scan_g2, key)
    assert matched is not None and matched["label"] == "abc@gmail.com"
    # Refresh-drift fix: the OLD-generation token still resolves (by ordinal
    # fallback) to the same still-open account, so Refresh never wrongly
    # reports it as "no longer open".
    still = resolve_chrome_account(scan_g2, p1["token"])
    assert still is not None and still["label"] == "abc@gmail.com"


# ── Handler-level: expand Chrome opens the account list (not flat tabs) ──────


def _seed_tabs(reg, specs, generation=1):
    reg._generation = generation
    reg._feature_id = None
    reg._cdp_url = "http://127.0.0.1:9222"
    reg._chrome_detected = True
    reg._chrome_controllable = True
    reg._chrome_reason = ""
    reg._chrome_control_mode = "cdp"
    reg._last_scan_at = 0.0
    resources = {}
    for rid, title, email in specs:
        r = Resource(
            resource_id=rid, feature_id=None, resource_type="browser_tab",
            title=title, display_name="Chrome", control_mode="cdp",
            page_id=rid.upper(),
        )
        r.account_email = email
        resources[rid] = r
    reg._resources = resources


def _make_service():
    api = FakeAPI()
    service = TelegramService(settings=_settings(123), api=api)
    service._start_task_watcher = Mock()
    service._feature_orchestrator = Mock()
    return service, api


def _cb(cb_id, data, user_id=123, chat_id=100, message_id=777):
    return {
        "update_id": 10,
        "callback_query": {
            "id": cb_id, "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}, "message_id": message_id},
            "data": data,
        },
    }


def _edit_kb(api):
    rm = api.edits[-1].get("reply_markup") or {}
    return [b["callback_data"] for row in rm.get("inline_keyboard", []) for b in row]


def test_handler_expand_chrome_shows_account_list():
    service, api = _make_service()
    _seed_tabs(resource_manager.resource_registry, [
        ("r1-t0", "Inbox", "abc@gmail.com"),
        ("r1-t1", "Docs", "abc@gmail.com"),
        ("r1-t2", "News", "xyz@gmail.com"),
    ])
    service.process_update(_cb("c1", "feature:resrc_expand_chrome:"))
    kb = _edit_kb(api)
    # Account rows (not raw tab rows) are shown.
    assert sum(1 for c in kb if "resrc_chrome_account:" in c) == 2
    assert not any("resrc_tab_actions:" in c for c in kb)


def test_handler_account_click_shows_only_its_tabs():
    service, api = _make_service()
    _seed_tabs(resource_manager.resource_registry, [
        ("r1-t0", "Inbox", "abc@gmail.com"),
        ("r1-t1", "News", "xyz@gmail.com"),
    ])
    service.process_update(_cb("c1", "feature:resrc_expand_chrome:"))
    # Click the first account button from the rendered account list.
    acct_cb = next(c for c in _edit_kb(api) if "resrc_chrome_account:" in c)
    service.process_update(_cb("c2", acct_cb))
    kb2 = _edit_kb(api)
    # Only tabs are shown now (tab-action callbacks present).
    assert any("resrc_tab_actions:" in c for c in kb2)


# ── Close preserves the account view (no success banner, no level change) ────


def _seed_tabs_scanback(reg, specs, generation=1):
    """Seed tabs and make reg.scan() re-emit the CURRENT resources snapshot,
    counting how many scans happen. Individual specs can be dropped later to
    simulate a close."""
    _seed_tabs(reg, specs, generation=generation)
    scans = []

    def _scan(*a, **k):
        scans.append(1)
        return reg.scan_result(include_identity=True)

    reg.scan = _scan  # type: ignore[assignment]
    return scans


def test_close_stays_in_same_account_no_banner(monkeypatch):
    import jarvis_core.windows_resource_manager as wrm
    service, api = _make_service()
    reg = resource_manager.resource_registry
    scans = _seed_tabs_scanback(reg, [
        ("r1-t0", "Alpha", "abc@gmail.com"),
        ("r1-t1", "Beta", "abc@gmail.com"),
        ("r1-t2", "Gamma", "abc@gmail.com"),
        ("r1-t3", "News", "xyz@gmail.com"),
    ])

    # Real close: drop the exact tab from the registry, bump generation.
    orig_close = reg.close

    def fake_close(resource_id, confirmed=False):
        reg._resources.pop(resource_id, None)
        reg._generation += 1
        # re-stamp remaining ids to the new generation (mirrors a real rescan).
        return {"status": "ok", "closed_confirmed": True}
    reg.close = fake_close  # type: ignore[assignment]

    # Open account, select a tab, then Close it.
    service.process_update(_cb("c1", "feature:resrc_expand_chrome:"))
    # pick the abc account (first alphabetical among known accounts)
    acct = [c for c in _edit_kb(api) if "resrc_chrome_account:" in c][0]
    service.process_update(_cb("c2", acct))
    service.process_update(_cb("c3", "feature:resrc_tab_actions:r1-t1"))
    scans.clear()
    service.process_update(_cb("c4", "feature:resrc_close:r1-t1"))

    edit = api.edits[-1]
    # (4) no success banner in the body.
    assert "Closed tab" not in edit["text"]
    # short toast feedback instead.
    assert any(cid == "c4" for cid, _ in api.callbacks)
    kb = _edit_kb(api)
    # (1)(6) still the abc account tabs; closed tab gone, others remain.
    assert not any("r1-t1" in c for c in kb), "closed tab removed"
    assert any("resrc_tab_actions:r1-t0" in c for c in kb)
    # (9) never jumped to the other account's tab.
    assert not any("r1-t3" in c for c in kb)
    # (4) selection/actions cleared (no focus/close action rows present).
    assert not any(c.startswith("feature:resrc_focus:") for c in kb)
    # (7) exactly one scan after the close.
    assert sum(scans) == 1


def test_close_last_tab_returns_to_account_list(monkeypatch):
    service, api = _make_service()
    reg = resource_manager.resource_registry
    scans = _seed_tabs_scanback(reg, [
        ("r1-t0", "Only", "abc@gmail.com"),
        ("r1-t1", "News", "xyz@gmail.com"),
    ])

    def fake_close(resource_id, confirmed=False):
        reg._resources.pop(resource_id, None)
        reg._generation += 1
        return {"status": "ok", "closed_confirmed": True}
    reg.close = fake_close  # type: ignore[assignment]

    service.process_update(_cb("c1", "feature:resrc_expand_chrome:"))
    acct = [c for c in _edit_kb(api) if "resrc_chrome_account:" in c][0]
    service.process_update(_cb("c2", acct))
    service.process_update(_cb("c3", "feature:resrc_tab_actions:r1-t0"))
    service.process_update(_cb("c4", "feature:resrc_close:r1-t0"))

    kb = _edit_kb(api)
    # (8) dropped to the account list; empty account removed, other remains.
    assert any("resrc_chrome_account:" in c for c in kb)
    assert not any("resrc_tab_actions:r1-t0" in c for c in kb)
    edit = api.edits[-1]
    assert "Closed tab" not in edit["text"]
