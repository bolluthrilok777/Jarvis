"""Declarative capabilities for safe application control."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class AppAdapter:
    app_name: str
    aliases: tuple[str, ...]
    supported_actions: tuple[str, ...]
    risk_level: str
    execution_method: str
    verification_method: str
    failure_behavior: str = "Report the reason and never claim completion."


ADAPTERS = {
    item.app_name: item for item in (
        AppAdapter("calculator", ("calc", "super calculator"), ("open", "calculate"),
                   "SAFE", "CalculatorAgent", "Focused window and displayed result"),
        AppAdapter("notepad", ("note pad",), ("open", "new_tab", "type", "save"),
                   "SAFE", "NotepadAgent", "Focused Notepad window"),
        AppAdapter("browser", ("chrome", "edge"), ("open", "read_page", "screenshot"),
                   "SAFE", "BrowserAgent", "Validated URL/result"),
        AppAdapter("file explorer", ("explorer",), ("open_folder", "search"),
                   "SAFE", "FileExplorerAgent", "Allowlisted resolved path"),
        AppAdapter("paint", (), ("open",), "SAFE", "PaintAgent", "Running process"),
        AppAdapter("youtube", (), ("search", "play_latest", "research"),
                   "SAFE", "YouTubeAgent", "Validated YouTube URL/metadata"),
        AppAdapter("google", (), ("search",), "SAFE", "GoogleAgent", "Encoded Google URL"),
        AppAdapter("notes", ("note",), ("create", "append", "list"),
                   "SAFE", "NotesAgent", "Existing append-only local file"),
    )
}


def find_adapter(name: str) -> AppAdapter | None:
    wanted = name.lower().strip()
    for adapter in ADAPTERS.values():
        if wanted == adapter.app_name or wanted in adapter.aliases:
            return adapter
    return None


def public_adapters() -> list[dict]:
    return [asdict(adapter) for adapter in ADAPTERS.values()]
