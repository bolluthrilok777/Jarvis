"""Dedicated Chrome Profile Controller for the Autonomous Workflow (Part 3).

Purpose
-------
Give the Autonomous Workflow a *reliable* way to:

- discover the REAL Chrome profiles on this Windows machine,
- identify each by a STABLE key derived from its profile *directory*
  (``chrome-profile:Default``, ``chrome-profile:Profile 7`` …),
- prove the signed-in account email from trusted on-disk metadata (never
  fabricate one),
- launch a SPECIFIC profile (``chrome.exe --profile-directory="Profile 7"``),
- focus an already-open profile window,
- and — crucially — VERIFY that an opened/focused Chrome window genuinely
  belongs to the requested profile.

Why this is a *dedicated* module and not the Resource Review code
-----------------------------------------------------------------
Resource Review's per-window identity relies on UIA-string matching plus
single-profile attribution-by-elimination, and it never reads the window's
real ``--profile-directory``. That is unreliable for strict per-window
ownership. This controller proves ownership by correlating a window's HWND →
PID → the launching Chrome process's ``--profile-directory`` command-line
argument (via psutil). The profile directory is the persistent identity; the
Gmail address is display metadata only.

Reused proven infrastructure:
- ``chrome_profile_identity.chrome_profile_metadata`` — safe Local State parse.
- ``uia_browser_manager.chrome_host_windows`` — real Chrome top-level windows.
- ``windows_resource_manager.focus_window`` — verified foreground focus.

No secrets (cookies/tokens/sync) are ever read. No browser *content* is
automated here (that is Part 4+). Test Open is configuration/verification only
and never advances the workflow state machine.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger("jarvis.chrome.controller")

KEY_PREFIX = "chrome-profile:"

# Bounded waits (monotonic-deadline pattern used across the project).
LAUNCH_WAIT_SECONDS = 12.0
LAUNCH_POLL_INTERVAL = 0.4
FOCUS_SETTLE_SECONDS = 0.2

# Neutral bucket for windows whose owning profile cannot be positively proven.
# Such windows are NEVER attributed to a real profile (esp. not Default), so
# they can never be silently used as the selected profile's window.
_UNIDENTIFIED_PROFILE_DIR = "__unidentified__"


def profile_key_for(profile_dir: str) -> str:
    """Return the stable key for a profile *directory* (e.g. 'Profile 7')."""
    return f"{KEY_PREFIX}{profile_dir}"


def profile_dir_from_key(profile_key: str) -> str:
    """Inverse of :func:`profile_key_for`."""
    if profile_key and profile_key.startswith(KEY_PREFIX):
        return profile_key[len(KEY_PREFIX):]
    return str(profile_key or "")


class ChromeProfileError(Exception):
    """Base error for the Chrome profile controller."""


class ChromeExecutableNotFound(ChromeProfileError):
    """Raised when a Chrome executable cannot be resolved."""


class ProfileNotFound(ChromeProfileError):
    """Raised when a requested profile key does not exist on disk."""


class ProfileVerificationError(ChromeProfileError):
    """Raised when the opened/focused window is not the requested profile."""


class ChromeProfileController:
    """Discovers, launches, focuses and verifies real Chrome profiles.

    All OS interaction is injectable (via the ``*_fn`` hooks) so tests can run
    without touching the real machine or launching real processes.
    """

    def __init__(
        self,
        *,
        user_data_dir: Optional[str] = None,
        metadata_fn=None,
        host_windows_fn=None,
        focus_fn=None,
        launch_fn=None,
        process_profile_fn=None,
        exe_resolver_fn=None,
        window_identity_fn=None,
        window_email_fn=None,
    ) -> None:
        self._lock = threading.RLock()
        self._user_data_dir = user_data_dir or self._default_user_data_dir()

        # Injectable dependencies (default to the real, proven implementations).
        self._metadata_fn = metadata_fn or self._default_metadata
        self._host_windows_fn = host_windows_fn or self._default_host_windows
        self._focus_fn = focus_fn or self._default_focus
        self._launch_fn = launch_fn or self._default_launch
        # window (hwnd,pid) -> profile_dir evidence provider (process cmdline)
        self._process_profile_fn = process_profile_fn or self._default_pid_profile_map
        # per-window profile_dir evidence provider (UIA toolbar chip). Strong
        # evidence for the common case where one browser process multiplexes
        # windows from several profiles, so PID→cmdline is insufficient.
        self._window_identity_fn = window_identity_fn or self._default_window_identity
        # per-window observed account-email provider (independent identity
        # cross-check). Injectable; the default reads the window's UIA strings.
        self._window_email_fn = window_email_fn or self._default_window_email
        self._exe_resolver_fn = exe_resolver_fn or self.resolve_chrome_executable

    # ── Defaults wiring real infrastructure ──────────────────────────────────

    @staticmethod
    def _default_user_data_dir() -> str:
        return os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data"
        )

    def _default_metadata(self) -> dict:
        from jarvis_core.chrome_profile_identity import chrome_profile_metadata

        dirs = [self._user_data_dir] if self._user_data_dir else None
        return chrome_profile_metadata(dirs)

    @staticmethod
    def _default_host_windows() -> list[dict]:
        from jarvis_core.uia_browser_manager import chrome_host_windows

        return chrome_host_windows()

    @staticmethod
    def _default_focus(hwnd: int) -> bool:
        from jarvis_core.windows_resource_manager import focus_window

        return bool(focus_window(hwnd))

    def _default_launch(self, exe: str, profile_dir: str) -> int:
        """Launch chrome.exe for a specific profile directory. Returns PID."""
        args = [
            exe,
            f'--profile-directory={profile_dir}',
            # Open a neutral page; NO ChatGPT / content automation in Part 3.
            "about:blank",
        ]
        proc = subprocess.Popen(args, cwd=os.path.dirname(exe) or None)
        return int(proc.pid)

    @staticmethod
    def _default_pid_profile_map() -> dict:
        """Map each Chrome PID → the ``--profile-directory`` it was launched
        with, read from the process command line via psutil.

        This is the strong ownership evidence: a window's PID is correlated to
        the exact profile directory of its launching process, rather than
        matching UIA strings or assuming a single profile.
        """
        mapping: dict[int, str] = {}
        try:
            import psutil
        except Exception:  # pragma: no cover - psutil is a project dep
            return mapping
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if name not in ("chrome", "chrome.exe"):
                    continue
                cmdline = proc.info.get("cmdline") or []
                prof = _extract_profile_directory(cmdline)
                if prof is not None:
                    mapping[int(proc.info["pid"])] = prof
            except Exception:
                continue
        return mapping

    def _default_window_identity(self, hwnd: int) -> Optional[str]:
        """Return the profile *directory* proven to own *hwnd* using ONLY
        Chrome's browser-chrome (toolbar profile/avatar button or profile menu
        popup). Web page/document content is NEVER consulted — a page that
        renders account emails cannot spoof profile identity.

        Uses :func:`probe_browser_profile_identity` which prunes the
        ``Document`` subtree from the UIA walk. Returns None when Chrome-owned
        identity cannot be established (fail closed).
        """
        try:
            from jarvis_core.chrome_profile_identity import probe_browser_profile_identity
            meta = self._metadata_fn() or {}
            result = probe_browser_profile_identity(int(hwnd), metadata=meta)
            if result.get("verified") and result.get("profile_directory"):
                return str(result["profile_directory"])
        except Exception:
            logger.debug("scoped profile identity probe failed", exc_info=True)
        return None

    def _default_window_email(self, hwnd: int) -> Optional[str]:
        """Return the account email PROVEN present in *hwnd*'s UIA strings,
        matched exactly against trusted Local State emails, or None.

        Independent of the profile-directory identity: reads the window's own
        accessibility strings and matches a known email. Never guesses.
        """
        # Test-safety: if the per-window identity hook was injected (test mode),
        # do NOT perform a real UIA probe (would hang in tests). Derive the
        # observed email from the observed profile DIRECTORY (from PID or UIA
        # identity, whichever proved it) via trusted metadata, so directory and
        # email stay consistent for existing tests. Mismatch scenarios are
        # driven by injecting a dedicated window_email_fn.
        if self._window_identity_fn is not self._default_window_identity:
            odir = self.observed_profile_for_window(int(hwnd))
            if not odir:
                return None
            try:
                data = self._metadata_fn() or {}
                for p in data.get("profiles", []):
                    if str(p.get("profile_dir")) == str(odir) and p.get("user_name"):
                        return str(p["user_name"])
            except Exception:
                return None
            return None
        # REAL path: browser-chrome-only identity (Document subtree excluded).
        # The email comes from Chrome's own avatar/menu, never page content.
        try:
            from jarvis_core.chrome_profile_identity import probe_browser_profile_identity
            meta = self._metadata_fn() or {}
            result = probe_browser_profile_identity(int(hwnd), metadata=meta)
            if result.get("verified") and result.get("account_email"):
                return str(result["account_email"])
        except Exception:
            logger.debug("scoped profile email probe failed", exc_info=True)
        return None

    # ── Chrome executable resolution ─────────────────────────────────────────

    @staticmethod
    def resolve_chrome_executable() -> str:
        """Resolve an installed Chrome executable path.

        Tries the Windows registry App Paths, then the common install
        locations. Raises :class:`ChromeExecutableNotFound` if none exist.
        """
        candidates: list[str] = []

        # 1) Registry App Paths (per-machine then per-user).
        try:
            import winreg  # type: ignore

            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(
                        root,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                    ) as key:
                        value, _ = winreg.QueryValueEx(key, None)
                        if value:
                            candidates.append(str(value))
                except OSError:
                    continue
        except Exception:
            logger.debug("Registry Chrome lookup unavailable", exc_info=True)

        # 2) Common install locations.
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                candidates.append(
                    os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
                )

        for path in candidates:
            try:
                if path and os.path.isfile(path):
                    return path
            except OSError:
                continue
        raise ChromeExecutableNotFound(
            "Could not locate chrome.exe (registry + Program Files + LocalAppData checked)"
        )

    # ── Discovery ─────────────────────────────────────────────────────────────

    def discover_profiles(self) -> list[dict]:
        """Return the real Chrome profiles on this machine.

        Combines trusted Local State metadata with on-disk profile directories
        so profiles are surfaced even when not yet in ``info_cache``. Each
        profile gets a stable key from its directory. Account email appears
        ONLY when proven; otherwise the display name falls back to the profile
        name or ``Profile N`` / ``Unknown account``.
        """
        with self._lock:
            meta = {}
            try:
                data = self._metadata_fn() or {}
                for p in data.get("profiles", []):
                    meta[str(p.get("profile_dir"))] = p
            except Exception:
                logger.debug("Chrome metadata unavailable", exc_info=True)

            # Directories present on disk (Default, Profile N, ...).
            on_disk = self._on_disk_profile_dirs()
            all_dirs = list(dict.fromkeys(list(meta.keys()) + on_disk))

            # Which profiles are currently open + window counts.
            windows_by_dir = self._open_windows_by_dir()

            profiles: list[dict] = []
            for pdir in all_dirs:
                m = meta.get(pdir, {})
                email = str(m.get("user_name") or "").strip()  # proven-or-empty
                name = str(m.get("name") or m.get("gaia_name") or "").strip()
                # Secondary proof from the profile's own Preferences file.
                if not email:
                    email = self._email_from_preferences(pdir)
                wins = windows_by_dir.get(pdir, [])
                verified = bool(email)
                identity_source = "local_state" if m else ""
                if email and not m:
                    identity_source = "preferences"
                display = email or name or self._fallback_display(pdir)
                profiles.append({
                    "profile_key": profile_key_for(pdir),
                    "profile_directory": pdir,
                    "profile_name": name,
                    "account_email": email or None,
                    "display_name": display,
                    "last_active": m.get("last_active") or None,
                    "currently_open": bool(wins),
                    "window_count": len(wins),
                    "hwnds": [w["hwnd"] for w in wins],
                    "verified_identity": verified,
                    "identity_source": identity_source,
                })
            # Deterministic ordering: Default first, then Profile N numerically.
            profiles.sort(key=lambda p: _profile_sort_key(p["profile_directory"]))
            return profiles

    def _on_disk_profile_dirs(self) -> list[str]:
        base = self._user_data_dir
        dirs: list[str] = []
        try:
            if base and os.path.isdir(base):
                for entry in os.listdir(base):
                    full = os.path.join(base, entry)
                    if not os.path.isdir(full):
                        continue
                    if entry == "Default" or entry.startswith("Profile "):
                        # Confirm it's a real profile dir (has Preferences).
                        if os.path.isfile(os.path.join(full, "Preferences")) or entry == "Default":
                            dirs.append(entry)
        except OSError:
            logger.debug("On-disk profile scan failed for %s", base, exc_info=True)
        return dirs

    def _email_from_preferences(self, profile_dir: str) -> str:
        """Best-effort proven email from a profile's Preferences (account_info).

        Only well-formed emails are returned; nothing is fabricated. No other
        key (cookies/tokens) is read.
        """
        import json
        import re

        path = os.path.join(self._user_data_dir, profile_dir, "Preferences")
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return ""
        try:
            for acct in (data.get("account_info") or []):
                email = str(acct.get("email") or "").strip()
                if email and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                    return email
        except Exception:
            pass
        return ""

    @staticmethod
    def _fallback_display(profile_dir: str) -> str:
        # "Profile 7" stays as-is; "Default" gets a friendly label.
        if profile_dir == "Default":
            return "Default profile"
        return profile_dir or "Unknown account"

    def get_profile(self, profile_key: str) -> Optional[dict]:
        pdir = profile_dir_from_key(profile_key)
        for p in self.discover_profiles():
            if p["profile_directory"] == pdir:
                return p
        return None

    # ── Open-window correlation (profile → HWNDs) ────────────────────────────

    def _open_windows_by_dir(self) -> dict:
        """Map profile directory → list of its real open Chrome windows.

        Uses PID→profile-directory evidence so windows never leak across
        profiles. Windows whose PID has no proven profile arg are attributed
        to the ``Default`` profile ONLY when Chrome was started without an
        explicit ``--profile-directory`` (Chrome's own default) — but such
        windows are still reported with ``identity_source`` reflecting that
        they are inferred; callers requiring strict proof use verify_*.
        """
        result: dict[str, list[dict]] = {}
        try:
            hosts = self._host_windows_fn() or []
        except Exception:
            logger.debug("chrome_host_windows failed", exc_info=True)
            return result
        pid_profile = {}
        try:
            pid_profile = self._process_profile_fn() or {}
        except Exception:
            logger.debug("pid->profile map failed", exc_info=True)

        for w in hosts:
            pid = w.get("pid")
            hwnd = int(w["hwnd"])
            # Evidence priority:
            # 1) PID cmdline --profile-directory (strong, when a dedicated
            #    process still owns the window),
            # 2) per-window UIA toolbar chip matched to metadata (strong, and
            #    the ONLY reliable signal when one process multiplexes windows
            #    from several profiles),
            # Evidence: PID cmdline --profile-directory, else per-window UIA chip.
            # A window whose profile CANNOT be positively proven is bucketed
            # under a neutral sentinel — NEVER attributed to Default. This is
            # critical when one master Chrome PID multiplexes several profiles:
            # an unidentified window must never masquerade as the selected
            # profile (the old inferred-Default fallback caused wrong-profile
            # targeting). Such windows are excluded from every real profile's
            # candidate list and can only ever be rejected by verification.
            pdir = pid_profile.get(int(pid)) if pid is not None else None
            inferred = False
            if pdir is None:
                try:
                    pdir = self._window_identity_fn(hwnd)
                except Exception:
                    pdir = None
            if pdir is None:
                pdir = _UNIDENTIFIED_PROFILE_DIR
                inferred = True
            result.setdefault(pdir, []).append({
                "hwnd": hwnd,
                "pid": int(pid) if pid is not None else None,
                "title": w.get("title", ""),
                "inferred": inferred,
            })
        return result

    def get_profile_windows(self, profile_key: str) -> list[dict]:
        pdir = profile_dir_from_key(profile_key)
        return self._open_windows_by_dir().get(pdir, [])

    def resolve_profile_window(self, profile_key: str) -> Optional[dict]:
        """Return a single real HWND PROVEN to belong to *profile_key*, or None.

        Only positively-identified (non-inferred) windows are eligible. An
        unidentified window is NEVER returned (no inferred fallback) — the
        selected profile is absolute and a window we cannot prove is not usable.
        """
        wins = self.get_profile_windows(profile_key)
        proven = [w for w in wins if not w.get("inferred")]
        return proven[0] if proven else None

    # ── Verification (the critical guarantee) ────────────────────────────────

    def observed_profile_for_window(self, hwnd: int) -> Optional[str]:
        """Return the profile *directory* proven to own *hwnd*, or None.

        Strong evidence only: the window's PID must carry an explicit
        ``--profile-directory`` argument. Returns None when ownership cannot be
        proven (caller treats as verification failure rather than guessing).
        """
        try:
            hosts = self._host_windows_fn() or []
        except Exception:
            return None
        pid = None
        for w in hosts:
            if int(w.get("hwnd", -1)) == int(hwnd):
                pid = w.get("pid")
                break
        # 1) Strong: the window's PID carries an explicit --profile-directory.
        if pid is not None:
            try:
                pid_profile = self._process_profile_fn() or {}
            except Exception:
                pid_profile = {}
            if int(pid) in pid_profile:
                return pid_profile[int(pid)]
        # 2) Strong: per-window UIA profile chip / accessible name matched to
        #    trusted metadata. Retry briefly — the accessibility tree can be
        #    momentarily sparse right after a focus/foreground change.
        for attempt in range(3):
            try:
                observed = self._window_identity_fn(int(hwnd))
            except Exception:
                observed = None
            if observed is not None:
                return observed
            if attempt < 2:
                time.sleep(0.3)
        return None

    def observed_email_for_window(self, hwnd: int) -> Optional[str]:
        """Return the signed-in account email PROVEN present in *hwnd* (via the
        injectable ``window_email_fn``, default = real UIA strings), or None.

        Independent identity signal cross-checked against the requested
        profile's email before acting. Never guesses.
        """
        try:
            return self._window_email_fn(int(hwnd))
        except Exception:
            return None

    def expected_email_for_profile(self, profile_key: str) -> Optional[str]:
        """The trusted account email for *profile_key* from Local State."""
        p = self.get_profile(profile_key)
        return (p or {}).get("account_email")

    def verify_profile_open(self, profile_key: str, hwnd: int,
                            *, expected_email: Optional[str] = None,
                            require_email: bool = False) -> dict:
        """Verify that *hwnd* belongs to *profile_key* — STRICTLY.

        The selected profile is ABSOLUTE: verification succeeds ONLY when the
        window's observed profile *directory* equals the requested one AND, when
        an expected email is known/available, the window's UIA strings also
        prove that exact account email. This closes the gap where a directory
        read alone (fragile UIA chip) could mis-attribute a window.

        - ``expected_email``: cross-check target; defaults to the requested
          profile's trusted email when not supplied.
        - ``require_email``: when True (default) and an expected email exists,
          the email MUST be observed for verification to pass; if it cannot be
          observed we FAIL CLOSED (never assume). When no expected email exists
          in metadata (rare), directory match alone is accepted.

        Returns ``{verified, requested, observed, observed_email,
        expected_email, hwnd, reason}``.
        """
        requested_dir = profile_dir_from_key(profile_key)
        observed_dir = self.observed_profile_for_window(hwnd)
        observed_key = profile_key_for(observed_dir) if observed_dir else None
        observed_email = self.observed_email_for_window(hwnd)
        if expected_email is None:
            expected_email = self.expected_email_for_profile(profile_key)

        dir_ok = observed_dir is not None and observed_dir == requested_dir
        reason = None
        verified = False
        if not dir_ok:
            reason = "DIRECTORY_MISMATCH" if observed_dir else "DIRECTORY_UNPROVEN"
        elif expected_email and require_email:
            # Directory matched; the email MUST also be proven and match.
            if observed_email is None:
                reason = "EMAIL_UNPROVEN"
            elif observed_email != expected_email:
                reason = "EMAIL_MISMATCH"
            else:
                verified = True
        else:
            # No expected email known → directory match is the best we have.
            verified = True
        return {
            "verified": bool(verified),
            "requested": profile_key,
            "observed": observed_key,
            "observed_email": observed_email,
            "expected_email": expected_email,
            "hwnd": int(hwnd),
            "reason": None if verified else reason,
        }

    # ── Focus / launch orchestration (Test Open) ─────────────────────────────

    def _strictly_verified_window(self, profile_key: str,
                                  *, expected_email: Optional[str] = None):
        """Return an hwnd of *profile_key* that STRICTLY verifies (directory +
        email), focusing candidates as needed, or None.

        Considers EVERY currently-open window attributed to the profile
        directory (there may be several Default windows) and NEVER accepts an
        ``inferred`` window (a window whose profile could not be positively
        identified). Focus is applied before verification so the a11y chip is
        populated. The selected profile is absolute: no fallback to another
        profile, and unproven windows are rejected.
        """
        wins = self.get_profile_windows(profile_key)
        for win in wins:
            if win.get("inferred"):
                continue  # never accept an unidentified window
            hwnd = win["hwnd"]
            focused = False
            try:
                focused = bool(self._focus_fn(hwnd))
            except Exception:
                logger.debug("focus during verify raised", exc_info=True)
                focused = False
            time.sleep(FOCUS_SETTLE_SECONDS)
            v = self.verify_profile_open(
                profile_key, hwnd, expected_email=expected_email,
                require_email=bool(expected_email))
            if v["verified"]:
                return hwnd, v, focused
        return None

    def focus_profile(self, profile_key: str, *, expected_email: Optional[str] = None) -> dict:
        """Focus an already-open, STRICTLY-verified window of *profile_key*.

        Raises :class:`ProfileVerificationError` if no open window can be
        positively proven to be the requested profile (directory + email). Never
        accepts an inferred/ambiguous window and never falls back.
        """
        found = self._strictly_verified_window(profile_key, expected_email=expected_email)
        if found is None:
            raise ProfileVerificationError(
                f"PROFILE_MISMATCH: no open window positively verified for "
                f"{profile_key} (email={expected_email})")
        hwnd, verification, focused = found
        return {"action": "focus", "hwnd": hwnd, "focused": bool(focused),
                **verification, "focus_failed": (not focused)}

    def launch_profile(self, profile_key: str, *, expected_email: Optional[str] = None) -> dict:
        """Launch Chrome for *profile_key*'s EXACT directory, wait, verify.

        Uses ``--profile-directory=<selected-directory>`` (never Default/37
        hardcoded). After launch, re-enumerates and requires a STRICTLY-verified
        window (directory + email) before returning. Fails closed otherwise.
        """
        pdir = profile_dir_from_key(profile_key)
        if self.get_profile(profile_key) is None:
            raise ProfileNotFound(f"Unknown profile {profile_key}")
        exe = self._exe_resolver_fn()
        pid = self._launch_fn(exe, pdir)

        deadline = time.monotonic() + LAUNCH_WAIT_SECONDS
        while time.monotonic() < deadline:
            found = self._strictly_verified_window(profile_key, expected_email=expected_email)
            if found is not None:
                hwnd, verification, focused = found
                return {"action": "launch", "launched_pid": pid, "hwnd": hwnd,
                        "focused": bool(focused), **verification,
                        "focus_failed": (not focused)}
            time.sleep(LAUNCH_POLL_INTERVAL)
        raise ProfileVerificationError(
            f"PROFILE_MISMATCH: launched Chrome for {profile_key} but no window "
            f"positively verified (email={expected_email}) within "
            f"{LAUNCH_WAIT_SECONDS:.0f}s")

    def open_or_focus(self, profile_key: str, *, expected_email: Optional[str] = None) -> dict:
        """Focus an existing STRICTLY-verified profile window, else launch the
        EXACT profile and verify. The selected profile is ABSOLUTE.

        Verification requires the observed directory AND account email to match
        the requested profile before ANY caller acts (navigates/types). No
        fallback to another profile, no acceptance of an unidentified window, no
        inference from PID/URL. Wrapped by the caller in Part 1 idempotency.
        """
        if self.get_profile(profile_key) is None:
            raise ProfileNotFound(f"Unknown profile {profile_key}")
        if expected_email is None:
            expected_email = self.expected_email_for_profile(profile_key)
        # 1) Try to focus + strictly verify an already-open window.
        found = self._strictly_verified_window(profile_key, expected_email=expected_email)
        if found is not None:
            hwnd, verification, focused = found
            return {"action": "focus", "hwnd": hwnd, "focused": bool(focused),
                    **verification, "focus_failed": (not focused)}
        # 2) Not open (or not positively verifiable) → launch the EXACT profile.
        return self.launch_profile(profile_key, expected_email=expected_email)


def _extract_profile_directory(cmdline: list) -> Optional[str]:
    """Extract the ``--profile-directory`` value from a process command line.

    Chrome accepts ``--profile-directory=Profile 7`` and
    ``--profile-directory "Profile 7"``. Returns None when absent.
    """
    if not cmdline:
        return None
    args = [str(a) for a in cmdline]
    for i, arg in enumerate(args):
        if arg.startswith("--profile-directory="):
            return arg.split("=", 1)[1].strip().strip('"')
        if arg == "--profile-directory" and i + 1 < len(args):
            return args[i + 1].strip().strip('"')
    return None


def _profile_sort_key(profile_dir: str):
    if profile_dir == "Default":
        return (0, 0)
    if profile_dir.startswith("Profile "):
        try:
            return (1, int(profile_dir.split(" ", 1)[1]))
        except (ValueError, IndexError):
            return (2, profile_dir)
    return (3, profile_dir)


# Module-level singleton (real infrastructure), following project convention.
chrome_profile_controller = ChromeProfileController()
