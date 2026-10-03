"""Telegram Resource Review dispatch for the allowlisted controller.

Every ``feature:resrc_*`` callback routes here; the proven ``ResourceRegistry``
and grouped ``resource_menus`` builders do all real work. Each action answers
the callback once (the lightweight loading toast) then re-renders the correct
contextual menu or sends the honest result - never a stuck "Working..." state.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from jarvis_core.resource_manager import (
    build_chrome_tab_detail,
    build_chrome_tabs_menu,
    build_close_confirmation,
    build_resources_menu,
    build_resource_detail,
    resource_inspector,
    resource_registry,
)
from jarvis_core.resource_menus import (
    build_all_apps_menu,
    build_chrome_account_list,
    build_chrome_account_tabs,
    build_chrome_accounts_menu,
    build_current_running_expanded_apps,
    build_current_running_expanded_chrome,
    build_current_running_menu,
    build_resource_review_home,
    build_window_tabs_menu,
    chrome_profiles,
)
from jarvis_core.telegram_bot_api import TelegramAPIError

logger = logging.getLogger("jarvis.telegram.resrc")


def _account_token_for_tab(scan: dict, resource_id: str) -> str:
    """Return the Chrome account token whose group contains ``resource_id``.

    Used so a tab tap / post-action render stays inside the SAME Gmail/profile
    account view. Empty string when the tab is not found.
    """
    for profile in chrome_profiles(scan or {}):
        for tab in profile.get("tabs", []):
            if tab.get("resource_id") == resource_id:
                return profile.get("token") or ""
    return ""


def _render_chrome_account_tab(service, chat_id, message_id, fid,
                               resource_id, banner: str = "") -> bool:
    """Re-render the account-tabs view with ``resource_id`` still selected.

    Returns True when rendered inside an account view; False when the tab could
    not be placed in any account (caller falls back to its own behaviour).
    """
    snapshot = _snapshot()
    token = _account_token_for_tab(snapshot, resource_id)
    if not token:
        return False
    built = build_chrome_account_tabs(fid, snapshot, token, tab_id=resource_id)
    if built is None:
        return False
    text, rows = built
    if banner:
        text = f"{banner}\n\n{text}"
    _render(service, chat_id, message_id, fid, text, rows)
    return True

# ── Strict read-only boundary ────────────────────────────────────────────────
# Resource Review is informational ONLY. These notices are the ENTIRE response
# to any mutating action (focus / per-resource screenshot / close). The review
# path must never reach the Desktop Controller.
READ_ONLY_NOTICE = (
    "\U0001f512 Resource Review is strictly read-only.\n"
    "It may only list and inspect resources \u2014 it can never close, focus, "
    "click, type or modify applications or browser tabs. Active desktop "
    "control stays available through normal Jarvis commands, never here."
)
READ_ONLY_SHOT_NOTICE = (
    "\U0001f512 Resource Review is strictly read-only.\n"
    "An individual app/tab screenshot would require focusing or selecting "
    "that target, which is not allowed here. Use \U0001f4f8 Current Screen "
    "for a fully read-only capture."
)
#: Placeholder notice shown when the user presses the Edit action. Edit is
#: intentionally NOT implemented yet - it must never type, click, send
#: keyboard input, change a file/webpage, run JavaScript/CDP mutations,
#: focus/close/screenshot anything, or change the feature stage.
EDIT_PLACEHOLDER_NOTICE = (
    "\u270f\ufe0f Edit controls will be implemented in a later phase."
)
#: Visible warning when the Back fresh-scan cannot reach the laptop; the last
#: known snapshot is still rendered in place (scan failure is never fatal and
#: never changes the feature stage).
SCAN_FAILED_NOTICE = (
    "\u26a0\ufe0f Resource refresh failed. Showing the last known resources."
)


def _read_only_refusal(service, chat_id, callback_id, notice) -> None:
    service._answer_callback(callback_id, "\U0001f512 Read-only")
    service._send(chat_id, notice)


def _feature_ctx() -> Optional[str]:
    try:
        return resource_registry._feature_id or None
    except Exception:
        return None


def _snapshot() -> dict:
    return resource_inspector.snapshot(include_identity=True)


def _rescan() -> None:
    resource_inspector.scan(feature_id=_feature_ctx())


def _refresh_resource_list(service, chat_id, message_id, feature_id) -> None:
    """\u2b05\ufe0f Back: ONE lightweight read-only rescan, then render the
    refreshed CURRENT RUNNING resource list on the SAME Telegram message.

    If the fresh scan fails, the last valid snapshot is shown in place with a
    clear warning - Back never crashes, never mutates anything, never starts
    Research and never changes the feature stage.
    """
    banner = ""
    try:
        _rescan()
    except Exception:
        logger.exception("Back resource scan failed")
        banner = SCAN_FAILED_NOTICE
    text, rows = build_current_running_menu(feature_id, _snapshot())
    if banner:
        text = f"{banner}\n\n{text}"
    _render(service, chat_id, message_id, feature_id, text, rows)


def _stable_identity(resource) -> Optional[dict]:
    """Cross-generation STABLE identity of one selected resource.

    Resource ids are generation-stamped and therefore NEVER stable; the
    stable identity is read from the EXISTING Resource fields only:

    * Application: the exact ``window_handle`` (HWND).
    * Chrome tab (CDP): ``page_id`` (the CDP target id).
    * Chrome tab (UIA): ``host_hwnd`` + ``runtime_id``.

    Returns ``None`` when the resource carries no exact identity.
    """
    if resource is None:
        return None
    if resource.resource_type == "browser_tab":
        mode = (resource.control_mode or "").lower()
        if resource.page_id and mode != "uia":
            return {"kind": "tab", "mode": "cdp",
                    "page_id": str(resource.page_id)}
        if resource.host_hwnd and resource.runtime_id:
            return {"kind": "tab", "mode": "uia",
                    "host_hwnd": int(resource.host_hwnd),
                    "runtime_id": ",".join(
                        str(x) for x in str(resource.runtime_id).split(",")
                        if x)}
        if resource.page_id:
            return {"kind": "tab", "mode": "cdp",
                    "page_id": str(resource.page_id)}
        return None
    if resource.window_handle:
        try:
            return {"kind": "app", "window_handle": int(resource.window_handle)}
        except (TypeError, ValueError):
            return None
    return None


def _match_app_identity(snapshot: dict,
                        identity: Optional[dict]) -> Optional[str]:
    """The FRESH-snapshot resource id of the SAME application (exact HWND)."""
    if not identity or identity.get("kind") != "app":
        return None
    for app in snapshot.get("applications") or []:
        if app.get("window_handle") is None:
            continue
        try:
            if int(app["window_handle"]) == int(identity["window_handle"]):
                return app.get("resource_id")
        except (TypeError, ValueError):
            continue
    return None


def _match_tab_identity(snapshot: dict,
                        identity: Optional[dict]) -> Optional[str]:
    """The FRESH-snapshot resource id of the SAME tab (exact tab identity).

    CDP tabs re-match on ``page_id``; UIA tabs re-match on the exact
    ``host_hwnd`` + ``runtime_id`` pair. Titles and tab positions are never
    identity - a renamed or moved tab still matches, a different tab never
    does.
    """
    if not identity or identity.get("kind") != "tab":
        return None
    for tab in snapshot.get("tabs") or []:
        if identity["mode"] == "cdp":
            if tab.get("page_id") and str(tab["page_id"]) == identity["page_id"]:
                return tab.get("resource_id")
            continue
        if tab.get("host_hwnd") is None:
            continue
        try:
            if int(tab["host_hwnd"]) != int(identity["host_hwnd"]):
                continue
        except (TypeError, ValueError):
            continue
        runtime_id = ",".join(
            str(x) for x in str(tab.get("runtime_id") or "").split(",") if x)
        if runtime_id and runtime_id == identity["runtime_id"]:
            return tab.get("resource_id")
    return None


def _refresh_expanded(service, chat_id, message_id, action: str,
                      param: str) -> None:
    """\U0001f504 Context-preserving Refresh for the expanded CURRENT RUNNING views.

    ``param`` carries either the SELECTED resource's id (Refresh pressed on a
    selected app/tab panel) or the feature id (no selection). The selected
    resource is resolved BEFORE the ONE lightweight read-only rescan and its
    stable identity captured; the FRESH snapshot is then re-matched so the
    selection follows the NEW generation-stamped resource id when the same
    window/tab still exists, otherwise the panel renders with NO selection.
    Applications expanded stays Applications expanded, Chrome expanded stays
    Chrome expanded, the SAME Telegram message is edited either way, and a
    failed scan shows the existing warning + the last known snapshot with the
    expanded context kept.
    """
    fid = _feature_ctx()
    identity: Optional[dict] = None
    if param:
        resource = resource_registry.get(param)
        if resource is not None:
            # Refresh was pressed on a SELECTED app/tab panel: the callback
            # carries that resource's generation-stamped id. Resolve it and
            # capture its stable identity BEFORE the rescan.
            fid = resource.feature_id or fid
            identity = _stable_identity(resource)
        else:
            # No selection: the callback carries the feature id (a stale
            # selected id simply refreshes without a selection).
            fid = fid or param
    banner = ""
    try:
        _rescan()
    except Exception:
        logger.exception("Context refresh scan failed")
        banner = SCAN_FAILED_NOTICE
    snapshot = _snapshot()
    if action == "resrc_refresh_apps":
        text, rows = build_current_running_expanded_apps(
            fid, snapshot, app_id=_match_app_identity(snapshot, identity))
    else:
        text, rows = build_current_running_expanded_chrome(
            fid, snapshot, tab_id=_match_tab_identity(snapshot, identity))
    if banner:
        text = f"{banner}\n\n{text}"
    _render(service, chat_id, message_id, fid, text, rows)


def _render(service, chat_id, message_id, feature_id, text, rows) -> None:
    """Render a menu by EDITING the triggering message in place (Telegram
    ``editMessageText``) so navigation never stacks new messages; fall back
    to a fresh message when the original cannot be edited."""
    # Guard: Telegram rejects the WHOLE inline keyboard (BUTTON_DATA_INVALID)
    # if any callback_data exceeds 64 UTF-8 bytes. Surface it loudly instead of
    # a silent dead button.
    try:
        for _row in (rows or []):
            for _btn in _row:
                _cb = (_btn.get("callback_data") or "").encode("utf-8")
                if len(_cb) > 64:
                    logger.error(
                        "Resource Review callback_data exceeds 64 bytes (%d): %r",
                        len(_cb), _btn.get("callback_data"),
                    )
    except Exception:
        logger.exception("callback_data length guard failed")
    if message_id and service._edit_message(
            chat_id, message_id, text,
            reply_markup={"inline_keyboard": rows}):
        return
    service._send(chat_id, text, reply_markup={"inline_keyboard": rows})
def handle_resource_review_action(service, user_id, chat_id, callback_id,
                                  route: str, message_id=None) -> None:
    """Dispatch one ``route`` of the form ``resrc_<action>:<param>``."""
    action, _, param = route.partition(":")
    _feedback = {
        "resrc_refresh": "\u23f3 Refreshing...",
        "resrc_refresh_run": "\u23f3 Refreshing...",
        "resrc_refresh_apps": "\u23f3 Refreshing...",
        "resrc_refresh_chrome": "\u23f3 Refreshing...",
        "resrc_refresh_tabs": "\u23f3 Refreshing...",
        "resrc_focus": "\U0001f441 Focusing...",
        "resrc_shot": "\U0001f4f8 Capturing...",
        "resrc_edit": "\u270f\ufe0f Edit mode",
        "resrc_screen": "\U0001f4f8 Capturing...",
        "resrc_close": "\u274c Closing...",
        "resrc_confirm_close": "\u274c Closing...",
        "resrc_back": "\u2b05\ufe0f Back...",
    }
    service._answer_callback(
        callback_id, _feedback.get(action, "Processing..."))
    fid = param or _feature_ctx()

    if action == "resrc_home":
        _render(service, chat_id, message_id, fid, *build_resource_review_home(
            fid, _snapshot()))
    elif action == "resrc_back":
        # \u2b05\ufe0f Back always leaves a detail / Edit placeholder screen with
        # a FRESH read-only scan: stale resources (apps/tabs closed on the
        # laptop) disappear from the refreshed CURRENT RUNNING list, rendered
        # by editing the SAME control-panel message.
        _refresh_resource_list(service, chat_id, message_id, fid)
    elif action == "resrc_refresh":
        # Re-scan live resources and update the home menu.
        _rescan()
        _render(service, chat_id, message_id, fid, *build_resource_review_home(
            fid, _snapshot()))
    elif action == "resrc_refresh_run":
        # \U0001f504 Refresh on the CURRENT RUNNING list reuses the same
        # Part-7 fresh-list helper as Back: ONE lightweight read-only rescan,
        # then edit the SAME control-panel message with the refreshed CURRENT
        # RUNNING list. A failed scan shows the existing warning + the last
        # known snapshot, and the Refresh button stays available for a retry.
        _refresh_resource_list(service, chat_id, message_id, fid)
    elif action in ("resrc_refresh_apps", "resrc_refresh_chrome"):
        # \U0001f504 Refresh WITH context from an expanded CURRENT RUNNING
        # panel: Applications stays expanded / Chrome stays expanded, and a
        # selected app/tab survives the rescan by its STABLE identity (exact
        # window_handle for applications; page_id for CDP tabs, host_hwnd +
        # runtime_id for UIA tabs). The selection follows the NEW
        # generation-stamped resource id when the same window/tab still
        # exists; otherwise the panel renders with no selection.
        _refresh_expanded(service, chat_id, message_id, action, param)
    elif action == "resrc_expand_apps":
        # 💻 Tap the Applications heading on the CURRENT RUNNING panel:
        # Applications EXPANDED, Chrome collapsed - edited onto the SAME
        # Telegram message. NO rescan: the CURRENT ResourceRegistry snapshot
        # is reused as-is.
        _render(service, chat_id, message_id, fid,
                *build_current_running_expanded_apps(fid, _snapshot()))
    elif action == "resrc_collapse_apps":
        # 💻 Tap the Applications heading again: back to the plain collapsed
        # CURRENT RUNNING panel on the SAME message. NO rescan - the CURRENT
        # ResourceRegistry snapshot only.
        _render(service, chat_id, message_id, fid, *build_current_running_menu(
            fid, _snapshot()))
    elif action == "resrc_expand_chrome":
        # 🌐 Tap the Chrome heading: show Chrome grouped by Gmail/profile
        # account (Level 2 - NOT the flat tab list). Applications collapses.
        # NO rescan: the CURRENT ResourceRegistry snapshot only.
        _render(service, chat_id, message_id, fid,
                *build_chrome_account_list(fid, _snapshot()))
    elif action == "resrc_chrome_account":
        # Tap ONE Gmail/profile account: show ONLY that account's tabs
        # (Level 3). NO rescan; the account token maps to the current snapshot.
        built = build_chrome_account_tabs(fid, _snapshot(), param)
        if built is None:
            # Account no longer exists - drop back to the account list.
            service._answer_callback(callback_id, "\u26a0\ufe0f Account changed")
            _render(service, chat_id, message_id, fid,
                    *build_chrome_account_list(fid, _snapshot()))
            return
        _render(service, chat_id, message_id, fid, *built)
    elif action == "resrc_refresh_chrome_accounts":
        # Refresh at the account-list level: exactly one fresh scan, recompute
        # the account groups/counts, stay on the account list.
        try:
            _rescan()
            banner = ""
        except Exception:
            logger.exception("Chrome account-list refresh scan failed")
            banner = SCAN_FAILED_NOTICE
        text, rows = build_chrome_account_list(fid, _snapshot())
        if banner:
            text = f"{banner}\n\n{text}"
        _render(service, chat_id, message_id, fid, text, rows)
    elif action == "resrc_refresh_chrome_account":
        # Refresh inside ONE account: capture the account's STABLE identity key
        # BEFORE the rescan, then re-match it in the fresh scan so the same
        # Gmail/profile survives the new generation. If it is gone, fall back
        # to the account list with a small warning.
        from jarvis_core.resource_menus import (
            lookup_stable_key,
            resolve_chrome_account,
            resolve_chrome_account_by_key,
        )
        # STABLE identity key from the token registry (position-independent);
        # fall back to resolving it from the current snapshot.
        stable_key = lookup_stable_key(param) or (
            (resolve_chrome_account(_snapshot(), param) or {}).get("key") or "")
        try:
            _rescan()
            banner = ""
        except Exception:
            logger.exception("Chrome account refresh scan failed")
            banner = SCAN_FAILED_NOTICE
        snapshot = _snapshot()
        matched = resolve_chrome_account_by_key(snapshot, stable_key)
        if matched is None:
            service._send(
                chat_id,
                "\u26a0\ufe0f That Chrome account is no longer open. "
                "Showing the current Chrome accounts.")
            _render(service, chat_id, message_id, fid,
                    *build_chrome_account_list(fid, snapshot))
            return
        text, rows = build_chrome_account_tabs(fid, snapshot, matched["token"])
        if banner:
            text = f"{banner}\n\n{text}"
        _render(service, chat_id, message_id, fid, text, rows)
    elif action == "resrc_collapse_chrome":
        # 🌐 Tap the Chrome heading again: back to the plain collapsed
        # CURRENT RUNNING panel on the SAME message. NO rescan - the CURRENT
        # ResourceRegistry snapshot only.
        _render(service, chat_id, message_id, fid, *build_current_running_menu(
            fid, _snapshot()))
    elif action == "resrc_run":
        _render(service, chat_id, message_id, fid, *build_current_running_menu(
            fid, _snapshot()))
    elif action == "resrc_app_actions":
        # Tap ONE app in the expanded Applications list: reveal THAT app's
        # inline action buttons (Focus / Screenshot / Close / Edit / Hide
        # Actions) on the SAME Telegram message; Applications stays expanded.
        resource = resource_registry.get(param)
        if resource is None:
            # Stale generation / app gone: never act - refresh the SAME
            # control-panel message with the latest resources (read-only).
            _stale_resource_changed(service, chat_id, message_id)
            return
        app_fid = resource.feature_id or _feature_ctx()
        _render(service, chat_id, message_id, app_fid,
                *build_current_running_expanded_apps(
                    app_fid, _snapshot(), app_id=param))
    elif action == "resrc_hide_app_actions":
        # Lock the inline action buttons again; Applications remain expanded
        # on the SAME message with no app actions open.
        _render(service, chat_id, message_id, fid,
                *build_current_running_expanded_apps(fid, _snapshot()))
    elif action == "resrc_tab_actions":
        # Tap ONE tab in the expanded Chrome list: reveal THAT tab's inline
        # action buttons (Focus / Screenshot / Close / Edit / Hide Actions)
        # on the SAME Telegram message; Chrome stays expanded.
        resource = resource_registry.get(param)
        if resource is None:
            # Stale generation / tab gone: never act - refresh the SAME
            # control-panel message with the latest resources (read-only).
            _stale_resource_changed(service, chat_id, message_id)
            return
        tab_fid = resource.feature_id or _feature_ctx()
        # Stay inside the SAME Gmail/profile account view with THIS tab
        # selected and its inline actions shown.
        if not _render_chrome_account_tab(
                service, chat_id, message_id, tab_fid, param):
            # Tab not placeable in an account (shouldn't happen) - safe fallback
            # to the account list.
            _render(service, chat_id, message_id, tab_fid,
                    *build_chrome_account_list(tab_fid, _snapshot()))
    elif action == "resrc_hide_tab_actions":
        # Clear the tab selection; stay inside the SAME account tabs view. The
        # account token is carried in the callback param.
        built = build_chrome_account_tabs(fid, _snapshot(), param)
        if built is None:
            _render(service, chat_id, message_id, fid,
                    *build_chrome_account_list(fid, _snapshot()))
        else:
            _render(service, chat_id, message_id, fid, *built)
    elif action == "resrc_wmenu":
        _render(service, chat_id, message_id, fid, *build_chrome_accounts_menu(
            fid, _snapshot()))
    elif action in ("resrc_wtabs", "resrc_refresh_tabs"):
        hwnd_s, _, page_s = (param or "").partition("@")
        if not hwnd_s:
            service._send(chat_id, "No Chrome window selected.")
            return
        try:
            page = int(page_s or 0)
        except ValueError:
            page = 0
        if action == "resrc_refresh_tabs":
            _rescan()
        built = build_window_tabs_menu(fid, _snapshot(), int(hwnd_s), page)
        if built is None:
            service._send(chat_id,
                          "That Chrome window is no longer in the current "
                          "scan. Use Refresh.")
            return
        _render(service, chat_id, message_id, fid, *built)
    elif action == "resrc_allapps":
        try:
            page = int(param or 0)
        except ValueError:
            page = 0
        fctx = _feature_ctx()
        _render(service, chat_id, message_id, fctx, *build_all_apps_menu(
            fctx, _snapshot(), page))
    elif action == "resrc_show":
        resource = resource_registry.get(param)
        if resource is None:
            service._send(chat_id,
                          "That resource is no longer in the current scan. "
                          "Use Refresh.")
            return
        _render(service, chat_id, message_id, resource.feature_id,
                *build_resource_detail(resource, read_only=False))
    elif action == "resrc_tab_show":
        resource = resource_registry.get(param)
        if resource is None:
            service._send(chat_id,
                          "That tab is no longer in the current scan. "
                          "Use Refresh.")
            return
        _render(service, chat_id, message_id, resource.feature_id,
                *build_chrome_tab_detail(resource, read_only=False))
    elif action == "resrc_tabs":
        _render(service, chat_id, message_id, fid, *build_chrome_tabs_menu(
            fid, _snapshot()))
    elif action == "resrc_tabs_refresh":
        _rescan()
        _render(service, chat_id, message_id, fid, *build_chrome_tabs_menu(
            fid, _snapshot()))
    elif action == "resrc_screen":
        _send_current_screen(service, chat_id, message_id, fid)
    elif action == "resrc_focus":
        _focus_resource(service, chat_id, message_id, param)
    elif action == "resrc_shot":
        _shot_resource(service, chat_id, message_id, param)
    elif action == "resrc_edit":
        _edit_resource(service, chat_id, message_id, param)
    elif action == "resrc_close":
        # Explicit user action: close ONE exact Chrome tab (never the
        # Chrome process, never every tab, never a stale resource).
        _close_resource(service, chat_id, message_id, param,
                        callback_id=callback_id)
    elif action == "resrc_confirm_close":
        # Explicit CONFIRMED close for an application window flagged
        # close_requires_confirmation (File Explorer, VS Code, Terminal...):
        # still exactly ONE HWND, still a graceful close request only.
        _close_resource(service, chat_id, message_id, param, confirmed=True,
                        callback_id=callback_id)
    elif action == "resrc_continue":
        _continue_review(service, chat_id, user_id, param)
    elif action == "resrc_cancel":
        _cancel_review(service, chat_id, user_id, param)
    else:
        service._send(chat_id, "Unknown resource action.")


def _edit_resource(service, chat_id, message_id, resource_id) -> None:
    """Placeholder for the Edit action on ONE exact resource.

    PURPOSELY PLACEHOLDER-ONLY: it never types text, clicks inside an app,
    sends keyboard input, edits a file or webpage, executes JavaScript or any
    CDP mutation command, focuses/closes/screenshots anything, or changes the
    feature stage. Applications: the SAME Resource Review control-panel message
    is re-rendered as the CURRENT RUNNING list with Applications expanded, the
    app still selected, its inline action buttons visible and the placeholder
    notice prepended above them. Chrome tabs: the SAME message is re-rendered
    as the CURRENT RUNNING list with Chrome expanded, the tab still selected,
    its inline action buttons visible and the placeholder notice prepended
    above them.

    Stale or unknown resources (old generation / tab gone) perform NOTHING and
    reuse the existing "Resource list changed" stale behavior.
    """
    target = resource_registry.get(resource_id)
    if target is None:
        _stale_resource_changed(service, chat_id, message_id)
        return
    if target.resource_type != "browser_tab":
        # Application Edit: stay in CURRENT RUNNING on the SAME panel with
        # Applications still EXPANDED, THIS app still selected and its inline
        # action buttons visible - the placeholder notice is prepended above
        # them. No rescan, no detail menu, no feature-stage change.
        text, rows = build_current_running_expanded_apps(
            target.feature_id,
            _snapshot(),
            app_id=target.resource_id
        )
        _render(service, chat_id, message_id, target.feature_id,
                EDIT_PLACEHOLDER_NOTICE + "\n\n" + text, rows)
        return
    # Chrome tab Edit: stay in CURRENT RUNNING on the SAME panel with Chrome
    # still selected inside its Gmail/profile account view - the placeholder
    # notice is prepended above its inline action buttons. No rescan, no detail
    # menu, no feature-stage change.
    if not _render_chrome_account_tab(
            service, chat_id, message_id, target.feature_id,
            target.resource_id, banner=EDIT_PLACEHOLDER_NOTICE):
        _render(service, chat_id, message_id, target.feature_id,
                *build_chrome_account_list(target.feature_id, _snapshot()))


def _stale_resource_changed(service, chat_id, message_id) -> None:
    """Generation mismatch: never act; refresh the SAME panel to the latest."""
    service._send(
        chat_id,
        "\u26a0\ufe0f Resource list changed. Showing the latest resources.")
    _rescan()
    fid = _feature_ctx()
    _render(service, chat_id, message_id, fid, *build_current_running_menu(
        fid, _snapshot()))


def _render_window_tabs(service, chat_id, message_id, hwnd: int,
                        banner: str = "") -> bool:
    """Re-render the SAME panel with one Chrome window's live tab context."""
    fid = _feature_ctx()
    built = build_window_tabs_menu(fid, _snapshot(), int(hwnd), 0)
    if built is None:
        return False
    text, rows = built
    if banner:
        text = f"{banner}\n{text}"
    _render(service, chat_id, message_id, fid, text, rows)
    return True


def _focus_resource(service, chat_id, message_id, resource_id) -> None:
    """Focus ONE exact user-selected Chrome tab (stable resource identity).

    The registry resolves the tab's exact identity (host window + runtime id,
    or CDP targetId when Chrome is CDP-controlled) and refuses stale
    generations. On success the SAME Resource Review message is re-rendered
    with that window's current tabs - no new panel, no stage change.
    """
    target = resource_registry.get(resource_id)
    hwnd = int(target.host_hwnd) if (target and target.host_hwnd) else 0
    result = resource_registry.focus(resource_id)
    if result.get("status") == "stale":
        _stale_resource_changed(service, chat_id, message_id)
        return
    if result.get("status") != "ok":
        if target is not None and target.resource_type != "browser_tab":
            # Application window: Windows refused foreground activation for
            # the EXACT HWND. Never fail silently - report visibly ON THE
            # SAME panel so the action menu stays usable. (Chrome tabs keep
            # their existing failure reporting below.)
            title = (target.title or "window")[:60]
            refreshed = resource_registry.get(resource_id)
            if refreshed is not None:
                text, rows = build_resource_detail(refreshed, read_only=False)
                _render(service, chat_id, message_id, target.feature_id,
                        f"\u26a0\ufe0f Unable to focus: {title}\n\n{text}",
                        rows)
                return
        service._send(
            chat_id,
            "\u26a0\ufe0f Focus failed: "
            f"{result.get('message', 'Target is not available.')}")
        return
    if target is not None and target.resource_type != "browser_tab":
        # Application (File Explorer, Windows Terminal, IDE, VS Code, ...):
        # it has no tab list. Stay in the SAME CURRENT RUNNING panel - keep
        # Applications expanded with THIS app still selected and its inline
        # action buttons visible (Focus / Screenshot / Close / Edit / Hide
        # Actions), and put the focus acknowledgement in the header. Only the
        # EXACT HWND resolved by the registry for this resource was brought
        # to the foreground.
        title = (target.title or "window")[:60]
        text, rows = build_current_running_expanded_apps(
            target.feature_id, _snapshot(), app_id=target.resource_id)
        _render(service, chat_id, message_id, target.feature_id,
                f"\u2705 Focused: {title}\n\n{text}", rows)
        return
    # Chrome tab (UIA or CDP): no drill-down, no separate message. Stay in
    # the SAME CURRENT RUNNING panel - keep Chrome expanded with THIS tab
    # still selected and its inline action buttons visible (Focus /
    # Screenshot / Close / Edit / Hide Actions), with the focus
    # acknowledgement in the header. Only the exact tab resolved by the
    # registry for this resource was activated.
    title = ((target.title if target else "") or "Chrome tab")[:60]
    tab_fid = target.feature_id if target else _feature_ctx()
    # Stay inside the SAME Gmail/profile account view with THIS tab selected.
    if not _render_chrome_account_tab(
            service, chat_id, message_id, tab_fid, resource_id,
            banner=f"\u2705 Focused: {title}"):
        _render(service, chat_id, message_id, tab_fid,
                *build_chrome_account_list(tab_fid, _snapshot()))


def _close_resource(service, chat_id, message_id, resource_id,
                    confirmed: bool = False, callback_id=None) -> None:
    """Close ONE exact user-selected resource; refresh the SAME panel.

    Chrome tabs: the registry closes only that exact tab (never the Chrome
    process, never every tab). Application windows: the registry posts a
    graceful WM_CLOSE to only that exact HWND (never the process, never
    sibling windows of the same app) and refuses stale generations. After a
    successful close the SAME Resource Review message is edited with a fresh
    scan so the closed resource disappears from the list.
    """
    target = resource_registry.get(resource_id)
    is_app = bool(target is not None
                  and target.resource_type != "browser_tab")
    if (is_app and not confirmed
            and getattr(target, "close_requires_confirmation", False)):
        # Risky application window: ask the user to confirm ON THIS PANEL
        # before any WM_CLOSE is posted. Nothing has been closed yet.
        _render(service, chat_id, message_id, target.feature_id,
                *build_close_confirmation(target))
        return
    hwnd = int(target.host_hwnd) if (target and target.host_hwnd) else 0
    title = ((target.title if target else "") or "tab")[:60]
    # Capture the tab's Chrome account (stable identity key) BEFORE closing so
    # we can return to the SAME account view afterwards.
    pre_account_key = ""
    if not is_app:
        pre_tok = _account_token_for_tab(_snapshot(), resource_id)
        if pre_tok:
            from jarvis_core.resource_menus import resolve_chrome_account
            _acc = resolve_chrome_account(_snapshot(), pre_tok)
            pre_account_key = (_acc or {}).get("key") or ""
    result = resource_registry.close(resource_id, confirmed=confirmed)
    if result.get("status") == "stale":
        _stale_resource_changed(service, chat_id, message_id)
        return
    if result.get("status") != "ok":
        service._send(
            chat_id,
            "\u26a0\ufe0f Close failed: "
            f"{result.get('message', 'Target is not available.')}")
        return
    if is_app and not result.get("closed_confirmed", False):
        # Graceful WM_CLOSE was delivered but the EXACT HWND is still alive:
        # the application is showing its own unsaved-work / confirmation
        # dialog. Jarvis NEVER dismisses that dialog - report visibly and
        # leave the Resource Review panel untouched for the user.
        service._send(chat_id, result.get(
            "message",
            "\u26a0\ufe0f Close requires confirmation on the laptop."))
        return
    _rescan()
    if is_app:
        # Application closed: refresh back to the SAME panel's CURRENT
        # RUNNING list with Applications still EXPANDED (closed window gone,
        # all other applications kept, no app action panel left selected).
        fid = _feature_ctx()
        text, rows = build_current_running_expanded_apps(fid, _snapshot())
        _render(service, chat_id, message_id, fid,
                f"\u2705 Closed: {title}\n\n{text}", rows)
        return
    # Chrome tab closed: seamlessly continue INSIDE the same Gmail/profile
    # account view. No "Closed tab" banner in the body and no navigation-level
    # change - only a short callback toast for feedback. The account is
    # re-matched by its STABLE identity captured BEFORE the close/rescan; the
    # OLD generation token is never reused. Selection/actions are cleared
    # (build_chrome_account_tabs called with no tab_id).
    fid = _feature_ctx()
    snapshot = _snapshot()
    from jarvis_core.resource_menus import resolve_chrome_account_by_key
    matched = (resolve_chrome_account_by_key(snapshot, pre_account_key)
               if pre_account_key else None)
    if matched is not None:
        # Account still has tabs: re-render its remaining tabs (NEW token),
        # selection cleared, updated count.
        service._answer_callback(callback_id, "\u274c Tab closed")
        _render(service, chat_id, message_id, fid,
                *build_chrome_account_tabs(fid, snapshot, matched["token"]))
        return
    # Last tab of that account was closed: drop ONE level to the Chrome account
    # list (the now-empty account is gone). Short toast, no banner.
    service._answer_callback(callback_id, "\u274c Last tab closed")
    _render(service, chat_id, message_id, fid,
            *build_chrome_account_list(fid, snapshot))


def _shot_resource(service, chat_id, message_id, resource_id) -> None:
    """📸 Capture the ONE exact selected resource and deliver it to Telegram.

    Identity is the generation-stamped ``resource_id`` + the exact HWND the
    registry recorded for it - never a process/executable name. A stale
    generation or a vanished window is answered honestly (panel refresh or
    a visible failure) and NEVER captures another window or the desktop.
    The screenshot is delivered as a separate image message and the
    Resource Review control panel stays valid: state remains RESOURCE_REVIEW.
    """
    try:
        res = resource_registry.screenshot(resource_id)
    except Exception as exc:
        logger.exception("Screenshot failed for %s", resource_id)
        service._send(
            chat_id,
            f"\u26a0\ufe0f Screenshot failed: could not be completed "
            f"({type(exc).__name__}).")
        return
    if res.get("status") == "stale":
        # Old generation / resource gone: never capture anything - refresh
        # the SAME panel with the latest resources (existing stale behavior).
        _stale_resource_changed(service, chat_id, message_id)
        return
    resource = res.get("resource") or {}
    title = (resource.get("title") or "").strip()
    caption = (
        f"\U0001f4f8 Screenshot: {title[:80]}"
        if title else "\U0001f4f8 Screenshot"
    )
    _deliver_screenshot(service, chat_id, res,
                        f"feature:resrc_shot:{resource_id}", caption=caption)


def _send_current_screen(service, chat_id, message_id, feature_id) -> None:
    try:
        result = resource_registry.screenshot_current_screen()
    except Exception:
        logger.exception("Current-screen capture failed")
        service._send(chat_id, "Current screen capture failed.")
        return
    delivered = _deliver_screenshot(service, chat_id, result,
                                    f"feature:resrc_screen:{feature_id or ''}")
    if delivered:
        # Restore the menu the capture was triggered from.
        _render(service, chat_id, message_id, feature_id,
                *build_resource_review_home(feature_id, _snapshot()))
def _deliver_screenshot(service, chat_id, result, retry_callback,
                        caption: str = "") -> bool:
    status = result.get("status")
    if status == "ok" and result.get("file"):
        path = result["file"]
        delivered = False
        try:
            service._require_api().send_photo(
                chat_id, path, caption=caption)
            delivered = True
        except TelegramAPIError as exc:
            logger.warning("Telegram photo delivery failed: %s", exc)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        if not delivered:
            service._send(
                chat_id,
                "Screenshot captured but Telegram delivery failed. "
                "Please retry.",
                reply_markup={"inline_keyboard": [[
                    {"text": "Retry", "callback_data": retry_callback},
                ]]})
        return delivered
    if status == "blocked":
        service._send(chat_id, result.get("message") or "Screenshot blocked.")
    else:
        service._send(
            chat_id,
            f"\u26a0\ufe0f Screenshot failed: "
            f"{result.get('message') or 'capture failed.'}")
    return False


def _continue_review(service, chat_id, user_id, feature_id) -> None:
    orch = service.feature_orchestrator
    try:
        result = orch.continue_after_resource_review(
            feature_id, telegram_user_id=user_id)
    except Exception:
        logger.exception("Could not continue feature %s", feature_id)
        service._send(chat_id, "Could not continue that feature.")
        return
    service._send(chat_id, result.get("message") or "Feature continued.")


def _cancel_review(service, chat_id, user_id, feature_id) -> None:
    orch = service.feature_orchestrator
    try:
        result = orch.cancel(feature_id, telegram_user_id=user_id)
    except Exception:
        logger.exception("Could not cancel feature %s", feature_id)
        service._send(chat_id, "Could not cancel that feature.")
        return
    service._send(chat_id, result.get("message") or "Feature cancelled.")