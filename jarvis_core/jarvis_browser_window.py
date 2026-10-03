"""Jarvis-owned dedicated Chrome window lifecycle (Part 7 browser ownership).

On this Chrome 152 environment none of the Chrome-owned per-window profile
signals are usable read-only: the avatar exposes only an ambiguous display
name, chrome://version is not UIA-readable (even with
``--force-renderer-accessibility``), all profiles share one master PID with no
observable ``--profile-directory``, and CDP is unavailable on the default User
Data. Re-identifying arbitrary pre-existing Chrome windows is therefore
impossible.

The accepted architecture is HARDENED LAUNCH PROVENANCE:

    selected account email
        → (trusted Local State) exactly one profile directory
        → Jarvis launches THAT directory in a dedicated new window at a unique
          marker URL  http://127.0.0.1:8000/browser-bind/<launch_token>
        → correlate the resulting NEW top-level window(s)
        → prove ownership by finding the ONE tab whose URL carries the exact
          unguessable launch token
        → bind that exact window + tab to the workflow (workflow-scoped)

This proves: Jarvis issued the launch for the trusted directory, this exact new
window resulted from it, and this exact tab carries our unique token — so no
arbitrary pre-existing window is ever adopted. Chrome does not expose an
independent second per-window profile signal in this environment; this is the
accepted verification model.

No secrets are read. Everything OS-facing is injected so tests never touch a
real browser.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Callable, Optional

logger = logging.getLogger("jarvis.browser.window")

MARKER_BASE = "http://127.0.0.1:8000/browser-bind"
NEW_WINDOW_WAIT_SECONDS = 15.0
NEW_WINDOW_POLL_INTERVAL = 0.5

VERIFICATION_METHOD = "local_state_profile_mapping_plus_launch_token_provenance"


class JarvisWindowError(Exception):
    """Base error for the dedicated-window lifecycle."""


class ProfileMappingInvalidError(JarvisWindowError):
    """The selected email does not map to exactly one trusted profile dir."""


class NoNewWindowError(JarvisWindowError):
    """The launch produced no new Chrome top-level window."""


class LaunchTokenNotFoundError(JarvisWindowError):
    """No new window carries the exact launch-token marker URL."""


class AmbiguousLaunchTokenError(JarvisWindowError):
    """More than one window carries the launch-token marker URL."""


# ── Path helpers (display / normalization only; not identity) ─────────────────

def normalize_profile_path(path: str) -> str:
    if not path:
        return ""
    p = str(path).strip().strip('"').replace("/", "\\")
    while p.endswith("\\"):
        p = p[:-1]
    try:
        p = os.path.normpath(p)
    except Exception:
        pass
    return p.lower()


def profile_directory_from_path(path: str) -> str:
    if not path:
        return ""
    p = str(path).strip().strip('"').replace("/", "\\")
    while p.endswith("\\"):
        p = p[:-1]
    return p.rsplit("\\", 1)[-1] if p else ""


# ── Email → trusted directory mapping (Local State) ───────────────────────────

def directories_for_email(email: str, meta: dict) -> list:
    """All trusted Local State profile directories whose account email equals
    *email* (case-insensitive). Email is unique per profile in practice, but we
    return the full list so the caller can reject any ambiguity."""
    e = (email or "").strip().lower()
    if not e:
        return []
    dirs = []
    for p in meta.get("profiles", []):
        if (p.get("user_name") or "").strip().lower() == e:
            dirs.append(str(p.get("profile_dir")))
    return dirs


# ── Controller ────────────────────────────────────────────────────────────────

class JarvisBrowserWindowController:
    """Creates + provenance-verifies a dedicated Jarvis-owned Chrome window per
    workflow using the trusted email→directory mapping and a unique launch
    token. Never adopts an arbitrary existing window.

    Injectable hooks (default to real implementations):
    - ``host_windows_fn``     : () -> list[{"hwnd","pid","title"}]
    - ``launch_new_window_fn``: (exe, profile_dir, marker_url) -> pid
    - ``exe_resolver_fn``     : () -> chrome.exe path
    - ``window_tab_urls_fn``  : (hwnd) -> list[{"runtime_id","tab_index","title","url"}]
    - ``navigate_fn``         : (hwnd, url) -> None   (Jarvis tab only)
    - ``metadata_fn``         : () -> Local State metadata
    - ``close_window_fn``     : (hwnd) -> bool        (only a Jarvis window)
    - ``token_fn``            : () -> str             (unique launch token)
    """

    def __init__(
        self,
        *,
        host_windows_fn: Optional[Callable] = None,
        launch_new_window_fn: Optional[Callable] = None,
        exe_resolver_fn: Optional[Callable] = None,
        window_tab_urls_fn: Optional[Callable] = None,
        navigate_fn: Optional[Callable] = None,
        metadata_fn: Optional[Callable] = None,
        close_window_fn: Optional[Callable] = None,
        token_fn: Optional[Callable] = None,
    ) -> None:
        self._host_windows_fn = host_windows_fn or self._default_host_windows
        self._launch_new_window_fn = launch_new_window_fn or self._default_launch_new_window
        self._exe_resolver_fn = exe_resolver_fn or self._default_exe
        self._window_tab_urls_fn = window_tab_urls_fn or self._default_window_tab_urls
        self._navigate_fn = navigate_fn or self._default_navigate
        self._metadata_fn = metadata_fn or self._default_metadata
        self._close_window_fn = close_window_fn or self._default_close_window
        self._token_fn = token_fn or (lambda: secrets.token_hex(16))  # 128-bit

    # ── default real implementations ─────────────────────────────────────────

    @staticmethod
    def _default_host_windows() -> list:
        from jarvis_core.uia_browser_manager import chrome_host_windows
        return chrome_host_windows()

    @staticmethod
    def _default_exe() -> str:
        from jarvis_core.chrome_profile_controller import chrome_profile_controller
        return chrome_profile_controller.resolve_chrome_executable()

    @staticmethod
    def _default_launch_new_window(exe: str, profile_dir: str, marker_url: str) -> int:
        import subprocess
        args = [exe, f'--profile-directory={profile_dir}', "--new-window", marker_url]
        proc = subprocess.Popen(args, cwd=os.path.dirname(exe) or None)
        return int(proc.pid)

    @staticmethod
    def _default_metadata() -> dict:
        from jarvis_core.chrome_profile_identity import chrome_profile_metadata
        return chrome_profile_metadata()

    @staticmethod
    def _default_close_window(hwnd: int) -> bool:
        try:
            from jarvis_core.windows_resource_manager import close_window
            return bool(close_window(int(hwnd)))
        except Exception:
            return False

    def _default_navigate(self, hwnd: int, url: str) -> None:
        from jarvis_core.chatgpt_browser_controller import ChatGPTBrowserController
        ChatGPTBrowserController()._navigate(int(hwnd), url)

    @staticmethod
    def _default_window_tab_urls(hwnd: int) -> list:
        """Enumerate the tabs of ONE Chrome window and read each tab's URL by its
        exact runtime_id (UIA). Returns a list of
        ``{runtime_id, tab_index, title, url}`` (url "" when unreadable)."""
        from jarvis_core import uia_browser_manager as u
        out = []
        enum = u.enumerate_tabs(host_hwnd=int(hwnd))
        for t in enum.get("tabs", []):
            rd = u.read_tab_url(int(hwnd), t.get("runtime_id"),
                                title=t.get("title", ""),
                                tab_index=t.get("tab_index", -1))
            out.append({"runtime_id": t.get("runtime_id"),
                        "tab_index": t.get("tab_index"),
                        "title": t.get("title"),
                        "url": (rd.get("url") or "") if rd.get("ok") else ""})
        return out

    # ── helpers ───────────────────────────────────────────────────────────────

    def _snapshot_hwnds(self) -> set:
        try:
            return {int(w["hwnd"]) for w in (self._host_windows_fn() or [])}
        except Exception:
            return set()

    def _resolve_directory(self, selected_email: str,
                           selected_profile_directory: Optional[str]) -> str:
        """Map selected email → exactly one trusted directory. If a directory
        was supplied it must still be the unique mapping for the email."""
        meta = self._metadata_fn() or {}
        dirs = directories_for_email(selected_email, meta)
        if len(dirs) == 0:
            raise ProfileMappingInvalidError(
                f"PROFILE_MAPPING_INVALID: email {selected_email!r} not found in "
                "trusted Local State")
        if len(set(dirs)) > 1:
            raise ProfileMappingInvalidError(
                f"PROFILE_MAPPING_INVALID: email {selected_email!r} maps to "
                f"multiple directories {sorted(set(dirs))}")
        mapped = dirs[0]
        if selected_profile_directory and selected_profile_directory != mapped:
            raise ProfileMappingInvalidError(
                f"PROFILE_MAPPING_INVALID: selected directory "
                f"{selected_profile_directory!r} != mapping {mapped!r} for "
                f"{selected_email!r}")
        return mapped

    @staticmethod
    def _url_has_token(url: str, token: str) -> bool:
        return bool(url) and bool(token) and (f"/browser-bind/{token}" in url
                                              or f"token={token}" in url)

    def open_verified_window(self, *, workflow_id: str, selected_profile_key: str,
                             selected_account_email: str,
                             selected_profile_directory: Optional[str] = None) -> dict:
        """Launch a dedicated Jarvis window for the trusted directory of the
        selected email, then prove ownership via the unique launch-token marker
        URL. Returns a workflow-scoped binding dict. Fails closed."""
        directory = self._resolve_directory(selected_account_email,
                                            selected_profile_directory)
        token = self._token_fn()
        marker_url = f"{MARKER_BASE}/{token}"
        exe = self._exe_resolver_fn()

        before = self._snapshot_hwnds()
        launch_pid = self._launch_new_window_fn(exe, directory, marker_url)

        deadline = time.monotonic() + NEW_WINDOW_WAIT_SECONDS
        chosen_hwnd = None
        chosen_tab = None
        while time.monotonic() < deadline:
            after = self._snapshot_hwnds()
            new_hwnds = sorted(after - before)
            if new_hwnds:
                # Among the NEW windows, find the one whose a tab carries our
                # exact token. (Never inspect pre-existing windows.)
                token_hits = []
                for h in new_hwnds:
                    for tab in (self._window_tab_urls_fn(h) or []):
                        if self._url_has_token(tab.get("url", ""), token):
                            token_hits.append((h, tab))
                if len(token_hits) == 1:
                    chosen_hwnd, chosen_tab = token_hits[0]
                    break
                if len(token_hits) > 1:
                    raise AmbiguousLaunchTokenError(
                        f"AMBIGUOUS_LAUNCH_TOKEN: {len(token_hits)} windows carry "
                        f"the launch token for {directory!r}")
                # New window(s) exist but token not visible yet → keep polling
                # (the marker page may still be loading).
            time.sleep(NEW_WINDOW_POLL_INTERVAL)

        if chosen_hwnd is None:
            # Distinguish "no new window at all" from "new window(s) but no
            # token" for an honest error.
            after = self._snapshot_hwnds()
            if not (after - before):
                raise NoNewWindowError(
                    f"NO_NEW_WINDOW: launch for {directory!r} produced no new "
                    f"Chrome window within {NEW_WINDOW_WAIT_SECONDS:.0f}s")
            raise LaunchTokenNotFoundError(
                f"LAUNCH_TOKEN_NOT_FOUND: no new window carried the launch token "
                f"for {directory!r}")

        mapped_email = self._email_for_directory(directory)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        binding = {
            "workflow_id": workflow_id,
            "owner": "jarvis",
            "selected_profile_key": selected_profile_key,
            "selected_profile_directory": directory,
            "selected_account_email": selected_account_email,
            "mapped_account_email": mapped_email,
            "launch_token": token,
            "launch_pid": launch_pid,
            "marker_url": marker_url,
            "hwnd": int(chosen_hwnd),
            "tab_identity": {
                "control_mode": "uia",
                "host_hwnd": int(chosen_hwnd),
                "runtime_id": ",".join(str(x) for x in (chosen_tab.get("runtime_id") or [])),
                "tab_index": int(chosen_tab.get("tab_index", -1)),
                "marker_url": marker_url,
                "owner": "jarvis",
                "workflow_id": workflow_id,
            },
            "created_at": now,
            "verified_at": now,
            "verification_method": VERIFICATION_METHOD,
            "verified": True,
        }
        return binding

    def _email_for_directory(self, directory: str) -> Optional[str]:
        try:
            meta = self._metadata_fn() or {}
            for p in meta.get("profiles", []):
                if str(p.get("profile_dir")) == str(directory):
                    return p.get("user_name")
        except Exception:
            pass
        return None

    # ── recovery ─────────────────────────────────────────────────────────────

    def is_binding_live(self, binding: dict, *, workflow_id: str) -> bool:
        """True only when the bound HWND still exists AND belongs to THIS
        workflow's Jarvis-owned binding. A persisted HWND alone is never trusted
        for action; the caller recreates when ownership cannot be proven."""
        if not binding or binding.get("owner") != "jarvis":
            return False
        if str(binding.get("workflow_id")) != str(workflow_id):
            return False
        hwnd = binding.get("hwnd")
        if not hwnd:
            return False
        if int(hwnd) not in self._snapshot_hwnds():
            return False
        # The window must still contain THIS workflow's tab (by runtime_id) OR
        # the marker/conversation — otherwise it drifted and we recreate.
        ident = binding.get("tab_identity") or {}
        want_rid = str(ident.get("runtime_id") or "")
        if not want_rid:
            return False
        for tab in (self._window_tab_urls_fn(int(hwnd)) or []):
            rid = ",".join(str(x) for x in (tab.get("runtime_id") or []))
            if rid and rid == want_rid:
                return True
        return False

    def recreate_for_conversation(self, *, workflow_id: str,
                                  selected_profile_key: str,
                                  selected_account_email: str,
                                  conversation_url: str,
                                  selected_profile_directory: Optional[str] = None) -> dict:
        """Recovery: create a NEW dedicated Jarvis window (new token), then
        navigate its marker tab directly to the persisted conversation URL and
        rebind. Never adopts an arbitrary existing window."""
        binding = self.open_verified_window(
            workflow_id=workflow_id, selected_profile_key=selected_profile_key,
            selected_account_email=selected_account_email,
            selected_profile_directory=selected_profile_directory)
        hwnd = int(binding["hwnd"])
        # Navigate the SAME (marker) tab to the persisted conversation.
        try:
            self._navigate_fn(hwnd, conversation_url)
        except Exception:
            logger.debug("recovery navigate to conversation failed", exc_info=True)
        binding["conversation_url"] = conversation_url
        return binding


# Module-level singleton (real infrastructure), project convention.
jarvis_browser_window_controller = JarvisBrowserWindowController()
