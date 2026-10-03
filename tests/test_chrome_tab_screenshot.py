"""Part 5 - 📸 Screenshot of ONE selected Chrome tab.

Covers ONLY the Chrome-tab screenshot callback path inside RESOURCE_REVIEW:

    Expand Chrome → select one tab → tab action menu → 📸 Screenshot
    → capture ONLY that exact CDP target → sendPhoto to Telegram
    → remain in RESOURCE_REVIEW.

Chrome/CDP is fully mocked: ``browser_resource_manager.screenshot_tab`` is
replaced at the registry boundary (recording the exact targetId it was asked
to capture), and the backend itself is tested against a fake CDP HTTP surface
plus a fake ``playwright.sync_api``. Fake captures write REAL tiny PNGs so
production validation runs - the real user's Chrome is never touched.

Identity contract under test: generation-stamped ``resource_id`` + the exact
CDP ``targetId`` (``page_id``) - never a title, a Chrome window, a tab index
or an account name.
"""

from __future__ import annotations

import base64
import os
import sys
import types
from unittest.mock import Mock

import pytest

import jarvis_core.browser_resource_manager as browser_resource_manager
import jarvis_core.resource_manager as resource_manager
import jarvis_core.uia_browser_manager as uia_browser_manager
import jarvis_core.windows_resource_manager as windows_resource_manager
from jarvis_core.resource_manager import Resource
from jarvis_core.telegram_service import TelegramService
from tests.test_telegram_integration import FakeAPI, _settings

CDP_URL = "http://127.0.0.1:9222"
TITLE = "ChatGPT \u2014 Jarvis Project"
OTHER_TITLE = "Docs"
SAME_URL = "https://chatgpt.com/c/abc"
TID_SEL = "TSEL01"
TID_OTHER = "TOTHER2"
TID_THIRD = "TTHIRD3"

STALE_TEXT = "Resource list changed"
CAPTURE_ACK = "\U0001f4f8 Capturing..."
FOCUS_ACK = "\U0001f441 Focusing..."
CLOSE_ACK = "\u274c Closing..."
FAIL_TEXT = "\u26a0\ufe0f Screenshot failed"
EXACT_UNAVAILABLE = "Exact Chrome tab capture unavailable"


# ── Harness (mirrors tests/test_resource_app_screenshot.py) ─────────────────


def _cb(cb_id, user_id, data, chat_id=100, message_id=777):
    return {
        "update_id": 10,
        "callback_query": {
            "id": cb_id,
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}, "message_id": message_id},
            "data": data,
        },
    }


def _seed_tabs(registry, tabs_spec):
    """Seed CDP-mode Chrome tabs: (resource_id, title, target_id[, raw_url])."""
    registry._generation = 1
    registry._feature_id = None
    registry._cdp_url = CDP_URL
    registry._chrome_detected = True
    registry._chrome_controllable = True
    registry._chrome_reason = ""
    registry._chrome_control_mode = "cdp"
    registry._last_scan_at = 0.0
    resources = {}
    for item in tabs_spec:
        rid, title, target_id = item[0], item[1], item[2]
        raw_url = item[3] if len(item) > 3 else ""
        resources[rid] = Resource(
            resource_id=rid, feature_id=None, resource_type="browser_tab",
            title=title, display_name="Chrome", page_id=target_id,
            executable="chrome.exe", control_mode="cdp", _raw_url=raw_url,
        )
    registry._resources = resources


def _make_service(monkeypatch, scan_counter):
    reg = resource_manager.resource_registry

    def _fake_scan(feature_id=None, cdp_url=None, include_identity=False,
                   read_only=False):
        scan_counter.append(1)
        reg._resources = {
            rid: r for rid, r in reg._resources.items() if r.status == "open"}
        return {"generation": reg._generation, "feature_id": feature_id,
                "applications": [], "tabs": [], "chrome_detected": True,
                "chrome_controllable": True, "chrome_reason": "",
                "chrome_control_mode": "cdp", "scanned_at": ""}

    monkeypatch.setattr(reg, "scan", _fake_scan)
    api = FakeAPI()
    service = TelegramService(settings=_settings(123), api=api)
    service._start_task_watcher = Mock()
    service._feature_orchestrator = Mock()
    return service, api


def _mock_success_tab_capture(monkeypatch, tmp_path, shot_calls):
    """Mock the exact-CDP-target capture: REAL PNG bytes, zero real shots."""

    def _fake_shot(cdp_url, target_id, out_path, raw_url=""):
        shot_calls.append((cdp_url, target_id, raw_url))
        import PIL.Image

        PIL.Image.new("RGB", (8, 5), (30, 30, 220)).save(out_path, "PNG")
        return True

    monkeypatch.setattr(browser_resource_manager, "screenshot_tab", _fake_shot)
    monkeypatch.setattr(resource_manager, "SCREENSHOT_DIR", tmp_path)


def _forbid_window_and_desktop(monkeypatch, calls):
    """Record any window/desktop control primitive so tests can assert that
    none of them ever fire on the Chrome-tab screenshot path."""

    def _recorder(name):
        def _f(*_a, **_kw):
            calls.append(name)
            return None
        return _f

    monkeypatch.setattr(
        windows_resource_manager, "focus_window", _recorder("focus_window"))
    monkeypatch.setattr(
        browser_resource_manager, "focus_tab", _recorder("focus_tab"))
    monkeypatch.setattr(
        uia_browser_manager, "focus_tab", _recorder("uia_focus_tab"))
    monkeypatch.setattr(
        windows_resource_manager, "enumerate_windows",
        _recorder("enumerate_windows"))
    monkeypatch.setattr(
        "jarvis_core.screenshot_service.capture_window",
        _recorder("capture_window"))
    monkeypatch.setattr(
        "jarvis_core.screenshot_service.capture_region",
        _recorder("capture_region"))
    import PIL.ImageGrab

    monkeypatch.setattr(PIL.ImageGrab, "grab", _recorder("imagegrab_grab"))


@pytest.fixture(autouse=True)
def _reset_registry():
    reg = resource_manager.resource_registry
    yield
    reg._generation = 0
    reg._resources = {}
    reg._feature_id = None
    reg._cdp_url = None
    reg._chrome_detected = False
    reg._chrome_controllable = False
    reg._chrome_reason = ""
    reg._chrome_control_mode = ""


# ── 1. Exact selected Chrome CDP target ─────────────────────────────────────────


def test_cdp_exact_selected_target_captured(monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_tabs(reg, [("r1-t1", TITLE, TID_SEL, SAME_URL),
                     ("r1-t2", OTHER_TITLE, TID_OTHER)])
    shot_calls: list = []
    _mock_success_tab_capture(monkeypatch, tmp_path, shot_calls)

    service.process_update(_cb("cbT1", 123, "feature:resrc_shot:r1-t1"))

    assert shot_calls == [(CDP_URL, TID_SEL, SAME_URL)], \
        "ONLY the exact selected CDP targetId is captured"
    assert len(api.photos) == 1
    assert api.photos[0]["chat_id"] == 100
    assert not scan_counter, "screenshot must not trigger a rescan"


# ── 2. Two same-title tabs → selected target only ──────────────────────────────


def test_two_same_title_tabs_capture_only_selected(monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_tabs(reg, [("r1-t1", TITLE, TID_OTHER),
                     ("r1-t2", TITLE, TID_SEL)])
    shot_calls: list = []
    _mock_success_tab_capture(monkeypatch, tmp_path, shot_calls)

    service.process_update(_cb("cbT2", 123, "feature:resrc_shot:r1-t2"))

    assert shot_calls == [(CDP_URL, TID_SEL, "")], \
        "same-title tabs must never swap: only the selected targetId is shot"
    assert len(api.photos) == 1


# ── 3. Stale tab → nothing captured, panel refreshed ───────────────────────────


def test_stale_tab_never_captures_and_refreshes_panel(
        monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_tabs(reg, [("r1-t1", TITLE, TID_SEL, SAME_URL),
                     ("r1-t2", OTHER_TITLE, TID_OTHER)])
    reg._generation = 2  # callback belongs to generation 1 -> stale

    def _forbidden(*_a, **_kw):
        raise AssertionError("capture attempted on a stale tab")

    monkeypatch.setattr(
        browser_resource_manager, "screenshot_tab", _forbidden)

    service.process_update(_cb("cbT3", 123, "feature:resrc_shot:r1-t1"))

    assert api.photos == [], "stale tab must never produce a screenshot"
    assert any(STALE_TEXT in m["text"] for m in api.messages), \
        "visible stale warning is shown"
    assert len(getattr(api, "edits", [])) >= 1, \
        "the SAME panel is refreshed with the latest resources"
    assert scan_counter, "stale path re-scans to show the latest resources"


# ── 4. Missing / dead target → honest failure, no wrong fallback ───────────────


def test_missing_or_dead_target_honest_failure_no_fallback(
        monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_tabs(reg, [("r1-t1", TITLE, TID_SEL, SAME_URL)])
    forbidden_calls: list = []
    monkeypatch.setattr(
        browser_resource_manager, "screenshot_tab",
        lambda *_a, **_kw: False)   # exact capture unavailable
    _forbid_window_and_desktop(monkeypatch, forbidden_calls)

    service.process_update(_cb("cbT4", 123, "feature:resrc_shot:r1-t1"))

    assert api.photos == [], "no wrong/dead image may be delivered"
    texts = [m["text"] for m in api.messages]
    assert any(FAIL_TEXT in t for t in texts), "visible failure message"
    assert any(EXACT_UNAVAILABLE in t for t in texts), \
        "the real reason is surfaced, never a silent fallback"
    assert forbidden_calls == [], \
        "no focus / window / desktop primitives ran on the failure path"


# ── 5. Successful image delivered to Telegram ──────────────────────────────────


def test_success_delivers_photo_and_cleans_temp(monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_tabs(reg, [("r1-t1", TITLE, TID_SEL)])
    _mock_success_tab_capture(monkeypatch, tmp_path, [])

    service.process_update(_cb("cbT5", 123, "feature:resrc_shot:r1-t1"))

    assert len(api.photos) == 1, "sendPhoto called exactly once"
    photo = api.photos[0]
    assert photo["chat_id"] == 100
    assert photo["existed"] is True, "temp file exists DURING upload"
    assert photo["size"] > 0
    assert not os.path.exists(photo["photo"]), \
        "temp file deleted after upload"
    assert api.messages == [], \
        "photo-only delivery: no extra success text message"
    assert getattr(api, "edits", []) == [], \
        "existing Resource Review control panel stays valid/untouched"
    assert not scan_counter

# ── 6. Caption contains selected tab title ─────────────────────────────────────


def test_caption_contains_selected_tab_title(monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_tabs(reg, [("r1-t1", TITLE, TID_SEL)])
    _mock_success_tab_capture(monkeypatch, tmp_path, [])

    service.process_update(_cb("cbT6", 123, "feature:resrc_shot:r1-t1"))

    assert len(api.photos) == 1
    assert api.photos[0]["caption"] == f"\U0001f4f8 Screenshot: {TITLE}"


# ── 7. State remains RESOURCE_REVIEW ───────────────────────────────────────────


def test_state_remains_resource_review_after_tab_screenshot(
        monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_tabs(reg, [("r1-t1", TITLE, TID_SEL)])
    _mock_success_tab_capture(monkeypatch, tmp_path, [])

    service.process_update(_cb("cbT7", 123, "feature:resrc_shot:r1-t1"))

    assert service._feature_orchestrator.mock_calls == [], \
        "screenshot must not advance/cancel the feature (stays RESOURCE_REVIEW)"
    assert getattr(api, "edits", []) == [], \
        "the Resource Review control panel message stays valid in place"
    assert reg.get("r1-t1") is not None, \
        "the selected tab stays tracked and usable"


# ── 8. Focus is not triggered for direct exact-CDP capture ─────────────────────


def test_focus_not_triggered_for_direct_cdp_capture(monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_tabs(reg, [("r1-t1", TITLE, TID_SEL, SAME_URL)])
    forbidden_calls: list = []
    _mock_success_tab_capture(monkeypatch, tmp_path, [])
    _forbid_window_and_desktop(monkeypatch, forbidden_calls)

    service.process_update(_cb("cbT8", 123, "feature:resrc_shot:r1-t1"))

    assert forbidden_calls == [], \
        "direct exact-CDP capture needs no Focus/activation"
    assert ("cbT8", CAPTURE_ACK) in api.callbacks, \
        "the callback is acknowledged as a screenshot capture"
    assert not any(text == FOCUS_ACK for _, text in api.callbacks), \
        "the Focus action flow is not triggered"
    all_texts = [m["text"] for m in api.messages] + \
        [e["text"] for e in getattr(api, "edits", [])]
    assert not any("Focused:" in t for t in all_texts), \
        "no Focus outcome message - this was a screenshot"
# ── 9. Close is never triggered ────────────────────────────────────────────────


def test_close_never_triggered_from_tab_screenshot(monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_tabs(reg, [("r1-t1", TITLE, TID_SEL)])
    _mock_success_tab_capture(monkeypatch, tmp_path, [])
    close_calls: list = []
    monkeypatch.setattr(
        browser_resource_manager, "close_tab",
        lambda *a, **k: close_calls.append("cdp") or True)
    monkeypatch.setattr(
        uia_browser_manager, "close_tab",
        lambda *a, **k: close_calls.append("uia") or True)
    monkeypatch.setattr(
        windows_resource_manager, "close_window",
        lambda *a, **k: close_calls.append("win") or True)

    service.process_update(_cb("cbT9", 123, "feature:resrc_shot:r1-t1"))

    assert close_calls == [], "Close must never run from a Screenshot press"
    assert not any(text == CLOSE_ACK for _, text in api.callbacks), \
        "the Close action flow is not triggered"
    all_texts = [m["text"] for m in api.messages] + \
        [e["text"] for e in getattr(api, "edits", [])]
    assert not any("Closed" in t for t in all_texts), \
        "no Close outcome message"


# ── 10. No whole-desktop fallback ──────────────────────────────────────────────


def test_no_whole_desktop_fallback_for_tab_capture(monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_tabs(reg, [("r1-t1", TITLE, TID_SEL)])
    _mock_success_tab_capture(monkeypatch, tmp_path, [])
    grab_calls: list = []
    import PIL.ImageGrab

    monkeypatch.setattr(PIL.ImageGrab, "grab",
                        lambda *a, **k: grab_calls.append(1))

    service.process_update(_cb("cbT10", 123, "feature:resrc_shot:r1-t1"))

    assert grab_calls == [], \
        "the desktop is never screenshotted for a tab (success path)"

    # And on the failure path the desktop still is never used as a fallback.
    monkeypatch.setattr(
        browser_resource_manager, "screenshot_tab", lambda *_a, **_kw: False)
    forbidden_calls: list = []
    _forbid_window_and_desktop(monkeypatch, forbidden_calls)

    photos_before: int = len(api.photos)
    service.process_update(_cb("cbT10b", 123, "feature:resrc_shot:r1-t1"))

    assert len(api.photos) == photos_before, \
        "still no image after the exact capture failed"
    assert forbidden_calls == [], "no whole-desktop / window fallback"
    texts = [m["text"] for m in api.messages]
    assert any(FAIL_TEXT in t for t in texts)


# ── 11. Other tabs / windows untouched ─────────────────────────────────────────


def test_other_tabs_and_windows_untouched(monkeypatch, tmp_path):
    scan_counter: list = []
    service, api = _make_service(monkeypatch, scan_counter)
    reg = resource_manager.resource_registry
    _seed_tabs(reg, [("r1-t1", OTHER_TITLE, TID_OTHER),
                     ("r1-t2", TITLE, TID_SEL),
                     ("r1-t3", OTHER_TITLE, TID_THIRD)])
    shot_calls: list = []
    forbidden_calls: list = []
    _mock_success_tab_capture(monkeypatch, tmp_path, shot_calls)
    _forbid_window_and_desktop(monkeypatch, forbidden_calls)

    service.process_update(_cb("cbT11", 123, "feature:resrc_shot:r1-t2"))

    assert shot_calls == [(CDP_URL, TID_SEL, "")], \
        "exactly ONE selected tab is captured"
    assert forbidden_calls == [], \
        "no other tab/window is focused or enumerated, no window grab"
    assert len(api.photos) == 1
    assert reg.get("r1-t1") is not None and reg.get("r1-t3") is not None, \
        "the other tracked tabs are untouched and still usable"
# ── Backend: exact-target screenshot_tab (CDP fully mocked) ─────────────────


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, target_id):
        self._target_id = target_id
        self.detached = False

    def send(self, method, **params):
        assert method == "Target.getTargetInfo"
        return {"targetInfo": {"targetId": self._target_id}}

    def detach(self):
        self.detached = True


class _FakePage:
    def __init__(self, url, target_id):
        self.url = url
        self._target_id = target_id
        self.screenshot_calls = []

    def screenshot(self, path=None, timeout=None):
        import PIL.Image

        PIL.Image.new("RGB", (7, 4), (10, 90, 210)).save(path, "PNG")
        self.screenshot_calls.append({"path": path, "timeout": timeout})


class _FakeContext:
    def __init__(self, pages):
        self.pages = pages

    def new_cdp_session(self, page):
        return _FakeSession(getattr(page, "_target_id", ""))


class _FakeBrowser:
    def __init__(self, contexts):
        self.contexts = contexts
        self.closed = False

    def close(self):
        self.closed = True


class _FakeSyncPlaywrightCM:
    def __init__(self, browser_factory):
        self._browser_factory = browser_factory

    def __enter__(self):
        import types as _t

        playwright = _t.SimpleNamespace()
        playwright.chromium = _t.SimpleNamespace()
        playwright.chromium.connect_over_cdp = self._browser_factory
        return playwright

    def __exit__(self, *_exc):
        return False


def _patch_playwright(monkeypatch, browser_factory=()):
    import sys as _sys
    import types as _t

    def _sync_playwright():
        return _FakeSyncPlaywrightCM(browser_factory)

    fake_pkg = _t.ModuleType("playwright")
    fake_pkg.__path__ = []
    fake_sync = _t.ModuleType("playwright.sync_api")
    fake_sync.sync_playwright = _sync_playwright
    monkeypatch.setitem(_sys.modules, "playwright", fake_pkg)
    monkeypatch.setitem(_sys.modules, "playwright.sync_api", fake_sync)


def test_backend_native_cdp_screenshot_exact_target(monkeypatch, tmp_path):
    """The native CDP screenshot endpoint is hit for the EXACT targetId and
    the png is written - no other target URL is ever touched."""
    calls: list = []
    png_path = tmp_path / "tab.png"

    def _fake_get(url, **kwargs):
        calls.append(url)
        if url.endswith("/json/list"):
            return _Resp([{"id": TID_SEL, "type": "page",
                           "url": SAME_URL, "title": TITLE}])
        if url.endswith(f"/json/{TID_SEL}/screenshot"):
            import io

            import PIL.Image

            buf = io.BytesIO()
            PIL.Image.new("RGB", (9, 6), (5, 5, 200)).save(buf, "PNG")
            return _Resp({"data": base64.b64encode(buf.getvalue()).decode()})
        raise AssertionError(f"unexpected CDP URL: {url}")

    monkeypatch.setattr(browser_resource_manager.requests, "get", _fake_get)

    ok = browser_resource_manager.screenshot_tab(
        CDP_URL, TID_SEL, str(png_path), SAME_URL)

    assert ok is True
    assert png_path.exists() and png_path.stat().st_size > 0
    assert any(u.endswith("/json/list") for u in calls)
    assert any(u.endswith(f"/json/{TID_SEL}/screenshot") for u in calls)
    assert not any(f"/json/{TID_OTHER}/" in u for u in calls), \
        "no other target is ever contacted"


def test_backend_dead_target_captures_nothing(monkeypatch, tmp_path):
    """The tracked target is gone from /json/list: capture refuses and no
    screenshot endpoint or Playwright attach is ever attempted."""
    calls: list = []
    png_path = tmp_path / "tab.png"

    def _fake_get(url, **kwargs):
        calls.append(url)
        if url.endswith("/json/list"):
            return _Resp([])   # the tracked target no longer exists
        raise AssertionError(f"screenshot endpoint must not be reached: {url}")

    monkeypatch.setattr(browser_resource_manager.requests, "get", _fake_get)
    _patch_playwright(
        monkeypatch, lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Playwright must never attach for a dead target")))

    ok = browser_resource_manager.screenshot_tab(
        CDP_URL, TID_SEL, str(png_path), SAME_URL)

    assert ok is False
    assert not png_path.exists(), "nothing is written for a dead target"
    assert len(calls) == 1 and calls[0].endswith("/json/list"), \
        "only the live-target check ran; no capture endpoint was touched"


def test_backend_playwright_fallback_matches_exact_targetid_never_title(
        monkeypatch, tmp_path):
    """Native endpoint unsupported → Playwright attaches and captures ONLY
    the page whose CDP targetId matches; a same-title/same-URL twin is never
    screenshotted and the browser is released."""
    calls: list = []
    png_path = tmp_path / "tab.png"
    exact = _FakePage(SAME_URL, TID_SEL)
    twin = _FakePage(SAME_URL, TID_OTHER)      # same URL AND same title
    browser = _FakeBrowser([_FakeContext([twin, exact])])

    def _fake_get(url, **kwargs):
        calls.append(url)
        if url.endswith("/json/list"):
            return _Resp([{"id": TID_SEL, "type": "page",
                           "url": SAME_URL, "title": TITLE}])
        return _Resp({}, status=404)   # native CDP endpoint unsupported

    monkeypatch.setattr(browser_resource_manager.requests, "get", _fake_get)
    _patch_playwright(monkeypatch, lambda base: browser)

    ok = browser_resource_manager.screenshot_tab(
        CDP_URL, TID_SEL, str(png_path), SAME_URL)

    assert ok is True
    assert len(exact.screenshot_calls) == 1, \
        "the exact targetId tab is captured exactly once"
    assert twin.screenshot_calls == [], \
        "the same-title/URL twin is NEVER screenshotted"
    assert png_path.exists()
    assert browser.closed, "the attached browser is always released"


def test_backend_playwright_no_match_returns_false_no_wrong_capture(
        monkeypatch, tmp_path):
    """No page carries the tracked targetId → nothing is captured, no title/
    first-page guessing, and the browser is still released."""
    png_path = tmp_path / "tab.png"
    only = _FakePage(SAME_URL, TID_OTHER)     # present but NOT the target
    browser = _FakeBrowser([_FakeContext([only])])

    def _fake_get(url, **kwargs):
        if url.endswith("/json/list"):
            return _Resp([{"id": TID_SEL, "type": "page",
                           "url": SAME_URL, "title": TITLE},
                          {"id": TID_OTHER, "type": "page",
                           "url": SAME_URL, "title": TITLE}])
        return _Resp({}, status=404)

    monkeypatch.setattr(browser_resource_manager.requests, "get", _fake_get)
    _patch_playwright(monkeypatch, lambda base: browser)

    ok = browser_resource_manager.screenshot_tab(
        CDP_URL, TID_SEL, str(png_path), SAME_URL)

    assert ok is False
    assert only.screenshot_calls == [], \
        "a different tab is never used as a wrong substitute"
    assert not png_path.exists(), "nothing is written when no exact match"
    assert browser.closed
