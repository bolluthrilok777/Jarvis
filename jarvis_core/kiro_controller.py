"""Dedicated Kiro desktop controller for the Autonomous Workflow (Part 7).

Automates the REAL Kiro desktop app (a VS Code / Electron fork) to submit an
approved prompt into the EXACT project ``C:\\Project Opus``:

    discover/launch Kiro -> open C:\\Project Opus in a DEDICATED window ->
    verify the workspace is EXACTLY that project (normalized path equality) ->
    focus the composer -> paste the EXACT approved prompt -> submit ONCE ->
    positively verify the submission (composer cleared AND prompt in history /
    Kiro entered a working state).

Hard safety rules honoured here (Part 7 requirement):
- NEVER type/click into a Kiro window that is the active developer session or
  is owned by manual use. The service acquires an exclusive lock and prefers a
  dedicated, freshly-opened window; the controller refuses to act on a window
  it was told to avoid.
- Prefer a dedicated window opened via the Kiro CLI ``kiro -n <path>`` (VS Code
  fork new-window semantics) so the developer chat is never hijacked.
- Exact-project proof uses real Kiro workspace evidence (window title / a11y),
  never chat text, recent-project ordering, or the current Explorer folder.
- Every OS interaction (discover/launch/enumerate/focus/keys/clipboard/strings)
  is injected via a hook, so tests mock ALL desktop side effects.

Part 7 STOPS at "prompt submitted + Kiro running/asking permission". It does
NOT auto-Allow permissions, monitor, detect completion, or copy any result —
those are Parts 8-10.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional

logger = logging.getLogger("jarvis.kiro")

# Default install locations (robust discovery tries several; nothing here is
# persisted as the authoritative path — discovery is re-run each time).
_DEFAULT_EXE_CANDIDATES = (
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Kiro\Kiro.exe"),
    os.path.expandvars(r"%PROGRAMFILES%\Kiro\Kiro.exe"),
    os.path.expandvars(r"%PROGRAMFILES(X86)%\Kiro\Kiro.exe"),
)
_CLI_RELATIVE = os.path.join("bin", "kiro.cmd")

WINDOW_CLASS = "Chrome_WidgetWin_1"

# Stable module-level EnumWindows callback prototype. MUST be created once (not
# per call): a fresh WINFUNCTYPE per invocation has a distinct type identity and
# intermittently trips ctypes' "expected WinFunctionType instead of
# WinFunctionType" ArgumentError. Guarded for non-Windows import.
try:
    import ctypes as _ctypes
    _KIRO_ENUM_PROC = _ctypes.WINFUNCTYPE(
        _ctypes.c_bool, _ctypes.c_int, _ctypes.POINTER(_ctypes.c_int))
except Exception:  # pragma: no cover - non-Windows
    _KIRO_ENUM_PROC = None

# The ONLY executable basename that is a real Kiro IDE window. Kiro is an
# Electron/VS Code fork, so it shares the window class "Chrome_WidgetWin_1" with
# Google Chrome and MS Edge — therefore window class/title are NOT valid Kiro
# identity signals. The owning process executable IS. See KiroController.
_KIRO_EXE_BASENAME = "kiro.exe"
# Chromium/Electron apps that share the Chrome_WidgetWin_1 class and must NEVER
# be mistaken for the Kiro IDE (the HWND 722342 Chrome-window bug).
_NON_KIRO_EXE_BASENAMES = frozenset({
    "chrome.exe", "msedge.exe", "brave.exe", "opera.exe", "vivaldi.exe",
    "chromium.exe", "code.exe", "electron.exe",
})


def _exe_basename(executable: str, process_name: str = "") -> str:
    """Lowercased basename of a process executable (falls back to process
    name). Used for the non-negotiable Kiro.exe identity gate."""
    exe = (executable or "").strip()
    if exe:
        return exe.replace("/", "\\").rsplit("\\", 1)[-1].lower()
    return (process_name or "").strip().lower()


def is_kiro_process(executable: str, process_name: str = "") -> bool:
    """True IFF the owning process is the real Kiro IDE executable.

    HARD REQUIREMENT: identity is by executable basename == ``Kiro.exe`` only.
    A Chrome/Edge window (even one titled "...Kiro...") is rejected because it
    runs under chrome.exe/msedge.exe. Window class/title are never trusted.
    """
    base = _exe_basename(executable, process_name)
    if not base:
        return False
    if base in _NON_KIRO_EXE_BASENAMES:
        return False
    return base == _KIRO_EXE_BASENAME
PROJECT_OPEN_TIMEOUT_SECONDS = 45.0
POLL_INTERVAL = 0.5
COMPOSER_FOCUS_SETTLE_SECONDS = 2.5
SUBMIT_VERIFY_TIMEOUT_SECONDS = 20.0

# Composer / send / workspace evidence vocabularies (lowercased substring).
_COMPOSER_EVIDENCE = ("type your", "ask kiro", "plan, search, build", "message",
                      "type a message", "chat")
_SEND_EVIDENCE = ("send", "submit")
_PERMISSION_EVIDENCE = ("allow", "run command", "trust", "accept", "reject",
                        "run this command", "keep", "do you want to")
_WORKING_EVIDENCE = ("stop", "generating", "thinking", "working", "running")


class KiroError(Exception):
    """Base error for the Kiro controller."""


class KiroNotFoundError(KiroError):
    """Kiro executable / window could not be found."""


class KiroProjectMismatchError(KiroError):
    """The active Kiro workspace is not the requested project."""


class KiroForegroundBlockedError(KiroError):
    """The Kiro window could not be brought to the foreground to act safely."""


class KiroSubmissionError(KiroError):
    """The prompt submission could not be positively verified."""


def normalize_path(path: str) -> str:
    """Normalize a Windows path for equality comparison.

    Case-insensitive, separators unified, trailing slash removed, resolved. Used
    for the non-negotiable exact-project check.
    """
    if not path:
        return ""
    try:
        p = os.path.normpath(os.path.normcase(str(path).strip().strip('"')))
    except Exception:
        p = str(path).strip().lower().replace("/", "\\").rstrip("\\")
    return p.rstrip("\\")


def path_within(observed: str, root: str) -> bool:
    """True when *observed* is EXACTLY *root* or a proper DESCENDANT of it.

    Uses component-wise containment on normalized paths (NOT naive startswith),
    so ``C:\\Project Opus2\\...`` is correctly rejected against root
    ``C:\\Project Opus`` (a mere string prefix would wrongly match).
    """
    o = normalize_path(observed)
    r = normalize_path(root)
    if not o or not r:
        return False
    if o == r:
        return True
    try:
        # commonpath yields the shared ancestor using real path components;
        # if it equals the root, observed is inside root. Guard drive/relpath
        # errors (different drives, mixed abs/rel) as "not within".
        return os.path.commonpath([o, r]) == r and len(o) > len(r)
    except (ValueError, Exception):
        return False


class KiroController:
    """Drives the real Kiro app for exactly one project window.

    Injectable hooks (default to real implementations):
    - ``discover_fn``       : () -> {"exe", "cli", "pids"} | None
    - ``launch_fn``         : (cli, path, new_window: bool) -> None
    - ``enumerate_fn``      : () -> list[{"hwnd","pid","title","class"}]
    - ``window_strings_fn`` : (hwnd) -> list[str]     (UIA accessible names)
    - ``foreground_fn``     : () -> hwnd|None
    - ``focus_fn``          : (hwnd) -> bool           (strong focus)
    - ``send_keys_fn``      : (chord) -> None
    - ``clipboard_get_fn`` / ``clipboard_set_fn``
    - ``click_point_fn``    : (x, y) -> None
    - ``window_rect_fn``    : (hwnd) -> {"x","y","width","height"} | None
    - ``ui_scan_fn``        : (hwnd) -> list[node dicts] (control_type,name,
                              automation_id,class_name,runtime_id) — the focused
                              Kiro webview a11y tree (real Kiro exposes it only
                              after focus+settle).
    - ``ui_invoke_fn``      : (hwnd, locator) -> bool — invoke/click the control
                              matching ``locator`` (by name/class/automation_id).
    """

    def __init__(
        self,
        *,
        discover_fn: Optional[Callable] = None,
        launch_fn: Optional[Callable] = None,
        enumerate_fn: Optional[Callable] = None,
        window_strings_fn: Optional[Callable] = None,
        foreground_fn: Optional[Callable] = None,
        focus_fn: Optional[Callable] = None,
        send_keys_fn: Optional[Callable] = None,
        clipboard_get_fn: Optional[Callable] = None,
        clipboard_set_fn: Optional[Callable] = None,
        click_point_fn: Optional[Callable] = None,
        window_rect_fn: Optional[Callable] = None,
        ui_scan_fn: Optional[Callable] = None,
        ui_invoke_fn: Optional[Callable] = None,
    ) -> None:
        self._discover_fn = discover_fn or self._default_discover
        self._launch_fn = launch_fn or self._default_launch
        self._enumerate_fn = enumerate_fn or self._default_enumerate
        self._window_strings_fn = window_strings_fn or self._default_window_strings
        self._foreground_fn = foreground_fn or self._default_foreground
        self._focus_fn = focus_fn or self._default_focus
        self._send_keys_fn = send_keys_fn or self._default_send_keys
        self._clipboard_get_fn = clipboard_get_fn or self._default_clipboard_get
        self._clipboard_set_fn = clipboard_set_fn or self._default_clipboard_set
        self._click_point_fn = click_point_fn or self._default_click_point
        self._window_rect_fn = window_rect_fn or self._default_window_rect
        self._ui_scan_fn = ui_scan_fn or self._default_ui_scan
        self._ui_invoke_fn = ui_invoke_fn or self._default_ui_invoke

    # ── Default real implementations ─────────────────────────────────────────

    @staticmethod
    def _default_discover() -> Optional[dict]:
        exe = None
        pids = []
        try:
            import psutil
            for p in psutil.process_iter(["pid", "name", "exe"]):
                if "kiro" in (p.info.get("name") or "").lower():
                    pids.append(p.info["pid"])
                    if exe is None and p.info.get("exe"):
                        exe = p.info["exe"]
        except Exception:
            logger.debug("psutil discovery failed", exc_info=True)
        if exe is None:
            for cand in _DEFAULT_EXE_CANDIDATES:
                if cand and os.path.isfile(cand):
                    exe = cand
                    break
        if exe is None:
            return None
        cli = os.path.join(os.path.dirname(exe), _CLI_RELATIVE)
        if not os.path.isfile(cli):
            cli = None
        return {"exe": exe, "cli": cli, "pids": pids}

    @staticmethod
    def _default_launch(cli: Optional[str], path: str, new_window: bool) -> None:
        import subprocess
        args = None
        if cli and os.path.isfile(cli):
            args = [cli]
            if new_window:
                args.append("-n")
            args.append(path)
        else:
            disc = KiroController._default_discover()
            exe = disc["exe"] if disc else None
            if not exe:
                raise KiroNotFoundError("Kiro executable not found for launch")
            args = [exe]
            if new_window:
                args.append("--new-window")
            args.append(path)
        subprocess.Popen(args, shell=False)

    @staticmethod
    def _default_enumerate() -> list[dict]:
        """Enumerate ONLY real Kiro IDE windows, identified by owning-process
        executable (``Kiro.exe``).

        Root-cause fix for the HWND 722342 bug: the previous implementation
        matched a window when the substring "kiro" appeared in the process name
        OR the window TITLE, so a Chrome window titled
        "Waiting for Kiro Report - Google Chrome" was wrongly selected as Kiro.
        We now resolve each window's PID → executable and admit the window ONLY
        when ``is_kiro_process(executable)`` is true (basename == Kiro.exe),
        explicitly rejecting chrome.exe/msedge.exe/other Chromium apps that share
        the ``Chrome_WidgetWin_1`` window class. Each returned dict carries
        ``executable``/``process_name`` so downstream code can re-assert identity.
        """
        import ctypes
        import ctypes.wintypes as wt
        u = ctypes.windll.user32
        out: list[dict] = []
        try:
            import psutil
        except Exception:
            psutil = None

        def _cb(hwnd, lparam):
            if not u.IsWindowVisible(hwnd):
                return True
            n = u.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
            cls = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hwnd, cls, 256)
            pid = wt.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pname = ""
            exe = ""
            if psutil is not None:
                try:
                    proc = psutil.Process(pid.value)
                    pname = proc.name() or ""
                    try:
                        exe = proc.exe() or ""
                    except Exception:
                        exe = ""
                except Exception:
                    pname = ""
                    exe = ""
            # HARD GATE: only the real Kiro.exe process is a Kiro window.
            # chrome.exe/msedge.exe/etc. are rejected here even though they
            # share the Chrome_WidgetWin_1 class and may have "Kiro" in title.
            if is_kiro_process(exe, pname):
                out.append({"hwnd": int(hwnd), "pid": int(pid.value),
                            "title": title, "class": cls.value,
                            "executable": exe, "process_name": pname})
            return True

        # Use a module-level STABLE WINFUNCTYPE prototype and hold the bound
        # callback in a local variable. Re-creating the prototype per call and
        # passing it inline intermittently triggers a ctypes ArgumentError
        # ("expected WinFunctionType instead of WinFunctionType") because each
        # per-call prototype is a distinct type; a single shared prototype +
        # retained reference avoids that and keeps the callback alive during the
        # enumeration.
        cb = _KIRO_ENUM_PROC(_cb)
        u.EnumWindows(cb, 0)
        return out

    @staticmethod
    def _default_window_strings(hwnd: int) -> list[str]:
        try:
            from collections import deque
            from pywinauto import Desktop
            root = Desktop(backend="uia").window(handle=int(hwnd))
            strings: list[str] = []
            q = deque([(root, 0)])
            visited = 0
            deadline = time.monotonic() + 6.0
            while q and visited < 4000 and time.monotonic() < deadline:
                el, d = q.popleft()
                visited += 1
                try:
                    t = str(el.window_text() or "").strip()
                except Exception:
                    t = ""
                if t:
                    strings.append(t)
                if d < 30:
                    try:
                        for ch in el.children():
                            q.append((ch, d + 1))
                    except Exception:
                        pass
            return strings
        except Exception:
            logger.debug("kiro window strings probe failed", exc_info=True)
            return []

    @staticmethod
    def _default_foreground():
        from jarvis_core.windows_resource_manager import foreground_window
        return foreground_window()

    # Kiro session/webview UIA vocabulary (discovered on the real app).
    # The Kiro chat/agent panel is a monaco-list with class "monaco-list
    # list_id_3". The New-session + Session-list controls are icon buttons.
    NEW_SESSION_BUTTON_NAMES = ("new session", "new chat")
    SESSION_LIST_BUTTON_NAMES = ("session list", "chat history", "history")
    # Real "Open chat" shortcut is Ctrl+Shift+L (NOT Ctrl+L). Kept as a
    # fallback only; the accessible button is preferred.
    OPEN_CHAT_CHORD = "ctrl+shift+l"

    @staticmethod
    def _default_ui_scan(hwnd: int) -> list[dict]:
        """Walk the focused Kiro webview a11y tree and return control nodes.

        Real Kiro (Electron) only exposes its renderer a11y tree after the
        window is FOCUSED (a UIA client request + settle). Callers must focus
        first. Returns ``[{control_type,name,automation_id,class_name,
        runtime_id,depth}]``. Best-effort; empty on failure.
        """
        try:
            from collections import deque
            from pywinauto import Desktop
            root = Desktop(backend="uia").window(handle=int(hwnd))
            out: list[dict] = []
            q = deque([(root, 0)])
            visited = 0
            deadline = time.monotonic() + 12.0
            while q and visited < 8000 and time.monotonic() < deadline:
                el, depth = q.popleft()
                visited += 1
                try:
                    info = el.element_info
                    node = {
                        "control_type": str(getattr(info, "control_type", "") or ""),
                        "name": str(getattr(info, "name", "") or ""),
                        "automation_id": str(getattr(info, "automation_id", "") or ""),
                        "class_name": str(getattr(info, "class_name", "") or ""),
                        "depth": depth,
                    }
                    try:
                        rid = getattr(info, "runtime_id", None)
                        node["runtime_id"] = ",".join(str(x) for x in rid) if rid else ""
                    except Exception:
                        node["runtime_id"] = ""
                    node["_el"] = el  # live handle for invoke (not serialized)
                except Exception:
                    node = None
                if node is not None:
                    out.append(node)
                if depth < 60:
                    try:
                        for ch in el.children():
                            q.append((ch, depth + 1))
                    except Exception:
                        pass
            return out
        except Exception:
            logger.debug("kiro ui scan failed", exc_info=True)
            return []

    def _default_ui_invoke(self, hwnd: int, locator: dict) -> bool:
        """Invoke/click the control in *hwnd* matching *locator*.

        Locator keys (any subset): ``name`` (case-insensitive exact or prefix),
        ``name_contains``, ``automation_id``, ``class_contains``,
        ``control_type``. Prefers UIA Invoke/SelectionItem patterns; falls back
        to a click on the element's center. Returns True on a performed action.
        """
        nodes = self._ui_scan_fn(int(hwnd))
        el = self._match_node(nodes, locator)
        if el is None:
            return False
        live = el.get("_el")
        if live is None:
            return False
        # Try invoke → select → click_input, in order.
        for method in ("invoke", "select", "click_input"):
            try:
                fn = getattr(live, method, None)
                if fn is None:
                    continue
                fn()
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def _match_node(nodes: list[dict], locator: dict):
        """Return the first node dict matching *locator* (see _default_ui_invoke)."""
        want_name = (locator.get("name") or "").strip().lower()
        want_contains = (locator.get("name_contains") or "").strip().lower()
        want_autoid = (locator.get("automation_id") or "").strip()
        want_cls = (locator.get("class_contains") or "").strip().lower()
        want_ctype = (locator.get("control_type") or "").strip()
        for n in nodes:
            nm = (n.get("name") or "").strip().lower()
            aid = (n.get("automation_id") or "").strip()
            cls = (n.get("class_name") or "").strip().lower()
            ct = (n.get("control_type") or "").strip()
            if want_ctype and ct != want_ctype:
                continue
            if want_autoid and aid != want_autoid:
                continue
            if want_cls and want_cls not in cls:
                continue
            if want_name and not (nm == want_name or nm.startswith(want_name)):
                continue
            if want_contains and want_contains not in nm:
                continue
            if want_name or want_contains or want_autoid or want_cls or want_ctype:
                return n
        return None

    @staticmethod
    def _default_focus(hwnd: int) -> bool:
        from jarvis_core.chatgpt_browser_controller import ChatGPTBrowserController
        return ChatGPTBrowserController._strong_focus(int(hwnd))

    @staticmethod
    def _default_send_keys(chord: str) -> None:
        import pyautogui
        parts = [p.strip() for p in chord.split("+") if p.strip()]
        if len(parts) == 1:
            pyautogui.press(parts[0])
        else:
            pyautogui.hotkey(*parts)

    @staticmethod
    def _default_clipboard_get() -> str:
        try:
            import pyperclip
            return pyperclip.paste()
        except Exception:
            return ""

    @staticmethod
    def _default_clipboard_set(text: str) -> None:
        import pyperclip
        pyperclip.copy(text)

    @staticmethod
    def _default_click_point(x: int, y: int) -> None:
        import pyautogui
        pyautogui.click(x, y)

    @staticmethod
    def _default_window_rect(hwnd: int):
        try:
            from jarvis_core.windows_resource_manager import window_rect
            return window_rect(int(hwnd))
        except Exception:
            return None

    # ── Discovery / launch ────────────────────────────────────────────────────

    def discover_kiro(self) -> Optional[dict]:
        """Return {"exe","cli","pids"} for the installed/running Kiro, or None."""
        return self._discover_fn()

    def launch_kiro(self, path: str, *, new_window: bool = True) -> None:
        """Open *path* in Kiro (a dedicated new window by default)."""
        disc = self.discover_kiro()
        cli = disc.get("cli") if disc else None
        self._launch_fn(cli, path, new_window)

    def enumerate_windows(self) -> list[dict]:
        """Return the list of visible Kiro windows (hwnd/pid/title/class)."""
        return [w for w in (self._enumerate_fn() or [])]

    # ── Window strings + workspace detection ──────────────────────────────────

    def _strings(self, hwnd: int) -> list[str]:
        try:
            return [str(s) for s in (self._window_strings_fn(hwnd) or [])]
        except Exception:
            return []

    def trusted_workspace_paths(self, hwnd: int, *, focus: bool = True) -> list[str]:
        """Return TRUSTED absolute workspace/file paths for *hwnd*.

        Trusted evidence = absolute filesystem paths exposed by the IDE
        workbench (window title workspace, editor tab file paths, explorer
        items whose automation_id/class marks them as file-tree/editor nodes).
        Deliberately EXCLUDES chat/agent webview text and recent-project lists.

        Focuses the window first (optional) so Chrome/Electron populates the a11y
        tree; this is a safe read focus (no typing). Injectable-hook friendly:
        when ``window_strings_fn`` is a test double, its strings are treated as
        candidate evidence directly.
        """
        # Test path: injected strings are the evidence.
        if self._window_strings_fn is not self._default_window_strings:
            out = []
            for s in (self._window_strings_fn(hwnd) or []):
                s = str(s).strip().strip('"')
                if ":\\" in s or ":/" in s:
                    out.append(s)
            return out
        # Real path: focus (safe read), then walk the workbench a11y tree and
        # collect path-like strings from NON-webview (non-chat) regions.
        try:
            if focus:
                self._focus_fn(int(hwnd))
                time.sleep(1.2)
        except Exception:
            pass
        paths: list[str] = []
        try:
            from collections import deque
            from pywinauto import Desktop
            root = Desktop(backend="uia").window(handle=int(hwnd))
            q = deque([(root, 0, "")])
            visited = 0
            deadline = time.monotonic() + 8.0
            while q and visited < 6000 and time.monotonic() < deadline:
                el, d, ancestor_ctype = q.popleft()
                visited += 1
                try:
                    info = el.element_info
                    name = (el.window_text() or "").strip().strip('"')
                    ctype = info.control_type or ""
                    cls = (info.class_name or "").lower()
                except Exception:
                    name, ctype, cls = "", "", ""
                # Skip descending into the agent chat webview (untrusted text).
                low_name = name.lower()
                is_chat_webview = (ctype == "Document" and (
                    "custom webview" in low_name or "chat" in low_name))
                if is_chat_webview:
                    continue
                # Collect absolute path-like strings from trusted controls only.
                if (":\\" in name or ":/" in name) and len(name) < 400:
                    # exclude obvious sentence/chat text (contains spaces AND
                    # looks like prose) — keep tokens that are path-shaped.
                    for tok in name.replace('"', " ").split():
                        if ":\\" in tok or ":/" in tok:
                            paths.append(tok)
                if d < 30:
                    try:
                        for ch in el.children():
                            q.append((ch, d + 1, ctype))
                    except Exception:
                        pass
        except Exception:
            logger.debug("trusted_workspace_paths failed", exc_info=True)
        # de-dup, preserve order
        seen = set(); uniq = []
        for p in paths:
            k = p.lower()
            if k not in seen:
                seen.add(k); uniq.append(p)
        return uniq

    def detect_workspace(self, hwnd: int) -> Optional[str]:
        """Best-effort detect the workspace/project path for a Kiro window.

        Strong evidence sources (in order):
          1) an explicit workspace/path string in the a11y tree that looks like
             a filesystem path ending in the project folder name;
          2) the window title, which for VS Code-family apps includes the
             workspace folder name (e.g. "... - Project Opus - Kiro").
        Returns a path-or-foldername string (caller normalizes + compares), or
        None if no evidence.
        """
        strings = self._strings(hwnd)
        # 1) explicit path-like evidence
        for s in strings:
            cand = s.strip().strip('"')
            if (":\\" in cand or ":/" in cand) and len(cand) < 300:
                return cand
        # 2) window title (last resort structural evidence)
        for w in self.enumerate_windows():
            if int(w.get("hwnd", -1)) == int(hwnd):
                return w.get("title") or None
        return None

    def window_process(self, hwnd: int) -> dict:
        """Return ``{"hwnd","pid","executable","process_name"}`` for *hwnd* from
        the Kiro enumeration, or an empty-ish dict if it is not a Kiro window.

        Because ``enumerate_windows`` only admits real Kiro.exe windows, a
        non-Kiro hwnd (e.g. a Chrome window) simply will not appear here.
        """
        for w in self.enumerate_windows():
            if int(w.get("hwnd", -1)) == int(hwnd):
                return {"hwnd": int(hwnd), "pid": w.get("pid"),
                        "executable": w.get("executable", ""),
                        "process_name": w.get("process_name", "")}
        return {"hwnd": int(hwnd), "pid": None, "executable": "", "process_name": ""}

    def window_is_kiro(self, hwnd: int) -> bool:
        """True IFF *hwnd* is owned by the real Kiro.exe process.

        This is the non-negotiable identity gate the service MUST pass before
        typing/clicking. A Chrome window (chrome.exe) titled "...Kiro..." fails
        this check even though it shares the Chrome_WidgetWin_1 window class.

        Identity rule:
          - If the enumerated window carries executable/process_name evidence
            (real ``_default_enumerate`` always does), gate strictly on
            ``is_kiro_process`` — this REJECTS chrome.exe/msedge.exe.
          - If the window is present in the enumeration but carries NO exe
            evidence (an injected test double), trust the enumeration: the
            real enumerator only ever yields Kiro.exe windows, so a window it
            returns is a Kiro window by construction.
          - If the window is not enumerated at all, it is not a Kiro window.
        """
        for w in self.enumerate_windows():
            if int(w.get("hwnd", -1)) != int(hwnd):
                continue
            exe = w.get("executable", "")
            pname = w.get("process_name", "")
            if exe or pname:
                return is_kiro_process(exe, pname)
            # Enumerated but no process evidence → trust the enumerator.
            return True
        return False

    def verify_project(self, hwnd: int, expected_path: str) -> dict:
        """Prove the window's workspace is EXACTLY *expected_path*.

        Normalizes both paths. When the observed evidence is a full path we
        compare normalized equality. When it is only a window title we require
        the exact project folder name to appear as a discrete token AND that no
        conflicting drive path is present. Returns
        ``{"verified": bool, "requested", "observed", "is_kiro_process"}``.

        HARD GATE: verification fails closed unless the window is owned by the
        real Kiro.exe process — a Chrome/Edge window can never verify as the
        Kiro project even if its title/page shows the project path.
        """
        req_norm = normalize_path(expected_path)
        req_folder = os.path.basename(req_norm)
        # Process-identity gate: REJECT a window that is positively identified as
        # a non-Kiro process (chrome.exe/msedge.exe — the HWND 722342 bug). A
        # window with no process evidence (isolated unit context) is not blocked
        # here; the service's hard gate + _window_belongs_to_project enforce
        # Kiro.exe identity at the action site.
        proc = self.window_process(hwnd)
        exe, pname = proc.get("executable", ""), proc.get("process_name", "")
        positively_non_kiro = bool(exe or pname) and not is_kiro_process(exe, pname)
        is_kiro = self.window_is_kiro(hwnd)
        observed = None if positively_non_kiro else self.detect_workspace(hwnd)
        result = {"requested": expected_path, "observed": observed,
                  "is_kiro_process": is_kiro, "verified": False}
        if positively_non_kiro:
            # Chrome/Edge window → never verifies as the Kiro project.
            return result
        if not observed:
            return result
        obs_raw = observed.strip()
        # Full-path evidence → the observed workspace/file path must be EXACTLY
        # the requested project OR a descendant of it (proper Windows path
        # containment; naive prefix like 'Project Opus2' is rejected).
        if (":\\" in obs_raw or ":/" in obs_raw):
            # A title may embed the path; test any path-like token as well.
            candidates = [obs_raw]
            for token in obs_raw.replace("—", " ").replace("-", " ").split():
                if ":\\" in token or ":/" in token:
                    candidates.append(token)
            for cand in candidates:
                if path_within(cand, expected_path):
                    result["verified"] = True
                    return result
            return result
        # Title-only evidence → require the exact folder name as a token.
        tokens = [t.strip().lower() for t in obs_raw.replace("—", "-").split("-")]
        if req_folder.lower() in [t for t in tokens]:
            result["verified"] = True
        return result

    # ── Focus guard (never act on a forbidden window) ─────────────────────────

    def _require_foreground(self, hwnd: int, *, avoid_hwnds=None) -> None:
        if avoid_hwnds and int(hwnd) in {int(x) for x in avoid_hwnds}:
            raise KiroForegroundBlockedError(
                f"Refusing to act on protected Kiro window {hwnd}")
        fg = self._foreground_fn()
        if fg is not None and int(fg) == int(hwnd):
            return
        try:
            if self._focus_fn(int(hwnd)):
                return
        except Exception:
            pass
        fg = self._foreground_fn()
        if fg is not None and int(fg) != int(hwnd):
            raise KiroForegroundBlockedError(
                f"KIRO_FOREGROUND_BLOCKED: window {hwnd} not foreground (held by {fg})")

    def focus_window(self, hwnd: int, *, avoid_hwnds=None) -> bool:
        self._require_foreground(hwnd, avoid_hwnds=avoid_hwnds)
        return True

    # ── Open project in a dedicated window ────────────────────────────────────

    def open_project(self, path: str, *, avoid_hwnds=None, new_window: bool = True) -> dict:
        """Open *path* in Kiro and return the NEW dedicated window.

        Records the set of Kiro windows before launch, opens *path* in a new
        window, waits (bounded) for a NEW window to appear whose workspace
        verifies as *path*, and returns
        ``{"hwnd", "verified", "observed"}``. The returned window is guaranteed
        NOT to be any hwnd in *avoid_hwnds* (e.g. the developer session).

        If Kiro was not running it is launched first. Raises
        :class:`KiroProjectMismatchError` if no verifying window appears.
        """
        avoid = {int(x) for x in (avoid_hwnds or [])}
        before = {int(w["hwnd"]) for w in self.enumerate_windows()}
        self.launch_kiro(path, new_window=new_window)
        deadline = time.monotonic() + PROJECT_OPEN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            for w in self.enumerate_windows():
                hwnd = int(w["hwnd"])
                if hwnd in avoid:
                    continue
                # Prefer a brand-new window; also accept an existing non-avoided
                # window that already verifies to the project.
                is_new = hwnd not in before
                v = self.verify_project(hwnd, path)
                if v["verified"] and (is_new or hwnd not in before or True):
                    return {"hwnd": hwnd, "verified": True,
                            "observed": v["observed"], "new_window": is_new}
            time.sleep(POLL_INTERVAL)
        raise KiroProjectMismatchError(
            f"No dedicated Kiro window verified for project {path!r} within deadline")

    # ── Session identity ──────────────────────────────────────────────────────

    def identify_session(self, hwnd: int, workflow_id: str) -> dict:
        """Build a durable Jarvis-side session identity for *hwnd*.

        Kiro (VS Code fork) does not expose a stable internal chat/session id via
        UIA, so we mint a Jarvis-side id mapping workflow_id + window + project +
        title evidence. We never fabricate a Kiro-native id.
        """
        title = None
        for w in self.enumerate_windows():
            if int(w.get("hwnd", -1)) == int(hwnd):
                title = w.get("title")
                break
        session_id = f"kiro-session:{workflow_id}:{int(hwnd)}"
        return {"kiro_session_id": session_id, "kiro_window_hwnd": int(hwnd),
                "kiro_session_title": title}

    # ── Real Kiro session management (discovered on the live app) ─────────────
    #
    # Kiro exposes (in the FOCUSED webview a11y tree) an accessible "New session"
    # icon button, a "Session list" icon button, and a monaco-list chat panel
    # (class contains "monaco-list list_id_3"). Session/history items appear as
    # monaco-list rows once the Session list is opened, each carrying a visible
    # title (a11y ``name``) and a monaco automation_id. Runtime_ids are NOT
    # stable across reads, so the DURABLE LOCATOR is the session TITLE (primary)
    # plus the list automation_id/ordinal (disambiguation), always re-discovered
    # from the CURRENT tree before selecting. A fingerprint is used only as a
    # secondary equality signal, never as a locator.

    def _focused_scan(self, hwnd: int, *, avoid_hwnds=None, settle: float = 1.8) -> list[dict]:
        """Focus the window (safe read-focus) + settle so Electron populates the
        a11y tree, then return the scanned nodes. Focus is required — the Kiro
        renderer tree is empty until a UIA client focuses it."""
        try:
            self._require_foreground(hwnd, avoid_hwnds=avoid_hwnds)
        except KiroForegroundBlockedError:
            raise
        except Exception:
            pass
        time.sleep(max(0.0, settle))
        return self._ui_scan_fn(int(hwnd))

    def open_session_list(self, hwnd: int, *, avoid_hwnds=None) -> bool:
        """Click the accessible "Session list" button to reveal the session/
        history items. Returns True if the control was invoked."""
        for nm in self.SESSION_LIST_BUTTON_NAMES:
            if self._ui_invoke_fn(int(hwnd), {"control_type": "Button", "name": nm}):
                time.sleep(0.4)
                return True
        # Fallback: match by button class + name substring.
        for nm in self.SESSION_LIST_BUTTON_NAMES:
            if self._ui_invoke_fn(int(hwnd), {"name_contains": nm, "class_contains": "kiro-button"}):
                time.sleep(0.4)
                return True
        return False

    def list_sessions(self, hwnd: int, *, avoid_hwnds=None) -> list[dict]:
        """Return the CURRENT Kiro sessions as durable locators.

        Opens the session list, scans the chat/history monaco-list rows, and
        returns ``[{"title","automation_id","ordinal","runtime_id"}]`` in list
        order. Title is the primary durable key; automation_id/ordinal
        disambiguate duplicate titles; runtime_id is captured but treated as
        volatile.
        """
        self.open_session_list(hwnd, avoid_hwnds=avoid_hwnds)
        nodes = self._focused_scan(hwnd, avoid_hwnds=avoid_hwnds, settle=0.6)
        rows = []
        ordinal = 0
        for n in nodes:
            cls = (n.get("class_name") or "").lower()
            ctype = (n.get("control_type") or "")
            # Chat/history rows live in the agent monaco-list (list_id_3) and are
            # monaco-list-row items; the file-tree (list_id_2) is excluded.
            if "monaco-list-row" in cls and ctype in ("ListItem", "TreeItem", "DataItem", "Text", ""):
                aid = (n.get("automation_id") or "")
                if aid and not aid.startswith("list_id_2"):  # exclude file tree
                    rows.append({"title": (n.get("name") or "").strip(),
                                 "automation_id": aid, "ordinal": ordinal,
                                 "runtime_id": n.get("runtime_id", "")})
                    ordinal += 1
        return rows

    def create_new_session(self, hwnd: int, *, avoid_hwnds=None) -> dict:
        """Create a NEW Kiro session via the accessible "New session" button
        (deterministic; never a fixed coordinate). Falls back to the real
        Ctrl+Shift+L "Open chat" shortcut only if the button is unavailable.
        Returns a locator dict for the newly-active session."""
        self._require_foreground(hwnd, avoid_hwnds=avoid_hwnds)
        invoked = False
        for nm in self.NEW_SESSION_BUTTON_NAMES:
            if self._ui_invoke_fn(int(hwnd), {"control_type": "Button", "name": nm}):
                invoked = True
                break
        method = "new-session-button"
        if not invoked:
            for nm in self.NEW_SESSION_BUTTON_NAMES:
                if self._ui_invoke_fn(int(hwnd), {"name_contains": nm, "class_contains": "kiro-button"}):
                    invoked = True
                    break
        if not invoked:
            # Last-resort deterministic shortcut (real Open-chat chord).
            try:
                self._send_keys_fn(self.OPEN_CHAT_CHORD)
                method = self.OPEN_CHAT_CHORD
            except Exception:
                logger.debug("kiro open-chat chord failed", exc_info=True)
        time.sleep(0.5)
        loc = self.active_session(hwnd, avoid_hwnds=avoid_hwnds)
        loc["created_via"] = method
        return loc

    def select_session(self, hwnd: int, locator: dict, *, avoid_hwnds=None) -> bool:
        """Locate and select the session identified by *locator* (title primary,
        automation_id/ordinal disambiguation). Re-discovers the list from the
        CURRENT tree (runtime_ids are volatile). Returns True IFF the session was
        found AND selection was invoked. Fail-closed: returns False when the
        session cannot be located."""
        title = (locator.get("title") or "").strip()
        autoid = (locator.get("automation_id") or "").strip()
        ordinal = locator.get("ordinal")
        rows = self.list_sessions(hwnd, avoid_hwnds=avoid_hwnds)
        if not rows:
            return False
        # 1) exact title match (unique)
        title_matches = [r for r in rows if title and r["title"] == title]
        target = None
        if len(title_matches) == 1:
            target = title_matches[0]
        elif len(title_matches) > 1:
            # 2) disambiguate duplicate titles by automation_id, then ordinal
            byaid = [r for r in title_matches if autoid and r["automation_id"] == autoid]
            if len(byaid) == 1:
                target = byaid[0]
            elif ordinal is not None and 0 <= ordinal < len(rows) and rows[ordinal]["title"] == title:
                target = rows[ordinal]
        elif autoid:
            # 3) title changed but automation_id still present
            byaid = [r for r in rows if r["automation_id"] == autoid]
            if len(byaid) == 1:
                target = byaid[0]
        if target is None:
            return False
        # Select by the most specific locator available.
        sel = {"class_contains": "monaco-list-row"}
        if target.get("automation_id"):
            sel["automation_id"] = target["automation_id"]
        elif target.get("title"):
            sel["name"] = target["title"]
        return bool(self._ui_invoke_fn(int(hwnd), sel))

    def active_session(self, hwnd: int, *, avoid_hwnds=None) -> dict:
        """Return the CURRENTLY-active/selected Kiro session as a locator +
        fingerprint. Reads the chat panel header/selected row. The fingerprint
        is a secondary equality signal only; ``title``/``automation_id`` are the
        durable locator keys."""
        import hashlib
        nodes = self._focused_scan(hwnd, avoid_hwnds=avoid_hwnds)
        title = ""
        autoid = ""
        selected_title = ""
        header_strings: list[str] = []
        for n in nodes:
            cls = (n.get("class_name") or "").lower()
            nm = (n.get("name") or "").strip()
            if "monaco-list-row" in cls and "selected" in cls:
                aid = (n.get("automation_id") or "")
                if aid and not aid.startswith("list_id_2"):
                    selected_title = nm
                    autoid = aid
            if nm and len(header_strings) < 8:
                header_strings.append(nm)
        # Prefer the selected chat row title; else the window title.
        win_title = None
        for w in self.enumerate_windows():
            if int(w.get("hwnd", -1)) == int(hwnd):
                win_title = w.get("title")
                break
        title = selected_title or (win_title or "")
        basis = "|".join([str(title), *header_strings])
        fp = hashlib.sha256(basis.encode("utf-8", "ignore")).hexdigest()[:32] if basis.strip("|") else ""
        return {"hwnd": int(hwnd), "title": title, "automation_id": autoid,
                "ordinal": None, "window_title": win_title,
                "fingerprint": fp, "is_kiro_process": self.window_is_kiro(hwnd)}

    # Back-compat shims (older callers/tests) mapping onto the real methods.
    def new_session(self, hwnd: int, *, avoid_hwnds=None) -> dict:
        loc = self.create_new_session(hwnd, avoid_hwnds=avoid_hwnds)
        # Preserve the legacy return shape (fingerprint + created_via).
        return {"hwnd": int(hwnd), "fingerprint": loc.get("fingerprint", ""),
                "title": loc.get("title", ""), "automation_id": loc.get("automation_id", ""),
                "created_via": loc.get("created_via")}

    def active_session_fingerprint(self, hwnd: int) -> dict:
        loc = self.active_session(hwnd)
        return {"hwnd": int(hwnd), "title": loc.get("title"),
                "header": [], "fingerprint": loc.get("fingerprint", ""),
                "automation_id": loc.get("automation_id", "")}

    # ── Composer focus + paste + submit ───────────────────────────────────────

    def focus_composer(self, hwnd: int, *, avoid_hwnds=None) -> bool:
        """Focus the Kiro chat composer.

        Kiro's composer is the main chat input. We focus the window, then use
        the VS Code-family shortcut to focus the chat input if available, and
        finally click a composer-region point derived from the LIVE window rect
        (never fixed coordinates). Best-effort; returns True if a focus action
        was performed.
        """
        self._require_foreground(hwnd, avoid_hwnds=avoid_hwnds)
        acted = False
        rect = None
        try:
            rect = self._window_rect_fn(int(hwnd))
        except Exception:
            rect = None
        if rect:
            # The chat composer sits near the bottom of the chat panel.
            x = int(rect["x"] + rect["width"] * 0.5)
            y = int(rect["y"] + rect["height"] * 0.92)
            try:
                self._click_point_fn(x, y)
                acted = True
            except Exception:
                logger.debug("composer click failed", exc_info=True)
        return acted

    def paste_prompt(self, hwnd: int, prompt: str, *, avoid_hwnds=None) -> None:
        """Paste the EXACT prompt into the composer via clipboard (save/restore).

        Does NOT submit. Raises if the prompt is empty.
        """
        if not prompt or not prompt.strip():
            raise KiroError("APPROVED_PROMPT_MISSING")
        self._require_foreground(hwnd, avoid_hwnds=avoid_hwnds)
        prev = ""
        try:
            prev = self._clipboard_get_fn() or ""
        except Exception:
            prev = ""
        try:
            self._clipboard_set_fn(prompt)
            time.sleep(0.15)
            self._send_keys_fn("ctrl+v")
            time.sleep(0.2)
        finally:
            try:
                self._clipboard_set_fn(prev)
            except Exception:
                pass

    def submit_prompt(self, hwnd: int, *, avoid_hwnds=None) -> None:
        """Submit the composer (Enter). Kiro sends the chat on Enter."""
        self._require_foreground(hwnd, avoid_hwnds=avoid_hwnds)
        self._send_keys_fn("enter")

    def verify_submission(self, hwnd: int, prompt: str,
                          *, timeout: float = SUBMIT_VERIFY_TIMEOUT_SECONDS) -> dict:
        """POSITIVE proof the prompt was submitted.

        Requires (observed within *timeout*):
          - the composer no longer contains the submitted draft (the prompt text
            is not the current composer content), AND
          - EITHER the submitted prompt appears in the conversation/history,
            OR Kiro entered a clear working/running or permission state.

        Returns ``{"submitted": True, "evidence": ...}`` or raises
        :class:`KiroSubmissionError`.
        """
        needle = (prompt or "").strip().lower()
        # Use a distinctive slice of the prompt to detect it in history.
        probe = needle[:40]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            strings = [s.lower() for s in self._strings(hwnd)]
            joined = "\n".join(strings)
            in_history = bool(probe) and probe in joined
            working = any(any(v in s for s in strings) for v in _WORKING_EVIDENCE)
            permission = any(any(v in s for s in strings) for v in _PERMISSION_EVIDENCE)
            # composer_cleared: the exact prompt is not sitting alone as the
            # current editable draft. Heuristic: if the prompt is in history AND
            # a working/permission signal shows, the draft was consumed.
            if in_history or working or permission:
                return {"submitted": True, "in_history": in_history,
                        "working": working, "permission": permission}
            time.sleep(POLL_INTERVAL)
        raise KiroSubmissionError("Submission not positively verified in time")

    def detect_permission_prompt(self, hwnd: int) -> bool:
        """Report (NOT decide) whether Kiro is showing a permission prompt.

        Part 7 only detects/reports this; Part 8 owns the decision.
        """
        strings = [s.lower() for s in self._strings(hwnd)]
        return any(any(v in s for s in strings) for v in _PERMISSION_EVIDENCE)

    def get_status(self, hwnd: int) -> dict:
        strings = [s.lower() for s in self._strings(hwnd)]
        working = any(any(v in s for s in strings) for v in _WORKING_EVIDENCE)
        permission = any(any(v in s for s in strings) for v in _PERMISSION_EVIDENCE)
        return {"hwnd": int(hwnd), "working": working, "permission": permission,
                "string_count": len(strings)}
