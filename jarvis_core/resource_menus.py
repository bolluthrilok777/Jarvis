"""Telegram menu builders for the Phase-1 Resource Review panel.

Rendered ONLY for the allowlisted Telegram resource-review flow; every payload
here is the identity-bearing scan (``scan_result(include_identity=True)``).

Spec contract (PHASE 1):

* main ``\U0001f9e9 RESOURCE REVIEW`` dashboard separates Applications and Chrome;
* Chrome tabs are grouped by account/profile identity (email preferred,
  honest fallback ``Profile N`` when unavailable) with ACCURATE per-profile
  tab counts (the count always equals the tabs currently detected);
* Chrome expand / collapse / app detail / tab detail / Back / Refresh /
  Continue / Cancel are all rendered on the SAME Telegram message
  (``editMessageText``), never stacked new messages;
* callbacks only carry COMPACT server-side tokens (resource ids that are
  generation-stamped ``r<gen>-a<hwnd>`` / ``r<gen>-t<idx>`` and profile
  tokens ``g<gen>-p<idx>``), never long titles; stale generation tokens are
  rejected by the handler before any action.

Nothing here persists, logs, or exposes tokens/cookies/sync secrets.
"""
from __future__ import annotations

from typing import Optional

#: Telegram stays comfortable well below its hard limits with these cap pages.
_TABS_PER_PAGE = 20
_OVERVIEW_ENTRIES_PER_PAGE = 30

_APP_EXE_BASENAMES = ("chrome.exe", "chrome")


def _nav(feature_id: Optional[str], action: str) -> str:
    return f"feature:{action}:{feature_id or ''}"


def _navx(action: str) -> str:
    """Feature-context navigation callback that carries NO feature id.

    Telegram rejects the WHOLE inline keyboard (Bad Request:
    BUTTON_DATA_INVALID) if ANY callback_data exceeds 64 UTF-8 bytes. A 36-char
    feature UUID pushed several section/hide/refresh callbacks (e.g.
    ``resrc_collapse_chrome:<uuid>`` = 66 bytes) over the limit, which is why
    tapping the Chrome heading / an application silently failed while the
    shorter Refresh/Back callbacks still worked. These navigation actions do
    NOT need the id in the payload: the handler resolves the active review
    feature server-side via the resource registry (``_feature_ctx()``). Keeping
    the payload id-free guarantees every callback stays well under 64 bytes for
    any feature id length. (Continue/Cancel still carry the exact feature id
    because the orchestrator acts on it, and those callbacks stay <64 bytes.)
    """
    return f"feature:{action}:"


def _short(label: str, limit: int = 36) -> str:
    label = " ".join(str(label or "").split())
    return label if len(label) <= limit else label[: limit - 1] + "\u2026"


def _is_chrome_app(app: dict) -> bool:
    if "chrome" in (app.get("display_name") or "").lower():
        return True
    basename = (app.get("executable") or "").rsplit("\\", 1)[-1].lower()
    return basename in _APP_EXE_BASENAMES


def _chrome_windows(scan: dict) -> list[dict]:
    """One entry per REAL Chrome top-level window in the CURRENT scan.

    Windows are derived from the exact ``host_hwnd`` of discovered tabs plus
    Chrome application-level windows, deduplicated by HWND and ordered by
    HWND so ordinal labels stay stable across refreshes. Identity labels are
    the registry's pre-computed evidence-ladder results; no label is ever
    invented here beyond the positional ``Chrome Window N`` fallback.
    """
    windows: dict[int, dict] = {}
    for tab in scan.get("tabs") or []:
        # UIA tabs carry the owning Chrome window HWND; CDP tabs do NOT
        # (they only have a page/target id). Bucket hwnd-less CDP tabs into a
        # synthetic window (key 0) so real, controllable tabs are still listed
        # and clickable - otherwise the expanded Chrome panel would show the
        # correct count in its heading but render zero tab rows.
        raw_hwnd = tab.get("host_hwnd")
        if raw_hwnd:
            try:
                hwnd = int(raw_hwnd)
            except (TypeError, ValueError):
                hwnd = 0
        else:
            hwnd = 0
        entry = windows.setdefault(
            hwnd, {"hwnd": hwnd, "tabs": [], "label": ""}
        )
        entry["tabs"].append(tab)
        label = (tab.get("identity_label") or "").strip()
        if not entry["label"] and label:
            entry["label"] = label
    for app in scan.get("applications") or []:
        if not _is_chrome_app(app):
            continue
        handle = app.get("window_handle") or app.get("host_hwnd")
        if not handle:
            continue
        try:
            hwnd = int(handle)
        except (TypeError, ValueError):
            continue
        entry = windows.setdefault(
            hwnd, {"hwnd": hwnd, "tabs": [], "label": ""}
        )
        label = (app.get("identity_label") or "").strip()
        if not entry["label"] and label:
            entry["label"] = label
    return [windows[hwnd] for hwnd in sorted(windows)]


def _window_label(entry: dict, window_number: int) -> str:
    """Evidence-ladder identity label with an honest positional fallback."""
    label = (entry.get("label") or "").strip()
    return label if label else f"Chrome Window {window_number}"


# ---------------------------------------------------------------------------
# Chrome account/profile grouping
# ---------------------------------------------------------------------------


def chrome_profiles(scan: dict) -> list[dict]:
    """Chrome tabs grouped by account/profile identity.

    Returns one dict per REAL profile seen in the CURRENT scan:

        {
          "token": "g<generation>-p<idx>",
          "email": "...",            # proven email or ""
          "profile_label": "...",    # proven profile display name or ""
          "label": "user@gmail.com", # best display string
          "count": N,                # EXACT tabs detected for this profile
          "tabs": [tab...],
        }

    Grouping key: proven ``account_email`` -> ``profile_label`` ->
    ``identity_label`` -> generic Chrome. An email is NEVER fabricated; the
    identity strings come from the registry's evidence ladder only.
    """
    buckets: dict[str, dict] = {}
    for tab in scan.get("tabs") or []:
        email = (tab.get("account_email") or "").strip()
        profile_label = (tab.get("profile_label") or "").strip()
        identity_label = (tab.get("identity_label") or "").strip()
        if email:
            key = "email:" + email.lower()
            label = email
            is_unknown = False
        elif profile_label:
            key = "profile:" + profile_label.lower()
            label = profile_label
            is_unknown = False
        elif identity_label:
            key = "id:" + identity_label.lower()
            label = identity_label
            is_unknown = False
        else:
            # No resolvable Chrome profile/account identity - these tabs are
            # NEVER dropped; they are grouped under "Other / Unknown".
            key = "unknown"
            label = "Other / Unknown"
            is_unknown = True
        entry = buckets.setdefault(
            key, {
                "token": "", "key": key, "is_unknown": is_unknown,
                "email": email, "profile_label": profile_label,
                "label": label, "count": 0, "tabs": [],
            }
        )
        entry["tabs"].append(tab)
        entry["count"] = len(entry["tabs"])

    gen = int((scan or {}).get("generation") or 0)
    # Known accounts first (alphabetical), the Unknown bucket always last.
    ordered = sorted(
        buckets.values(),
        key=lambda p: (p["is_unknown"], (p["label"] or "").lower()),
    )
    for idx, profile in enumerate(ordered, 1):
        profile["token"] = f"g{gen}-p{idx}"
        # Record token -> STABLE profile identity key so a later refresh can
        # re-match the SAME profile even after the generation/order changes.
        # Position is never used as identity.
        _remember_account_token(profile["token"], profile["key"])
    return ordered


# ── Chrome account navigation-token -> stable-identity registry ──────────────
#
# Telegram callback_data must stay short (<=64 bytes), so account buttons carry
# only a compact ``g<gen>-p<idx>`` navigation token. Persistent account context
# (used by Refresh/Close to re-find the SAME profile after a rescan) is the
# proven STABLE identity key (``email:`` / ``profile:`` / ``id:``), kept here in
# a small bounded in-memory map. Account list ORDER is never used as identity.
_ACCOUNT_TOKEN_KEYS: dict[str, str] = {}
_ACCOUNT_TOKEN_ORDER: list[str] = []
_ACCOUNT_TOKEN_MAX = 512


def _remember_account_token(token: str, stable_key: str) -> None:
    if not token or not stable_key:
        return
    if token not in _ACCOUNT_TOKEN_KEYS:
        _ACCOUNT_TOKEN_ORDER.append(token)
        if len(_ACCOUNT_TOKEN_ORDER) > _ACCOUNT_TOKEN_MAX:
            old = _ACCOUNT_TOKEN_ORDER.pop(0)
            _ACCOUNT_TOKEN_KEYS.pop(old, None)
    _ACCOUNT_TOKEN_KEYS[token] = stable_key


def lookup_stable_key(token: str) -> str:
    """Stable profile identity key previously bound to a navigation token."""
    return _ACCOUNT_TOKEN_KEYS.get(token or "", "")


def count_chrome_accounts(scan: dict) -> int:
    """Number of Chrome accounts/profiles seen in a scan snapshot."""
    return len(chrome_profiles(scan or {}))


def _profile_display_label(profile: dict, index: int) -> str:
    """Best display label: email / profile name / ``Profile N`` fallback."""
    if profile.get("email"):
        return profile["email"]
    if profile.get("profile_label"):
        return profile["profile_label"]
    return f"Profile {index}"


def _account_icon(profile: dict) -> str:
    """📧 for a resolved Gmail/profile account, 👤 for Other / Unknown."""
    return "\U0001f464" if profile.get("is_unknown") else "\U0001f4e7"


def resolve_chrome_account(scan: dict, token: str) -> Optional[dict]:
    """Resolve a Chrome account token to its profile group in the CURRENT scan.

    Tokens are ``g<generation>-p<idx>`` (generation-stamped, short, always well
    under Telegram's 64-byte callback limit). Matching first tries the exact
    token; if the generation advanced (e.g. after a Refresh) it re-matches by
    the STABLE identity key (proven email / profile / identity label) so the
    same real Gmail account survives a rescan. Returns ``None`` only when that
    account no longer exists at all.
    """
    profiles = chrome_profiles(scan or {})
    if not token:
        return None
    for p in profiles:
        if p.get("token") == token:
            return p
    # Token generation drifted. Resolve the token to its STABLE profile key via
    # the token->stable-key registry captured at render time, then re-match by
    # that stable identity. Position/index is NEVER used as identity, so a
    # changed account ordering can never redirect a stale token to a different
    # profile.
    stable_key = lookup_stable_key(token)
    if stable_key:
        return resolve_chrome_account_by_key(scan, stable_key)
    return None


def resolve_chrome_account_by_key(scan: dict, key: str) -> Optional[dict]:
    """Re-match a Chrome account by its STABLE identity key across a rescan."""
    if not key:
        return None
    for p in chrome_profiles(scan or {}):
        if p.get("key") == key:
            return p
    return None
# ---------------------------------------------------------------------------
# Icons (cosmetic only - never part of a resource identity)
# ---------------------------------------------------------------------------


def _tab_sort_key(tab: dict):
    try:
        return int(tab.get("tab_index") or 0)
    except (TypeError, ValueError):
        return 0


_TAB_ICON_RULES = (
    ("gmail", "\U0001f4e7"),
    ("mail.google", "\U0001f4e7"),
    ("chatgpt", "\U0001f916"),
    ("openai", "\U0001f916"),
    ("new tab", "\u2795"),
    ("jarvis", "\U0001f3e0"),
    ("whatsapp", "\U0001f4ac"),
    ("web.whatsapp", "\U0001f4ac"),
    ("google calendar", "\U0001f4c5"),
    ("calendar", "\U0001f4c5"),
    ("google drive", "\U0001f4c1"),
    ("drive", "\U0001f4c1"),
    ("docs.google", "\U0001f4c1"),
    ("sheets.google", "\U0001f4c1"),
    ("slides.google", "\U0001f4c1"),
    ("github", "\U0001f419"),
    ("youtube", "\u25b6\ufe0f"),
    ("google", "\U0001f50d"),
    ("search", "\U0001f50d"),
)


def _tab_icon(title: str) -> str:
    """Cosmetic icon from an OBVIOUS tab title; unknown -> globe."""
    t = (title or "").lower()
    for keyword, icon in _TAB_ICON_RULES:
        if keyword in t:
            return icon
    return "\U0001f310"


_APP_ICON_RULES = [
    ("whatsapp", "\U0001f4ac"),
    ("notepad", "\U0001f4dd"),
    ("terminal", "\U0001f5a5\ufe0f"),
    ("cmd", "\U0001f5a5\ufe0f"),
    ("powershell", "\U0001f5a5\ufe0f"),
    ("pwsh", "\U0001f5a5\ufe0f"),
    ("bash", "\U0001f5a5\ufe0f"),
    ("wsl", "\U0001f5a5\ufe0f"),
    ("calculator", "\U0001f9ee"),
    ("explorer", "\U0001f4c1"),
    ("word", "\U0001f4c4"),
    ("excel", "\U0001f4ca"),
    ("powerpoint", "\U0001f4d1"),
    ("outlook", "\U0001f4e7"),
    ("slack", "\U0001f4ac"),
    ("spotify", "\U0001f3b5"),
    ("obsidian", "\U0001f9e9"),
    ("notion", "\U0001f9e9"),
    ("vscode", "\U0001f9e9"),
    ("studio", "\U0001f9e9"),
    ("code", "\U0001f9e9"),
]


def _app_icon(app: dict) -> str:
    """Cosmetic icon for an application row; unknown apps default to a puzzle."""
    name = (app.get("display_name") or "").lower()
    title = (app.get("title") or "").lower()
    exe = (app.get("executable") or "").rsplit("\\", 1)[-1].lower()
    hay = f"{name} {exe} {title}"
    for keyword, icon in _APP_ICON_RULES:
        if keyword in hay:
            return icon
    return "\U0001f9e9"
def build_all_apps_menu(
    feature_id: Optional[str], scan: dict, page: Optional[int] = 0,
) -> tuple[str, list]:
    """The complete laptop resource overview.

    Non-Chrome applications first, then Chrome windows each showing its
    account/profile identity and ONLY its own tabs. Every item stays
    selectable where Telegram button limits permit; large views paginate with
    Previous / Next instead of silently truncating.
    """
    applications = scan.get("applications") or []
    windows = _chrome_windows(scan)
    apps = [a for a in applications if not _is_chrome_app(a)]

    # (kind, display_text, callback_or_None)
    entries: list[tuple[str, str, Optional[str]]] = []
    if apps:
        entries.append(("header", "APPLICATIONS", None))
    for idx, app in enumerate(apps, 1):
        label = app.get("display_name") or app.get("title") or "Application"
        if app.get("title") and app["title"].lower() != label.lower():
            label = f"{label} \u2014 {app['title']}"
        rid = app.get("resource_id")
        entries.append((
            "app", f"{idx}. {_short(label, 60)}",
            f"feature:resrc_show:{rid}" if rid else None,
        ))
    if windows:
        entries.append(("header", "CHROME", None))
    for number, window in enumerate(windows, 1):
        wlabel = _window_label(window, number)
        entries.append((
            "window",
            f"{_short(wlabel, 44)} \u2014 {len(window['tabs'])} tabs",
            f"feature:resrc_wtabs:{window['hwnd']}@0",
        ))
        for tab in sorted(window["tabs"], key=_tab_sort_key):
            rid = tab.get("resource_id")
            entries.append((
                "tab",
                f"   \u2022 {_short(tab.get('title') or 'Chrome tab', 54)}",
                f"feature:resrc_tab_show:{rid}" if rid else None,
            ))

    total = len(entries)
    page = max(0, page or 0)
    start = page * _OVERVIEW_ENTRIES_PER_PAGE
    page_entries = entries[start:start + _OVERVIEW_ENTRIES_PER_PAGE]

    lines = ["\U0001f9e9 ALL APPS & TABS"]
    if not page_entries:
        lines.append("Nothing to display on this page.")
    for _kind, text, _cb in page_entries:
        lines.append(text)

    rows: list[list[dict]] = []
    for kind, text, cb in page_entries:
        if cb is None:
            continue
        if kind == "app":
            icon = "\U0001f4c4"
            label = _short(text, 40)
        elif kind == "window":
            icon = "\U0001f310"
            label = _short(text, 40)
        else:
            icon = "\U0001f5a5"
            label = _short(text, 60)
        rows.append([{"text": f"{icon} {label}", "callback_data": cb}])

    total_pages = max(0, (total - 1) // _OVERVIEW_ENTRIES_PER_PAGE)
    nav: list[dict] = []
    if page > 0:
        nav.append({
            "text": "\u25c0 Previous",
            "callback_data": f"feature:resrc_allapps:{page - 1}",
        })
    nav.append({
        "text": "\U0001f504 Refresh",
        "callback_data": _nav(feature_id, "resrc_refresh"),
    })
    if page < total_pages:
        nav.append({
            "text": "Next \u25b6",
            "callback_data": f"feature:resrc_allapps:{page + 1}",
        })
    rows.append(nav)
    rows.append([{
        "text": "\U0001f310 Chrome Accounts / Windows",
        "callback_data": _nav(feature_id, "resrc_wmenu"),
    }, {
        "text": "\u2b05\ufe0f Back",
        "callback_data": _nav(feature_id, "resrc_home"),
    }])
    return "\n".join(lines), rows


# ---------------------------------------------------------------------------
# Main dashboard: RESOURCE REVIEW
# ---------------------------------------------------------------------------


def _app_row_name(app: dict) -> str:
    label = app.get("display_name") or app.get("title") or "Application"
    if app.get("title") and app["title"].lower() != label.lower():
        label = f"{label} \u2014 {app['title']}"
    return label


def _captured_label(scan: dict) -> str:
    at = (scan or {}).get("scanned_at") or ""
    if not at:
        return "just now"
    value = str(at)
    try:
        return value[:16].replace("T", " ")  # ISO -> "YYYY-MM-DD HH:MM"
    except Exception:  # noqa: BLE001 - cosmetic only
        return _short(value, 20)


def build_resource_review_home(
    feature_id: Optional[str], scan: dict
) -> tuple[str, list]:
    """The button-first Resource Review HOME screen.

    Counts only - the actual applications/tabs are never dumped here; the
    Current Running button is the single entry point that drills into
    them. The home panel never lists resource rows.
    """
    applications = scan.get("applications") or []
    tabs = scan.get("tabs") or []
    windows = _chrome_windows(scan)
    lines = [
        "\U0001f9e9 FEATURE RESOURCE REVIEW",
        "",
        f"Applications: {len(applications)}",
        f"Chrome windows/profiles: {len(windows)}",
        f"Chrome tabs: {len(tabs)}",
    ]
    if scan.get("chrome_detected") and not windows:
        lines.append(
            "Chrome is running, but its windows/tabs could not be read "
            "right now."
        )

    rows: list[list[dict]] = [
        [{
            "text": "\U0001f9e9 Current Running",
            "callback_data": _nav(feature_id, "resrc_run"),
        }],
    ]
    if feature_id:
        rows.append([{
            "text": "\u25b6\ufe0f Continue Feature",
            "callback_data": _nav(feature_id, "resrc_continue"),
        }])
        rows.append([{
            "text": "\u274c Cancel",
            "callback_data": _nav(feature_id, "resrc_cancel"),
        }])
    else:
        rows.append([{
            "text": "\u274c Close",
            "callback_data": "feature:noop",
        }])
    return "\n".join(lines), rows


def build_current_running_menu(
    feature_id: Optional[str], scan: dict
) -> tuple[str, list]:
    """The live CURRENT RUNNING panel with expandable headings.

    Shows two clickable headings: Applications (N) and Chrome (N).
    Tapping a heading expands/collapses the list within the SAME message.
    Only one section can be expanded at a time.
    """
    applications = scan.get("applications") or []
    windows = _chrome_windows(scan)
    apps = [a for a in applications if not _is_chrome_app(a)]

    lines = ["\U0001f5a5\ufe0f CURRENT RUNNING", ""]
    if not apps and not windows:
        lines.append("Nothing running yet.")

    # Applications heading
    app_count = len(apps)
    lines.append(f"\U0001f4bb Applications ({app_count})")

    # Chrome heading
    chrome_count = len(scan.get("tabs") or [])
    lines.append(f"\U0001f310 Chrome ({chrome_count})")

    rows: list[list[dict]] = []
    # Expand/Collapse Applications button
    rows.append([{
        "text": f"\U0001f4bb Applications ({app_count})",
        "callback_data": _navx("resrc_expand_apps"),
    }])
    # Expand/Collapse Chrome button
    rows.append([{
        "text": f"\U0001f310 Chrome ({chrome_count})",
        "callback_data": _navx("resrc_expand_chrome"),
    }])
    rows.append([{
        "text": "\U0001f504 Refresh",
        "callback_data": _navx("resrc_refresh_run"),
    }])
    rows.append([{
        "text": "\u2b05\ufe0f Back",
        "callback_data": _navx("resrc_home"),
    }])
    return "\n".join(lines), rows


# ---------------------------------------------------------------------------
# Expandable headings for Current Running (Part 8C-1)
# ---------------------------------------------------------------------------


def build_current_running_expanded_apps(
    feature_id: Optional[str], scan: dict,
    app_id: Optional[str] = None,
) -> tuple[str, list]:
    """Expanded view showing Applications list with Chrome collapsed.

    Every app entry is clickable (``feature:resrc_app_actions:<rid>``). Tapping
    one reveals that app's action buttons INLINE on the SAME message; only ONE
    app can be selected at a time (``app_id``), so tapping another app simply
    moves the actions to it. ``\U0001f53c Hide Actions``
    (``feature:resrc_hide_app_actions``) collapses the actions again. The
    action buttons are only rendered - no action behaviour is wired up yet.
    The \U0001f504 Refresh row carries the context (``resrc_refresh_apps`` plus
    the selected app's resource id when one is open) so a refresh keeps
    Applications expanded and re-matches the SAME window by its exact HWND.
    """
    applications = scan.get("applications") or []
    windows = _chrome_windows(scan)
    apps = [a for a in applications if not _is_chrome_app(a)]

    lines = ["\U0001f5a5\ufe0f CURRENT RUNNING", ""]

    # Applications heading (expanded)
    app_count = len(apps)
    lines.append(f"\U0001f4bb Applications ({app_count})")
    # List all apps; the selected app (if any) shows its actions inline.
    selected = None
    if app_id:
        for app in apps:
            if app.get("resource_id") == app_id:
                selected = app
                break
    # Clean UI contract: applications appear as INLINE BUTTONS only (never
    # duplicated into the body). When one app is selected, show a single small
    # status line so the user sees what they are acting on.
    if selected is not None:
        lines.append(f"\u25b6\ufe0f {_short(_app_row_name(selected), 60)}")
    lines.append("")
    # Chrome heading (collapsed)
    chrome_count = len(scan.get("tabs") or [])
    lines.append(f"\U0001f310 Chrome ({chrome_count})")

    rows: list[list[dict]] = []
    # One clickable row per app entry (reveals that app's inline actions).
    for app in apps:
        rid = app.get("resource_id")
        if not rid:
            continue
        rows.append([{
            "text": f"{_app_icon(app)} {_short(_app_row_name(app), 60)}",
            "callback_data": f"feature:resrc_app_actions:{rid}",
        }])
    # Inline action buttons for the selected app ONLY.
    if selected is not None:
        rid = selected.get("resource_id") or ""
        rows.append([
            {"text": "\U0001f3af Focus",
             "callback_data": f"feature:resrc_focus:{rid}"},
            {"text": "\U0001f4f8 Screenshot",
             "callback_data": f"feature:resrc_shot:{rid}"},
        ])
        rows.append([
            {"text": "\u274c Close",
             "callback_data": f"feature:resrc_close:{rid}"},
            {"text": "\u270f\ufe0f Edit",
             "callback_data": f"feature:resrc_edit:{rid}"},
        ])
        rows.append([{
            "text": "\U0001f53c Hide Actions",
            "callback_data": _navx("resrc_hide_app_actions"),
        }])
    # Collapse Applications button
    rows.append([{
        "text": f"\U0001f4bb Applications ({app_count})",
        "callback_data": _navx("resrc_collapse_apps"),
    }])
    # Expand Chrome button (will collapse Apps)
    rows.append([{
        "text": f"\U0001f310 Chrome ({chrome_count})",
        "callback_data": _navx("resrc_expand_chrome"),
    }])
    # \U0001f504 Refresh WITH context: the SAME panel stays Applications
    # expanded; when ONE app is selected the callback carries THAT app's
    # resource id so the refresh can re-match the exact window (HWND) in the
    # fresh scan and keep the selection on its NEW generation-stamped id.
    # When ONE app is selected the callback carries THAT app's short resource
    # id so the refresh re-matches the exact window (HWND). With no selection
    # the payload is id-free (handler resolves the feature from context) so the
    # callback can never exceed Telegram's 64-byte limit.
    refresh_sel = (selected or {}).get("resource_id")
    rows.append([{
        "text": "\U0001f504 Refresh",
        "callback_data": (f"feature:resrc_refresh_apps:{refresh_sel}"
                          if refresh_sel else _navx("resrc_refresh_apps")),
    }])
    rows.append([{
        "text": "\u2b05\ufe0f Back",
        "callback_data": _navx("resrc_home"),
    }])
    return "\n".join(lines), rows


def build_current_running_expanded_chrome(
    feature_id: Optional[str], scan: dict,
    tab_id: Optional[str] = None,
) -> tuple[str, list]:
    """Expanded view showing Chrome tabs with Applications collapsed.

    Every REAL Chrome tab entry is clickable (``feature:resrc_tab_actions:<rid>``
    on its real resource id). Tapping one reveals that tab's action buttons
    INLINE on the SAME message; only ONE tab can be selected at a time
    (``tab_id``), so tapping another tab simply moves the actions to it.
    ``\U0001f53c Hide Actions`` (``feature:resrc_hide_tab_actions``) collapses
    the actions again. The action buttons are only rendered - no action
    behaviour is wired up yet. The \U0001f504 Refresh row carries the context
    (``resrc_refresh_chrome`` plus the selected tab's resource id when one is
    open) so a refresh keeps Chrome expanded and re-matches the SAME tab by
    its exact identity (page_id for CDP, host_hwnd + runtime_id for UIA).
    """
    applications = scan.get("applications") or []
    windows = _chrome_windows(scan)
    apps = [a for a in applications if not _is_chrome_app(a)]

    lines = ["\U0001f5a5\ufe0f CURRENT RUNNING", ""]

    # Applications heading (collapsed)
    app_count = len(apps)
    lines.append(f"\U0001f4bb Applications ({app_count})")

    lines.append("")
    # Chrome heading (expanded)
    chrome_count = len(scan.get("tabs") or [])
    lines.append(f"\U0001f310 Chrome ({chrome_count})")
    # List all tabs from all windows; the selected tab (if any) shows its
    # action labels inline directly under it.
    selected = None
    if tab_id:
        for window in windows:
            for tab in window["tabs"]:
                if tab.get("resource_id") == tab_id:
                    selected = tab
                    break
            if selected is not None:
                break

    # Clean UI contract: tabs appear as INLINE BUTTONS only (never duplicated
    # into the body). When one tab is selected, show a single small status line.
    if selected is not None:
        lines.append(
            f"\u25b6\ufe0f {_short(selected.get('title') or 'Chrome tab', 58)}"
        )

    rows: list[list[dict]] = []
    # One clickable row per REAL Chrome tab (reveals that tab's inline actions
    # on the SAME message - never the old resrc_tab_show drill-down).
    for window in windows:
        for tab in sorted(window["tabs"], key=_tab_sort_key):
            rid = tab.get("resource_id")
            if not rid:
                continue
            title = tab.get("title") or "Chrome tab"
            rows.append([{
                "text": f"{_tab_icon(title)} {_short(title, 58)}",
                "callback_data": f"feature:resrc_tab_actions:{rid}",
            }])
            if tab is selected:
                rows.append([
                    {"text": "\U0001f3af Focus",
                     "callback_data": f"feature:resrc_focus:{rid}"},
                    {"text": "\U0001f4f8 Screenshot",
                     "callback_data": f"feature:resrc_shot:{rid}"},
                ])
                rows.append([
                    {"text": "\u274c Close",
                     "callback_data": f"feature:resrc_close:{rid}"},
                    {"text": "\u270f\ufe0f Edit",
                     "callback_data": f"feature:resrc_edit:{rid}"},
                ])
                rows.append([{
                    "text": "\U0001f53c Hide Actions",
                    "callback_data": _navx("resrc_hide_tab_actions"),
                }])
    # Expand Applications button (will collapse Chrome)
    rows.append([{
        "text": f"\U0001f4bb Applications ({app_count})",
        "callback_data": _navx("resrc_expand_apps"),
    }])
    # Collapse Chrome button
    rows.append([{
        "text": f"\U0001f310 Chrome ({chrome_count})",
        "callback_data": _navx("resrc_collapse_chrome"),
    }])
    # \U0001f504 Refresh WITH context: the SAME panel stays Chrome expanded;
    # when ONE tab is selected the callback carries THAT tab's resource id so
    # the refresh can re-match the exact tab identity (page_id for CDP,
    # host_hwnd + runtime_id for UIA) in the fresh scan and keep the selection
    # on its NEW generation-stamped id.
    # Selected tab -> carry its short resource id so the refresh re-matches the
    # exact tab; no selection -> id-free payload (feature resolved from context)
    # so the callback can never exceed Telegram's 64-byte limit.
    refresh_sel = (selected or {}).get("resource_id")
    rows.append([{
        "text": "\U0001f504 Refresh",
        "callback_data": (f"feature:resrc_refresh_chrome:{refresh_sel}"
                          if refresh_sel else _navx("resrc_refresh_chrome")),
    }])
    rows.append([{
        "text": "\u2b05\ufe0f Back",
        "callback_data": _navx("resrc_home"),
    }])
    return "\n".join(lines), rows


# ---------------------------------------------------------------------------
# Chrome profile expand / collapse (SAME message, Step 6)
# ---------------------------------------------------------------------------


def build_chrome_expanded(
    feature_id: Optional[str], scan: dict, profile_token: str
) -> Optional[tuple[str, list]]:
    """Expanded tab list for ONE Chrome account/profile.

    Returns ``None`` when the token is stale (generation mismatch) so the
    handler can render ``Resource list changed`` + the refreshed home.
    Rendered by EDITING the same Resource Review message.
    """
    gen_s, _, idx_s = (profile_token or "").partition("-p")
    try:
        gen = int(gen_s)
        index = int(idx_s)
    except (TypeError, ValueError):
        return None
    current_gen = int((scan or {}).get("generation") or 0)
    if gen != current_gen:
        return None
    profiles = chrome_profiles(scan)
    if not (1 <= index <= len(profiles)):
        return None
    profile = profiles[index - 1]
    tabs = sorted(profile["tabs"], key=_tab_sort_key)

    lines = [
        f"\U0001f310 Chrome \u2014 {_short(profile['label'], 44)}",
        "",
        f"{profile['count']} tabs open",
        "",
    ]
    if tabs:
        for i, tab in enumerate(tabs, 1):
            title = tab.get("title") or "Chrome tab"
            marker = (
                " \u2022active"
                if (tab.get("active") or tab.get("selected")) else ""
            )
            lines.append(f"{i}. {_short(title, 70)}{marker}")
    else:
        lines.append("No tabs open in this profile right now.")

    rows: list[list[dict]] = []
    for tab in tabs:
        rid = tab.get("resource_id")
        if not rid:
            continue
        title = tab.get("title") or "Chrome tab"
        rows.append([{
            "text": f"{_tab_icon(title)} {_short(title, 58)}",
            "callback_data": f"feature:resrc_tab_show:{rid}",
        }])
    rows.append([{
        "text": "\u25b2 Collapse",
        "callback_data": f"feature:resrc_collapse:{profile['token']}",
    }])
    rows.append([{
        "text": "\u2b05\ufe0f Back",
        "callback_data": f"feature:resrc_back:{feature_id or ''}",
    }])
    return "\n".join(lines), rows


def build_chrome_collapse(
    feature_id: Optional[str], scan: dict, profile_token: str
) -> tuple[str, list]:
    """Collapse -> back to the compact Resource Review dashboard (SAME msg)."""
    return build_resource_review_home(feature_id, scan)


# ---------------------------------------------------------------------------
# Per-window Chrome account menus (exact host_hwnd grouping)
# ---------------------------------------------------------------------------


def build_chrome_account_list(
    feature_id: Optional[str], scan: dict
) -> tuple[str, list]:
    """Level 2 - Chrome grouped by REAL Gmail/profile account.

    Tapping the Chrome heading opens THIS view (not the flat tab list). Tabs
    are grouped by the proven Chrome profile/account identity (email ->
    profile -> identity label), aggregated across ALL Chrome windows that share
    the same account. Tabs with no resolvable identity are grouped under
    "Other / Unknown" and never dropped. The heading total always equals the
    real tab count, and the account counts always sum to that total.

    Callbacks carry only a short generation-stamped account token
    (``g<gen>-p<idx>``) so they stay well under Telegram's 64-byte limit.
    """
    profiles = chrome_profiles(scan)
    total = len(scan.get("tabs") or [])

    lines = ["\U0001f310 CHROME", ""]
    if not profiles:
        if scan.get("chrome_detected"):
            lines.append(
                "Chrome is running, but no readable tabs were found. "
                "Press \U0001f504 Refresh to re-scan."
            )
        else:
            lines.append("No Chrome browser detected.")

    rows: list[list[dict]] = []
    for profile in profiles:
        if profile["count"] <= 0:
            continue  # never render a zero-tab account group
        icon = _account_icon(profile)
        label = _short(profile["label"], 40)
        rows.append([{
            "text": f"{icon} {label} ({profile['count']})",
            "callback_data": f"feature:resrc_chrome_account:{profile['token']}",
        }])
    rows.append([{
        "text": "\U0001f504 Refresh",
        "callback_data": _navx("resrc_refresh_chrome_accounts"),
    }])
    rows.append([{
        "text": "\u2b05\ufe0f Back",
        "callback_data": _navx("resrc_collapse_chrome"),
    }])
    return "\n".join(lines), rows


def build_chrome_account_tabs(
    feature_id: Optional[str], scan: dict, account_token: str,
    tab_id: Optional[str] = None,
) -> Optional[tuple[str, list]]:
    """Level 3 - the tabs of ONE selected Gmail/profile account.

    Returns ``None`` when the account no longer exists in the CURRENT scan so
    the handler can drop back to the account list with a small warning. Only
    that account's tabs are shown as inline buttons; a selected tab reveals the
    existing inline actions (Focus / Screenshot / Close / Edit / Hide Actions)
    exactly as before, targeting the tab's real resource id.
    """
    profile = resolve_chrome_account(scan, account_token)
    if profile is None:
        return None
    tabs = sorted(profile["tabs"], key=_tab_sort_key)

    lines = [f"\U0001f310 {_short(profile['label'], 50)}", ""]
    selected = None
    if tab_id:
        for tab in tabs:
            if tab.get("resource_id") == tab_id:
                selected = tab
                break
    if selected is not None:
        lines.append(f"\u25b6\ufe0f {_short(selected.get('title') or 'Chrome tab', 58)}")

    rows: list[list[dict]] = []
    for tab in tabs:
        rid = tab.get("resource_id")
        if not rid:
            continue
        title = tab.get("title") or "Chrome tab"
        rows.append([{
            "text": f"{_tab_icon(title)} {_short(title, 58)}",
            "callback_data": f"feature:resrc_tab_actions:{rid}",
        }])
        if tab is selected:
            rows.append([
                {"text": "\U0001f3af Focus",
                 "callback_data": f"feature:resrc_focus:{rid}"},
                {"text": "\U0001f4f8 Screenshot",
                 "callback_data": f"feature:resrc_shot:{rid}"},
            ])
            rows.append([
                {"text": "\u274c Close",
                 "callback_data": f"feature:resrc_close:{rid}"},
                {"text": "\u270f\ufe0f Edit",
                 "callback_data": f"feature:resrc_edit:{rid}"},
            ])
            rows.append([{
                "text": "\U0001f53c Hide Actions",
                "callback_data": f"feature:resrc_chrome_account:{account_token}",
            }])
    # Refresh re-matches THIS account by stable identity (handler captures the
    # account key before the rescan); Back returns to the account list.
    rows.append([{
        "text": "\U0001f504 Refresh",
        "callback_data": f"feature:resrc_refresh_chrome_account:{account_token}",
    }])
    rows.append([{
        "text": "\u2b05\ufe0f Back",
        "callback_data": _navx("resrc_expand_chrome"),
    }])
    return "\n".join(lines), rows


def build_chrome_accounts_menu(
    feature_id: Optional[str], scan: dict
) -> tuple[str, list]:
    """The dedicated per-window/account Chrome section.

    One entry per REAL Chrome top-level window - never a merge of windows -
    labelled with the evidence-ladder identity (email when proven, honest
    fallback when not) plus its exact live tab count. Selecting one opens
    ONLY that window's tabs (exact ``host_hwnd`` match).
    """
    windows = _chrome_windows(scan)
    lines = ["\U0001f310 CHROME", ""]
    if not windows:
        if scan.get("chrome_detected"):
            lines.append(
                "Chrome is running, but no readable window was found. "
                "Press \U0001f504 Refresh to re-scan."
            )
        else:
            lines.append("No Chrome browser detected.")
    for number, entry in enumerate(windows, 1):
        lines.append(
            f"\U0001f4e7 {_window_label(entry, number)}"
            f" \u2014 {len(entry['tabs'])} tabs"
        )

    rows: list[list[dict]] = []
    for number, entry in enumerate(windows, 1):
        label = _short(_window_label(entry, number), 40)
        rows.append([{
            "text": f"\U0001f310 {label} \u2014 {len(entry['tabs'])} tabs",
            "callback_data": f"feature:resrc_wtabs:{entry['hwnd']}@0",
        }])
    rows.append([{
        "text": "\U0001f504 Refresh",
        "callback_data": _nav(feature_id, "resrc_refresh"),
    }])
    rows.append([{
        "text": "\u2b05\ufe0f Back",
        "callback_data": _nav(feature_id, "resrc_home"),
    }])
    return "\n".join(lines), rows


def build_window_tabs_menu(
    feature_id: Optional[str], scan: dict, hwnd: int, page: Optional[int] = 0,
) -> Optional[tuple[str, list]]:
    """Tabs belonging to ONE exact Chrome window only.

    Filters strictly by ``host_hwnd == hwnd`` (the parent Chrome top-level
    window this tab was discovered under). Returns ``None`` when that window
    is no longer part of the CURRENT scan (stale-button rejection). Every tab
    keeps its real ``resource_id`` so detail actions target the exact tab.
    Paginated with Previous/Next - never silently truncated.
    """
    windows = _chrome_windows(scan)
    window_entry = None
    window_ordinal = 1
    for number, entry in enumerate(windows, 1):
        if int(entry["hwnd"]) == int(hwnd):
            window_entry = entry
            window_ordinal = number
            break
    if window_entry is None:
        return None  # stale - the Chrome window closed or moved.
    window_label = _window_label(window_entry, window_ordinal)
    window_tabs = sorted(window_entry["tabs"], key=_tab_sort_key)
    total = len(window_tabs)
    page = max(0, page or 0)
    start = page * _TABS_PER_PAGE
    visible = window_tabs[start:start + _TABS_PER_PAGE]

    lines = [
        f"\U0001f310 {_short(window_label, 50)}",
        f"{total} tabs open",
        "",
    ]
    if not visible:
        lines.append("No tabs to display on this page.")
    else:
        for index, tab in enumerate(visible, start + 1):
            title = tab.get("title") or "Chrome tab"
            lines.append(f"{index}. {_short(title, 60)}")
            if tab.get("selected") or tab.get("active"):
                lines.append("   \u2022 active")

    rows: list[list[dict]] = []
    for tab in visible:
        rid = tab.get("resource_id")
        if not rid:
            continue
        title = tab.get("title") or "Chrome tab"
        rows.append([{
            "text": f"{_tab_icon(title)} {_short(title, 60)}",
            "callback_data": f"feature:resrc_tab_show:{rid}",
        }])
    total_pages = max(0, (total - 1) // _TABS_PER_PAGE)
    nav: list[dict] = []
    if page > 0:
        nav.append({
            "text": "\u25c0 Previous",
            "callback_data": f"feature:resrc_wtabs:{hwnd}@{page - 1}",
        })
    nav.append({
        "text": "\U0001f504 Refresh Tabs",
        "callback_data": f"feature:resrc_refresh_tabs:{hwnd}@{page}",
    })
    if page < total_pages:
        nav.append({
            "text": "Next \u25b6",
            "callback_data": f"feature:resrc_wtabs:{hwnd}@{page + 1}",
        })
    rows.append(nav)
    rows.append([{
        "text": "\u2b05\ufe0f Back",
        "callback_data": _nav(feature_id, "resrc_run"),
    }])
    return "\n".join(lines), rows
