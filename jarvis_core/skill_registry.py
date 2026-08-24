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
            if not out["gui_ok"]:
                warning = gui_result.get("warning", "Calculator did not confirm the displayed result.")
                # Retain the computed value for troubleshooting, but a GUI
                # request is incomplete until Calculator displays the value.
                out.update({
                    "status": "error",
                    "warning": warning,
                    "message": f"Calculator was not updated: {warning}",
                })
            elif gui_result.get("displayed_result") is not None:
                out["displayed_result"] = gui_result["displayed_result"]
        return out
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def _exec_browser_open(url: str) -> dict:
    import subprocess
    safe_url = url.strip()
    if not safe_url.startswith(("http://", "https://")):
        safe_url = "https://" + safe_url
    # Use 'start' shell command on Windows to open URL in the default browser
    # and force it to the foreground (unlike webbrowser.open which can open behind)
    try:
        subprocess.Popen(
            f'start "" "{safe_url}"',
            shell=True,
        )
    except Exception:
        # Fallback to webbrowser if shell start fails
        import webbrowser
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
            # YouTube sometimes returns a minimal/anti-bot page without ids.
            # The date-filtered search page is a useful truthful fallback.
            webbrowser.open(search_url)
            return {
                "status": "done", "query": query, "url": search_url,
                "warning": "YouTube did not expose a playable result directly.",
                "message": "Opened YouTube results sorted by newest upload.",
            }
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


def _exec_notepad_save(folder_name: str, file_name: str) -> dict:
    """Save the active Notepad tab to an allowlisted folder using Save As."""
    try:
        import pyautogui
        import pygetwindow as gw
        import time
        from jarvis_core.config import ALLOWED_FOLDERS

        folder = folder_name.lower().strip()
        if folder not in ALLOWED_FOLDERS:
            return {"status": "error", "message": f"Folder not in allowlist: {folder}"}

        # A filename must never be allowed to escape the requested folder.
        safe_name = file_name.strip().strip('"').strip()
        if not safe_name or any(char in safe_name for char in '<>:"/\\|?*'):
            return {"status": "error", "message": "The requested filename is invalid."}
        if safe_name in {".", ".."} or safe_name.rstrip(". ") != safe_name:
            return {"status": "error", "message": "The requested filename is invalid."}
        if not Path(safe_name).suffix:
            safe_name += ".txt"

        target = Path(ALLOWED_FOLDERS[folder]) / safe_name
        if target.exists():
            return {
                "status": "error",
                "message": f"File already exists and will not be overwritten: {target}",
            }

        active = gw.getActiveWindow()
        if not active or "notepad" not in getattr(active, "title", "").lower():
            return {"status": "error", "message": "Notepad is not the focused window."}

        # Ctrl+Shift+S forces the Save As dialog even if a tab was previously saved.
        pyautogui.hotkey("ctrl", "shift", "s")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            save_windows = gw.getWindowsWithTitle("Save As")
            if save_windows:
                break
            time.sleep(0.2)
        else:
            return {"status": "error", "message": "Notepad's Save As dialog did not open."}

        save_window = save_windows[0]
        if getattr(save_window, "isMinimized", False):
            save_window.restore()
        save_window.activate()
        time.sleep(0.2)
        # In the standard Windows dialog Alt+N focuses File name. Passing the
        # full allowed path avoids relying on the dialog's current directory.
        pyautogui.hotkey("alt", "n")
        pyautogui.write(str(target), interval=0.02)
        pyautogui.press("enter")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if target.exists():
                return {"status": "done", "message": f"Saved document to {target}", "file": str(target)}
            time.sleep(0.2)
        return {"status": "error", "message": f"Notepad did not save the document to {target}."}
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
    data = get_system_status(launch_task_manager=False)

    cpu_temp = data.get("cpu_temp_c")
    temp_str = f"{cpu_temp}°C" if cpu_temp is not None else "N/A"

    lines = [
        f"🖥️  CPU       : {data['cpu_percent']}%",
        f"🌡️  Temp      : {temp_str}",
        f"🧠  RAM       : {data['ram_percent']}%  ({data['ram_used_gb']} / {data['ram_total_gb']} GB)",
        f"💾  Disk (C:) : {data['disk_percent']}%  ({data['disk_used_gb']} / {data['disk_total_gb']} GB)",
        f"⚙️  Processes : {data['running_processes']} running",
    ]
    if "gpu_percent" in data:
        gpu_temp = data.get("gpu_temp_c")
        lines.insert(3, f"🎮  GPU       : {data['gpu_percent']}%  (Temp: {gpu_temp}°C)")
    if "vram_used_gb" in data:
        lines.insert(4, f"📦  VRAM      : {data['vram_used_gb']} / {data['vram_total_gb']} GB")

    lines.append("")
    lines.append("📊  Live monitor is now active in the dashboard.")
    lines.append("💡  Need Task Manager? Press Ctrl + Shift + Esc")

    data["status"] = "done"
    data["summary"] = "\n".join(lines)
    data["panel"] = "system_status"   # ← tells the UI to open the live monitor
    return data


def _exec_gmail_report(request: str) -> dict:
    from jarvis_core.gmail_reporting import run_gmail_report

    return run_gmail_report(request)


def _exec_gmail_details(category: str, start_date: str, end_date: str) -> dict:
    from jarvis_core.gmail_reporting import get_gmail_details

    return get_gmail_details(category, start_date, end_date)


def _exec_gmail_draft_view(draft_id: str) -> dict:
    from jarvis_core.gmail_reporting import view_gmail_draft

    return view_gmail_draft(draft_id)


def _exec_gmail_draft_keep(draft_id: str) -> dict:
    from jarvis_core.gmail_reporting import keep_gmail_draft

    return keep_gmail_draft(draft_id)


def _exec_gmail_draft_send(draft_id: str) -> dict:
    from jarvis_core.gmail_reporting import send_existing_gmail_draft

    return send_existing_gmail_draft(draft_id)





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
    description="Save the active Notepad file to an allowlisted folder (requires confirmation)",
    required_params=["folder_name", "file_name"],
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

registry.register(SkillDefinition(
    skill_id="gmail.report",
    description="Scan an exact Gmail date range and build an authoritative report",
    required_params=["request"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_gmail_report,
))

registry.register(SkillDefinition(
    skill_id="gmail.details",
    description="Show safe stored Gmail report details",
    required_params=["category", "start_date", "end_date"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_gmail_details,
))

registry.register(SkillDefinition(
    skill_id="gmail.draft.view",
    description="View an existing Gmail draft",
    required_params=["draft_id"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_gmail_draft_view,
))

registry.register(SkillDefinition(
    skill_id="gmail.draft.keep",
    description="Keep an existing Gmail draft without sending it",
    required_params=["draft_id"],
    optional_params=[],
    requires_confirmation=False,
    executor=_exec_gmail_draft_keep,
))

registry.register(SkillDefinition(
    skill_id="gmail.draft.send",
    description="Send one existing Gmail draft after explicit confirmation",
    required_params=["draft_id"],
    optional_params=[],
    requires_confirmation=True,
    executor=_exec_gmail_draft_send,
))
