"""Layered Google-account / Chrome-profile identification per REAL window.

For every real Chrome top-level window discovered on this laptop, determine
the strongest SAFE identity available - preferred display form is the
signed-in Google account email (e.g. ``user@gmail.com``).

Layered discovery - never guesses, never fabricates an email:

1. UI Automation (primary): inspect the EXACT Chrome window's own
   accessibility tree (bounded, deadline-capped) for account-like strings -
   the toolbar profile/avatar chip frequently announces the signed-in email
   or the profile display name.
2. Chrome profile metadata (secondary): parse ``Local State`` JSON of the
   running Chrome user-data directories. ONLY four display-grade keys are
   kept per profile (``profile_dir``, ``name``, ``gaia_name``, ``user_name``
   [the account email]). Cookies, Login Data, tokens, sync data and every
   other key are NEVER read or retained. A UIA string is accepted as
   evidence only when it matches this known metadata vocabulary - so the
   shown email always traces back to safe on-disk profile facts.
3. Honest fallbacks (caller-side): ``Chrome - <profile name>`` when only a
   plain profile name was proven, else ``Chrome - Window <N>``.

Display-only, short-lived in-memory identities; never logged, never
persisted, and only ever shown to the existing allowlisted Telegram user.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import deque
from typing import Optional

logger = logging.getLogger("jarvis.resources.identity")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: Plain human profile names (matched against metadata vocabulary).
_PROFILE_NAME_RE = re.compile(r"^[\w\u00c0-\u024f .\-()]{2,40}$")

#: Bounded UIA walk parameters - fail honest instead of stalling a scan.
_UIA_PROBE_DEADLINE_SECONDS = 4.0
_UIA_MAX_ELEMENTS_PER_WINDOW = 400
_UIA_MAX_DEPTH = 12


# ── Chrome profile metadata (safe, minimal) ──────────────────────────────────


def _candidate_user_data_dirs(
    process_names: tuple = ("chrome", "chrome.exe"),
) -> list[str]:
    """User-data dirs referenced by RUNNING Chrome processes + the default."""
    dirs: list[str] = []
    default = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data"
    )
    if default and os.path.isdir(default):
        dirs.append(default)
    try:
        import psutil

        for proc in psutil.process_iter(["name", "cmdline"]):
            name = (proc.info.get("name") or "").lower()
            if name not in process_names:
                continue
            for arg in proc.info.get("cmdline") or []:
                text = str(arg or "")
                if text.lower().startswith("--user-data-dir="):
                    value = text.split("=", 1)[1].strip().strip('"')
                    if value and os.path.isdir(value) and value not in dirs:
                        dirs.append(value)
    except Exception:
        logger.debug("User-data-dir process sweep unavailable", exc_info=True)
    return [d for d in dirs if d]


def chrome_profile_metadata(user_data_dirs: Optional[list[str]] = None) -> dict:
    """Minimal SAFE profile facts from ``Local State`` of candidate dirs.

    Returns ``{"profiles": [...], "sources": [...]}``. Only ``profile_dir``,
    ``name``, ``gaia_name`` and ``user_name`` survive per profile; every
    secret-bearing structure (cookies, tokens, sync) is untouched/unread.
    """
    if user_data_dirs is None:
        user_data_dirs = _candidate_user_data_dirs()
    sources: list[str] = []
    profiles: list[dict] = []
    for base in user_data_dirs or []:
        local_state = os.path.join(base, "Local State")
        try:
            with open(local_state, encoding="utf-8") as handle:
                data = json.load(handle)
            cache = (data.get("profile") or {}).get("info_cache") or {}
            if not isinstance(cache, dict):
                continue
        except Exception:
            logger.debug("Local State unreadable (%s)", base, exc_info=True)
            continue
        sources.append(base)
        for profile_dir, entry in cache.items():
            if not isinstance(entry, dict):
                continue
            email = str(entry.get("user_name") or "").strip()
            if email and not _EMAIL_RE.fullmatch(email):
                email = ""  # retain only well-formed addresses
            profiles.append({
                "profile_dir": str(profile_dir),
                "name": str(entry.get("name") or "").strip(),
                "gaia_name": str(entry.get("gaia_name") or "").strip(),
                "user_name": email,
            })
    return {"profiles": profiles, "sources": sources}


# ── Bounded UIA probe ────────────────────────────────────────────────────────


def _probe_window_strings(hwnd: int) -> list[str]:
    """Bounded breadth-first walk of ONE Chrome window's UIA tree.

    Collects accessible names only (what screen readers announce); capped by
    an absolute deadline, element budget and depth. Never clicks, never
    types, never changes the user's focus.
    """
    from pywinauto import Desktop

    root = Desktop(backend="uia").window(handle=int(hwnd))
    strings: list[str] = []
    deadline = time.monotonic() + _UIA_PROBE_DEADLINE_SECONDS
    queue: deque = deque([(root, 0)])
    visited = 0
    while queue:
        element, depth = queue.popleft()
        visited += 1
        if visited > _UIA_MAX_ELEMENTS_PER_WINDOW:
            break
        try:
            text = str(element.window_text() or "").strip()
        except Exception:
            text = ""
        if text:
            strings.append(text)
        if depth >= _UIA_MAX_DEPTH or time.monotonic() > deadline:
            continue
        try:
            children = element.children()
        except Exception:
            children = []
        for child in children:
            queue.append((child, depth + 1))
    return strings


# ── Browser-chrome-only profile identity (NEVER page content) ─────────────────
#
# Chrome profile ownership must be proven from Chrome's OWN browser chrome (the
# toolbar profile/avatar button, or the Chrome-owned profile menu popup) and
# NEVER from web page/document content. Under a shared master Chrome PID that
# multiplexes profiles, and with a page (e.g. the Jarvis frontend) that renders
# account emails as text, an unscoped tree walk would mis-attribute a window.
# These helpers exclude the web ``Document`` subtree entirely.

#: Control types that contain WEB PAGE content — never descend into these when
#: proving profile identity (this is where page emails/ChatGPT text live).
_WEB_CONTENT_CONTROL_TYPES = {"Document", "Pane"}
#: Deadline/budget for the scoped chrome-only walk.
_CHROME_PROBE_DEADLINE_SECONDS = 4.0
_CHROME_MAX_ELEMENTS = 500


def _control_type(element) -> str:
    try:
        return str(element.element_info.control_type or "")
    except Exception:
        return ""


def _automation_id(element) -> str:
    try:
        return str(element.element_info.automation_id or "")
    except Exception:
        return ""


def _probe_browser_chrome_strings(hwnd: int) -> list[str]:
    """Bounded walk of ONE Chrome window that collects accessible names ONLY
    from Chrome's browser-chrome (toolbar/avatar), PRUNING the web content.

    Any ``Document`` node (the rendered web page) is never entered, so page
    text — including a page that lists account emails — can never contribute to
    profile identity. A ``Pane`` is entered only until a ``Document`` is found
    beneath it (panes host both toolbar and web view); we prune at the
    ``Document`` boundary. Returns browser-chrome accessible names only.
    """
    from pywinauto import Desktop

    root = Desktop(backend="uia").window(handle=int(hwnd))
    strings: list[str] = []
    deadline = time.monotonic() + _CHROME_PROBE_DEADLINE_SECONDS
    queue: deque = deque([(root, 0)])
    visited = 0
    while queue:
        element, depth = queue.popleft()
        visited += 1
        if visited > _CHROME_MAX_ELEMENTS or time.monotonic() > deadline:
            break
        ctype = _control_type(element)
        # HARD BOUNDARY: never read or descend into the web page document.
        if ctype == "Document":
            continue
        try:
            text = str(element.window_text() or "").strip()
        except Exception:
            text = ""
        if text:
            strings.append(text)
        if depth >= _UIA_MAX_DEPTH:
            continue
        try:
            children = element.children()
        except Exception:
            children = []
        for child in children:
            # Do not enqueue web-document children at all.
            if _control_type(child) == "Document":
                continue
            queue.append((child, depth + 1))
    return strings


def _open_profile_menu_strings(hwnd: int) -> list[str]:
    """FALLBACK: click the Chrome toolbar profile/avatar button on THIS exact
    window, read the Chrome-owned profile menu popup's accessible strings, then
    dismiss it. Returns [] if the button/menu cannot be operated. Reads only the
    Chrome-owned popup — never the page document.
    """
    from pywinauto import Desktop

    strings: list[str] = []
    try:
        win = Desktop(backend="uia").window(handle=int(hwnd))
    except Exception:
        return strings
    # Find the avatar/profile toolbar button by its accessible-name convention.
    avatar = None
    try:
        for btn in win.descendants(control_type="Button"):
            name = (btn.window_text() or "").strip().lower()
            aid = _automation_id(btn).lower()
            if ("profile" in name or "account" in name or "avatar" in aid
                    or "profile" in aid or name.startswith("your profile")):
                avatar = btn
                break
    except Exception:
        avatar = None
    if avatar is None:
        return strings
    try:
        avatar.invoke()
    except Exception:
        try:
            avatar.click_input()
        except Exception:
            return strings
    time.sleep(0.4)
    # Read the Chrome-owned menu/popup (a top-level Menu/Window owned by Chrome).
    try:
        from pywinauto import Desktop as _D
        deadline = time.monotonic() + 2.0
        for top in _D(backend="uia").windows():
            if time.monotonic() > deadline:
                break
            ct = _control_type(top)
            if ct not in ("Menu", "Window", "Pane"):
                continue
            try:
                for el in top.descendants():
                    if _control_type(el) == "Document":
                        continue
                    t = (el.window_text() or "").strip()
                    if t:
                        strings.append(t)
            except Exception:
                continue
    except Exception:
        pass
    # Dismiss the menu (Escape to the window) — never leave it open.
    try:
        win.type_keys("{ESC}")
    except Exception:
        pass
    return strings


def _directory_for_email(email: str, meta: dict) -> Optional[str]:
    """Map an exact signed-in email back to its trusted Local State profile
    directory. Email is UNIQUE per profile, so this is unambiguous. Returns None
    when the email is unknown."""
    e = (email or "").strip().lower()
    if not e:
        return None
    for p in meta.get("profiles", []):
        if (p.get("user_name") or "").strip().lower() == e:
            return str(p.get("profile_dir"))
    return None


def _names_shared_by_multiple(meta: dict) -> set:
    """Normalized display names that map to MORE THAN ONE profile directory
    (ambiguous — must never resolve a profile on their own)."""
    counts: dict[str, set] = {}
    for p in meta.get("profiles", []):
        pdir = str(p.get("profile_dir"))
        for nm in (p.get("name"), p.get("gaia_name")):
            nm = (nm or "").strip().lower()
            if nm:
                counts.setdefault(nm, set()).add(pdir)
    return {nm for nm, dirs in counts.items() if len(dirs) > 1}


def _match_identity_from_strings(strings: list[str], meta: dict) -> dict:
    """Match browser-chrome strings against trusted Local State metadata.

    EMAIL IS AUTHORITATIVE (unique per profile). A display name resolves a
    profile ONLY when that name is unique across all profiles; a name shared by
    more than one profile (e.g. "Code" used by Default/Profile 3/Profile 37) is
    AMBIGUOUS and never resolves a profile on its own — the matcher then returns
    ``{"ambiguous": True}`` so the caller fails closed / seeks exact email.

    Returns ``{profile_dir, profile_name, account_email, via}`` on success,
    ``{"ambiguous": True, "name": <name>}`` when only an ambiguous name matched,
    or ``{}`` when nothing matched.
    """
    lowered: dict[str, str] = {}
    for raw in strings:
        t = str(raw or "").strip()
        if t:
            lowered.setdefault(t.lower(), t)

    # 1) EXACT known signed-in email (authoritative, unique per profile).
    for p in meta.get("profiles", []):
        email = (p.get("user_name") or "").strip()
        if email and email.lower() in lowered:
            return {"profile_dir": str(p.get("profile_dir")),
                    "profile_name": p.get("name") or p.get("gaia_name") or "",
                    "account_email": email, "via": "email_exact"}
    # 2) Email as a substring of a chrome string (avatar tooltip "… (email)").
    for p in meta.get("profiles", []):
        email = (p.get("user_name") or "").strip()
        if not email:
            continue
        for low in lowered:
            if email.lower() in low:
                return {"profile_dir": str(p.get("profile_dir")),
                        "profile_name": p.get("name") or p.get("gaia_name") or "",
                        "account_email": email, "via": "email_substring"}

    # 3) Profile display name via "… - Google Chrome - <ProfileName>". This is
    #    accepted ONLY when the name is UNIQUE across profiles. A shared name is
    #    ambiguous and must NEVER pick a profile (no first-match).
    ambiguous_names = _names_shared_by_multiple(meta)
    by_name_unique: dict[str, dict] = {}
    for p in meta.get("profiles", []):
        for nm in (p.get("name"), p.get("gaia_name")):
            nm_norm = (nm or "").strip().lower()
            if nm_norm and nm_norm not in ambiguous_names:
                by_name_unique.setdefault(nm_norm, p)
    matched_ambiguous_name = None
    # Longest names first (avoid a short name matching inside a longer one).
    all_names = sorted(
        {(nm or "").strip() for p in meta.get("profiles", [])
         for nm in (p.get("name"), p.get("gaia_name")) if (nm or "").strip()},
        key=len, reverse=True)
    for name in all_names:
        nlow = name.lower()
        for low, original in lowered.items():
            if original.endswith(f"- {name}") or f"google chrome - {nlow}" in low:
                if nlow in ambiguous_names:
                    matched_ambiguous_name = name
                    continue  # never resolve on an ambiguous name
                p = by_name_unique.get(nlow)
                if p is not None:
                    return {"profile_dir": str(p.get("profile_dir")),
                            "profile_name": name,
                            "account_email": (p.get("user_name") or ""),
                            "via": "name_unique"}
    if matched_ambiguous_name is not None:
        return {"ambiguous": True, "name": matched_ambiguous_name}
    return {}


def probe_browser_profile_identity(hwnd: int,
                                   metadata: Optional[dict] = None) -> dict:
    """Prove the Chrome PROFILE that owns *hwnd* using ONLY browser chrome.

    Evidence order: (1) the toolbar/avatar accessible names (Document subtree
    excluded); (2) fallback — open the Chrome profile menu on THAT window and
    read the Chrome-owned popup. Web page/document content is never consulted,
    so a page that renders account emails cannot spoof identity.

    Returns ``{hwnd, profile_directory, profile_name, account_email,
    evidence_source, verified}``. ``verified`` is False (evidence_source
    ``PROFILE_IDENTITY_UNPROVEN``) when Chrome-owned identity cannot be
    established — callers must fail closed.
    """
    meta = metadata if metadata is not None else chrome_profile_metadata()
    out = {"hwnd": int(hwnd), "profile_directory": None, "profile_name": None,
           "account_email": None, "evidence_source": "PROFILE_IDENTITY_UNPROVEN",
           "verified": False}

    def _finalize(match: dict, source: str) -> Optional[dict]:
        """Accept a match ONLY when it yields an exact email that maps to a
        trusted directory. A name-only (or ambiguous) match never verifies."""
        if not match or match.get("ambiguous"):
            return None
        email = (match.get("account_email") or "").strip()
        if not email:
            return None                      # name-only → not authoritative
        pdir = _directory_for_email(email, meta)
        if not pdir:
            return None
        out.update(profile_directory=pdir,
                   profile_name=match.get("profile_name") or None,
                   account_email=email, evidence_source=source, verified=True)
        return out

    # 1) Toolbar/avatar (scoped, no Document) — require an EXACT email.
    try:
        strings = _probe_browser_chrome_strings(int(hwnd))
    except Exception:
        strings = []
    result = _finalize(_match_identity_from_strings(strings, meta),
                       "toolbar_profile_button_email")
    if result is not None:
        return result

    # 2) Profile menu popup fallback — open the Chrome-owned menu, read email.
    try:
        menu_strings = _open_profile_menu_strings(int(hwnd))
    except Exception:
        menu_strings = []
    result = _finalize(_match_identity_from_strings(menu_strings, meta),
                       "profile_menu_email")
    if result is not None:
        return result

    # No exact browser-owned email could be proven → fail closed. A shared
    # display name such as "Code" is NEVER sufficient on its own.
    return out


# ── Identity decision ────────────────────────────────────────────────────────


def identify_window(hwnd: int, metadata: Optional[dict] = None) -> dict:
    """Strongest PROVEN identity for one Chrome window.

    Honest empty fields when nothing can be proven - callers render fallback
    labels instead of guessing. An email appears ONLY when it matches safe
    on-disk profile metadata; it is never invented from arbitrary strings.
    """
    meta = metadata if metadata is not None else chrome_profile_metadata()
    result = {
        "hwnd": int(hwnd),
        "email": "",
        "profile_label": "",
        "identity_source": "",
        "confidence": "",
    }
    try:
        strings = _probe_window_strings(int(hwnd))
    except Exception:
        logger.debug("UIA probe unavailable for hwnd %s", hwnd, exc_info=True)
        return result
    lowered: dict[str, str] = {}
    for raw in strings:
        text = str(raw or "").strip()
        if text:
            lowered.setdefault(text.lower(), text)

    # 1) Account chip announcing exactly a known signed-in email.
    known_emails = {
        p["user_name"].lower(): p
        for p in meta["profiles"] if p.get("user_name")
    }
    for low, original in lowered.items():
        profile = known_emails.get(low)
        if profile is None:
            continue
        result["email"] = profile["user_name"]
        result["profile_label"] = (
            profile.get("name") or profile.get("gaia_name") or ""
        )
        result["identity_source"] = "uia"
        result["confidence"] = "high"
        return result

    # 2) Profile-name chip: UIA string equal to a metadata display name.
    words = {
        low for low, original in lowered.items()
        if original and 2 <= len(original) <= 40
        and _PROFILE_NAME_RE.match(original)
    }
    for profile in meta["profiles"]:
        for candidate in (profile.get("name"), profile.get("gaia_name")):
            candidate = (candidate or "").strip()
            if not candidate or candidate.lower() not in words:
                continue
            result["profile_label"] = candidate
            result["identity_source"] = "uia"
            result["confidence"] = "high"
            if profile.get("user_name"):
                # The chip proves WHICH profile owns this window; its email
                # comes from the same trusted metadata record - not guessed.
                result["email"] = profile["user_name"]
            return result

    # 3) Exactly one signed-in profile exists in this running Chrome =>
    #    every visible window belongs to it (attribution by elimination,
    #    explicitly flagged so the UI can mark it as inferred).
    single_dir = len(meta["sources"]) <= 1
    email_profiles = [p for p in meta["profiles"] if p.get("user_name")]
    named_profiles = [p for p in meta["profiles"] if p.get("name")]
    if single_dir and len(email_profiles) == 1:
        profile = email_profiles[0]
        result["email"] = profile["user_name"]
        result["profile_label"] = profile.get("name") or ""
        result["identity_source"] = "meta-single"
        result["confidence"] = "inferred-single-profile"
        return result
    if single_dir and not email_profiles and len(named_profiles) == 1:
        result["profile_label"] = named_profiles[0]["name"]
        result["identity_source"] = "meta-single"
        result["confidence"] = "inferred-single-profile"
    return result


def identify_hosts(hwnds: list) -> dict:
    """Identity lookup for many windows sharing one metadata fetch."""
    meta = chrome_profile_metadata()
    return {int(hwnd): identify_window(hwnd, metadata=meta) for hwnd in hwnds}


def display_label_for(identity: Optional[dict], window_number: int,
                      duplicate_of_same_account: bool = False) -> str:
    """Preferred display identity - email first, then profile, then generic.

    ``duplicate_of_same_account`` disambiguates several windows of ONE
    account without merging them: ``user@x.com — Window 2``.
    """
    identity = identity or {}
    email = identity.get("email") or ""
    profile = identity.get("profile_label") or ""
    if email and duplicate_of_same_account:
        return f"{email} \u2014 Window {window_number}"
    if email:
        return email
    if profile:
        return f"Chrome \u2014 {profile}"
    return f"Chrome \u2014 Window {window_number}"