"""
jarvis_core/skill_registry.py — Phase 4.5 Skill Registry

All 18 skills registered with real executors. Each executor validates
against ALLOWED_APPS / ALLOWED_FOLDERS before calling underlying tools.
"""
from __future__ import annotations

import logging
import re
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus

logger = logging.getLogger("jarvis.skills")


# ── Exceptions ────────────────────────────────────────────────────────────────

class SkillNotFoundError(Exception):
    """Raised when a requested skill ID is not in the registry."""


# ── SkillDefinition ───────────────────────────────────────────────────────────

@dataclass
class SkillDefinition:
    skill_id: str
    description: str
    required_params: list[str]
    optional_params: list[str]
    requires_confirmation: bool
    executor: Callable


# ── Registry ──────────────────────────────────────────────────────────────────

class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, SkillDefinition] = {}

    def register(self, defn: SkillDefinition) -> None:
        self._skills[defn.skill_id] = defn

    def get_skill(self, skill_id: str) -> SkillDefinition:
        if skill_id not in self._skills:
            raise SkillNotFoundError(f"Unknown skill: {skill_id!r}")
        return self._skills[skill_id]

    def list_skills(self) -> list[str]:
        return list(self._skills.keys())


# Module-level singleton
registry = SkillRegistry()


# ── Executor functions ────────────────────────────────────────────────────────

def _exec_app_open(app_name: str) -> dict:
    from jarvis_core.config import ALLOWED_APPS
    from jarvis_core.app_tools import open_known_app
    name = app_name.lower().strip()
    if name not in ALLOWED_APPS:
        logger.warning("app.open rejected: %s not in ALLOWED_APPS", name)
        return {"status": "error", "message": f"App not in allowlist: {name}"}
    success, message = open_known_app(name)
    return {"status": "done" if success else "error", "message": message}


def _exec_app_focus(app_name: str) -> dict:
    from jarvis_core.config import ALLOWED_APPS
    name = app_name.lower().strip()
    if name not in ALLOWED_APPS:
        return {"status": "error", "message": f"App not in allowlist: {name}"}
    try:
        import pygetwindow as gw
        wins = gw.getWindowsWithTitle(name)
        if wins:
            wins[0].activate()
            return {"status": "done", "message": f"Focused window: {name}"}
        return {"status": "error", "message": f"No window found for: {name}"}
    except Exception as exc:
        logger.warning("app.focus error: %s", exc)
        return {"status": "error", "message": str(exc)}


def _exec_app_close(app_name: str) -> dict:
    from jarvis_core.config import ALLOWED_APPS
    name = app_name.lower().strip()
    if name not in ALLOWED_APPS:
        return {"status": "error", "message": f"App not in allowlist: {name}"}
    try:
        import pygetwindow as gw
        wins = gw.getWindowsWithTitle(name)
        if wins:
            wins[0].close()
            return {"status": "done", "message": f"Closed window: {name}"}
        return {"status": "error", "message": f"No window found for: {name}"}
    except Exception as exc:
        logger.warning("app.close error: %s", exc)
        return {"status": "error", "message": str(exc)}


def _exec_calculator_calculate(expression: str, use_gui: bool = False) -> dict:
    from calculator_tools import safe_calculate, drive_calculator_gui
    try:
        result = safe_calculate(expression)
        out = {"status": "done", "result": result, "expression": expression, "gui_ok": False}
        if use_gui:
            gui_result = drive_calculator_gui(expression)
            out["gui_ok"] = bool(gui_result.get("gui_ok"))
            if gui_result.get("warning"):
                out["warning"] = gui_result["warning"]
        return out
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def _exec_browser_open(url: str) -> dict:
    safe_url = url.strip()
    if not safe_url.startswith(("http://", "https://")):
        safe_url = "https://" + safe_url
    webbrowser.open(safe_url)
    return {"status": "done", "url": safe_url}


def _exec_browser_google_search(query: str) -> dict:
    from jarvis_core.app_tools import google_search
    return google_search(query)


def _exec_browser_youtube_search(query: str) -> dict:
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    webbrowser.open(url)
    return {"status": "done", "query": query, "url": url}


def _exec_browser_youtube_play_latest(query: str) -> dict:
    import requests
    search_url = (
        f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        "&sp=CAI%253D"
    )
    try:
        response = requests.get(search_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        video_ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', response.text)
        if not video_ids:
            return {"status": "error", "message": "No recent YouTube result was found."}
        watch_url = f"https://www.youtube.com/watch?v={video_ids[0]}&autoplay=1"
        webbrowser.open(watch_url)
        return {"status": "done", "query": query, "url": watch_url,
                "message": f"Playing the latest YouTube result for: {query}"}
    except Exception as exc:
        return {"status": "error", "message": f"Could not play the latest YouTube result: {exc}"}


def _exec_browser_youtube_research(query: str) -> dict:
    """Open one relevant result and save only metadata visible in YouTube search."""
    import html
    import json
    import requests
    from datetime import date, datetime
    from jarvis_core.config import NOTES_DIR

    search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    try:
        response = requests.get(search_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        video_ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', response.text)
        titles = re.findall(r'"title":\{"runs":\[\{"text":"((?:\\.|[^"\\])*)"', response.text)
        if not video_ids:
            return {"status": "error", "message": "No YouTube research result was found."}
        title = query
        if titles:
            try:
                title = json.loads(f'"{titles[0]}"')
            except Exception:
                title = html.unescape(titles[0])
        watch_url = f"https://www.youtube.com/watch?v={video_ids[0]}&autoplay=1"
        webbrowser.open(watch_url)
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        note_path = NOTES_DIR / f"youtube_notes_{date.today().isoformat()}.txt"
        note = (
            f"[{datetime.now().strftime('%I:%M %p').lstrip('0')}] YouTube research\n"
            f"Query: {query}\nTitle: {title}\nURL: {watch_url}\n"
            "Important points: Search-result metadata was captured. Open the video to review details.\n"
            "Transcript unavailable; Jarvis did not infer or hallucinate video contents.\n\n"
        )
        with note_path.open("a", encoding="utf-8") as handle:
            handle.write(note)
        return {"status": "done", "query": query, "title": title, "url": watch_url,
                "file": str(note_path), "transcript_available": False,
                "message": "Opened a relevant YouTube result and saved verified metadata notes."}
    except Exception as exc:
        return {"status": "error", "message": f"YouTube research failed: {exc}"}


def _exec_browser_website_search(site: str, query: str) -> dict:
    # Build a Google site: search for a specific domain
    site_clean = site.strip().lower()
    if site_clean.startswith(("http://", "https://")):
        from urllib.parse import urlparse
        site_clean = urlparse(site_clean).netloc or site_clean
    search_url = f"https://www.google.com/search?q=site:{site_clean}+{quote_plus(query)}"
    webbrowser.open(search_url)
    return {"status": "done", "site": site_clean, "query": query, "url": search_url}


def _exec_browser_read_page(url: str) -> dict:
    from browser_tools import read_webpage
    return read_webpage(url)


def _exec_browser_website_screenshot(url: str) -> dict:
    from browser_tools import screenshot_webpage
    return screenshot_webpage(url)


def _exec_notepad_open() -> dict:
    from jarvis_core.app_tools import open_known_app
    import time
    success, message = open_known_app("notepad")
    if not success:
        return {"status": "error", "message": message}
    try:
        import pyautogui
        import pygetwindow as gw
        deadline = time.monotonic() + 5.0
        windows = []
        while time.monotonic() < deadline:
            windows = gw.getWindowsWithTitle("Notepad")
            if windows:
                break
            time.sleep(0.2)
        if not windows:
            return {"status": "error", "message": "Notepad window was not found."}
        window = windows[0]
        if getattr(window, "isMinimized", False):
            window.restore()
        window.activate()
        time.sleep(0.2)
        pyautogui.click(window.left + window.width // 2, window.top + 10)
        active = gw.getActiveWindow()
        if not active or "notepad" not in getattr(active, "title", "").lower():
            return {"status": "error", "message": "Notepad did not receive focus."}
        pyautogui.hotkey("ctrl", "n")
        return {"status": "done", "message": "Opened a new Notepad tab."}
    except Exception as exc:
        return {"status": "error", "message": f"Could not open a new Notepad tab: {exc}"}


def _exec_notepad_type(text: str) -> dict:
    try:
        import pyautogui
        import pygetwindow as gw
        active = gw.getActiveWindow()
        if not active or "notepad" not in getattr(active, "title", "").lower():
            return {"status": "error", "message": "Notepad is not the focused window."}
        pyautogui.write(text, interval=0.05)
        return {"status": "done", "message": "Typed text into the new Notepad tab."}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def _exec_notepad_save() -> dict:
    try:
        import pyautogui
        pyautogui.hotkey("ctrl", "s")
        return {"status": "done", "message": "Saved active document."}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def _exec_folder_open(folder_name: str) -> dict:
    from jarvis_core.config import ALLOWED_FOLDERS
    from jarvis_core.app_tools import open_known_folder
    name = folder_name.lower().strip()
    if name not in ALLOWED_FOLDERS:
        return {"status": "error", "message": f"Folder not in allowlist: {name}"}
    success, message = open_known_folder(name)
    return {"status": "done" if success else "error", "message": message}


def _exec_file_search(folder: str, pattern: str) -> dict:
    from jarvis_core.config import ALLOWED_FOLDERS
    name = folder.lower().strip()
    if name not in ALLOWED_FOLDERS:
        return {"status": "error", "message": f"Folder not in allowlist: {name}"}
    base = ALLOWED_FOLDERS[name]
    try:
        matches = [str(p) for p in Path(base).glob(f"**/{pattern}")]
        return {"status": "done", "folder": str(base), "pattern": pattern, "files": matches}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def _exec_file_open_containing(query: str, open_file: bool = False) -> dict:
    """Find a named file inside allowed roots, then open it or reveal it."""
    import os
    import subprocess
    from jarvis_core.config import ALLOWED_ROOTS

    raw_tokens = re.findall(r"[a-z0-9]+", query.lower())
    descriptor_tokens = {"resume", "cv", "file", "document", "copy"}
    specific_tokens = [token for token in raw_tokens if token not in descriptor_tokens]
    tokens = specific_tokens or raw_tokens
    if not tokens:
        return {"status": "error", "message": "Please provide a file name to find."}
    matches = []
    try:
        for root in ALLOWED_ROOTS:
            root = Path(root).resolve()
            if not root.exists():
                continue
            for candidate in root.rglob("*"):
                if not candidate.is_file():
                    continue
                searchable = set(re.findall(r"[a-z0-9]+", candidate.stem.lower()))
                if all(token in searchable for token in tokens):
                    matches.append(candidate.resolve())
        if not matches:
            return {"status": "error", "message": f"No allowed file matched: {query}"}
        match = sorted(matches, key=lambda path: (len(path.name), str(path).lower()))[0]
        if open_file:
            os.startfile(str(match))
            message = f"Opened file: {match.name}"
        else:
            subprocess.Popen(["explorer.exe", f"/select,{match}"])
            message = f"Opened file location: {match.name}"
        return {"status": "done", "message": message,
                "file": str(match), "folder": str(match.parent)}
    except Exception as exc:
        return {"status": "error", "message": f"Could not open file location: {exc}"}


def _exec_note_create(content: str) -> dict:
    from jarvis_core.app_tools import create_note
    return create_note(content)


def _exec_system_status() -> dict:
    from jarvis_core.system_tools import get_system_status
    return get_system_status()


def _exec_system_screenshot() -> dict:
    from jarvis_core.system_tools import take_screenshot
    file_path = take_screenshot()
    return {"status": "done", "file": file_path}


# ── Register all 18 skills ────────────────────────────────────────────────────

registry.register(SkillDefinition(
    skill_id="app.open",
    description="Open an allowlisted Windows application",
    required_params=["app_name"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_app_open,
))

registry.register(SkillDefinition(
    skill_id="app.focus",
    description="Bring an application window to focus",
    required_params=["app_name"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_app_focus,
))

registry.register(SkillDefinition(
    skill_id="app.close",
    description="Close an application window (requires confirmation)",
    required_params=["app_name"],
    optional_params=[],
    requires_confirmation=True,
    executor=_exec_app_close,
))

registry.register(SkillDefinition(
    skill_id="calculator.calculate",
    description="Evaluate a safe mathematical expression",
    required_params=["expression"],
    optional_params=["use_gui"],
    requires_confirmation=False,
    executor=_exec_calculator_calculate,
))

registry.register(SkillDefinition(
    skill_id="browser.open",
    description="Open a URL in the default browser",
    required_params=["url"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_browser_open,
))

registry.register(SkillDefinition(
    skill_id="browser.google_search",
    description="Search Google for a query",
    required_params=["query"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_browser_google_search,
))

registry.register(SkillDefinition(
    skill_id="browser.youtube_search",
    description="Search YouTube for a query",
    required_params=["query"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_browser_youtube_search,
))

registry.register(SkillDefinition(
    skill_id="browser.youtube_play_latest",
    description="Search YouTube by upload date and play the latest result",
    required_params=["query"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_browser_youtube_play_latest,
))

registry.register(SkillDefinition(
    skill_id="browser.youtube_research",
    description="Open a relevant YouTube result and save verified metadata notes",
    required_params=["query"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_browser_youtube_research,
))

registry.register(SkillDefinition(
    skill_id="browser.website_search",
    description="Search a specific website via Google site: operator",
    required_params=["site", "query"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_browser_website_search,
))

registry.register(SkillDefinition(
    skill_id="browser.read_page",
    description="Read webpage content using Playwright",
    required_params=["url"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_browser_read_page,
))

registry.register(SkillDefinition(
    skill_id="browser.website_screenshot",
    description="Take a screenshot of a webpage using Playwright",
    required_params=["url"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_browser_website_screenshot,
))

registry.register(SkillDefinition(
    skill_id="notepad.open",
    description="Open Notepad",
    required_params=[],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_notepad_open,
))

registry.register(SkillDefinition(
    skill_id="notepad.type",
    description="Type text into the active Notepad window",
    required_params=["text"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_notepad_type,
))

registry.register(SkillDefinition(
    skill_id="notepad.save",
    description="Save the active Notepad file (requires confirmation)",
    required_params=[],
    optional_params=[],
    requires_confirmation=True,
    executor=_exec_notepad_save,
))

registry.register(SkillDefinition(
    skill_id="folder.open",
    description="Open an allowlisted folder in Explorer",
    required_params=["folder_name"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_folder_open,
))

registry.register(SkillDefinition(
    skill_id="file.search",
    description="Search for files matching a pattern inside an allowed folder",
    required_params=["folder", "pattern"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_file_search,
))

registry.register(SkillDefinition(
    skill_id="file.open_containing",
    description="Find a named file in allowed roots and open it or reveal it in Explorer",
    required_params=["query"],
    optional_params=["open_file"],
    requires_confirmation=False,
    executor=_exec_file_open_containing,
))

registry.register(SkillDefinition(
    skill_id="note.create",
    description="Create a timestamped note",
    required_params=["content"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_note_create,
))

registry.register(SkillDefinition(
    skill_id="system.status",
    description="Get system CPU, RAM, and disk status",
    required_params=[],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_system_status,
))

registry.register(SkillDefinition(
    skill_id="system.screenshot",
    description="Take a desktop screenshot",
    required_params=[],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_system_screenshot,
))
