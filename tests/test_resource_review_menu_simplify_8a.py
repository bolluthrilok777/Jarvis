"""Part 8A - Resource Review HOME menu simplification.

Focused, menu-only tests proving the top-level home panel now exposes a
single navigation button (Current Running) instead of dumping every
drill-down target (Chrome / All Apps / Current Screen / Refresh) as
top-level buttons - while the underlying panels and their actions stay
completely intact.

All backends are faked - no real windows, Chrome, or Telegram traffic.
"""
from __future__ import annotations

from jarvis_core.resource_menus import (
    build_current_running_menu,
    build_resource_review_home,
)


def _scan(n_apps: int = 2, n_tabs: int = 5, n_windows: int = 3) -> dict:
    """A realistic Resource Review scan result with faked backends."""
    tabs = [
        {
            "id": f"tab{i}",
            "title": f"Tab {i}",
            "url": f"https://{i}.com",
            "host_hwnd": 1000 + (i % n_windows),
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


# 1. The single top-level navigation button on the home screen.
def test_home_single_current_running_button():
    text, rows = build_resource_review_home("f1", _scan())
    datas = _flat(rows)
    # Exactly one primary navigation button + Continue + Cancel.
    assert datas == [
        "feature:resrc_run:f1",
        "feature:resrc_continue:f1",
        "feature:resrc_cancel:f1",
    ]


# 2. The home panel dumps NO resource rows (no apps/tabs/windows listed).
def test_home_dumps_no_resource_rows():
    text, rows = build_resource_review_home("f1", _scan(n_apps=5, n_tabs=9, n_windows=4))
    datas = _flat(rows)
    # Exactly the three navigation buttons; no drill-down rows leaked here.
    assert len(datas) == 3  # Current Running + Continue Feature + Cancel
    for cb in datas:
        assert not any(
            token in cb for token in ("resrc_show", "resrc_wtabs", "resrc_allapps")
        )
    assert "feature:resrc_run:f1" in datas
    assert "feature:resrc_continue:f1" in datas
    assert "feature:resrc_cancel:f1" in datas


# 3. Without a feature_id, Continue is gone and Close replaces it.
def test_home_without_feature_id_uses_close():
    text, rows = build_resource_review_home(None, _scan())
    datas = _flat(rows)
    assert "feature:resrc_continue:" not in datas
    assert "feature:resrc_run:" in datas  # Current Running, empty server token
    assert "feature:noop" in datas  # âŒ Close


# 4. The count-only summary line is preserved verbatim.
def test_home_counts_message_preserved():
    text, rows = build_resource_review_home("f1", _scan(n_apps=2, n_tabs=5, n_windows=3))
    assert "\U0001f9e9 FEATURE RESOURCE REVIEW" in text
    assert "Applications: 2" in text
    assert "Chrome windows/profiles: 3" in text
    assert "Chrome tabs: 5" in text


# 5. The home message header text is unchanged.
def test_home_message_header_unchanged():
    text, rows = build_resource_review_home("f1", _scan())
    texts = [b["text"] for row in rows for b in row]
    assert text.startswith("\U0001f9e9 FEATURE RESOURCE REVIEW")
    assert "\U0001f9e9 Current Running" in texts
    assert "\u25b6\ufe0f Continue Feature" in texts
    assert "\u274c Cancel" in texts


# 6. No top-level Refresh / Current Screen / Chrome / All Apps buttons remain.
def test_home_has_no_toplevel_refresh_or_screen():
    text, rows = build_resource_review_home("f1", _scan())
    datas = _flat(rows)
    assert "feature:resrc_refresh:f1" not in datas
    assert "feature:resrc_screen:f1" not in datas
    assert "feature:resrc_wmenu:f1" not in datas
    assert "feature:resrc_allapps:0" not in datas


# 7. The Current Running panel still carries its contextual Refresh + Back.
def test_current_running_menu_keeps_refresh_and_back():
    text, rows = build_current_running_menu("f1", _scan())
    datas = _flat(rows)
    assert "feature:resrc_refresh_run:" in datas  # ðŸ”„ Refresh
    assert "feature:resrc_home:" in datas  # â¬…ï¸ Back


# 8. Current Running still drills through to per-window + per-app rows.
def test_current_running_menu_still_drills_through():
    text, rows = build_current_running_menu("f1", _scan(n_apps=2, n_tabs=5, n_windows=3))
    datas = _flat(rows)
    # Collapsed: group count summaries instead of old per-row drill-downs.
    combined = text + " " + " ".join(b["text"] for row in rows for b in row)
    assert "Applications (2)" in combined
    assert "Chrome (5)" in combined  # 5 real tabs (tabs=5, was windows=3)
    # The two collapsed drill-down entry points.
    assert any("resrc_expand_apps" in cb for cb in datas)
    assert any("resrc_expand_chrome" in cb for cb in datas)
    # No old-style per-window / per-app rows leak into the collapsed view.
    assert not any("resrc_wtabs" in cb for cb in datas)
    assert not any("resrc_show" in cb for cb in datas)
