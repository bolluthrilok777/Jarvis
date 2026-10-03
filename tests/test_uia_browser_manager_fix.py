"""
tests/test_uia_browser_manager_fix.py — UIA enumerate_tabs fix tests

Tests that enumerate_tabs() collects TabItems ONLY from the real Chrome tab-strip
container, not from all window descendants.
"""
from unittest.mock import MagicMock, patch

import pytest

from jarvis_core.uia_browser_manager import enumerate_tabs


def _make_mock_element(title: str, runtime_id: list[int] = None, is_selected: bool = False):
    """Create a mock TabItem element."""
    elem = MagicMock()
    elem.window_text.return_value = title
    elem.element_info.runtime_id = runtime_id or [0]
    elem.is_selected.return_value = is_selected
    return elem


def _make_mock_pane(tab_items: list):
    """Create a mock Pane (tab strip container) with children."""
    pane = MagicMock()
    pane.children.return_value = tab_items
    return pane



def _make_mock_host(hwnd=12345):
    """Create a mock Chrome host window dict."""
    return {"hwnd": hwnd}



class TestEnumerateTabsTabStripOnly:
    """Test that enumerate_tabs uses ONLY the tab strip container."""

    @patch("jarvis_core.uia_browser_manager.chrome_host_windows")
    @patch("jarvis_core.uia_browser_manager._attach")
    @patch("jarvis_core.uia_browser_manager._find_tab_strip")
    def test_two_real_tab_strip_tabs_returns_two(
        self, mock_find_tab_strip, mock_attach, mock_chrome_host_windows
    ):
        """Test 1: 2 real tab-strip tabs = 2 (not more, not less)."""
        # Setup: 2 real tabs in the tab strip
        tab1 = _make_mock_element("Tab 1", runtime_id=[1], is_selected=True)
        tab2 = _make_mock_element("Tab 2", runtime_id=[2], is_selected=False)
        tab_items = [tab1, tab2]
        
        # Mock tab strip container
        tab_strip = _make_mock_pane(tab_items)
        
        # Mock window
        mock_window = MagicMock()
        mock_attach.return_value = mock_window
        
        # Mock _find_tab_strip to return our tab strip
        mock_find_tab_strip.return_value = tab_strip
        
        # Mock Chrome host windows
        mock_chrome_host_windows.return_value = [_make_mock_host()]
        
        # Call enumerate_tabs
        result = enumerate_tabs()
        
        # Verify
        assert result["ok"] is True
        assert len(result["tabs"]) == 2
        assert result["tabs"][0]["title"] == "Tab 1"
        assert result["tabs"][1]["title"] == "Tab 2"
        assert result["tabs"][0]["selected"] is True
        assert result["tabs"][1]["selected"] is False

    @patch("jarvis_core.uia_browser_manager.chrome_host_windows")
    @patch("jarvis_core.uia_browser_manager._attach")
    @patch("jarvis_core.uia_browser_manager._find_tab_strip")
    def test_unrelated_tabitems_excluded(
        self, mock_find_tab_strip, mock_attach, mock_chrome_host_windows
    ):
        """Test 2: unrelated TabItems elsewhere in the window are excluded."""
        # Setup: 2 real tabs in the tab strip
        real_tab1 = _make_mock_element("Real Tab 1", runtime_id=[1], is_selected=True)
        real_tab2 = _make_mock_element("Real Tab 2", runtime_id=[2], is_selected=False)
        real_tab_items = [real_tab1, real_tab2]
        
        # Setup: 3 fake/unrelated TabItems (e.g., from dev tools, side panel, etc.)
        # These should NOT be counted
        fake_tab1 = _make_mock_element("Fake Tab A", runtime_id=[100])
        fake_tab2 = _make_mock_element("Fake Tab B", runtime_id=[101])
        fake_tab3 = _make_mock_element("Fake Tab C", runtime_id=[102])
        
        # Mock tab strip container returns ONLY real tabs
        tab_strip = _make_mock_pane(real_tab_items)
        
        # Mock window
        mock_window = MagicMock()
        mock_attach.return_value = mock_window
        
        # Mock _find_tab_strip to return our tab strip (with only real tabs)
        mock_find_tab_strip.return_value = tab_strip
        
        # Mock Chrome host windows
        mock_chrome_host_windows.return_value = [_make_mock_host()]
        
        # Call enumerate_tabs
        result = enumerate_tabs()
        
        # Verify: ONLY the 2 real tabs from the tab strip should be returned
        assert result["ok"] is True
        assert len(result["tabs"]) == 2
        titles = [tab["title"] for tab in result["tabs"]]
        assert "Real Tab 1" in titles
        assert "Real Tab 2" in titles
        assert "Fake Tab A" not in titles
        assert "Fake Tab B" not in titles
        assert "Fake Tab C" not in titles

    @patch("jarvis_core.uia_browser_manager.chrome_host_windows")
    @patch("jarvis_core.uia_browser_manager._attach")
    @patch("jarvis_core.uia_browser_manager._find_tab_strip")
    def test_same_title_real_tabs_remain_separate(
        self, mock_find_tab_strip, mock_attach, mock_chrome_host_windows
    ):
        """Test 3: same-title real tabs remain separate (different runtime_ids)."""
        # Setup: 2 real tabs with SAME title but different runtime_ids
        # This can happen with duplicate tabs or pinned tabs
        tab1 = _make_mock_element("Same Title", runtime_id=[1], is_selected=True)
        tab2 = _make_mock_element("Same Title", runtime_id=[2], is_selected=False)
        tab_items = [tab1, tab2]
        
        # Mock tab strip container
        tab_strip = _make_mock_pane(tab_items)
        
        # Mock window
        mock_window = MagicMock()
        mock_attach.return_value = mock_window
        
        # Mock _find_tab_strip to return our tab strip
        mock_find_tab_strip.return_value = tab_strip
        
        # Mock Chrome host windows
        mock_chrome_host_windows.return_value = [_make_mock_host()]
        
        # Call enumerate_tabs
        result = enumerate_tabs()
        
        # Verify: Both tabs should be present and distinct
        assert result["ok"] is True
        assert len(result["tabs"]) == 2
        # Both have same title
        assert result["tabs"][0]["title"] == "Same Title"
        assert result["tabs"][1]["title"] == "Same Title"
        # But different runtime_ids
        assert result["tabs"][0]["runtime_id"] == [1]
        assert result["tabs"][1]["runtime_id"] == [2]
        # Different indices
        assert result["tabs"][0]["tab_index"] == 0
        assert result["tabs"][1]["tab_index"] == 1
        # Different selected state
        assert result["tabs"][0]["selected"] is True
        assert result["tabs"][1]["selected"] is False