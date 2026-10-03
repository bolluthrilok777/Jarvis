"""Part 8C-2B - clickable Chrome tab entries with inline action buttons.

Expanded Chrome tab entries are clickable ON the SAME Telegram message.
Tapping one tab reveals that tab's action buttons (🎯 Focus / 📸 Screenshot /
❌ Close / ✏️ Edit / 🔼 Hide Actions) inline; only ONE tab can have its
actions expanded at a time, and Hide Actions collapses them again. The action
buttons are only rendered here - no action behaviour is wired up yet.
"""
from __future__ import annotations

from jarvis_core.resource_menus import (
    build_current_running_expanded_apps,
    build_current_running_expanded_chrome,
    build_current_running_menu,
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
            "tab_index": i,
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


# 1. Each expanded Chrome tab entry is clickable.
def test_chrome_tab_clickable():
    text, rows = build_current_running_expanded_chrome(
        "f1", _scan(n_apps=2, n_tabs=3))
    datas = _flat(rows)

    # Every real tab gets its own clickable row - never the old resrc_tab_show
    # drill-down.
    assert "feature:resrc_tab_actions:r0-t0" in datas
    assert "feature:resrc_tab_actions:r0-t1" in datas
    assert "feature:resrc_tab_actions:r0-t2" in datas
    assert not any("resrc_tab_show" in cb for cb in datas)
    tab_texts = [b["text"] for row in rows for b in row]
    assert any("Real Tab 0" in t for t in tab_texts), \
        "tab names are inline button text"
    assert any("Real Tab 1" in t for t in tab_texts), \
        "tab names are inline button text"


# 2. Clicking a tab shows its action buttons inline on the SAME message.
def test_clicking_tab_shows_action_buttons_inline():
    text, rows = build_current_running_expanded_chrome(
        "f1", _scan(n_apps=2, n_tabs=3), tab_id="r0-t0")
    datas = _flat(rows)

    # Action buttons for the selected tab are rendered.
    assert "feature:resrc_focus:r0-t0" in datas
    assert "feature:resrc_shot:r0-t0" in datas
    assert "feature:resrc_close:r0-t0" in datas
    assert "feature:resrc_edit:r0-t0" in datas
    assert any("resrc_hide_tab_actions" in cb for cb in datas)

    # The action labels appear as inline BUTTON text.
    action_texts = [b["text"] for row in rows for b in row]
    assert any("\U0001f3af Focus" in t for t in action_texts)
    assert any("\U0001f4f8 Screenshot" in t for t in action_texts)
    assert any("\u274c Close" in t for t in action_texts)
    assert any("\u270f\ufe0f Edit" in t for t in action_texts)
    assert any("\U0001f53c Hide Actions" in t for t in action_texts)

    # The other tabs stay listed WITHOUT any actions.
    assert any("Real Tab 1" in t for t in action_texts)
    assert "feature:resrc_focus:r0-t1" not in datas


# 3. Clicking a second tab collapses the first tab's actions.
def test_second_tab_collapses_first():
    scan = _scan(n_apps=2, n_tabs=3)
    text_a, rows_a = build_current_running_expanded_chrome(
        "f1", scan, tab_id="r0-t0")
    text_b, rows_b = build_current_running_expanded_chrome(
        "f1", scan, tab_id="r0-t1")
    datas_a = _flat(rows_a)
    datas_b = _flat(rows_b)

    # Exactly ONE tab carries actions per view.
    assert "feature:resrc_focus:r0-t0" in datas_a
    assert "feature:resrc_focus:r0-t1" not in datas_a
    assert "feature:resrc_focus:r0-t1" in datas_b
    assert "feature:resrc_focus:r0-t0" not in datas_b
    assert sum(1 for cb in datas_a
               if cb.startswith("feature:resrc_focus:")) == 1
    assert sum(1 for cb in datas_b
               if cb.startswith("feature:resrc_focus:")) == 1


# 4. Hide Actions collapses the selected tab's actions.
def test_hide_actions_collapses_selected_tab():
    scan = _scan(n_apps=2, n_tabs=3)
    text, rows = build_current_running_expanded_chrome(
        "f1", scan, tab_id="r0-t0")
    datas = _flat(rows)

    # Hide Actions is offered for the selected tab.
    assert any("resrc_hide_tab_actions" in cb for cb in datas)

    # Rendering without a selection (the Hide Actions target) shows the plain
    # expanded list with NO action buttons at all.
    hidden_text, hidden_rows = build_current_running_expanded_chrome("f1", scan)
    hidden = _flat(hidden_rows)
    assert not any("resrc_focus" in cb for cb in hidden)
    assert not any("resrc_shot" in cb for cb in hidden)
    assert not any("resrc_close" in cb for cb in hidden)
    assert not any("resrc_edit" in cb for cb in hidden)
    assert not any("resrc_hide_tab_actions" in cb for cb in hidden)
    hidden_texts = [b["text"] for row in hidden_rows for b in row]
    assert any("Real Tab 0" in t for t in hidden_texts), \
        "tab names are inline button text"
    assert any("Real Tab 1" in t for t in hidden_texts), \
        "tab names are inline button text"


# 5. Every view stays on the SAME Telegram message (valid text+rows to edit).
def test_same_message_preserved():
    scan = _scan(n_apps=2, n_tabs=3)
    views = [
        build_current_running_menu("f1", scan),
        build_current_running_expanded_apps("f1", scan),
        build_current_running_expanded_chrome("f1", scan),
        build_current_running_expanded_chrome("f1", scan, tab_id="r0-t0"),
        build_current_running_expanded_chrome("f1", scan, tab_id="r0-t1"),
    ]
    for text, rows in views:
        assert isinstance(text, str) and isinstance(rows, list)
        assert "CURRENT RUNNING" in text

    # The inline flow never re-opens the OLD resrc_tab_show drill-down screen.
    datas = (_flat(views[2][1]) + _flat(views[3][1]) + _flat(views[4][1]))
    assert not any("resrc_tab_show" in cb for cb in datas)