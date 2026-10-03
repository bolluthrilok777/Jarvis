"""JarvisKiroSession — production-grade Kiro application + session ownership.

Mirrors the hardened Chrome ``JarvisBrowserWindowController`` pattern, but for
the Kiro IDE (an Electron/VS Code fork). It answers, deterministically and
fail-closed, four questions for every workflow that reaches Kiro:

  1. Is the target a REAL Kiro.exe process (never a Chrome/Edge window that
     merely shares the ``Chrome_WidgetWin_1`` window class or has "Kiro" in its
     title — the HWND 722342 root-cause bug)?
  2. Is the Kiro window on EXACTLY the workflow's project (``C:\\Project Opus``)?
  3. Which exact Jarvis-OWNED Kiro chat/session does this workflow use, and is
     that session STILL the active one (or did a human switch to another)?
  4. After a restart/reconnect, can the SAME session be re-selected and the
     workflow resumed from its exact durable substate?

Kiro exposes no stable native chat/session id via UIA, so ownership is proven
by (Kiro.exe pid) + project verification + a Jarvis-minted session id bound to
an observable *active-session fingerprint*. The binding is persisted in the
workflow ``metadata["kiro_binding"]`` bag (durable across restart) following the
existing ``set_metadata`` convention. Nothing here types/clicks — it is the
identity/ownership layer the WorkflowKiroService consults before any action.
"""
from __future__ import annotations

import logging
from typing import Optional

from jarvis_core.timezone_utils import jarvis_now

logger = logging.getLogger("jarvis.kirosession")

# Metadata bag key for the durable binding (mirrors chrome binding conventions).
BINDING_KEY = "kiro_binding"
# Metadata bag key for a detected, un-resolved human session switch.
MISMATCH_KEY = "kiro_session_mismatch"
OWNER = "jarvis"


def _now_iso() -> str:
    return jarvis_now().isoformat()


class KiroSessionError(Exception):
    """Ownership/identity error (mapped to 4xx / fail-closed by callers)."""


class JarvisKiroSessionManager:
    """Create, persist, verify, and re-attach Jarvis-owned Kiro sessions.

    All Kiro/desktop interaction is delegated to an injected controller (the
    real :class:`KiroController` in production, a fake in tests) exposing:
    ``enumerate_windows``, ``window_is_kiro``, ``window_process``,
    ``verify_project``, ``new_session``, ``active_session_fingerprint``,
    ``identify_session``. Persistence is delegated to an injected engine
    exposing ``get_workflow``/``set_metadata``.
    """

    def __init__(self, *, engine=None, kiro_controller=None,
                 project_path: str = r"C:\Project Opus") -> None:
        self._engine_ref = engine
        self._kiro_ref = kiro_controller
        self._project_path = project_path

    # ── Dependencies ──────────────────────────────────────────────────────────

    @property
    def engine(self):
        if self._engine_ref is not None:
            return self._engine_ref
        from jarvis_core.workflow_engine import workflow_engine
        return workflow_engine

    @property
    def kiro(self):
        if self._kiro_ref is not None:
            return self._kiro_ref
        from jarvis_core.kiro_controller import KiroController
        self._kiro_ref = KiroController()
        return self._kiro_ref

    # ── Binding read/write ─────────────────────────────────────────────────────

    def get_binding(self, workflow_id: str) -> Optional[dict]:
        wf = self.engine.get_workflow(workflow_id)
        if wf is None:
            return None
        return (wf.metadata or {}).get(BINDING_KEY) or None

    def _persist_binding(self, workflow_id: str, binding: dict) -> None:
        self.engine.set_metadata(workflow_id, BINDING_KEY, binding)

    # ── Kiro.exe process identity gate ─────────────────────────────────────────

    def assert_kiro_window(self, hwnd: int) -> dict:
        """Return the process info IFF *hwnd* is a real Kiro.exe window, else
        raise. This is the non-negotiable gate that rejects Chrome windows."""
        if not self.kiro.window_is_kiro(hwnd):
            info = self.kiro.window_process(hwnd)
            raise KiroSessionError(
                f"NOT_KIRO_PROCESS hwnd={hwnd} "
                f"exe={info.get('executable') or info.get('process_name')!r}")
        return self.kiro.window_process(hwnd)

    def verify_project_window(self, hwnd: int) -> dict:
        """Prove *hwnd* is a Kiro.exe window on EXACTLY the project. Fail-closed.
        Returns the verify_project result (with ``is_kiro_process``)."""
        self.assert_kiro_window(hwnd)
        v = self.kiro.verify_project(hwnd, self._project_path)
        if not v.get("verified"):
            raise KiroSessionError(
                f"KIRO_PROJECT_MISMATCH observed={v.get('observed')!r}")
        return v

    # ── First-time: create a NEW Jarvis-owned session + bind ───────────────────

    def create_and_bind(self, workflow_id: str, hwnd: int, *,
                         iteration: int = 0, avoid_hwnds=None) -> dict:
        """Create a NEW Jarvis-owned Kiro session in the verified project window
        and persist a durable :class:`JarvisKiroSessionBinding`.

        The workflow must NOT reuse whatever human session is currently active —
        we always create a fresh session so Jarvis owns it. The binding stores a
        durable SESSION LOCATOR (title + automation_id + ordinal) — enough to
        FIND and re-select the session later — not merely a fingerprint. Returns
        the binding.
        """
        proc = self.assert_kiro_window(hwnd)
        self.verify_project_window(hwnd)
        # Create the fresh session via the real accessible control.
        loc = self.kiro.create_new_session(hwnd, avoid_hwnds=avoid_hwnds)
        ident = self.kiro.identify_session(hwnd, workflow_id)
        now = _now_iso()
        locator = {
            "title": loc.get("title") or ident.get("kiro_session_title") or "",
            "automation_id": loc.get("automation_id", ""),
            "ordinal": loc.get("ordinal"),
        }
        binding = {
            "binding_id": f"kiro-binding:{workflow_id}:{iteration}:{int(hwnd)}",
            "workflow_id": workflow_id,
            "owner": OWNER,
            "project_path": self._project_path,
            "kiro_process_pid": proc.get("pid"),
            "kiro_process_executable": proc.get("executable", ""),
            "kiro_window_hwnd": int(hwnd),
            "kiro_session_id": ident["kiro_session_id"],
            "kiro_session_title": locator["title"],
            # DURABLE LOCATOR (primary): used to relocate + re-select the session.
            "session_locator": locator,
            # Secondary equality signal only (never used as a locator).
            "session_fingerprint": loc.get("fingerprint", ""),
            "session_created_via": loc.get("created_via"),
            "created_at": now,
            "last_verified_at": now,
            "iteration": iteration,
            "status": "bound",
        }
        self._persist_binding(workflow_id, binding)
        logger.info("Kiro session bound for %s → hwnd=%s pid=%s session=%s locator=%s",
                    workflow_id, hwnd, proc.get("pid"), ident["kiro_session_id"], locator)
        return binding

    # ── Liveness / ownership verification ──────────────────────────────────────

    def is_binding_live(self, workflow_id: str) -> bool:
        """True only when the persisted binding still points at a live Kiro.exe
        window, on the right project, with the SAME active-session fingerprint.

        A persisted HWND alone is never trusted. Returns False (fail-closed) on
        any drift: window gone, wrong process, wrong project, or the active
        session fingerprint changed (a human switched sessions)."""
        binding = self.get_binding(workflow_id)
        if not binding or binding.get("owner") != OWNER:
            return False
        if str(binding.get("workflow_id")) != str(workflow_id):
            return False
        hwnd = binding.get("kiro_window_hwnd")
        if not hwnd:
            return False
        open_hwnds = {int(w["hwnd"]) for w in self.kiro.enumerate_windows()}
        if int(hwnd) not in open_hwnds:
            return False
        if not self.kiro.window_is_kiro(int(hwnd)):
            return False
        v = self.kiro.verify_project(int(hwnd), self._project_path)
        if not v.get("verified"):
            return False
        # Active session must still be OUR session (detects a human switch).
        return self._active_is_bound(binding, int(hwnd))

    def _active_is_bound(self, binding: dict, hwnd: int) -> bool:
        """True when the CURRENT active session equals the bound session by the
        durable locator (title/automation_id), with the fingerprint as a
        secondary confirmation. Locator is authoritative."""
        cur = self.kiro.active_session(hwnd)
        loc = binding.get("session_locator") or {}
        want_title = str(loc.get("title") or binding.get("kiro_session_title") or "")
        want_aid = str(loc.get("automation_id") or "")
        cur_title = str(cur.get("title") or "")
        cur_aid = str(cur.get("automation_id") or "")
        # Primary: automation_id (most stable within a session list snapshot).
        if want_aid and cur_aid:
            return want_aid == cur_aid
        # Else title equality (the durable primary key).
        if want_title:
            return cur_title == want_title
        # Last resort: fingerprint equality.
        want_fp = str(binding.get("session_fingerprint") or "")
        return bool(want_fp) and want_fp == str(cur.get("fingerprint") or "")

    def active_session_matches(self, workflow_id: str) -> dict:
        """Compare the CURRENT active Kiro session against the bound one.

        Returns ``{"match": bool, "reason", "expected", "active", "hwnd"}``.
        ``match`` is False (with a reason) for: no binding, window gone, wrong
        process, wrong project, or a differing active-session fingerprint.
        Never mutates anything.
        """
        binding = self.get_binding(workflow_id)
        if not binding:
            return {"match": False, "reason": "no_binding", "expected": None,
                    "active": None, "hwnd": None}
        hwnd = binding.get("kiro_window_hwnd")
        loc = binding.get("session_locator") or {}
        expected = loc.get("title") or binding.get("kiro_session_title")
        result = {"match": False, "reason": "", "expected": expected,
                  "active": None, "hwnd": hwnd,
                  "expected_session_id": binding.get("kiro_session_id"),
                  "expected_session_title": binding.get("kiro_session_title"),
                  "expected_locator": loc}
        if not hwnd:
            result["reason"] = "no_window"
            return result
        open_hwnds = {int(w["hwnd"]) for w in self.kiro.enumerate_windows()}
        if int(hwnd) not in open_hwnds:
            result["reason"] = "window_gone"
            return result
        if not self.kiro.window_is_kiro(int(hwnd)):
            result["reason"] = "not_kiro_process"
            return result
        v = self.kiro.verify_project(int(hwnd), self._project_path)
        if not v.get("verified"):
            result["reason"] = "project_mismatch"
            return result
        cur = self.kiro.active_session(int(hwnd))
        result["active"] = cur.get("title")
        result["active_session_title"] = cur.get("title")
        result["active_locator"] = {"title": cur.get("title"),
                                    "automation_id": cur.get("automation_id")}
        if not self._active_is_bound(binding, int(hwnd)):
            result["reason"] = "session_switched"
            return result
        result["match"] = True
        result["reason"] = "ok"
        return result

    # ── Human session-switch detection + fail-closed pause ─────────────────────

    def detect_and_record_mismatch(self, workflow_id: str, *,
                                    substate: str = "") -> Optional[dict]:
        """Verify ownership before a dangerous op. If the active session no
        longer matches the bound session, persist a durable mismatch record and
        return it (caller must PAUSE, take ZERO Kiro actions). Returns None when
        ownership is intact (safe to proceed).
        """
        cmp = self.active_session_matches(workflow_id)
        if cmp.get("match"):
            return None
        # Fail closed: record the mismatch durably for the confirmation flow.
        import secrets
        binding = self.get_binding(workflow_id) or {}
        record = {
            "mismatch_id": secrets.token_hex(8),
            "workflow_id": workflow_id,
            "detected_at": _now_iso(),
            "reason": cmp.get("reason"),
            "expected_session": binding.get("kiro_session_id"),
            "expected_fingerprint": cmp.get("expected"),
            "active_fingerprint": cmp.get("active"),
            "active_session_title": cmp.get("active_session_title"),
            "workflow_substate": substate,
            "resolved": False,
            "decision": None,
        }
        self.engine.set_metadata(workflow_id, MISMATCH_KEY, record)
        logger.warning("Kiro session mismatch for %s: %s (expected=%s active=%s)",
                       workflow_id, cmp.get("reason"),
                       cmp.get("expected"), cmp.get("active"))
        return record

    def get_mismatch(self, workflow_id: str) -> Optional[dict]:
        wf = self.engine.get_workflow(workflow_id)
        if wf is None:
            return None
        return (wf.metadata or {}).get(MISMATCH_KEY) or None

    def clear_mismatch(self, workflow_id: str, *, decision: str) -> None:
        rec = dict(self.get_mismatch(workflow_id) or {})
        if not rec:
            return
        rec["resolved"] = True
        rec["decision"] = decision
        rec["resolved_at"] = _now_iso()
        self.engine.set_metadata(workflow_id, MISMATCH_KEY, rec)

    # ── Continue: re-select the ORIGINAL Jarvis session ────────────────────────

    def reselect_bound_session(self, workflow_id: str, *, avoid_hwnds=None) -> dict:
        """Re-activate the workflow's ORIGINAL Jarvis-owned Kiro session.

        Verifies Kiro.exe + project on the bound hwnd, focuses it, and confirms
        the active-session fingerprint matches the binding. Fail-closed: raises
        if the original session cannot be proven (caller then offers New/Cancel).
        Returns the refreshed binding.
        """
        binding = self.get_binding(workflow_id)
        if not binding:
            raise KiroSessionError("NO_KIRO_BINDING")
        hwnd = binding.get("kiro_window_hwnd")
        if not hwnd:
            raise KiroSessionError("NO_KIRO_WINDOW")
        self.assert_kiro_window(int(hwnd))
        self.verify_project_window(int(hwnd))
        # Bring the exact bound window to the foreground (safe; no typing).
        try:
            self.kiro.focus_window(int(hwnd), avoid_hwnds=avoid_hwnds)
        except Exception:
            logger.debug("reselect focus failed", exc_info=True)
        # If the bound session is already active, we're done. Otherwise ACTIVELY
        # relocate + select it via the durable locator (title/automation_id),
        # re-discovering the current session list (runtime_ids are volatile).
        if not self._active_is_bound(binding, int(hwnd)):
            loc = binding.get("session_locator") or {
                "title": binding.get("kiro_session_title")}
            selected = self.kiro.select_session(int(hwnd), loc, avoid_hwnds=avoid_hwnds)
            if not selected:
                # Fail closed: could not LOCATE/select the original session.
                raise KiroSessionError(
                    f"KIRO_ORIGINAL_SESSION_NOT_FOUND locator={loc}")
            # Verify the selection actually made our session active.
            if not self._active_is_bound(binding, int(hwnd)):
                raise KiroSessionError("KIRO_ORIGINAL_SESSION_NOT_ACTIVE_AFTER_SELECT")
        b = dict(binding)
        b["last_verified_at"] = _now_iso()
        b["status"] = "bound"
        self._persist_binding(workflow_id, b)
        return b

    # ── New session: rebind, keep history ──────────────────────────────────────

    def rebind_new_session(self, workflow_id: str, hwnd: int, *,
                           iteration: int = 0, avoid_hwnds=None) -> dict:
        """Create a NEW Jarvis-owned session for the SAME workflow and rebind.

        The prior binding is retained in a durable ``kiro_binding_history`` list
        so the old session identity is never lost. Returns the new binding.
        """
        prior = self.get_binding(workflow_id)
        new_binding = self.create_and_bind(
            workflow_id, hwnd, iteration=iteration, avoid_hwnds=avoid_hwnds)
        if prior:
            wf = self.engine.get_workflow(workflow_id)
            history = list((wf.metadata or {}).get("kiro_binding_history") or [])
            history.append(prior)
            self.engine.set_metadata(workflow_id, "kiro_binding_history", history)
        return new_binding


# Module-level singleton (real infrastructure), project convention.
jarvis_kiro_session_manager = JarvisKiroSessionManager()
