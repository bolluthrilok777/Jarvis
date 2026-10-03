"""Dedicated ChatGPT browser controller for the Autonomous Workflow (Part 4).

Automates a REAL Chrome window — the EXACT profile the user selected in Part 3 —
to bootstrap a persistent ChatGPT conversation:

    focus+verify selected profile → open chatgpt.com → detect login/ready →
    new chat → paste the Master Requirement (clipboard) → submit ONCE →
    positively detect generation start AND completion (UI evidence, never a
    fixed sleep) → capture the real conversation URL → (restore/verify).

Design rules honoured here:
- All Chrome/profile ownership goes through the Part 3 ``ChromeProfileController``
  (``open_or_focus`` / ``resolve_profile_window`` / ``verify_profile_open``).
  Every browser action first proves it is acting in the requested profile.
- Prefer accessibility (UIA) + deterministic keyboard shortcuts + clipboard.
  No fixed screen coordinates. No ``sleep(30)`` completion logic.
- Completion requires POSITIVE evidence: generation was observed to start, the
  "Stop"/streaming indicator disappeared, the latest assistant message exposes
  its completed controls (Copy), and the composer is ready — stable across a
  few observations.
- Every real OS interaction (focus, keys, clipboard, UIA walk) is injected via
  a hook, so automated tests mock all browser side effects.

Part 4 does NOT copy the assistant response (that is Part 6). It only confirms
the Master Requirement response completed and captures the conversation URL.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

logger = logging.getLogger("jarvis.chatgpt")

CHATGPT_URL = "https://chatgpt.com/"
CHATGPT_HOSTS = ("chatgpt.com", "chat.openai.com")

# Bounded waits (monotonic-deadline convention used across the project).
READY_TIMEOUT_SECONDS = 30.0
NAV_SETTLE_SECONDS = 1.0
POLL_INTERVAL = 0.5
# Time to let ChatGPT's SPA auto-focus its composer after the page is ready,
# before pasting the prompt (prevents pasting into the omnibox).
COMPOSER_FOCUS_SETTLE_SECONDS = 3.5
GENERATION_START_TIMEOUT_SECONDS = 25.0
GENERATION_COMPLETE_TIMEOUT_SECONDS = 240.0
# Completion must be observed stably across this many consecutive polls.
COMPLETION_STABLE_OBSERVATIONS = 3

# Accessibility-string evidence vocabularies (lowercased substring match).
_LOGIN_EVIDENCE = (
    "log in", "sign up", "welcome back", "create account", "continue with google",
)
_READY_EVIDENCE = (
    "message chatgpt", "send a message", "ask anything", "how can i help",
    "chatgpt", "new chat",
)
_GENERATING_EVIDENCE = ("stop generating", "stop streaming", "stop")
_COMPLETED_CONTROL_EVIDENCE = ("copy", "read aloud", "regenerate", "good response")

# ── Part 5: real control names calibrated LIVE against ChatGPT ──────────────
# Captured from the exact Part-4 conversation window (pywinauto UIA). See the
# calibration record for the full IDLE→GENERATING→COMPLETED evidence. These are
# matched as lowercased substrings against Button/Group accessible names.
#   Composer   : Edit  automation_id='prompt-textarea'  class='ProseMirror'
#   Send       : Button name 'Send prompt'   (present only with a draft)
#   Stop       : Button name 'Stop answering' (the definitive streaming signal)
#   Copy       : Button name 'Copy response'
#   Resp. bar  : Group  name 'Response actions'
COMPOSER_AUTOMATION_ID = "prompt-textarea"
_STOP_NAMES = ("stop answering", "stop generating", "stop streaming", "stop")
_SEND_NAMES = ("send prompt", "send message", "send")
_COPY_NAMES = ("copy response", "copy")
_RESPONSE_ACTIONS_GROUP = "response actions"
_COMPOSER_NAMES = ("ask anything", "message chatgpt", "send a message")


class ChatGPTError(Exception):
    """Base error for the ChatGPT browser controller."""


class LoginRequiredError(ChatGPTError):
    """ChatGPT shows a login/signup page in the selected profile."""


class ChatGPTNotReadyError(ChatGPTError):
    """ChatGPT UI did not become ready within the deadline."""


class GenerationTimeoutError(ChatGPTError):
    """ChatGPT did not finish generating within the deadline."""


class ConversationUrlError(ChatGPTError):
    """The captured URL is not a valid ChatGPT conversation URL."""


class ProfileOwnershipError(ChatGPTError):
    """The acting window is not the requested Chrome profile."""


class TabIdentityUnavailableError(ChatGPTError):
    """No REAL, controllable tab identity could be obtained for the workflow's
    ChatGPT tab (no CDP ``target_id`` and no UIA ``host_hwnd`` + ``runtime_id``).

    The workflow FAILS CLOSED here — Jarvis never downgrades to window-level
    ownership (active tab / HWND+URL / Ctrl+L on whatever is focused), because
    that could overwrite one of the user's other tabs (Gmail, personal ChatGPT,
    the Jarvis frontend). The saved conversation URL is a recovery anchor, never
    ownership proof.
    """


class ForegroundBlockedError(ChatGPTError):
    """The selected Chrome window cannot be brought to the foreground.

    Typically caused by another process (e.g. a remote-desktop client) holding
    the input foreground so Windows denies ``SetForegroundWindow``. Reported
    honestly rather than typing into the wrong window.
    """


class ChatGPTBrowserController:
    """Drives ChatGPT in an exact Chrome profile window.

    Injectable hooks (default to real implementations):
    - ``profile_controller``  : Part 3 controller (focus/verify/window).
    - ``focus_verify_fn``     : (profile_key) -> {"hwnd", "verified", ...}
    - ``window_strings_fn``   : (hwnd) -> list[str]   (UIA accessible names)
    - ``send_keys_fn``        : (chord) -> None        (deterministic hotkey)
    - ``type_text_fn``        : (text) -> None
    - ``clipboard_get_fn`` / ``clipboard_set_fn``
    - ``foreground_fn``       : () -> hwnd|None
    """

    def __init__(
        self,
        *,
        profile_controller=None,
        focus_verify_fn: Optional[Callable] = None,
        window_strings_fn: Optional[Callable] = None,
        send_keys_fn: Optional[Callable] = None,
        type_text_fn: Optional[Callable] = None,
        clipboard_get_fn: Optional[Callable] = None,
        clipboard_set_fn: Optional[Callable] = None,
        foreground_fn: Optional[Callable] = None,
        click_point_fn: Optional[Callable] = None,
        copy_click_fn: Optional[Callable] = None,
        discover_tabs_fn: Optional[Callable] = None,
        focus_tab_cdp_fn: Optional[Callable] = None,
        close_tab_cdp_fn: Optional[Callable] = None,
        focus_tab_uia_fn: Optional[Callable] = None,
        close_tab_uia_fn: Optional[Callable] = None,
        create_tab_cdp_fn: Optional[Callable] = None,
        create_tab_uia_fn: Optional[Callable] = None,
        read_tab_url_uia_fn: Optional[Callable] = None,
        jarvis_window_controller=None,
    ) -> None:
        self._profile_controller = profile_controller
        self._focus_verify_fn = focus_verify_fn or self._default_focus_verify
        self._window_strings_fn = window_strings_fn or self._default_window_strings
        self._send_keys_fn = send_keys_fn or self._default_send_keys
        self._type_text_fn = type_text_fn or self._default_type_text
        self._clipboard_get_fn = clipboard_get_fn or self._default_clipboard_get
        self._clipboard_set_fn = clipboard_set_fn or self._default_clipboard_set
        self._foreground_fn = foreground_fn or self._default_foreground
        # click hook (composer focus). Default uses pyautogui; tests inject a
        # recorder or leave it None to rely on SPA auto-focus.
        self._click_point_fn = click_point_fn if click_point_fn is not None else self._default_click_point
        # Part 6: latest-response Copy click hook. Default None → real UIA path
        # (copy_latest_response uses iface_invoke / click_input). Tests inject a
        # recorder so no real browser is touched.
        self._copy_click_fn = copy_click_fn
        # Fix B (real tab identity): the workflow ChatGPT tab is identified,
        # focused and (only for proven Jarvis duplicates) closed through the
        # existing REAL tab-control layer — CDP targetId when Chrome exposes a
        # remote-debugging endpoint, otherwise UIA (host_hwnd + runtime_id +
        # index). All injected so tests never touch a real browser.
        self._discover_tabs_fn = discover_tabs_fn
        self._focus_tab_cdp_fn = focus_tab_cdp_fn
        self._close_tab_cdp_fn = close_tab_cdp_fn
        self._focus_tab_uia_fn = focus_tab_uia_fn
        self._close_tab_uia_fn = close_tab_uia_fn
        self._create_tab_cdp_fn = create_tab_cdp_fn
        self._create_tab_uia_fn = create_tab_uia_fn
        # UIA-only URL reader (Chrome 152 has no CDP): reads the exact tab's
        # address-bar URL by runtime_id. Injected in tests.
        self._read_tab_url_uia_fn = read_tab_url_uia_fn
        # Jarvis-owned dedicated-window controller (Part 7 browser ownership):
        # launches + chrome://version-verifies a dedicated window for the
        # selected profile. Injected in tests.
        self._jarvis_window_controller = jarvis_window_controller
        # Positive Jarvis ownership evidence: the exact tab identities Jarvis
        # itself created/established for a workflow (keyed by workflow_id). A
        # same-conversation duplicate is only closable when it appears here —
        # a manually-opened copy of the same conversation is NEVER closed.
        # The durable copy lives in the persisted binding; this in-memory set is
        # the fast path within a process lifetime.
        self._owned_tab_keys: dict = {}

    # ── Default real implementations ─────────────────────────────────────────

    @property
    def profile_controller(self):
        if self._profile_controller is not None:
            return self._profile_controller
        from jarvis_core.chrome_profile_controller import chrome_profile_controller
        return chrome_profile_controller

    def _default_focus_verify(self, profile_key: str, *, expected_email=None) -> dict:
        """Focus (or launch) the exact profile window and verify ownership
        (directory + account email). The selected profile is absolute."""
        return self.profile_controller.open_or_focus(
            profile_key, expected_email=expected_email)

    @staticmethod
    def _default_window_strings(hwnd: int) -> list[str]:
        from jarvis_core.chrome_profile_identity import _probe_window_strings
        return _probe_window_strings(int(hwnd))

    @staticmethod
    def _default_send_keys(chord: str) -> None:
        import pyautogui
        # chord like "ctrl+l", "ctrl+v", "ctrl+c", "enter", "ctrl+a"
        parts = [p.strip() for p in chord.split("+") if p.strip()]
        if len(parts) == 1:
            pyautogui.press(parts[0])
        else:
            pyautogui.hotkey(*parts)

    @staticmethod
    def _default_type_text(text: str) -> None:
        import pyautogui
        pyautogui.write(text, interval=0.01)

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
    def _default_foreground():
        from jarvis_core.windows_resource_manager import foreground_window
        return foreground_window()

    def _focus_composer(self, hwnd: int) -> None:
        """Move keyboard focus into the ChatGPT composer.

        Clicks a point in the composer region computed from the LIVE window
        rectangle (never a fixed screen coordinate): horizontally centred,
        ~86% down the window height where ChatGPT's input box sits on a
        new-chat page. Best-effort — if the click hook is unavailable the SPA
        auto-focus + settle is relied upon.
        """
        if self._click_point_fn is None:
            return
        try:
            from jarvis_core.windows_resource_manager import window_rect
            rect = window_rect(int(hwnd))
        except Exception:
            rect = None
        if not rect:
            return
        x = int(rect["x"] + rect["width"] * 0.5)
        y = int(rect["y"] + rect["height"] * 0.86)
        try:
            self._click_point_fn(x, y)
        except Exception:
            logger.debug("composer click failed", exc_info=True)

    @staticmethod
    def _default_click_point(x: int, y: int) -> None:
        import pyautogui
        pyautogui.click(x, y)

    @staticmethod
    def _strong_focus(hwnd: int) -> bool:
        """Foreground *hwnd* using AttachThreadInput to defeat the foreground
        lock, then verify. Returns True only when *hwnd* is really foreground.

        NOTE: this can still legitimately fail when another process (e.g. a
        remote-desktop client) holds the input foreground and Windows denies
        the switch. In that case we report failure rather than typing blindly.
        """
        import ctypes
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
        except Exception:
            return False
        hwnd = int(hwnd)
        try:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            if user32.GetForegroundWindow() == hwnd:
                return True
            fg = user32.GetForegroundWindow()
            tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            tid_tg = user32.GetWindowThreadProcessId(hwnd, None)
            cur = kernel32.GetCurrentThreadId()
            a1 = a2 = False
            try:
                if tid_fg and tid_fg != cur:
                    a1 = bool(user32.AttachThreadInput(cur, tid_fg, True))
                if tid_tg and tid_tg != cur:
                    a2 = bool(user32.AttachThreadInput(cur, tid_tg, True))
                # Alt tap grants foreground permission on many builds.
                user32.keybd_event(0x12, 0, 0, 0)
                user32.keybd_event(0x12, 0, 2, 0)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                if a1:
                    user32.AttachThreadInput(cur, tid_fg, False)
                if a2:
                    user32.AttachThreadInput(cur, tid_tg, False)
            time.sleep(0.12)
            return user32.GetForegroundWindow() == hwnd
        except Exception:
            logger.debug("_strong_focus failed", exc_info=True)
            return False

    # ── Profile ownership guard ──────────────────────────────────────────────

    def ensure_selected_profile(self, profile_key: str,
                                *, expected_email: Optional[str] = None) -> int:
        """Focus + STRICTLY verify the exact selected profile. Returns its HWND.

        The selected profile is ABSOLUTE: verification requires the observed
        profile directory AND (when known) the account email to match. Raises
        :class:`ProfileOwnershipError` if ownership cannot be positively proven
        — we NEVER continue in another Chrome profile, and NO browser side
        effect (navigate/type) happens until this returns.
        """
        # Pass the expected email through to the Part 3 controller when the
        # focus-verify hook accepts it (real open_or_focus does; test doubles
        # may not — fall back gracefully).
        result = None
        if expected_email is not None:
            try:
                result = self._focus_verify_fn(profile_key, expected_email=expected_email)
            except TypeError:
                result = self._focus_verify_fn(profile_key)
        else:
            result = self._focus_verify_fn(profile_key)
        if not result or not result.get("verified"):
            raise ProfileOwnershipError(
                f"PROFILE_MISMATCH: could not positively verify selected profile "
                f"{profile_key} (observed_dir={result.get('observed') if result else None}, "
                f"observed_email={result.get('observed_email') if result else None}, "
                f"expected_email={expected_email}, reason={result.get('reason') if result else None})"
            )
        hwnd = result.get("hwnd")
        if not hwnd:
            raise ProfileOwnershipError("No window handle for the selected profile")
        return int(hwnd)

    def _strings(self, hwnd: int) -> list[str]:
        try:
            return [str(s) for s in (self._window_strings_fn(hwnd) or [])]
        except Exception:
            logger.debug("window strings probe failed", exc_info=True)
            return []

    def _deep_strings(self, hwnd: int) -> list[str]:
        """Deeper (still bounded) UIA walk to reach ChatGPT message controls.

        The shallow probe (used for the toolbar profile chip) cannot reach the
        message-action buttons (Copy / Stop) which live deep in the page tree.
        This bounded deeper walk does, without running away. Injectable via
        window_strings_fn in tests (falls back to the shallow hook there).
        """
        # In tests, window_strings_fn is injected — reuse it (tests drive state).
        if self._window_strings_fn is not self._default_window_strings:
            return self._strings(hwnd)
        try:
            from collections import deque
            from pywinauto import Desktop
            root = Desktop(backend="uia").window(handle=int(hwnd))
            strings: list[str] = []
            deadline = time.monotonic() + 6.0
            q = deque([(root, 0)])
            visited = 0
            while q and time.monotonic() < deadline and visited < 3000:
                el, d = q.popleft()
                visited += 1
                try:
                    t = str(el.window_text() or "").strip()
                except Exception:
                    t = ""
                if t:
                    strings.append(t)
                if d < 28:
                    try:
                        for ch in el.children():
                            q.append((ch, d + 1))
                    except Exception:
                        pass
            return strings
        except Exception:
            logger.debug("deep window strings probe failed", exc_info=True)
            return []

    @staticmethod
    def _any_evidence(strings: list[str], vocab) -> bool:
        low = [s.lower() for s in strings]
        return any(any(v in s for s in low) for v in vocab)

    # ── State detection ──────────────────────────────────────────────────────

    def detect_state(self, hwnd: int) -> str:
        """Return one of READY / LOGIN_REQUIRED / GENERATING / UNKNOWN."""
        strings = self._strings(hwnd)
        if self._any_evidence(strings, _GENERATING_EVIDENCE):
            return "GENERATING"
        if self._any_evidence(strings, _LOGIN_EVIDENCE) and not self._any_evidence(
            strings, ("message chatgpt", "send a message", "ask anything")
        ):
            return "LOGIN_REQUIRED"
        if self._any_evidence(strings, _READY_EVIDENCE):
            return "READY"
        return "UNKNOWN"

    # ── Navigation ─────────────────────────────────────────────────────────

    def open_chatgpt(self, profile_key: str, *, expected_email: Optional[str] = None,
                     navigate: bool = True) -> dict:
        """Focus the exact profile (verified: directory + email) and return its
        verified HWND. When ``navigate`` is True (default) also navigate the
        window's active tab to chatgpt.com. Set ``navigate=False`` to only
        verify+focus so the caller performs a SINGLE navigation itself (avoids
        the redundant root→root double-navigation during bootstrap).

        No navigation happens before verification. The selected profile is
        absolute (directory + email); a wrong/unproven profile raises before any
        keystroke."""
        hwnd = self.ensure_selected_profile(profile_key, expected_email=expected_email)
        if navigate:
            self._navigate(hwnd, CHATGPT_URL)
        return {"hwnd": hwnd}

    def open_chatgpt_in_new_tab(self, profile_key: str, *, workflow_id: str,
                                expected_email: Optional[str] = None) -> dict:
        """Verify the EXACT selected profile window, then create ONE NEW,
        Jarvis-owned tab inside THAT window and open a fresh ChatGPT there.

        This is the bootstrap entry point. It NEVER navigates the window's
        currently-active tab (which may be the Jarvis frontend or any user tab)
        — it creates a dedicated new tab, focuses it, and only then navigates
        that new tab to ChatGPT root. The selected profile is absolute; a
        wrong/unproven profile raises before any keystroke; if a real new-tab
        identity cannot be obtained it fails closed (never overwrites a tab).

        Returns ``{"hwnd", "identity"}`` where identity is the workflow-scoped,
        Jarvis-created tab identity (CDP target_id or UIA host_hwnd+runtime_id),
        with its URL verified to be ChatGPT.
        """
        # 1) Resolve + strictly verify the selected profile window (dir + email).
        hwnd = self.ensure_selected_profile(profile_key, expected_email=expected_email)
        # 2) Create EXACTLY ONE new tab in THAT verified window and open ChatGPT
        #    root in it (fresh conversation). create_owned_tab focuses the new
        #    tab and navigates IT (never the active tab), then verifies the tab's
        #    URL is ChatGPT before returning — fail-closed otherwise.
        created = self.create_owned_tab(
            hwnd, workflow_id=workflow_id, conversation_url=CHATGPT_URL,
            expect_chatgpt_root=True)
        return {"hwnd": hwnd, "identity": created.get("identity")}

    @property
    def jarvis_window_controller(self):
        if self._jarvis_window_controller is not None:
            return self._jarvis_window_controller
        from jarvis_core.jarvis_browser_window import jarvis_browser_window_controller
        return jarvis_browser_window_controller

    def open_chatgpt_in_dedicated_window(self, profile_key: str, *, workflow_id: str,
                                         selected_profile_directory: str,
                                         expected_email: Optional[str] = None) -> dict:
        """AUTHORITATIVE Part-7 open (hardened launch provenance): launch a
        DEDICATED Jarvis-owned Chrome window for the trusted directory of the
        selected email, prove ownership via the unique launch-token MARKER URL
        (never chrome://version, avatar, title, PID or page content), then reuse
        THAT window's marker tab to open a fresh ChatGPT.

        Jarvis owns the window it uses; it NEVER adopts an arbitrary existing
        window (even one that appears to be the selected profile), and NEVER
        touches the Jarvis frontend. Fails closed (propagates
        :class:`JarvisWindowError`) if the window can't be proven.

        Returns ``{"hwnd", "window_binding", "identity"}`` where identity is the
        Jarvis-owned ChatGPT tab identity (the reused marker tab).
        """
        binding = self.jarvis_window_controller.open_verified_window(
            workflow_id=workflow_id, selected_profile_key=profile_key,
            selected_account_email=expected_email or "",
            selected_profile_directory=selected_profile_directory)
        hwnd = int(binding["hwnd"])
        # Reuse the SAME Jarvis marker tab → ChatGPT. Fewest side effects: no
        # extra tab is created and no pre-existing tab is navigated.
        self._navigate(hwnd, CHATGPT_URL)
        self.ensure_chatgpt_ready(hwnd)
        # The workflow-owned ChatGPT tab identity is the marker tab captured by
        # the provenance layer (same runtime_id we just navigated). Prefer it;
        # fall back to reading the active tab if the binding lacks it.
        identity = binding.get("tab_identity") or self._identity_of_active_tab(
            hwnd, workflow_id=workflow_id, conversation_url=CHATGPT_URL)
        if isinstance(identity, dict):
            identity = dict(identity)
            identity.setdefault("conversation_url", CHATGPT_URL)
            identity.setdefault("created_by", "jarvis")
        return {"hwnd": hwnd, "window_binding": binding, "identity": identity}

    def _identity_of_active_tab(self, hwnd: int, *, workflow_id: str,
                                conversation_url: str) -> Optional[dict]:
        """Capture the exact identity of the currently-active tab in *hwnd*
        (the reused Jarvis verification tab). CDP target_id if available, else
        the UIA runtime_id of the selected tab."""
        disc = self._discover_tabs()
        tabs = list(disc.get("tabs") or [])
        # Prefer the selected tab in THIS window.
        for tab in tabs:
            if tab.get("host_hwnd") is not None:
                try:
                    if int(tab["host_hwnd"]) != int(hwnd):
                        continue
                except (TypeError, ValueError):
                    continue
            if tab.get("selected") and self._identity_available(tab):
                self._register_owned(workflow_id, tab)
                return self._tab_identity(tab, workflow_id=workflow_id,
                                          conversation_url=conversation_url,
                                          created_by_jarvis=True)
        # Fallback: any identifiable tab in this window.
        for tab in tabs:
            if tab.get("host_hwnd") is not None and int(tab.get("host_hwnd")) != int(hwnd):
                continue
            if self._identity_available(tab):
                self._register_owned(workflow_id, tab)
                return self._tab_identity(tab, workflow_id=workflow_id,
                                          conversation_url=conversation_url,
                                          created_by_jarvis=True)
        return None

    def _navigate(self, hwnd: int, url: str) -> None:
        """Ctrl+L → type URL → Enter, only when the exact window is foreground."""
        self._require_foreground(hwnd)
        self._send_keys_fn("ctrl+l")
        time.sleep(0.15)
        self._type_text_fn(url)
        time.sleep(0.1)
        self._send_keys_fn("enter")
        time.sleep(NAV_SETTLE_SECONDS)

    def _require_foreground(self, hwnd: int) -> None:
        """Ensure *hwnd* is the foreground window before sending keys.

        Attempts a strong focus once; if the exact window still isn't
        foreground we refuse to type (never into the wrong window) and raise
        :class:`ForegroundBlockedError`.
        """
        fg = self._foreground_fn()
        if fg is not None and int(fg) == int(hwnd):
            return
        # Try once to take the foreground robustly.
        try:
            if self._strong_focus(int(hwnd)):
                return
        except Exception:
            pass
        fg = self._foreground_fn()
        if fg is not None and int(fg) != int(hwnd):
            raise ForegroundBlockedError(
                f"FOREGROUND_BLOCKED: selected Chrome window {hwnd} could not be "
                f"brought to the foreground (held by {fg}). Refusing to type."
            )

    def read_current_url(self, hwnd: int) -> str:
        """Read the address-bar URL (Ctrl+L → Ctrl+C → clipboard), restore clip.

        Reliable page-identity signal that does not depend on deep page-content
        accessibility. Returns the raw string (may be empty).
        """
        self._require_foreground(hwnd)
        prev = ""
        try:
            prev = self._clipboard_get_fn() or ""
        except Exception:
            prev = ""
        url = ""
        try:
            self._send_keys_fn("ctrl+l")
            time.sleep(0.15)
            self._send_keys_fn("ctrl+c")
            time.sleep(0.15)
            url = (self._clipboard_get_fn() or "").strip()
            self._send_keys_fn("escape")
        finally:
            try:
                self._clipboard_set_fn(prev)
            except Exception:
                pass
        return url

    @staticmethod
    def _url_is_chatgpt(url: str) -> bool:
        from urllib.parse import urlsplit
        if not url:
            return False
        cand = url if "://" in url else "https://" + url
        try:
            host = (urlsplit(cand).hostname or "").lower()
        except ValueError:
            return False
        if host.startswith("www."):
            host = host[4:]
        return host in CHATGPT_HOSTS

    @staticmethod
    def _url_is_login(url: str) -> bool:
        from urllib.parse import urlsplit
        cand = url if "://" in url else "https://" + url
        try:
            path = (urlsplit(cand).path or "").lower()
        except ValueError:
            return False
        return any(seg in path for seg in ("/auth", "/login", "/api/auth"))

    def ensure_chatgpt_ready(self, hwnd: int, *, allow_login_page: bool = False) -> str:
        """Wait (bounded) until ChatGPT is READY. Raise on login/timeout.

        Readiness is proven by BOTH signals, preferring the reliable one:
          1) the address-bar URL is a chatgpt.com URL (not a login/auth URL);
          2) OR the page accessibility exposes composer/ready evidence.
        A login/auth URL or a login page raises :class:`LoginRequiredError`.
        """
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        last = "UNKNOWN"
        while time.monotonic() < deadline:
            # URL-based evidence (robust; independent of deep page UIA).
            url = self.read_current_url(hwnd)
            if url and self._url_is_chatgpt(url):
                if self._url_is_login(url) and not allow_login_page:
                    raise LoginRequiredError("CHATGPT_LOGIN_REQUIRED")
                return "READY"
            # Accessibility evidence (secondary).
            last = self.detect_state(hwnd)
            if last == "LOGIN_REQUIRED" and not allow_login_page:
                raise LoginRequiredError("CHATGPT_LOGIN_REQUIRED")
            if last in ("READY", "GENERATING"):
                return last
            time.sleep(POLL_INTERVAL)
        if last == "LOGIN_REQUIRED":
            raise LoginRequiredError("CHATGPT_LOGIN_REQUIRED")
        raise ChatGPTNotReadyError("ChatGPT UI not ready within deadline")

    # ── New chat ──────────────────────────────────────────────────────────

    def start_new_chat(self, hwnd: int) -> None:
        """Create a NEW ChatGPT conversation deterministically.

        Navigating to the ChatGPT ROOT (``https://chatgpt.com/``) always opens a
        fresh new-chat composer with no prior conversation reused. We do NOT use
        a keyboard shortcut: ChatGPT's in-app shortcuts collide with Chrome's
        own shortcuts (e.g. Ctrl+Shift+O is Chrome's Bookmark Manager), which
        would navigate the browser away from ChatGPT. The root URL is the
        reliable, collision-free way to guarantee a new conversation.
        """
        self._navigate(hwnd, CHATGPT_URL)
        # Confirm we landed on ChatGPT (not a login page) and the URL is root
        # (a fresh chat has no /c/<id> yet).
        self.ensure_chatgpt_ready(hwnd)

    # ── Prompt submission ────────────────────────────────────────────────────

    def submit_prompt(self, hwnd: int, prompt: str) -> dict:
        """Paste the EXACT prompt into the composer and submit it once.

        Uses clipboard paste (save/restore prior clipboard). Focuses the
        composer via a click-free keyboard path: the ChatGPT composer is the
        default focus target on a new chat, and we additionally send the paste
        into the foreground window only.
        """
        if not prompt or not prompt.strip():
            raise ChatGPTError("MASTER_REQUIREMENT_MISSING")
        self._require_foreground(hwnd)
        # SAFETY: never paste/submit unless we are genuinely on a ChatGPT page.
        # This prevents the prompt from being pasted into the omnibox (which
        # would trigger a web search) or the wrong tab.
        url = self.read_current_url(hwnd)
        if not self._url_is_chatgpt(url):
            raise ChatGPTError(
                f"Refusing to submit: not on a ChatGPT page (url={url[:60]!r})"
            )
        # Focus the composer explicitly. Readiness checks read the address bar
        # (Ctrl+L), which can leave the OMNIBOX focused — pasting then would
        # trigger a web search. Prefer UIA set_focus() on the prompt-textarea
        # (LIVE-calibrated as reliable); fall back to a live-rect click.
        if not self._focus_composer_uia(hwnd):
            self._focus_composer(hwnd)
        time.sleep(COMPOSER_FOCUS_SETTLE_SECONDS)

        prev_clip = ""
        try:
            prev_clip = self._clipboard_get_fn() or ""
        except Exception:
            prev_clip = ""
        try:
            self._clipboard_set_fn(prompt)
            time.sleep(0.15)
            self._send_keys_fn("ctrl+v")   # into the auto-focused composer
            time.sleep(0.3)
            self._send_keys_fn("enter")    # ChatGPT submits the composer on Enter
        finally:
            try:
                self._clipboard_set_fn(prev_clip)
            except Exception:
                pass
        # NOTE: submission success is CONFIRMED downstream by the URL
        # transitioning to chatgpt.com/c/<id> in detect_generation_started /
        # wait_for_generation_complete. If the paste had gone to the wrong
        # control (e.g. omnibox), that transition never happens and the
        # bootstrap fails honestly rather than claiming success.
        return {"submitted": True}

    # ── Generation detection (positive evidence, no fixed sleep) ─────────────

    @staticmethod
    def _url_has_conversation_id(url: str) -> bool:
        """True when the URL is a persistent conversation (chatgpt.com/c/<id>)."""
        from urllib.parse import urlsplit
        if not url:
            return False
        cand = url if "://" in url else "https://" + url
        try:
            p = urlsplit(cand)
        except ValueError:
            return False
        host = (p.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host in CHATGPT_HOSTS and "/c/" in (p.path or "")

    def detect_generation_started(self, hwnd: int, *, timeout: float = GENERATION_START_TIMEOUT_SECONDS) -> bool:
        """Poll until generation is observed to have started.

        Strong, page-content-independent evidence: submitting the first message
        in a NEW chat transitions the URL from the root to
        ``chatgpt.com/c/<id>`` (the conversation is created). We also accept the
        deep-UIA 'Stop' streaming indicator. Either proves the message was
        accepted and generation began.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            url = self.read_current_url(hwnd)
            if self._url_has_conversation_id(url):
                return True
            strings = self._deep_strings(hwnd)
            if self._any_evidence(strings, _GENERATING_EVIDENCE):
                return True
            time.sleep(POLL_INTERVAL)
        return False

    def wait_for_generation_complete(
        self, hwnd: int, *, timeout: float = GENERATION_COMPLETE_TIMEOUT_SECONDS
    ) -> dict:
        """Wait for POSITIVE completion evidence, stable across observations.

        Requires BOTH:
          - the conversation URL is a persistent ``/c/<id>`` (message accepted),
          - the generating/streaming 'Stop' indicator is ABSENT (deep UIA),
        observed stably across ``COMPLETION_STABLE_OBSERVATIONS`` consecutive
        polls. Presence of a completed-response control (Copy/Regenerate) is
        accepted as additional confirmation when the deep walk exposes it.

        Never uses a fixed sleep as the completion signal. Raises
        :class:`GenerationTimeoutError` if completion is not confirmed in time.
        """
        deadline = time.monotonic() + timeout
        stable = 0
        saw_conversation = False
        while time.monotonic() < deadline:
            url = self.read_current_url(hwnd)
            if self._url_has_conversation_id(url):
                saw_conversation = True
            strings = self._deep_strings(hwnd)
            generating = self._any_evidence(strings, _GENERATING_EVIDENCE)
            has_controls = self._any_evidence(strings, _COMPLETED_CONTROL_EVIDENCE)
            # Completion: conversation exists AND not currently generating.
            # A completed-response control is strong extra confirmation.
            if saw_conversation and not generating:
                stable += 1
                if stable >= COMPLETION_STABLE_OBSERVATIONS and (has_controls or stable >= COMPLETION_STABLE_OBSERVATIONS + 2):
                    return {"complete": True, "observations": stable,
                            "url": url, "had_controls": has_controls}
            else:
                stable = 0  # a single transient miss must not count as done
            time.sleep(POLL_INTERVAL)
        raise GenerationTimeoutError("ChatGPT generation did not complete in time")

    # ── Part 5: targeted follow-up probes (URL does NOT change) ──────────────
    #
    # For a follow-up command the conversation is ALREADY chatgpt.com/c/<id>,
    # so the URL transition used in Part 4 cannot signal generation. Detection
    # here is purely accessibility-based, using the SAME evidence vocabularies
    # calibrated against real ChatGPT in Part 4:
    #   - GENERATING  : the deep-UIA tree exposes a "Stop"/"stop streaming"
    #                   control while the assistant is streaming a reply.
    #   - COMPLETE    : that Stop control is ABSENT *and* the latest response's
    #                   completed controls ("Copy"/"Regenerate"/"Read aloud"/
    #                   "Good response") are present, stable across observations.
    # A single missing "Stop" is NOT enough — streaming has micro-gaps — so we
    # require COMPLETION_STABLE_OBSERVATIONS consecutive confirmations and never
    # a fixed sleep.

    def probe_page_state(self, hwnd: int) -> dict:
        """Targeted, bounded ChatGPT page-state probe (the ChatGPTPageStateProbe).

        Walks ONLY enough of the deep-UIA tree to locate the calibrated anchors
        (composer, Stop/Send buttons, Copy button, Response-actions group) and
        returns a structured snapshot. Injectable in tests via
        ``window_strings_fn`` (then the anchors are derived from the injected
        strings). ``UNKNOWN`` is a valid result — we never guess.

        Returns::

            {state, composer_ready, stop_visible, send_visible, copy_visible,
             response_actions_visible, evidence}

        where ``state`` is one of IDLE / GENERATING / COMPLETED_CANDIDATE /
        UNKNOWN. Note this single observation cannot distinguish COMPLETED from
        IDLE (both have no Stop); the caller's ``wait_for_followup_complete``
        adds the "generation was previously observed" + stability requirement.
        """
        # Test path: window_strings_fn injected → derive anchors from strings.
        if self._window_strings_fn is not self._default_window_strings:
            strings = self._strings(hwnd)
            return self._state_from_strings(strings)
        # Real path: targeted UIA walk collecting only Button/Group/Edit anchors.
        return self._probe_uia_state(hwnd)

    @staticmethod
    def _classify(*, composer_ready: bool, stop_visible: bool,
                  copy_visible: bool, response_actions_visible: bool) -> str:
        if stop_visible:
            return "GENERATING"
        if composer_ready and (copy_visible or response_actions_visible):
            # Cannot tell COMPLETED vs IDLE-after-a-turn from one frame; the
            # caller disambiguates using saw_generating + stability.
            return "COMPLETED_CANDIDATE"
        if composer_ready:
            return "IDLE"
        return "UNKNOWN"

    def _state_from_strings(self, strings: list[str]) -> dict:
        low = [str(s).lower() for s in (strings or [])]
        def has(vocab):
            return any(any(v in s for s in low) for v in vocab)
        composer_ready = has(_COMPOSER_NAMES)
        stop_visible = has(_STOP_NAMES)
        send_visible = has(_SEND_NAMES)
        copy_visible = has(_COPY_NAMES) or has(_COMPLETED_CONTROL_EVIDENCE)
        response_actions_visible = any(_RESPONSE_ACTIONS_GROUP in s for s in low)
        evidence = [s for s in (strings or []) if str(s).lower() in low][:20]
        return {
            "state": self._classify(
                composer_ready=composer_ready, stop_visible=stop_visible,
                copy_visible=copy_visible,
                response_actions_visible=response_actions_visible),
            "composer_ready": composer_ready,
            "stop_visible": stop_visible,
            "send_visible": send_visible,
            "copy_visible": copy_visible,
            "response_actions_visible": response_actions_visible,
            "evidence": list(strings or [])[:20],
        }

    def _probe_uia_state(self, hwnd: int) -> dict:
        """Bounded real UIA walk targeting the composer/message/toolbar anchors."""
        composer_ready = stop_visible = send_visible = False
        copy_visible = response_actions_visible = False
        evidence: list[str] = []
        try:
            from collections import deque
            from pywinauto import Desktop
            root = Desktop(backend="uia").window(handle=int(hwnd))
        except Exception:
            logger.debug("probe_uia_state: cannot attach UIA", exc_info=True)
            return {
                "state": "UNKNOWN", "composer_ready": False, "stop_visible": False,
                "send_visible": False, "copy_visible": False,
                "response_actions_visible": False, "evidence": [],
            }
        q = deque([(root, 0)])
        visited = 0
        deadline = time.monotonic() + 6.0
        while q and visited < 3500 and time.monotonic() < deadline:
            el, d = q.popleft()
            visited += 1
            try:
                info = el.element_info
                name = (el.window_text() or "").strip()
                ctype = info.control_type or ""
                aid = info.automation_id or ""
            except Exception:
                name, ctype, aid = "", "", ""
            low = name.lower()
            if ctype == "Edit" and aid == COMPOSER_AUTOMATION_ID:
                composer_ready = True
            elif ctype == "Edit" and any(v in low for v in _COMPOSER_NAMES):
                composer_ready = True
            if ctype == "Button" and name:
                if any(v in low for v in _STOP_NAMES):
                    stop_visible = True
                    evidence.append(name)
                if any(v in low for v in _SEND_NAMES):
                    send_visible = True
                if any(v in low for v in _COPY_NAMES):
                    copy_visible = True
                    evidence.append(name)
            if ctype == "Group" and low == _RESPONSE_ACTIONS_GROUP:
                response_actions_visible = True
                evidence.append(name)
            if d < 24:
                try:
                    for ch in el.children():
                        q.append((ch, d + 1))
                except Exception:
                    pass
        return {
            "state": self._classify(
                composer_ready=composer_ready, stop_visible=stop_visible,
                copy_visible=copy_visible,
                response_actions_visible=response_actions_visible),
            "composer_ready": composer_ready,
            "stop_visible": stop_visible,
            "send_visible": send_visible,
            "copy_visible": copy_visible,
            "response_actions_visible": response_actions_visible,
            "evidence": evidence[:20],
        }

    def get_chat_state(self, hwnd: int) -> dict:
        """Back-compat snapshot built on the structured probe.

        Keeps the ``generating`` / ``has_controls`` / ``composer_ready`` keys the
        follow-up waiters use, now sourced from :meth:`probe_page_state` and the
        REAL calibrated control names (Stop answering / Copy response / …).
        """
        p = self.probe_page_state(hwnd)
        return {
            "generating": p["stop_visible"],
            "has_controls": p["copy_visible"] or p["response_actions_visible"],
            "composer_ready": p["composer_ready"],
            "state": p["state"],
            "strings": p.get("evidence", []),
        }

    def is_generating(self, hwnd: int) -> bool:
        """True when the 'Stop answering' streaming control is present."""
        return bool(self.probe_page_state(hwnd)["stop_visible"])

    def is_response_complete(self, hwnd: int) -> bool:
        """True for a single observation of completion evidence.

        One-frame completion = not generating AND completed-response controls
        exposed. Stability + "generation previously observed" are enforced by
        :meth:`wait_for_followup_complete`, not here.
        """
        p = self.probe_page_state(hwnd)
        return (not p["stop_visible"]) and (
            p["copy_visible"] or p["response_actions_visible"]
        )

    def _focus_composer_uia(self, hwnd: int) -> bool:
        """Focus the ProseMirror composer directly via UIA ``set_focus()``.

        LIVE-calibrated: the composer is ``Edit automation_id='prompt-textarea'``
        and a UIA ``set_focus()`` on it reliably moves keyboard focus into the
        editor — a geometric click did NOT (it silently no-op'd twice). Returns
        True when the composer element was found and focused. In tests (where
        ``window_strings_fn`` is injected) this is skipped and returns False so
        the geometric-click fallback path is exercised deterministically.
        """
        if self._window_strings_fn is not self._default_window_strings:
            return False
        try:
            from collections import deque
            from pywinauto import Desktop
            root = Desktop(backend="uia").window(handle=int(hwnd))
        except Exception:
            return False
        q = deque([(root, 0)])
        visited = 0
        deadline = time.monotonic() + 5.0
        while q and visited < 3500 and time.monotonic() < deadline:
            el, d = q.popleft()
            visited += 1
            try:
                info = el.element_info
                if (info.control_type or "") == "Edit" and (
                    (info.automation_id or "") == COMPOSER_AUTOMATION_ID
                ):
                    el.set_focus()
                    return True
            except Exception:
                pass
            if d < 24:
                try:
                    for ch in el.children():
                        q.append((ch, d + 1))
                except Exception:
                    pass
        return False

    def submit_command(self, hwnd: int, text: str) -> dict:
        """Paste an EXACT follow-up command into the composer and submit once.

        Same safety envelope as :meth:`submit_prompt` (foreground guard, verify
        we are on a ChatGPT page, clipboard save/paste/restore, single Enter).
        Composer focus is obtained via UIA ``set_focus()`` on the
        ``prompt-textarea`` Edit (LIVE-calibrated as reliable); if that fails we
        fall back to the live-rect geometric click. Distinct from
        :meth:`submit_prompt` so Part 5 follow-ups are auditable separately.
        """
        if not text or not text.strip():
            raise ChatGPTError("COMMAND_TEXT_MISSING")
        self._require_foreground(hwnd)
        url = self.read_current_url(hwnd)
        if not self._url_is_chatgpt(url):
            raise ChatGPTError(
                f"Refusing to submit: not on a ChatGPT page (url={url[:60]!r})"
            )
        if not self._focus_composer_uia(hwnd):
            # Fallback: click the composer region derived from the live rect.
            self._focus_composer(hwnd)
        time.sleep(COMPOSER_FOCUS_SETTLE_SECONDS)
        prev_clip = ""
        try:
            prev_clip = self._clipboard_get_fn() or ""
        except Exception:
            prev_clip = ""
        try:
            self._clipboard_set_fn(text)
            time.sleep(0.15)
            self._send_keys_fn("ctrl+v")
            time.sleep(0.3)
            self._send_keys_fn("enter")
        finally:
            try:
                self._clipboard_set_fn(prev_clip)
            except Exception:
                pass
        return {"submitted": True}

    def wait_for_followup_generation_start(
        self, hwnd: int, *, timeout: float = GENERATION_START_TIMEOUT_SECONDS
    ) -> bool:
        """Poll until POSITIVE evidence the follow-up reply began streaming.

        Unlike Part 4 (root→/c/<id> URL transition), a follow-up keeps the same
        URL, so the ONLY start evidence is the Stop/streaming control appearing
        in the deep-UIA tree. Returns True on first observation; False on
        timeout (caller decides — a very fast reply may finish before we see
        Stop, so callers treat a timeout here as "check completion directly").
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_generating(hwnd):
                return True
            time.sleep(POLL_INTERVAL)
        return False

    def wait_for_followup_complete(
        self, hwnd: int, *, timeout: float = GENERATION_COMPLETE_TIMEOUT_SECONDS
    ) -> dict:
        """Wait for stable follow-up completion via accessibility only.

        Completion requires, observed stably across
        ``COMPLETION_STABLE_OBSERVATIONS`` consecutive polls:
          - the Stop/streaming control is ABSENT, AND
          - the completed-response controls (Copy/Regenerate/...) are PRESENT.

        A single missing Stop resets the counter (streaming has micro-gaps).
        Never a fixed sleep. Raises :class:`GenerationTimeoutError` on timeout.
        """
        deadline = time.monotonic() + timeout
        stable = 0
        while time.monotonic() < deadline:
            state = self.get_chat_state(hwnd)
            complete_now = (not state["generating"]) and state["has_controls"]
            if complete_now:
                stable += 1
                if stable >= COMPLETION_STABLE_OBSERVATIONS:
                    return {
                        "complete": True,
                        "observations": stable,
                        "had_controls": True,
                    }
            else:
                stable = 0
            time.sleep(POLL_INTERVAL)
        raise GenerationTimeoutError(
            "ChatGPT follow-up generation did not complete in time"
        )

    # ── Part 6: latest-response location + Copy + clipboard capture ──────────
    #
    # LIVE-calibrated against a real multi-turn ChatGPT conversation:
    #   - Assistant turns expose a Group name='Response actions' and, within it,
    #     a Button name='Copy response'. User turns expose 'Your message actions'
    #     + 'Copy message' (which MUST NOT be used).
    #   - The a11y tree exposes the conversation ONLY when the window is genuinely
    #     on ChatGPT and freshly rendered — callers MUST restore+verify the SAME
    #     conversation first (Part 6 does).
    #   - The LATEST assistant response's Copy = the LAST 'Copy response' button
    #     in document (BFS) order. Newer turns are lower on the page.
    #   - The button is invokable via UIA (iface_invoke); fallback is click_input
    #     on its LIVE rect (never fixed screen coordinates — the window may be on
    #     a secondary monitor with a negative X).

    _COPY_RESPONSE_NAME = "copy response"
    _COPY_MESSAGE_NAME = "copy message"

    def _iter_copy_response_elements(self, hwnd: int):
        """BFS the ChatGPT window, returning ('Copy response') Button elements in
        document order (list of pywinauto wrappers). Excludes user-turn
        'Copy message'. Bounded walk (same budget as _deep_strings)."""
        from collections import deque
        from pywinauto import Desktop
        root = Desktop(backend="uia").window(handle=int(hwnd))
        q = deque([(root, 0)])
        visited = 0
        deadline = time.monotonic() + 8.0
        found = []
        while q and visited < 6000 and time.monotonic() < deadline:
            el, d = q.popleft()
            visited += 1
            try:
                info = el.element_info
                name = (el.window_text() or "").strip().lower()
                ctype = info.control_type or ""
            except Exception:
                name, ctype = "", ""
            if ctype == "Button" and name == self._COPY_RESPONSE_NAME:
                found.append(el)
            if d < 30:
                try:
                    for ch in el.children():
                        q.append((ch, d + 1))
                except Exception:
                    pass
        return found

    def find_latest_assistant_response(self, hwnd: int) -> dict:
        """Locate the LATEST assistant response and prove it is the newest.

        Returns ``{"index": <0-based>, "count": <n>, "found": True}`` where
        ``index`` is the position (in document order) of the newest assistant
        response among all assistant responses. Raises :class:`ChatGPTError`
        (``LATEST_RESPONSE_NOT_FOUND``) when no assistant response is present —
        we never fall back to an older response.

        In tests (``window_strings_fn`` injected) the assistant responses are
        derived from the injected strings: each occurrence of 'copy response'
        (case-insensitive) counts as one assistant response in order.
        """
        if self._window_strings_fn is not self._default_window_strings:
            strings = [str(s).lower() for s in self._strings(hwnd)]
            n = sum(1 for s in strings if self._COPY_RESPONSE_NAME in s)
            if n <= 0:
                raise ChatGPTError("LATEST_RESPONSE_NOT_FOUND")
            return {"index": n - 1, "count": n, "found": True}
        els = self._iter_copy_response_elements(hwnd)
        if not els:
            raise ChatGPTError("LATEST_RESPONSE_NOT_FOUND")
        return {"index": len(els) - 1, "count": len(els), "found": True}

    @staticmethod
    def _conversation_id(conversation_url: str) -> str:
        """The stable /c/<id> conversation id from a ChatGPT URL (or the path)."""
        from urllib.parse import urlsplit
        try:
            p = urlsplit(conversation_url if "://" in conversation_url
                         else "https://" + conversation_url)
        except ValueError:
            return conversation_url or ""
        path = (p.path or "").rstrip("/")
        if "/c/" in path:
            return path.split("/c/", 1)[1]
        return path

    @staticmethod
    def _normalize_content(text: str) -> str:
        """Normalize assistant content for a stable content hash: strip trailing
        whitespace per line and collapse surrounding blank lines. Never alters
        the SUBSTANCE — only whitespace at the edges — so a regenerated response
        with different words yields a different hash while the same response
        read twice yields the same hash."""
        if not text:
            return ""
        lines = [ln.rstrip() for ln in str(text).replace("\r\n", "\n").split("\n")]
        return "\n".join(lines).strip()

    def compute_response_identity(self, conversation_url: str, *,
                                  assistant_index: int, content: str) -> str:
        """REAL GPT response identity: distinguishes R1 vs a regenerated R2 in
        the SAME iteration (different content), and recognises the SAME response
        observed repeatedly (same content).

        identity = sha256(conversation_id : assistant_message_index :
                          normalized_content_hash)[:32]
        """
        import hashlib
        conv_id = self._conversation_id(conversation_url)
        norm = self._normalize_content(content)
        content_hash = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        raw = f"{conv_id}:{int(assistant_index)}:{content_hash}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def read_latest_response_text(self, hwnd: int) -> Optional[str]:
        """Best-effort read of the LATEST assistant response's TEXT without
        clicking Copy (used to derive the response identity BEFORE deciding
        whether to copy). Returns None when the content cannot be read cheaply;
        callers must then rely on the post-copy captured content for identity.

        On the injected-test path, ``window_strings_fn`` may expose a special
        marker string ``"__RESPONSE_TEXT__:<text>"`` giving the latest response
        text deterministically."""
        if self._window_strings_fn is not self._default_window_strings:
            for s in self._strings(hwnd):
                if str(s).startswith("__RESPONSE_TEXT__:"):
                    return str(s).split("__RESPONSE_TEXT__:", 1)[1]
            return None
        # Real path: reading full message text reliably via UIA is not always
        # possible; return None and let the post-copy captured content define
        # the identity (the durable copied-latch still guarantees copy-once).
        return None

    def find_latest_response_copy_control(self, hwnd: int):
        """Return the pywinauto Button wrapper for the LATEST response's Copy.

        The latest response's Copy control is the LAST 'Copy response' button in
        document order. Raises :class:`ChatGPTError` (``COPY_CONTROL_NOT_FOUND``)
        when none exist — never returns an older response's control.

        Not used on the injected-test path (tests exercise copy_latest_response
        via the clicker hook), only on the real UIA path.
        """
        els = self._iter_copy_response_elements(hwnd)
        if not els:
            raise ChatGPTError("COPY_CONTROL_NOT_FOUND")
        return els[-1]

    def copy_latest_response(self, hwnd: int) -> dict:
        """Click the LATEST assistant response's Copy control exactly once.

        Prefers UIA ``invoke()`` on the proven control; falls back to
        ``click_input()`` on its LIVE rectangle. Raises :class:`ChatGPTError`
        if the latest response / its Copy control cannot be proven (never copies
        an older response).

        On the injected-test path a ``copy_click_fn`` hook is invoked instead of
        touching a real browser, and the number of assistant responses is taken
        from the injected strings so "click the latest" is still asserted.
        """
        self._require_foreground(hwnd)
        # Prove the latest response exists (raises if none).
        latest = self.find_latest_assistant_response(hwnd)

        if self._copy_click_fn is not None:
            # Test/inject path: hand the proven index to the hook.
            self._copy_click_fn(hwnd, latest["index"])
            return {"clicked": True, "method": "hook",
                    "index": latest["index"], "count": latest["count"]}

        control = self.find_latest_response_copy_control(hwnd)
        method = None
        try:
            control.invoke()
            method = "uia_invoke"
        except Exception:
            logger.debug("Copy invoke() failed; trying click_input", exc_info=True)
            try:
                control.click_input()
                method = "click_input"
            except Exception as exc:
                raise ChatGPTError(f"COPY_CLICK_FAILED: {exc}") from exc
        return {"clicked": True, "method": method,
                "index": latest["index"], "count": latest["count"]}

    def capture_clipboard_via_sentinel(
        self,
        hwnd: int,
        *,
        copy_action: Optional[Callable] = None,
        timeout: float = 8.0,
        sentinel: Optional[str] = None,
    ) -> dict:
        """Copy with POSITIVE proof: sentinel → click → poll until clipboard
        changes away from the sentinel → capture EXACT text.

        Flow:
          1. save the user's current clipboard,
          2. write a unique sentinel to the clipboard,
          3. run ``copy_action`` (defaults to :meth:`copy_latest_response`),
          4. poll until the clipboard != sentinel (or timeout),
          5. capture the EXACT resulting text (never trimmed/rewritten),
          6. validate non-empty,
          7. restore the user's previous clipboard,
          8. return ``{text, length, sha256, method, index, count}``.

        Raises :class:`ChatGPTError` (``GPT_COPY_FAILED``) if the clipboard never
        changes away from the sentinel (Copy did not succeed) or the captured
        text is empty. Never assumes success from the click alone.
        """
        import hashlib
        import uuid
        sentinel = sentinel or f"__JARVIS_COPY_SENTINEL__{uuid.uuid4().hex}__"
        try:
            prev_clip = self._clipboard_get_fn() or ""
        except Exception:
            prev_clip = ""

        captured = None
        click_info = {}
        try:
            self._clipboard_set_fn(sentinel)
            time.sleep(0.1)
            act = copy_action or (lambda: self.copy_latest_response(hwnd))
            click_info = act() or {}
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    cur = self._clipboard_get_fn()
                except Exception:
                    cur = None
                if cur is not None and cur != sentinel:
                    captured = cur
                    break
                time.sleep(POLL_INTERVAL)
        finally:
            # Restore the user's clipboard only after we've captured (or failed).
            try:
                if captured is not None:
                    self._clipboard_set_fn(prev_clip)
            except Exception:
                pass

        if captured is None:
            raise ChatGPTError("GPT_COPY_FAILED: clipboard unchanged (sentinel)")
        if not str(captured).strip():
            raise ChatGPTError("GPT_COPY_FAILED: captured text is empty")
        text = str(captured)
        return {
            "text": text,
            "length": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "method": click_info.get("method"),
            "index": click_info.get("index"),
            "count": click_info.get("count"),
        }

    # ── Real Jarvis-owned ChatGPT TAB identity ───────────────────────────────
    #
    # A window HWND + conversation URL is NOT a browser-tab identity. This layer
    # gives the workflow a REAL, verifiable tab identity using the existing
    # tab-control subsystem (browser_resource_manager.discover_tabs):
    #   - CDP mode  : the tab's Chrome DevTools ``target_id`` (durable while the
    #                 tab lives; changes when Chrome restarts).
    #   - UIA mode  : the tab's ``host_hwnd`` + ``runtime_id`` (+ index/title)
    #                 (durable within a session; recycled on restart).
    # The CROSS-RESTART anchor is always the ChatGPT conversation URL (/c/<id>),
    # which survives Chrome restarts and window/tab-id recycling. A tab is only
    # ever the workflow's OWNED tab when its URL matches the saved conversation
    # URL AND it lives in the verified profile window. This is what lets us tell
    # apart (A) the Jarvis workflow tab, (B) the user's unrelated ChatGPT tabs
    # (different URLs), and (C) another workflow's tab (different conversation).

    def _discover_tabs(self) -> dict:
        """Real (or injected) tab enumeration. Never raises; returns a dict with
        ``tabs`` (possibly empty) and ``cdp_url``/``controllable`` fields."""
        fn = self._discover_tabs_fn
        if fn is None:
            try:
                from jarvis_core import browser_resource_manager
                fn = browser_resource_manager.discover_tabs
            except Exception:
                logger.debug("discover_tabs import failed", exc_info=True)
                return {"tabs": [], "controllable": False, "cdp_url": None}
        try:
            return fn() or {"tabs": []}
        except Exception:
            logger.debug("discover_tabs failed", exc_info=True)
            return {"tabs": [], "controllable": False, "cdp_url": None}

    @staticmethod
    def _tab_url(tab: dict) -> str:
        return str(tab.get("raw_url") or tab.get("url") or "").strip()

    def read_uia_tab_url(self, host_hwnd: int, runtime_id, *, title: str = "",
                         tab_index: int = -1) -> dict:
        """Read the address-bar URL of the EXACT UIA tab (by runtime_id), safely
        and fail-closed. Returns ``{"ok", "url", "code", "reason"}``. Codes:
        ``TAB_IDENTITY_STALE`` (exact tab gone / not selectable) and
        ``TAB_URL_UNAVAILABLE`` (URL could not be positively read)."""
        fn = self._read_tab_url_uia_fn
        if fn is None:
            try:
                from jarvis_core import uia_browser_manager
                fn = uia_browser_manager.read_tab_url
            except Exception:
                return {"ok": False, "url": "", "code": "TAB_URL_UNAVAILABLE",
                        "reason": "UIA URL reader unavailable"}
        rid = runtime_id
        if isinstance(rid, str):
            rid = [int(x) for x in rid.split(",") if x.strip()]
        try:
            return fn(int(host_hwnd), list(rid or []), title=title,
                      tab_index=int(tab_index)) or {
                "ok": False, "url": "", "code": "TAB_URL_UNAVAILABLE", "reason": ""}
        except Exception as exc:
            return {"ok": False, "url": "", "code": "TAB_URL_UNAVAILABLE",
                    "reason": f"{type(exc).__name__}: {exc}"}

    def identify_jarvis_frontend(self, *, jarvis_hosts=("127.0.0.1:8000", "localhost:8000"),
                                 jarvis_path_markers=("/ui", "/"),
                                 candidate_hwnds: Optional[list] = None) -> Optional[dict]:
        """READ-ONLY: find the Chrome tab hosting the Jarvis frontend by its real
        URL (never by title). Reads tab URLs across candidate Chrome windows and
        returns ``{"host_hwnd", "runtime_id", "tab_index", "title", "url"}`` for
        the first tab whose URL matches a Jarvis host, or None.

        This creates NO tabs and navigates nothing; it only reads address bars
        (restoring the clipboard). Titles such as 'TeleAutomation' are never used
        as evidence.
        """
        disc = self._discover_tabs()
        tabs = list(disc.get("tabs") or [])
        for tab in tabs:
            if str(tab.get("control_mode") or "").lower() != "uia":
                # CDP tabs already carry a URL.
                url = self._tab_url(tab)
            else:
                if candidate_hwnds is not None and tab.get("host_hwnd") not in candidate_hwnds:
                    continue
                res = self.read_uia_tab_url(
                    int(tab.get("host_hwnd")), tab.get("runtime_id") or [],
                    title=str(tab.get("title") or ""),
                    tab_index=int(tab.get("tab_index", -1)))
                url = res.get("url") or "" if res.get("ok") else ""
            if not url:
                continue
            from urllib.parse import urlsplit
            try:
                p = urlsplit(url if "://" in url else "https://" + url)
            except ValueError:
                continue
            netloc = (p.netloc or "").lower()
            if any(h in netloc for h in jarvis_hosts):
                return {"host_hwnd": tab.get("host_hwnd"),
                        "runtime_id": tab.get("runtime_id"),
                        "tab_index": tab.get("tab_index"),
                        "title": tab.get("title"), "url": url}
        return None

    def _resolve_tab_url(self, tab: dict) -> str:
        """URL for a discovered tab. CDP tabs already carry the URL; UIA tabs do
        NOT (Chrome exposes no URL via UIA), so we read the exact tab's URL by
        runtime_id. Returns "" when it cannot be positively read."""
        direct = self._tab_url(tab)
        if direct:
            return direct
        if str(tab.get("control_mode") or "").lower() == "uia" and tab.get("host_hwnd") is not None:
            res = self.read_uia_tab_url(
                int(tab["host_hwnd"]), tab.get("runtime_id") or [],
                title=str(tab.get("title") or ""),
                tab_index=int(tab.get("tab_index", -1)))
            if res.get("ok"):
                return res.get("url") or ""
        return ""

    def _tabs_for_conversation(self, tabs: list[dict], conversation_url: str,
                               *, host_hwnd: Optional[int] = None) -> list[dict]:
        """All discovered tabs whose URL is the SAME conversation as
        *conversation_url*. When *host_hwnd* is given, restrict to tabs in that
        exact Chrome window so we never match a tab in another window. UIA tab
        URLs are read on demand (by runtime_id) since UIA does not expose them.
        The resolved URL is cached back onto the tab dict as ``raw_url`` so the
        identity record can carry it."""
        out = []
        for tab in tabs or []:
            if host_hwnd is not None and tab.get("host_hwnd") is not None:
                try:
                    if int(tab["host_hwnd"]) != int(host_hwnd):
                        continue
                except (TypeError, ValueError):
                    continue
            url = self._resolve_tab_url(tab)
            if url and self.verify_same_conversation(conversation_url, url):
                tab["raw_url"] = url          # cache for identity + display
                out.append(tab)
        return out

    @staticmethod
    def _tab_identity(tab: dict, *, workflow_id: str, conversation_url: str,
                      created_by_jarvis: bool = False) -> dict:
        """Build the durable, workflow-scoped tab identity record from a
        discovered tab. Includes workflow_id so a binding can NEVER be confused
        with another workflow's tab, and ``created_by`` positive-ownership
        evidence when Jarvis itself created the tab."""
        mode = str(tab.get("control_mode") or "").lower()
        ident = {
            "owner": "jarvis",
            "workflow_id": workflow_id,
            "conversation_url": conversation_url,
            "control_mode": mode,
            "created_by": "jarvis" if created_by_jarvis else None,
        }
        # The URL is evidence for conversation binding (kept alongside the
        # exact-tab identity). For UIA tabs this is the read-back address bar.
        url = tab.get("raw_url") or tab.get("url") or ""
        if url:
            ident["url"] = url
        if mode == "cdp":
            ident["target_id"] = tab.get("target_id") or tab.get("page_id")
        else:
            ident["host_hwnd"] = tab.get("host_hwnd")
            ident["runtime_id"] = ",".join(
                str(x) for x in (tab.get("runtime_id") or []))
            ident["tab_index"] = int(tab.get("tab_index", -1))
            ident["title"] = str(tab.get("title") or "")[:120]
        return ident

    def _identity_matches_tab(self, ident: dict, tab: dict) -> bool:
        """True only when *tab* IS the exact tab recorded in *ident* (never a
        title/position guess for UIA; exact runtime-id, else nothing)."""
        if not ident:
            return False
        mode = str(ident.get("control_mode") or "").lower()
        if str(tab.get("control_mode") or "").lower() != mode:
            return False
        if mode == "cdp":
            tid = tab.get("target_id") or tab.get("page_id")
            return bool(ident.get("target_id")) and str(tid) == str(ident["target_id"])
        # UIA: exact host_hwnd + runtime_id.
        try:
            if int(tab.get("host_hwnd")) != int(ident.get("host_hwnd")):
                return False
        except (TypeError, ValueError):
            return False
        rid = ",".join(str(x) for x in (tab.get("runtime_id") or []))
        return bool(ident.get("runtime_id")) and rid == ident["runtime_id"]

    @staticmethod
    def _tab_key(tab_or_ident: dict) -> Optional[str]:
        """A stable string key for a tab/identity used to track Jarvis-created
        ownership (CDP target_id or ``uia:<host>:<runtime>``)."""
        mode = str(tab_or_ident.get("control_mode") or "").lower()
        if mode == "cdp":
            tid = tab_or_ident.get("target_id") or tab_or_ident.get("page_id")
            return f"cdp:{tid}" if tid else None
        host = tab_or_ident.get("host_hwnd")
        rid = tab_or_ident.get("runtime_id")
        if isinstance(rid, (list, tuple)):
            rid = ",".join(str(x) for x in rid)
        if host is None or not rid:
            return None
        return f"uia:{host}:{rid}"

    def _register_owned(self, workflow_id: str, tab_or_ident: dict,
                        *, external_owned: Optional[set] = None) -> None:
        key = self._tab_key(tab_or_ident)
        if key is None:
            return
        self._owned_tab_keys.setdefault(str(workflow_id), set()).add(key)
        if external_owned is not None:
            external_owned.add(key)

    def _is_owned(self, workflow_id: str, tab: dict,
                  *, external_owned: Optional[set] = None) -> bool:
        """POSITIVE Jarvis ownership check for THIS workflow. True only when the
        exact tab key was created/established by Jarvis for this workflow — a
        matching conversation URL ALONE is never sufficient (a user may open the
        same conversation manually)."""
        key = self._tab_key(tab)
        if key is None:
            return False
        if key in self._owned_tab_keys.get(str(workflow_id), set()):
            return True
        if external_owned and key in external_owned:
            return True
        return False

    def _identity_available(self, tab: dict) -> bool:
        """True only when a REAL controllable identity exists (CDP target_id or
        UIA host_hwnd+runtime_id). Window/URL alone is NOT identity."""
        mode = str(tab.get("control_mode") or "").lower()
        if mode == "cdp":
            return bool(tab.get("target_id") or tab.get("page_id"))
        if mode == "uia":
            return tab.get("host_hwnd") is not None and bool(tab.get("runtime_id"))
        return False

    def establish_owned_tab(self, hwnd: int, *, workflow_id: str,
                            conversation_url: str,
                            owned_keys: Optional[set] = None) -> dict:
        """Establish EXACTLY ONE workflow-owned ChatGPT tab and return its
        durable identity. Called right after the conversation URL is captured.

        FAIL-CLOSED (B1): if no tab on the conversation has a REAL controllable
        identity (CDP target_id or UIA host_hwnd+runtime_id), this raises
        :class:`TabIdentityUnavailableError`. Jarvis never downgrades to
        window/URL ownership.

        Steps (all safe, no user tab touched):
          1. enumerate real tabs;
          2. find tabs in the verified window on the SAME conversation URL;
          3. if >1 → deterministic canonical + close ONLY the PROVEN Jarvis
             duplicates (positive ownership evidence — never a manual copy);
          4. register + return the canonical tab's exact identity.

        Returns ``{"identity": <dict>, "count": 1, "closed": <int>}``.
        """
        disc = self._discover_tabs()
        tabs = list(disc.get("tabs") or [])
        mine = self._tabs_for_conversation(tabs, conversation_url, host_hwnd=hwnd)
        # Also consider CDP tabs (no host_hwnd) matching the conversation.
        if not mine:
            mine = self._tabs_for_conversation(tabs, conversation_url)
        # FAIL-CLOSED: only tabs with a REAL identity qualify as ownable.
        mine = [t for t in mine if self._identity_available(t)]
        if not mine:
            raise TabIdentityUnavailableError(
                "TAB_IDENTITY_UNAVAILABLE: no controllable ChatGPT tab identity "
                f"(CDP target_id / UIA runtime_id) for conversation "
                f"{conversation_url!r}. Refusing to proceed with window/URL-only "
                "ownership.")
        closed = 0
        if len(mine) > 1:
            canonical, closed = self._reduce_to_canonical(
                mine, workflow_id=workflow_id, conversation_url=conversation_url,
                cdp_url=disc.get("cdp_url"), owned_keys=owned_keys)
            mine = [canonical]
        # This is Jarvis's canonical tab for the workflow → positive ownership.
        self._register_owned(workflow_id, mine[0], external_owned=owned_keys)
        ident = self._tab_identity(
            mine[0], workflow_id=workflow_id, conversation_url=conversation_url,
            created_by_jarvis=True)
        return {"identity": ident, "count": 1, "closed": closed, "reason": ""}

    def _reduce_to_canonical(self, matched: list[dict], *, workflow_id: str,
                             conversation_url: str, cdp_url: Optional[str],
                             owned_keys: Optional[set] = None) -> tuple[dict, int]:
        """Given >1 tabs on the SAME workflow conversation, pick a deterministic
        canonical tab and close the rest.

        STRENGTHENED (B2c): a duplicate is closed ONLY when it carries POSITIVE
        Jarvis ownership for THIS workflow (it was created/established by Jarvis)
        AND is on the expected conversation. A same-conversation tab the USER
        opened manually (no ownership marker) is NEVER closed. Never a "close
        all". Returns (canonical_tab, num_closed)."""
        # Deterministic order: prefer a Jarvis-owned tab as canonical, then CDP
        # by target_id, UIA by (host_hwnd, tab_index).
        def _key(t: dict):
            owned = 0 if self._is_owned(workflow_id, t, external_owned=owned_keys) else 1
            if str(t.get("control_mode") or "").lower() == "cdp":
                return (owned, 0, str(t.get("target_id") or t.get("page_id") or ""))
            return (owned, 1, int(t.get("host_hwnd") or 0), int(t.get("tab_index", 0)))

        ordered = sorted(matched, key=_key)
        canonical = ordered[0]
        closed = 0
        for dup in ordered[1:]:
            # AUTHORISATION to close requires ALL of:
            #   (1) same conversation URL as the workflow, AND
            #   (2) POSITIVE Jarvis ownership for THIS workflow.
            if not self.verify_same_conversation(conversation_url, self._tab_url(dup)):
                continue
            if not self._is_owned(workflow_id, dup, external_owned=owned_keys):
                logger.info("Leaving same-conversation tab untouched (no Jarvis "
                            "ownership evidence) — likely a manual user copy")
                continue
            if self._close_tab(dup, cdp_url=cdp_url):
                closed += 1
        return canonical, closed

    def _close_tab(self, tab: dict, *, cdp_url: Optional[str]) -> bool:
        """Close EXACTLY one proven-duplicate Jarvis tab by its exact identity.
        Never closes the browser or any other tab. Returns True on success."""
        mode = str(tab.get("control_mode") or "").lower()
        try:
            if mode == "cdp":
                fn = self._close_tab_cdp_fn
                if fn is None:
                    from jarvis_core import browser_resource_manager
                    fn = browser_resource_manager.close_tab
                return bool(fn(cdp_url or "", tab.get("target_id") or tab.get("page_id")))
            fn = self._close_tab_uia_fn
            if fn is None:
                from jarvis_core import uia_browser_manager
                fn = uia_browser_manager.close_tab
            out = fn(int(tab.get("host_hwnd") or 0),
                     [int(x) for x in (tab.get("runtime_id") or [])],
                     str(tab.get("title") or ""), int(tab.get("tab_index", -1)))
            # UIA close_tab returns a dict; CDP-style returns bool.
            if isinstance(out, dict):
                return bool(out.get("ok"))
            return bool(out)
        except Exception:
            logger.debug("close duplicate tab failed", exc_info=True)
            return False

    def resolve_owned_tab(self, hwnd: int, *, workflow_id: str,
                          conversation_url: str,
                          identity: Optional[dict] = None) -> dict:
        """Resolve the workflow's canonical owned tab for a follow-up action.

        Ownership rule (fail-closed): a tab counts as owned ONLY when its URL is
        the SAME conversation AND (when we have a saved identity) it is the exact
        recorded tab, OR — for cross-restart, when the recorded target_id/
        runtime_id is gone — a tab in the verified window on the SAME
        conversation URL (the conversation is the durable cross-restart anchor).

        Returns ``{"state": one of
            'reused'      → persisted identity still present & matches,
            'replaced'    → identity gone/stale but a same-conversation tab found
                            (caller updates the binding),
            'missing'     → no tab on this conversation exists (caller must
                            create exactly one replacement, then re-establish),
            'ambiguous'   → >1 same-conversation tabs (caller runs cleanup first),
            'uncontrollable' → tab layer not available (fall back to window+URL),
          "identity": <dict|None>, "closed": <int>}``.

        This NEVER adopts a tab whose conversation URL differs (user tab or
        another workflow's tab).
        """
        if identity is not None and str(identity.get("workflow_id") or "") not in (
                "", str(workflow_id)):
            # Belongs to a DIFFERENT workflow → never adopt it.
            return {"state": "cross_workflow_rejected", "identity": None, "closed": 0}

        # A persisted identity is positive Jarvis ownership evidence — re-register
        # it (it may have been created in a prior process before a restart).
        owned_keys: set = set()
        if identity is not None and str(identity.get("owner") or "") == "jarvis":
            self._register_owned(workflow_id, identity, external_owned=owned_keys)

        disc = self._discover_tabs()
        tabs = list(disc.get("tabs") or [])

        mine = self._tabs_for_conversation(tabs, conversation_url, host_hwnd=hwnd)
        if not mine:
            mine = self._tabs_for_conversation(tabs, conversation_url)
        # Only tabs with a REAL identity qualify (never window/URL-only).
        mine = [t for t in mine if self._identity_available(t)]

        if not mine:
            # No controllable tab on this conversation → caller must CREATE one.
            return {"state": "missing", "identity": None, "closed": 0}

        if len(mine) > 1:
            canonical, closed = self._reduce_to_canonical(
                mine, workflow_id=workflow_id, conversation_url=conversation_url,
                cdp_url=disc.get("cdp_url"), owned_keys=owned_keys)
            self._register_owned(workflow_id, canonical, external_owned=owned_keys)
            new_ident = self._tab_identity(
                canonical, workflow_id=workflow_id,
                conversation_url=conversation_url, created_by_jarvis=True)
            return {"state": "replaced", "identity": new_ident, "closed": closed}

        only = mine[0]
        if identity is not None and self._identity_matches_tab(identity, only):
            return {"state": "reused", "identity": identity, "closed": 0}
        # Same conversation, but the recorded target/runtime id is gone (stale /
        # recycled / Chrome restarted) — accept the conversation as the anchor
        # and refresh the identity. We do NOT act on a tab of a different URL.
        self._register_owned(workflow_id, only, external_owned=owned_keys)
        new_ident = self._tab_identity(
            only, workflow_id=workflow_id, conversation_url=conversation_url,
            created_by_jarvis=True)
        return {"state": "replaced", "identity": new_ident, "closed": 0}

    def create_owned_tab(self, hwnd: int, *, workflow_id: str,
                         conversation_url: str,
                         expect_chatgpt_root: bool = False) -> dict:
        """Create EXACTLY ONE new REAL browser tab for the workflow, navigate it
        to *conversation_url*, and return its real identity.

        This NEVER navigates/overwrites an existing tab: it uses the CDP
        ``create_tab`` (a brand-new page target) or, without CDP, UIA Ctrl+T on
        the EXACT verified window. The user's Gmail / personal ChatGPT / Jarvis
        tabs are untouched. Raises :class:`TabIdentityUnavailableError` when no
        real new-tab identity can be obtained (fail closed — never Ctrl+L the
        active tab).

        When ``expect_chatgpt_root`` is True the new tab is opening a FRESH
        ChatGPT (bootstrap), so the read-back URL is only required to be a
        ChatGPT URL (there is no ``/c/<id>`` yet); otherwise the read-back URL
        must be the SAME conversation as *conversation_url*.

        Returns ``{"identity": <dict>, "created": True}``.
        """
        disc = self._discover_tabs()
        cdp_url = disc.get("cdp_url")
        created_tab = None

        # Prefer CDP: creates a brand-new target and navigates it directly.
        cdp_fn = self._create_tab_cdp_fn
        if cdp_fn is None:
            try:
                from jarvis_core import browser_resource_manager
                cdp_fn = browser_resource_manager.create_tab
            except Exception:
                cdp_fn = None
        if cdp_fn is not None and cdp_url:
            try:
                res = cdp_fn(cdp_url, conversation_url)
                if res and res.get("target_id"):
                    created_tab = {"control_mode": "cdp",
                                   "target_id": res["target_id"],
                                   "raw_url": res.get("raw_url") or conversation_url}
            except Exception:
                logger.debug("CDP create_tab failed", exc_info=True)

        # UIA fallback: Ctrl+T on the exact verified window, then navigate it.
        if created_tab is None:
            uia_fn = self._create_tab_uia_fn
            if uia_fn is None:
                try:
                    from jarvis_core import uia_browser_manager
                    uia_fn = uia_browser_manager.create_tab
                except Exception:
                    uia_fn = None
            if uia_fn is not None:
                try:
                    res = uia_fn(int(hwnd))
                    if res and res.get("ok") and res.get("runtime_id"):
                        created_tab = {"control_mode": "uia",
                                       "host_hwnd": int(hwnd),
                                       "runtime_id": list(res["runtime_id"]),
                                       "tab_index": int(res.get("tab_index", -1)),
                                       "title": str(res.get("title") or "")}
                        # Focus the brand-new tab, then navigate THAT tab.
                        self._focus_owned_tab(
                            self._tab_identity(created_tab, workflow_id=workflow_id,
                                               conversation_url=conversation_url),
                            cdp_url=None)
                        self._navigate(int(hwnd), conversation_url)
                        # VERIFY the URL of THAT exact new tab (read-back by
                        # runtime_id) equals the conversation. Fail closed on a
                        # mismatch — never leave an unverified owned tab.
                        rb = self.read_uia_tab_url(
                            int(hwnd), created_tab["runtime_id"],
                            title=created_tab.get("title", ""),
                            tab_index=created_tab.get("tab_index", -1))
                        rb_url = rb.get("url") or ""
                        if expect_chatgpt_root:
                            url_ok = rb.get("ok") and self._url_is_chatgpt(rb_url)
                        else:
                            url_ok = rb.get("ok") and self.verify_same_conversation(
                                conversation_url, rb_url)
                        if url_ok:
                            created_tab["raw_url"] = rb_url
                        else:
                            raise TabIdentityUnavailableError(
                                "TAB_URL_UNVERIFIED: the new UIA tab did not "
                                f"resolve to ChatGPT/the conversation ({rb.get('code')}).")
                except TabIdentityUnavailableError:
                    raise
                except Exception:
                    logger.debug("UIA create_tab failed", exc_info=True)

        if created_tab is None or not self._identity_available(created_tab):
            raise TabIdentityUnavailableError(
                "TAB_CREATE_FAILED: could not create a controllable new ChatGPT "
                "tab (no CDP endpoint and UIA new-tab unavailable). Refusing to "
                "overwrite the active tab.")

        self._register_owned(workflow_id, created_tab)
        identity = self._tab_identity(
            created_tab, workflow_id=workflow_id, conversation_url=conversation_url,
            created_by_jarvis=True)
        return {"identity": identity, "created": True}

    # ── Conversation URL capture ─────────────────────────────────────────────

    def capture_conversation_url(self, hwnd: int) -> str:
        """Read the ACTUAL current conversation URL via Ctrl+L → Ctrl+C.

        Saves/restores the clipboard, validates the URL belongs to ChatGPT and
        is not a login URL. Raises :class:`ConversationUrlError` otherwise.
        """
        self._require_foreground(hwnd)
        prev_clip = ""
        try:
            prev_clip = self._clipboard_get_fn() or ""
        except Exception:
            prev_clip = ""
        url = ""
        try:
            self._send_keys_fn("ctrl+l")   # focus address bar (selects URL)
            time.sleep(0.15)
            self._send_keys_fn("ctrl+c")   # copy the selected URL
            time.sleep(0.15)
            url = (self._clipboard_get_fn() or "").strip()
            # Return focus to the page (Escape leaves the address bar).
            self._send_keys_fn("escape")
        finally:
            try:
                self._clipboard_set_fn(prev_clip)
            except Exception:
                pass
        return self._validate_conversation_url(url)

    @staticmethod
    def _validate_conversation_url(url: str) -> str:
        from urllib.parse import urlsplit

        if not url:
            raise ConversationUrlError("No URL captured from the address bar")
        # The address bar may omit the scheme; normalise for parsing.
        candidate = url if "://" in url else ("https://" + url)
        try:
            parts = urlsplit(candidate)
        except ValueError as exc:
            raise ConversationUrlError(f"Unparseable URL: {url!r}") from exc
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host not in CHATGPT_HOSTS:
            raise ConversationUrlError(f"URL is not a ChatGPT URL: {host!r}")
        path = (parts.path or "").lower()
        if any(seg in path for seg in ("/auth", "/login", "/api/auth")):
            raise ConversationUrlError("Captured a login URL, not a conversation")
        return candidate

    # ── Workflow-binding-aware restore (Part 5/6/later stages) ────────────────

    def restore_workflow_owned_conversation(self, *, workflow_id: str,
                                            conversation_url: str,
                                            window_binding: dict,
                                            tab_identity: Optional[dict] = None,
                                            selected_profile_key: Optional[str] = None,
                                            selected_account_email: Optional[str] = None
                                            ) -> dict:
        """Restore the workflow's ChatGPT conversation using the PERSISTED
        Jarvis-owned window binding — NEVER legacy profile discovery.

        LIVE path (binding still valid): the bound Jarvis window HWND exists AND
        the persisted tab (runtime_id/target_id) is present in THAT exact window
        AND its URL is the saved conversation → focus the exact tab and reuse it
        directly. NONE of ``ensure_selected_profile`` / ``open_or_focus`` /
        ``verify_profile_open`` / ``launch_profile`` / ``discover_profiles`` run.

        RECOVERY path (binding dead/stale): the bound HWND is gone, the tab is
        missing, ownership can't be proven, or the URL can't be restored on that
        tab → hardened launch-token recovery via
        ``jarvis_window_controller.recreate_for_conversation`` (new token, new
        dedicated window, navigate the marker tab to the SAME saved
        conversation, updated binding). NEVER adopts an arbitrary window; NEVER
        calls the legacy profile verifier.

        Returns ``{hwnd, identity, window_binding, requested_url, observed_url,
        same, reused, recovered}``.
        """
        validated = self._validate_conversation_url(conversation_url)
        ident = tab_identity or (window_binding or {}).get("tab_identity")

        # 1) LIVE reuse — only if the binding is this workflow's Jarvis binding.
        live = self._try_reuse_live_binding(
            workflow_id=workflow_id, conversation_url=validated,
            window_binding=window_binding, tab_identity=ident)
        if live is not None:
            return live

        # 2) RECOVERY — hardened launch-token recreate (no legacy verifier).
        binding = self.jarvis_window_controller.recreate_for_conversation(
            workflow_id=workflow_id,
            selected_profile_key=selected_profile_key
                or (window_binding or {}).get("selected_profile_key"),
            selected_account_email=selected_account_email
                or (window_binding or {}).get("selected_account_email"),
            conversation_url=validated,
            selected_profile_directory=(window_binding or {}).get(
                "selected_profile_directory"))
        hwnd = int(binding["hwnd"])
        self.ensure_chatgpt_ready(hwnd)
        observed = self.capture_conversation_url(hwnd)
        same = self.verify_same_conversation(validated, observed)
        new_ident = binding.get("tab_identity")
        if isinstance(new_ident, dict):
            new_ident = dict(new_ident)
            new_ident.setdefault("conversation_url", validated)
            new_ident.setdefault("created_by", "jarvis")
        return {"hwnd": hwnd, "identity": new_ident, "window_binding": binding,
                "requested_url": validated, "observed_url": observed,
                "same": same, "reused": False, "recovered": True}

    def _try_reuse_live_binding(self, *, workflow_id: str, conversation_url: str,
                                window_binding: Optional[dict],
                                tab_identity: Optional[dict]) -> Optional[dict]:
        """Return a reuse result when the persisted binding is still LIVE for
        THIS workflow, else None (caller recovers). Performs NO legacy profile
        calls: it only enumerates tabs INSIDE the bound HWND and reads the exact
        tab's URL."""
        b = window_binding or {}
        if b.get("owner") != "jarvis":
            return None
        if str(b.get("workflow_id") or "") != str(workflow_id):
            return None
        hwnd = b.get("hwnd")
        if not hwnd:
            return None
        # The bound window must still exist (real Chrome host windows only).
        try:
            from jarvis_core import uia_browser_manager as _u
            live_hwnds = {int(h["hwnd"]) for h in _u.chrome_host_windows()}
        except Exception:
            live_hwnds = set()
        if int(hwnd) not in live_hwnds:
            return None
        ident = tab_identity or b.get("tab_identity") or {}
        want_rid = str(ident.get("runtime_id") or "")
        want_tid = str(ident.get("target_id") or "")
        if not want_rid and not want_tid:
            return None
        # Enumerate tabs ONLY inside the bound window and find the exact tab.
        for tab in (self._window_tabs_in(hwnd) or []):
            if want_tid:
                tid = str(tab.get("target_id") or tab.get("page_id") or "")
                if not tid or tid != want_tid:
                    continue
            else:
                rid = ",".join(str(x) for x in (tab.get("runtime_id") or []))
                if not rid or rid != want_rid:
                    continue
            # Exact owned tab found. Read its URL and require same conversation.
            url = self._resolve_tab_url(tab)
            if not url or not self.verify_same_conversation(conversation_url, url):
                return None
            # Focus the exact owned tab (never navigates a different tab).
            self._focus_owned_tab(ident, cdp_url=None)
            observed = url
            reused_ident = dict(ident)
            reused_ident.setdefault("conversation_url", conversation_url)
            reused_ident.setdefault("owner", "jarvis")
            reused_ident.setdefault("workflow_id", workflow_id)
            return {"hwnd": int(hwnd), "identity": reused_ident,
                    "window_binding": b, "requested_url": conversation_url,
                    "observed_url": observed, "same": True,
                    "reused": True, "recovered": False}
        return None

    def _window_tabs_in(self, hwnd: int) -> list:
        """Discovered tabs restricted to one HWND (CDP tabs have no host_hwnd so
        they are all considered; UIA tabs are filtered to the exact window)."""
        disc = self._discover_tabs()
        out = []
        for tab in (disc.get("tabs") or []):
            hh = tab.get("host_hwnd")
            if hh is not None:
                try:
                    if int(hh) != int(hwnd):
                        continue
                except (TypeError, ValueError):
                    continue
            out.append(tab)
        return out

    # ── Restore / verify an existing conversation ────────────────────────────

    def restore_conversation(self, profile_key: str, conversation_url: str,
                             *, expected_email: Optional[str] = None) -> dict:
        """Focus the exact profile and navigate to the saved conversation URL.

        Does NOT create a new chat. Verifies the profile and that the resulting
        URL matches the saved conversation.
        """
        validated = self._validate_conversation_url(conversation_url)
        hwnd = self.ensure_selected_profile(profile_key, expected_email=expected_email)
        self._navigate(hwnd, validated)
        # Give the SPA a moment then confirm ChatGPT is present (not login).
        self.ensure_chatgpt_ready(hwnd)
        observed = self.capture_conversation_url(hwnd)
        same = self.verify_same_conversation(validated, observed)
        return {"hwnd": hwnd, "requested_url": validated,
                "observed_url": observed, "same": same}

    def restore_owned_conversation(self, profile_key: str, conversation_url: str,
                                   *, workflow_id: str,
                                   identity: Optional[dict] = None,
                                   expected_email: Optional[str] = None) -> dict:
        """Tab-ownership-aware restore for a follow-up stage (command/approval).

        Guarantees exactly ONE workflow-owned tab on the SAME conversation:
          - verify the exact profile (directory + email) FRESH;
          - resolve the workflow's owned tab from a real tab scan;
              * reused   → focus that exact tab, then confirm same conversation;
              * replaced → an existing same-conversation tab (stale id / dup
                           cleaned) → focus/navigate it, refresh identity;
              * missing / cross_workflow_rejected → CREATE EXACTLY ONE new REAL
                           tab (never overwrite the active tab), navigate THAT
                           tab to the saved conversation URL, obtain its real
                           identity.
          - NEVER creates a NEW conversation, NEVER touches a user tab or another
            workflow's tab, and NEVER downgrades to window/URL-only ownership
            (fail closed via :class:`TabIdentityUnavailableError`).

        Returns the same shape as :meth:`restore_conversation` plus
        ``tab_state`` and ``identity`` (the possibly-refreshed identity the
        caller should persist) and ``duplicate_tabs_closed``.
        """
        validated = self._validate_conversation_url(conversation_url)
        hwnd = self.ensure_selected_profile(profile_key, expected_email=expected_email)

        res = self.resolve_owned_tab(
            hwnd, workflow_id=workflow_id, conversation_url=validated,
            identity=identity)
        state = res.get("state")
        closed = res.get("closed", 0)
        new_identity = res.get("identity", identity)

        if state in ("reused", "replaced"):
            # Focus the exact owned tab first (so the correct tab is active),
            # then navigate to the saved conversation to guarantee identity.
            self._focus_owned_tab(new_identity, cdp_url=None)
            self._navigate(hwnd, validated)
            self.ensure_chatgpt_ready(hwnd)
        elif state in ("missing", "cross_workflow_rejected"):
            # Create EXACTLY ONE new REAL tab and navigate IT (never the active
            # tab) to the SAME conversation. Fails closed if no real tab identity
            # can be obtained.
            created = self.create_owned_tab(
                hwnd, workflow_id=workflow_id, conversation_url=validated)
            new_identity = created.get("identity", new_identity)
            self.ensure_chatgpt_ready(hwnd)
        else:
            # Any unexpected state: fail closed rather than acting on the active
            # tab. (There is no window/URL-only fallback.)
            raise TabIdentityUnavailableError(
                f"TAB_STATE_UNRESOLVED: {state!r} for {validated!r}; refusing to "
                "act on an unidentified tab.")

        observed = self.capture_conversation_url(hwnd)
        same = self.verify_same_conversation(validated, observed)
        return {"hwnd": hwnd, "requested_url": validated, "observed_url": observed,
                "same": same, "tab_state": state, "identity": new_identity,
                "duplicate_tabs_closed": closed}

    def _focus_owned_tab(self, identity: Optional[dict], *,
                         cdp_url: Optional[str]) -> bool:
        """Focus the exact owned tab by its identity (best-effort). Never raises."""
        if not identity:
            return False
        mode = str(identity.get("control_mode") or "").lower()
        try:
            if mode == "cdp" and identity.get("target_id"):
                fn = self._focus_tab_cdp_fn
                if fn is None:
                    from jarvis_core import browser_resource_manager
                    fn = browser_resource_manager.focus_tab
                return bool(fn(cdp_url or "", identity["target_id"]))
            if mode == "uia" and identity.get("host_hwnd") is not None:
                fn = self._focus_tab_uia_fn
                if fn is None:
                    from jarvis_core import uia_browser_manager
                    fn = uia_browser_manager.focus_tab
                rid = [int(x) for x in str(identity.get("runtime_id") or "").split(",") if x]
                return bool(fn(int(identity["host_hwnd"]), rid,
                               str(identity.get("title") or ""),
                               int(identity.get("tab_index", -1))))
        except Exception:
            logger.debug("focus owned tab failed", exc_info=True)
        return False

    @staticmethod
    def verify_same_conversation(saved_url: str, observed_url: str) -> bool:
        """True when both URLs point to the same ChatGPT conversation path."""
        from urllib.parse import urlsplit

        def _key(u: str):
            try:
                p = urlsplit(u if "://" in u else "https://" + u)
            except ValueError:
                return None
            host = (p.hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            return (host, (p.path or "").rstrip("/").lower())

        a, b = _key(saved_url), _key(observed_url)
        return a is not None and a == b
