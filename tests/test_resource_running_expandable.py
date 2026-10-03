"""Part 8C-1 - Current Running expandable headings.

Tests that the Current Running screen now shows expandable headings
for Applications and Chrome instead of listing all items directly.
All interactions happen on the SAME Telegram message.
"""
from __future__ import annotations

from jarvis_core.resource_menus import (
    build_current_running_menu,
    build_current_running_expanded_apps,
    build_current_running_expanded_chrome,
    build_resource_review_home,
)

def _scan(n_apps: int = 2, n_tabs: int = 3, n_windows: int = 2) -> dict:
    """A realistic scan result with faked backends."""
    tabs = [
        {
            "id": f"tab{i}",
            "title": f"Real Tab {i}",
            "url": f"https://{i}.com",
            "host_hwnd": 1000 + (i % n_windows),
            "resource_id": f"r0-t{i}",
        }
        for i in range(n_tabs)
    ]
    apps = [
        {
            "name": f"app{i}",
            "resource_id": f"r0-a{i}",
            "display_name": f"App {i}",
            "executable": "app.exe",
        }
        for i in range(n_apps)
    ]
    return {
        "applications": apps,
        "tabs": tabs,
        "chrome_detected": True,
    }


def _flat(rows: list) -> list:
    out = []
    for row in rows:
        for b in row:
            out.append(b["callback_data"])
    return out


# 1. Initial view shows collapsed headings with counts
def test_initial_collapsed_headings():
    """Initial view shows Applications (N) and Chrome (N) as headings."""
    text, rows = build_current_running_menu("f1", _scan(n_apps=2, n_tabs=3))
    datas = _flat(rows)
    
    # Should have exactly 4 rows: Applications, Chrome, Refresh, Back
    assert len(datas) == 4
    assert any("resrc_expand_apps" in cb for cb in datas)
    assert any("resrc_expand_chrome" in cb for cb in datas)
    assert any("resrc_refresh_run" in cb for cb in datas)
    assert any("resrc_home" in cb for cb in datas)
    
    # Text should show heading format with counts
    assert "💻 Applications (2)" in text
    assert "🌐 Chrome (3)" in text  # 2 Chrome windows
    # No individual app or tab names should appear
    assert "App 1" not in text
    assert "Real Tab 1" not in text


# 2. Applications expands to show app list
def test_applications_expands():
    """Tapping Applications heading expands to show app list."""
    text, rows = build_current_running_expanded_apps("f1", _scan(n_apps=2, n_tabs=3))
    datas = _flat(rows)
    
    # Clean UI contract: apps appear as INLINE BUTTONS only (not duplicated in
    # the message body).
    assert "💻 Applications (2)" in text
    assert "App 1" not in text
    assert "App 0" not in text
    assert any("App 1" in b["text"] for row in rows for b in row)
    assert any("App 0" in b["text"] for row in rows for b in row)
    # Chrome should still be collapsed
    assert "🌐 Chrome (3)" in text
    # No tab details in the collapsed Chrome section.
    assert "Real Tab" not in text
    
    # Applications heading should now be a collapse button
    assert any("resrc_collapse_apps" in cb for cb in datas)


# 3. Applications collapses back to heading
def test_applications_collapses():
    """Tapping expanded Applications collapses it."""
    text1, rows1 = build_current_running_expanded_apps("f1", _scan(n_apps=2, n_tabs=3))
    text2, rows2 = build_current_running_menu("f1", _scan(n_apps=2, n_tabs=3))
    
    # Collapsed should not show app names
    assert "App 1" not in text2
    assert "App 2" not in text2


# 4. Chrome expands to show tabs
def test_chrome_expands():
    """Tapping Chrome heading expands to show tabs."""
    text, rows = build_current_running_expanded_chrome("f1", _scan(n_apps=2, n_tabs=3))
    datas = _flat(rows)
    
    # Clean UI contract: tabs appear as INLINE BUTTONS only (not duplicated in
    # the message body).
    assert "🌐 Chrome (3)" in text
    assert "Real Tab" not in text
    assert any("Real Tab" in b["text"] for row in rows for b in row)
    # Applications should be collapsed (no app names in the collapsed section).
    assert "💻 Applications (2)" in text
    assert "App 1" not in text
    
    # Chrome heading should now be a collapse button
    assert any("resrc_collapse_chrome" in cb for cb in datas)


# 5. Chrome collapses back to heading
def test_chrome_collapses():
    """Tapping expanded Chrome collapses it."""
    text1, rows1 = build_current_running_expanded_chrome("f1", _scan(n_apps=2, n_tabs=3))
    text2, rows2 = build_current_running_menu("f1", _scan(n_apps=2, n_tabs=3))
    
    # Collapsed should not show tab names
    assert "Real Tab" not in text2


# 6. Opening Chrome collapses Applications if open
def test_opening_chrome_collapses_applications():
    """When Chrome is expanded, Applications should be collapsed."""
    text, rows = build_current_running_expanded_chrome("f1", _scan(n_apps=2, n_tabs=3))
    
    # Apps should be collapsed (just heading, no app names)
    assert "💻 Applications (2)" in text
    assert "App 1" not in text
    assert "App 2" not in text


# 7. Same message_id always preserved (no new messages)
def test_same_message_preserved():
    """All operations should use editMessageText, never send new message."""
    scan = _scan()
    
    text1, rows1 = build_current_running_menu("f1", scan)
    text2, rows2 = build_current_running_expanded_apps("f1", scan)
    text3, rows3 = build_current_running_expanded_chrome("f1", scan)
    
    # All should return valid text and rows
    assert isinstance(text1, str) and isinstance(rows1, list)
    assert isinstance(text2, str) and isinstance(rows2, list)
    assert isinstance(text3, str) and isinstance(rows3, list)


# 8. No action buttons yet (Focus/Screenshot/Close/Edit)
def test_no_action_buttons_yet():
    """Expanded views should not include action buttons."""
    text_apps, rows_apps = build_current_running_expanded_apps("f1", _scan())
    text_chrome, rows_chrome = build_current_running_expanded_chrome("f1", _scan())
    
    all_callbacks = _flat(rows_apps) + _flat(rows_chrome)
    
    # Should not have any action callbacks
    for cb in all_callbacks:
        assert "resrc_focus" not in cb
        assert "resrc_screenshot" not in cb
        assert "resrc_close" not in cb
        assert "resrc_edit" not in cb


# ── Regression: CDP Chrome tabs (no host_hwnd) must still list + be clickable ─
#
# Bug report: tapping "Chrome (11)" showed the heading/count but NO tab rows and
# no per-tab Focus/Screenshot/Close options. Root cause: CDP-discovered tabs
# carry a page/target id but NO Windows ``host_hwnd``; the expanded builder used
# to drop every hwnd-less tab, so the panel rendered empty despite a non-zero
# count. Tabs without host_hwnd must still be listed and clickable.


def _cdp_scan(n_tabs: int = 4, n_apps: int = 2) -> dict:
    """A CDP-style scan: real tabs with page ids but NO host_hwnd."""
    tabs = [
        {
            "title": f"CDP Tab {i}",
            "safe_display_target": f"site-{i}.example",
            "resource_id": f"r0-t{i}",
            "control_mode": "cdp",
            "page_id": f"PAGE{i}",
            "tab_index": i,
            # NOTE: deliberately no "host_hwnd".
        }
        for i in range(n_tabs)
    ]
    apps = [
        {"resource_id": f"r0-a{i}", "display_name": f"App {i}",
         "executable": "app.exe"}
        for i in range(n_apps)
    ]
    return {"applications": apps, "tabs": tabs, "chrome_detected": True}


def test_cdp_tabs_without_host_hwnd_are_listed_and_clickable():
    scan = _cdp_scan(n_tabs=4)
    text, rows = build_current_running_expanded_chrome("f1", scan)
    datas = _flat(rows)

    # Heading count reflects all tabs.
    assert "🌐 Chrome (4)" in text
    # Every real CDP tab renders a clickable row that opens its action panel.
    for i in range(4):
        assert any(f"resrc_tab_actions:r0-t{i}" in cb for cb in datas), (
            f"CDP tab r0-t{i} must be listed and clickable")


def test_cdp_tab_selected_shows_focus_screenshot_close():
    scan = _cdp_scan(n_tabs=4)
    text, rows = build_current_running_expanded_chrome(
        "f1", scan, tab_id="r0-t2")
    datas = _flat(rows)
    # Selecting a CDP tab reveals its inline action sub-options.
    assert any("resrc_focus:r0-t2" in cb for cb in datas)
    assert any("resrc_shot:r0-t2" in cb for cb in datas)
    assert any("resrc_close:r0-t2" in cb for cb in datas)
    assert any("resrc_hide_tab_actions" in cb for cb in datas)


# ── Regression: selecting an item must CHANGE the panel text ─────────────────
#
# Bug: tapping an app/tab showed "Processing..." then nothing. Root cause was
# Telegram rejecting editMessageText when only the inline keyboard changed but
# the message TEXT stayed identical ("message is not modified"), so the Focus /
# Screenshot / Close sub-options never appeared. The selected item's name must
# now appear in the message body so the text always differs on selection.


def test_selecting_app_changes_panel_text_and_shows_actions():
    scan = _scan(n_apps=3, n_tabs=0)
    unselected_text, _ = build_current_running_expanded_apps("f1", scan)
    selected_text, rows = build_current_running_expanded_apps(
        "f1", scan, app_id="r0-a1")
    datas = _flat(rows)

    assert selected_text != unselected_text, (
        "selecting an app must change the message text (small status line) so "
        "Telegram accepts the in-place edit")
    # The action sub-options are present for the selected app as BUTTONS.
    assert any("🎯 Focus" in b["text"] for row in rows for b in row)
    assert any("resrc_focus:r0-a1" in cb for cb in datas)
    assert any("resrc_shot:r0-a1" in cb for cb in datas)
    assert any("resrc_close:r0-a1" in cb for cb in datas)


def test_selecting_tab_changes_panel_text_and_shows_actions():
    scan = _scan(n_apps=0, n_tabs=3)
    unselected_text, _ = build_current_running_expanded_chrome("f1", scan)
    selected_text, rows = build_current_running_expanded_chrome(
        "f1", scan, tab_id="r0-t1")
    datas = _flat(rows)

    assert selected_text != unselected_text
    assert any("🎯 Focus" in b["text"] for row in rows for b in row)
    assert any("resrc_focus:r0-t1" in cb for cb in datas)
    assert any("resrc_shot:r0-t1" in cb for cb in datas)
    assert any("resrc_close:r0-t1" in cb for cb in datas)


# ── Regression: callback_data 64-byte contract (BUTTON_DATA_INVALID) ─────────
#
# Live root cause: Telegram rejects the WHOLE inline keyboard with
# "Bad Request: BUTTON_DATA_INVALID" if ANY callback_data exceeds 64 UTF-8
# bytes. Embedding a 36-char feature UUID pushed several section/hide/refresh
# callbacks (e.g. resrc_collapse_chrome:<uuid> = 66 bytes) over the limit, so
# tapping the Chrome heading / an application silently failed while the shorter
# Refresh/Back callbacks still worked. Every generated callback must stay <=64
# bytes for any realistic feature id + resource id.

_REAL_FID = "a0ba512a-852a-4b8e-bf98-e77d5e448858"  # 36-char UUID (worst case)


def _rich_scan():
    return {
        "generation": 7,
        "applications": [
            {"resource_id": "r7-a132708", "display_name": "Whatsapp.Root",
             "executable": "w.exe", "window_handle": 132708},
            {"resource_id": "r7-a3802614", "display_name": "Kiro",
             "executable": "k.exe", "window_handle": 3802614},
        ],
        "tabs": [
            {"resource_id": "r7-t0", "title": "Gmail", "host_hwnd": 0,
             "tab_index": 0},
            {"resource_id": "r7-t1", "title": "Docs", "host_hwnd": 0,
             "tab_index": 1},
        ],
        "chrome_detected": True,
    }


def _all_callbacks(rows):
    return [b["callback_data"] for row in rows for b in row]


def _assert_all_within_limit(rows, label):
    for cb in _all_callbacks(rows):
        n = len(cb.encode("utf-8"))
        assert n <= 64, f"{label}: callback exceeds 64 bytes ({n}): {cb!r}"


def test_all_resource_review_callbacks_within_64_bytes():
    scan = _rich_scan()
    panels = {
        "home": build_resource_review_home(_REAL_FID, scan),
        "collapsed": build_current_running_menu(_REAL_FID, scan),
        "apps": build_current_running_expanded_apps(_REAL_FID, scan),
        "apps+sel": build_current_running_expanded_apps(
            _REAL_FID, scan, app_id="r7-a132708"),
        "chrome": build_current_running_expanded_chrome(_REAL_FID, scan),
        "chrome+sel": build_current_running_expanded_chrome(
            _REAL_FID, scan, tab_id="r7-t1"),
    }
    for label, (_text, rows) in panels.items():
        _assert_all_within_limit(rows, label)


def test_continue_cancel_still_carry_feature_id():
    """Continue/Cancel must still carry the exact feature id (the orchestrator
    acts on it) and stay within the 64-byte limit."""
    _text, rows = build_resource_review_home(_REAL_FID, _rich_scan())
    datas = _all_callbacks(rows)
    assert any(cb == f"feature:resrc_continue:{_REAL_FID}" for cb in datas)
    assert any(cb == f"feature:resrc_cancel:{_REAL_FID}" for cb in datas)
    _assert_all_within_limit(rows, "home")
