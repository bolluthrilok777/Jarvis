"""WorkflowKiroService — Part 7 coordinator.

Consumes a Part 6 APPROVAL and drives the approved prompt into the EXACT Kiro
project as a dedicated, non-conflicting session:

    (WAITING_FOR_APPROVAL, approval.status == approved)
      → validate the approval (state + status + non-empty prompt + hash match)
      → acquire the EXCLUSIVE KiroAutomationLock
      → open C:\\Project Opus in a DEDICATED Kiro window (never the dev session)
      → verify the workspace is EXACTLY that project (normalized path)
      → prepare a durable Jarvis-side session identity
      → focus the composer, paste the EXACT approved prompt, submit ONCE
      → positively verify the submission (composer cleared + in history / working)
      → WAITING_FOR_APPROVAL → KIRO_STARTING → KIRO_RUNNING
      → persist kiro session/window/project identity
                                              (legal FAILED/recovery on error)

Part 7 STOPS there. It NEVER auto-Allows a permission prompt, decides
permissions, monitors Kiro, detects completion, or copies any Kiro result —
those are Parts 8-10. If Kiro shows a permission prompt right after submission,
that is accepted as proof Kiro received the prompt; the workflow is reported as
KIRO_WAITING_PERMISSION (detected, not decided).

Safety: the service acquires the exclusive lock, prefers a freshly-opened
dedicated window, and passes the set of protected windows (e.g. the active
developer session) to the controller so it refuses to type into them. The
blocking desktop work runs on a dedicated worker thread so the Telegram poller
and FastAPI are never blocked.
"""
from __future__ import annotations

import hashlib
import logging
import os
import queue
import threading
from typing import Callable, Optional

from jarvis_core.timezone_utils import jarvis_now

logger = logging.getLogger("jarvis.kirosvc")

PROJECT_PATH = r"C:\Project Opus"

# Realtime event types (Part 2 bus).
EV_LOCK_REQUESTED = "kiro_lock_requested"
EV_LOCK_ACQUIRED = "kiro_lock_acquired"
EV_LOCK_FAILED = "kiro_lock_failed"
EV_OPENING = "kiro_opening"
EV_WINDOW_FOUND = "kiro_window_found"
EV_PROJECT_VERIFYING = "kiro_project_verifying"
EV_PROJECT_VERIFIED = "kiro_project_verified"
EV_PROJECT_MISMATCH = "kiro_project_mismatch"
EV_SESSION_PREPARING = "kiro_session_preparing"
EV_SESSION_READY = "kiro_session_ready"
EV_PROMPT_SUBMITTING = "kiro_prompt_submitting"
EV_PROMPT_SUBMITTED = "kiro_prompt_submitted"
EV_RUNNING = "kiro_running"
EV_WAITING_PERMISSION = "kiro_waiting_permission"
EV_SUBMISSION_FAILED = "kiro_submission_failed"
EV_SESSION_MISMATCH = "kiro_session_mismatch"
EV_SESSION_RESELECTED = "kiro_session_reselected"

ACTION_KIRO_SUBMIT = "kiro-submit-approved-prompt"


def _now_iso() -> str:
    return jarvis_now().isoformat()


def _sha(t: str) -> str:
    return hashlib.sha256((t or "").encode("utf-8")).hexdigest()


def _norm(path: str) -> str:
    """Normalize a Windows path for equality/prefix comparison (shared with the
    controller's normalize_path)."""
    from jarvis_core.kiro_controller import normalize_path
    return normalize_path(path)


class KiroServiceError(Exception):
    """Validation/coordination error (mapped to 4xx by routes)."""


class WorkflowKiroService:
    """Schedules and runs the approved-prompt → Kiro submission for a workflow."""

    def __init__(
        self,
        *,
        engine=None,
        kiro_controller=None,
        lock=None,
        publish_fn: Optional[Callable] = None,
        run_inline: bool = False,
        project_path: str = PROJECT_PATH,
    ) -> None:
        self._engine_ref = engine
        self._kiro_ref = kiro_controller
        self._lock_ref = lock
        self._publish_fn = publish_fn
        self._run_inline = run_inline
        self._project_path = project_path

        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._start_lock = threading.RLock()
        self._active: set[str] = set()
        self._active_lock = threading.Lock()
        # Windows the service must never automate (e.g. the active developer
        # Kiro session). Populated by callers/tests; empty by default.
        self._protected_hwnds: set[int] = set()

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

    @property
    def lock(self):
        if self._lock_ref is not None:
            return self._lock_ref
        from jarvis_core.kiro_automation_lock import kiro_automation_lock
        return kiro_automation_lock

    @property
    def session_mgr(self):
        """Jarvis-owned Kiro session/ownership manager (identity layer)."""
        if getattr(self, "_session_mgr_ref", None) is not None:
            return self._session_mgr_ref
        from jarvis_core.jarvis_kiro_session import JarvisKiroSessionManager
        self._session_mgr_ref = JarvisKiroSessionManager(
            engine=self._engine_ref, kiro_controller=self.kiro,
            project_path=self._project_path)
        return self._session_mgr_ref

    def set_session_manager(self, mgr) -> None:
        """Inject a session manager (tests)."""
        self._session_mgr_ref = mgr

    def set_protected_windows(self, hwnds) -> None:
        """Register Kiro windows the service must NEVER automate (dev session)."""
        self._protected_hwnds = {int(h) for h in (hwnds or [])}

    # ── Manual exclusive handoff ──────────────────────────────────────────────
    #
    # Kiro's Agent session identity / composer emptiness are NOT reliably
    # exposed via UIA, so we do NOT infer them. Instead the USER explicitly
    # confirms, from the Jarvis UI, that they have opened a blank Kiro
    # "New Session" for C:\Project Opus and will not touch Kiro until the test
    # finishes. That confirmation is a durable, single-use grant that permits
    # automating the exact confirmed (normally-protected) window for ONE
    # submission. It is a user-supplied fact, never inferred automation
    # evidence, and it is CONSUMED after a positively-verified submission.

    def grant_handoff(self, workflow_id: str, *, target_hwnd=None,
                      project_path: str = None, purpose: str = "part7_live_test") -> dict:
        """Persist a durable, user-confirmed exclusive handoff for a workflow.

        Requires the workflow to be an approved, awaiting-approval workflow (the
        same gate as starting Kiro) so a handoff cannot be granted for an
        ineligible workflow. If ``target_hwnd`` is omitted, the current single
        Project Opus Kiro window is used. Returns the handoff record.
        """
        wf = self.engine.get_workflow(workflow_id)
        if wf is None:
            raise KiroServiceError("Workflow not found")
        # Reuse the approval gate (state + status + non-empty prompt + hash).
        self._validate_approval(wf)

        project_path = project_path or self._project_path
        if _norm(project_path) != _norm(self._project_path):
            raise KiroServiceError(
                f"HANDOFF_PROJECT_MISMATCH (requested {project_path!r})")

        # Resolve the target hwnd dynamically from CURRENT Kiro windows using
        # TRUSTED workspace evidence (path containment). zero → NO_TARGET,
        # many → AMBIGUOUS, exactly one → bind it. An explicit target_hwnd, if
        # given, must itself be a currently-verifying project window.
        if target_hwnd is not None:
            target_hwnd = int(target_hwnd)
            open_hwnds = {int(w["hwnd"]) for w in self.kiro.enumerate_windows()}
            if target_hwnd not in open_hwnds:
                raise KiroServiceError("HANDOFF_TARGET_NOT_OPEN")
            if not self._window_belongs_to_project(target_hwnd):
                raise KiroServiceError("HANDOFF_TARGET_NOT_PROJECT")
        else:
            matches = self._project_window_candidates()
            if len(matches) == 0:
                raise KiroServiceError("HANDOFF_NO_TARGET_WINDOW")
            if len(matches) > 1:
                raise KiroServiceError(f"HANDOFF_AMBIGUOUS_TARGET {sorted(matches)}")
            target_hwnd = matches[0]

        record = {
            "confirmed": True,
            "confirmed_at": _now_iso(),
            "target_hwnd": int(target_hwnd),
            "project_path": self._project_path,
            "purpose": purpose,
            "consumed": False,
        }
        self.engine.set_metadata(workflow_id, "kiro_manual_handoff", record)
        logger.info("Manual exclusive handoff granted for %s → hwnd %s",
                    workflow_id, target_hwnd)
        return record

    def _window_belongs_to_project(self, hwnd: int) -> bool:
        """True when *hwnd* is a REAL Kiro.exe window AND has TRUSTED workspace
        evidence (exact-or-descendant path) belonging to C:\\Project Opus.

        HARD GATE: the owning process must be Kiro.exe. A Chrome/Edge window
        (the HWND 722342 bug) is rejected here before any path evidence is even
        considered. Uses trusted IDE paths + the containment-aware
        verify_project; never chat text or recents.
        """
        from jarvis_core.kiro_controller import path_within
        # Non-negotiable process identity gate first.
        try:
            if hasattr(self.kiro, "window_is_kiro") and not self.kiro.window_is_kiro(hwnd):
                return False
        except Exception:
            logger.debug("window_is_kiro check failed", exc_info=True)
            return False
        try:
            # 1) trusted absolute workspace/file paths from the workbench.
            for p in (self.kiro.trusted_workspace_paths(hwnd) or []):
                if path_within(p, self._project_path):
                    return True
            # 2) verify_project (also containment-aware) as a secondary source.
            v = self.kiro.verify_project(hwnd, self._project_path)
            if v.get("verified"):
                return True
            obs = v.get("observed") or ""
            if obs and path_within(obs, self._project_path):
                return True
        except Exception:
            logger.debug("window_belongs_to_project failed", exc_info=True)
        return False

    def _project_window_candidates(self) -> list:
        """Return the list of CURRENT Kiro window hwnds whose TRUSTED evidence
        belongs to C:\\Project Opus (exact or descendant). Order-stable."""
        out = []
        try:
            for w in self.kiro.enumerate_windows():
                hwnd = int(w["hwnd"])
                if self._window_belongs_to_project(hwnd):
                    out.append(hwnd)
        except Exception:
            logger.debug("project_window_candidates failed", exc_info=True)
        return out

    def _resolve_project_window_hwnd(self):
        """Back-compat: return the single project window hwnd, or None (0 or >1
        candidates both return None here; grant_handoff distinguishes them)."""
        matches = self._project_window_candidates()
        return matches[0] if len(matches) == 1 else None

    def _valid_handoff(self, wf, *, expected_prompt_hash: str):
        """Return the handoff record IFF it is a valid, unconsumed grant for the
        exact project + a currently-open project window; else None.

        Does NOT itself decide the target hwnd equality against a chosen window;
        the caller enforces target_hwnd == the window it will act on.
        """
        h = (wf.metadata or {}).get("kiro_manual_handoff") or {}
        if not h.get("confirmed") or h.get("consumed"):
            return None
        if _norm(h.get("project_path") or "") != _norm(self._project_path):
            return None
        if not h.get("target_hwnd"):
            return None
        return h

    def _consume_handoff(self, workflow_id: str) -> None:
        """Mark the manual handoff consumed (single-use). A second submission
        requires a fresh grant. Safe if metadata is missing."""
        try:
            wf = self.engine.get_workflow(workflow_id)
            h = dict((wf.metadata or {}).get("kiro_manual_handoff") or {})
            if not h:
                return
            h["confirmed"] = False
            h["consumed"] = True
            h["consumed_at"] = _now_iso()
            self.engine.set_metadata(workflow_id, "kiro_manual_handoff", h)
            logger.info("Manual handoff consumed for %s", workflow_id)
        except Exception:
            logger.debug("consume handoff failed", exc_info=True)

    # ── Realtime publish ─────────────────────────────────────────────────────

    def _publish(self, workflow_id: str, event_type: str, payload: Optional[dict] = None) -> None:
        if self._publish_fn is not None:
            try:
                self._publish_fn(workflow_id, event_type, payload or {})
            except Exception:
                logger.debug("kiro publish failed", exc_info=True)
            return
        try:
            from jarvis_core.workflow_events_bus import workflow_event_bus
            workflow_event_bus.publish(
                workflow_id,
                {"type": event_type, "workflow_id": workflow_id, "kiro": payload or {}},
            )
        except Exception:
            logger.debug("kiro publish failed", exc_info=True)

    # ── Worker lifecycle ──────────────────────────────────────────────────────

    def start_worker(self) -> None:
        if self._run_inline:
            return
        with self._start_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._worker_loop, name="workflow-kiro", daemon=True)
            self._worker.start()
            logger.info("Workflow Kiro worker started")

    def stop_worker(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=timeout)
        self._worker = None

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            wid = job.get("workflow_id")
            try:
                self._run_start(wid)
            except Exception:
                logger.exception("Kiro start failed for %s", wid)
            finally:
                if wid:
                    with self._active_lock:
                        self._active.discard(wid)

    # ── Approval validation ────────────────────────────────────────────────

    # States from which _run_start may legally (re-)drive Kiro. WAITING_FOR_
    # APPROVAL is the first-time entry; the KIRO_* states occur when resuming
    # after a session-mismatch pause/restart (idempotent submit prevents dupes).
    _RESUME_STATES = ("KIRO_STARTING", "KIRO_RUNNING", "KIRO_WAITING_PERMISSION")

    def _validate_approval(self, wf, *, allow_resume: bool = False) -> str:
        """Return the exact approved prompt or raise KiroServiceError.

        Verifies: state == WAITING_FOR_APPROVAL (or a resume state when
        ``allow_resume``), approval.status in {approved, edited},
        approved_gpt_prompt non-empty, and the approved prompt hash matches the
        recorded approval hash (no stale iteration).
        """
        ok_states = ("WAITING_FOR_APPROVAL",) + (self._RESUME_STATES if allow_resume else ())
        if wf.state not in ok_states:
            raise KiroServiceError(f"NOT_APPROVED_STATE (state={wf.state})")
        appr = (wf.metadata or {}).get("approval") or {}
        if appr.get("status") not in ("approved", "edited"):
            raise KiroServiceError(f"NOT_APPROVED (status={appr.get('status')})")
        prompt = wf.approved_gpt_prompt or ""
        if not prompt.strip():
            raise KiroServiceError("APPROVED_PROMPT_MISSING")
        # Hash match: the approved prompt must match the hash recorded at
        # approval time (guards against a stale approved prompt).
        recorded = appr.get("approved_prompt_hash")
        if recorded and _sha(prompt) != recorded:
            raise KiroServiceError("APPROVED_PROMPT_HASH_MISMATCH")
        return prompt

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, workflow_id: str) -> dict:
        """Validate + schedule the Kiro submission for a workflow."""
        wf = self.engine.get_workflow(workflow_id)
        if wf is None:
            raise KiroServiceError("Workflow not found")
        # Validate approval up front (fast 4xx). The prompt is re-read on the
        # worker to avoid acting on stale data.
        self._validate_approval(wf)
        with self._active_lock:
            if workflow_id in self._active:
                raise KiroServiceError("KIRO_START_ALREADY_RUNNING")
            self._active.add(workflow_id)
        if self._run_inline:
            try:
                self._run_start(workflow_id)
            finally:
                with self._active_lock:
                    self._active.discard(workflow_id)
        else:
            self.start_worker()
            self._queue.put({"workflow_id": workflow_id})
        return {"scheduled": True, "workflow_id": workflow_id}

    def is_active(self, workflow_id: str) -> bool:
        with self._active_lock:
            return workflow_id in self._active

    # ── The start sequence ────────────────────────────────────────────────────

    def _run_start(self, workflow_id: str) -> None:
        engine = self.engine
        wf = engine.get_workflow(workflow_id)
        if wf is None:
            return
        iteration = wf.iteration_number or 0
        lock_acquired = False
        try:
            # allow_resume=True: after a session-mismatch pause (or restart) the
            # workflow re-enters here in a KIRO_* state; the idempotent submit
            # action prevents any duplicate paste/Enter.
            prompt = self._validate_approval(wf, allow_resume=True)
            prompt_hash = _sha(prompt)

            # 1) Acquire the exclusive Kiro automation lock.
            self._publish(workflow_id, EV_LOCK_REQUESTED, {})
            if not self.lock.acquire(workflow_id):
                self._publish(workflow_id, EV_LOCK_FAILED, {"owner": self.lock.owner()})
                raise KiroServiceError(
                    f"KIRO_LOCK_HELD by {self.lock.owner()}")
            lock_acquired = True
            self._publish(workflow_id, EV_LOCK_ACQUIRED, {"owner": workflow_id})

            # 2) WAITING_FOR_APPROVAL → KIRO_STARTING (recovery-tolerant).
            if wf.state == "WAITING_FOR_APPROVAL":
                engine.transition(workflow_id, "KIRO_STARTING",
                                  message="Opening Kiro for approved prompt")

            # 3) Choose the target window.
            #  - MANUAL HANDOFF path: the user explicitly granted exclusive
            #    control of the existing (normally-protected) Project Opus
            #    window. Reuse that exact confirmed hwnd; do NOT open a new
            #    window. The hwnd is permitted for THIS action only via
            #    allow_hwnd, while the default protection remains in place.
            #  - DEFAULT path: open the EXACT project in a DEDICATED window,
            #    avoiding all protected windows.
            handoff = self._valid_handoff(engine.get_workflow(workflow_id),
                                          expected_prompt_hash=prompt_hash)
            allow_hwnd = None
            self._publish(workflow_id, EV_OPENING, {"project": self._project_path,
                          "manual_handoff": bool(handoff)})
            if handoff:
                hwnd = int(handoff["target_hwnd"])
                # Prove the confirmed window is still open + on the exact project
                # BEFORE we permit acting on it.
                open_hwnds = {int(w["hwnd"]) for w in self.kiro.enumerate_windows()}
                if hwnd not in open_hwnds:
                    raise KiroServiceError(f"HANDOFF_WINDOW_GONE hwnd={hwnd}")
                allow_hwnd = hwnd  # permit this protected hwnd for this action
                opened = {"hwnd": hwnd, "verified": True, "manual_handoff": True}
            else:
                opened = self.kiro.open_project(
                    self._project_path, avoid_hwnds=self._protected_hwnds,
                    new_window=True)
                hwnd = opened["hwnd"]
            # HARD PROCESS-IDENTITY GATE (root-cause fix for HWND 722342): the
            # chosen window MUST be a real Kiro.exe window. A Chrome/Edge window
            # (shared Chrome_WidgetWin_1 class) is refused here before any focus,
            # paste, or Enter — regardless of handoff vs default path.
            try:
                if hasattr(self.kiro, "window_is_kiro") and not self.kiro.window_is_kiro(hwnd):
                    info = self.kiro.window_process(hwnd) if hasattr(self.kiro, "window_process") else {}
                    raise KiroServiceError(
                        f"KIRO_TARGET_NOT_KIRO_PROCESS hwnd={hwnd} "
                        f"exe={info.get('executable') or info.get('process_name')!r}")
            except KiroServiceError:
                raise
            except Exception:
                logger.debug("kiro process-identity gate errored", exc_info=True)
                raise KiroServiceError(f"KIRO_TARGET_IDENTITY_UNVERIFIABLE hwnd={hwnd}")
            # Effective avoid set for controller calls: keep protecting every
            # protected window EXCEPT the explicitly-allowed handoff hwnd.
            act_avoid = {h for h in self._protected_hwnds if allow_hwnd is None or int(h) != int(allow_hwnd)}
            self._publish(workflow_id, EV_WINDOW_FOUND, {"hwnd": hwnd,
                          "manual_handoff": bool(handoff)})

            # 4) Verify the workspace is EXACTLY the project (normalized).
            #    (Immediately-before-paste re-verification below as well.)
            self._publish(workflow_id, EV_PROJECT_VERIFYING, {"hwnd": hwnd})
            verify = self.kiro.verify_project(hwnd, self._project_path)
            if not verify.get("verified"):
                self._publish(workflow_id, EV_PROJECT_MISMATCH, {
                    "requested": self._project_path, "observed": verify.get("observed")})
                raise KiroServiceError(
                    f"KIRO_PROJECT_MISMATCH observed={verify.get('observed')!r}")
            self._publish(workflow_id, EV_PROJECT_VERIFIED, {
                "hwnd": hwnd, "observed": verify.get("observed")})

            # 5) Prepare a durable session identity + persist.
            #    Create (or reuse) a NEW Jarvis-OWNED Kiro session so we never
            #    hijack whichever human session happens to be active. The
            #    binding records Kiro.exe pid/exe + window + session fingerprint
            #    and survives restart. If a live binding already exists (e.g.
            #    resume after restart), reuse it instead of creating another.
            self._publish(workflow_id, EV_SESSION_PREPARING, {})
            force_new = bool((engine.get_workflow(workflow_id).metadata or {})
                             .get("kiro", {}).get("force_new_session"))
            existing = self.session_mgr.get_binding(workflow_id)
            try:
                if force_new:
                    # User chose "New Kiro Session": create a fresh Jarvis
                    # session and rebind, retaining the old binding in history.
                    binding = self.session_mgr.rebind_new_session(
                        workflow_id, hwnd, iteration=iteration,
                        avoid_hwnds=act_avoid)
                    k = dict((engine.get_workflow(workflow_id).metadata or {}).get("kiro") or {})
                    k.pop("force_new_session", None)
                    engine.set_metadata(workflow_id, "kiro", k)
                elif existing:
                    # A binding already exists (resume / continuous run). Verify
                    # ownership rather than silently creating a new session. If a
                    # human switched the active session away, fail closed and
                    # pause — NEVER rebind over the human's choice automatically.
                    if self.session_mgr.is_binding_live(workflow_id):
                        binding = existing
                    else:
                        mismatch = self.session_mgr.detect_and_record_mismatch(
                            workflow_id, substate="PRE_SUBMIT")
                        if mismatch is not None:
                            self._enter_session_mismatch_pause(workflow_id, mismatch)
                            return
                        # Binding not live for a non-session reason (window
                        # gone/wrong project) → fail closed.
                        raise KiroServiceError("KIRO_BINDING_NOT_LIVE")
                else:
                    # First-time entry: create a NEW Jarvis-owned session.
                    binding = self.session_mgr.create_and_bind(
                        workflow_id, hwnd, iteration=iteration,
                        avoid_hwnds=act_avoid)
            except KiroServiceError:
                raise
            except Exception as exc:
                # Fail closed: never paste if we cannot establish ownership.
                raise KiroServiceError(f"KIRO_SESSION_BIND_FAILED: {exc}") from exc
            ident = {
                "kiro_session_id": binding["kiro_session_id"],
                "kiro_window_hwnd": binding["kiro_window_hwnd"],
                "kiro_session_title": binding.get("kiro_session_title"),
            }
            engine.update_workflow_context(
                workflow_id,
                kiro_session_id=ident["kiro_session_id"],
                kiro_project_path=self._project_path)
            engine.set_metadata(workflow_id, "kiro", {
                "window_hwnd": ident["kiro_window_hwnd"],
                "session_title": ident.get("kiro_session_title"),
                "session_fingerprint": binding.get("session_fingerprint"),
                "binding_id": binding.get("binding_id"),
                "kiro_process_pid": binding.get("kiro_process_pid"),
                "kiro_process_executable": binding.get("kiro_process_executable"),
                "project_path": self._project_path,
                "observed_project": verify.get("observed"),
                "last_verified_at": _now_iso(),
                "lock_owner": workflow_id,
                "status": "session_ready",
                "substate": "SESSION_READY",
            })
            self._publish(workflow_id, EV_SESSION_READY, {"session_id": ident["kiro_session_id"]})

            # 5a) CONTINUOUS OWNERSHIP CHECK before any dangerous op. If a human
            #     switched the active Kiro session away from ours, fail closed:
            #     record the mismatch, request confirmation, and take ZERO Kiro
            #     actions (no paste/Enter/permission/capture).
            mismatch = self.session_mgr.detect_and_record_mismatch(
                workflow_id, substate="PRE_SUBMIT")
            if mismatch is not None:
                self._enter_session_mismatch_pause(workflow_id, mismatch)
                return

            # 5b) MANDATORY re-checks immediately before any paste (handoff path):
            #     re-verify exact project on the confirmed hwnd, re-confirm the
            #     approved prompt hash is unchanged, and require the window to be
            #     brought to the foreground. Any failure here raises BEFORE the
            #     submit action begins, so the handoff is NOT consumed.
            if handoff:
                reverify = self.kiro.verify_project(hwnd, self._project_path)
                if not reverify.get("verified"):
                    obs = (reverify.get("observed") or "")
                    if not (obs and _norm(obs).startswith(_norm(self._project_path))):
                        raise KiroServiceError(
                            f"KIRO_PROJECT_MISMATCH (pre-paste) observed={obs!r}")
                cur_prompt = engine.get_workflow(workflow_id).approved_gpt_prompt or ""
                if _sha(cur_prompt) != prompt_hash:
                    raise KiroServiceError("APPROVED_PROMPT_HASH_CHANGED")
                # foreground must actually become the confirmed window.
                if not self.kiro.focus_window(hwnd, avoid_hwnds=act_avoid):
                    raise KiroServiceError("KIRO_FOREGROUND_NOT_ACHIEVED")

            # 6) Submit the EXACT approved prompt ONCE (idempotent action).
            submit_action = f"{ACTION_KIRO_SUBMIT}:{workflow_id}:{iteration}:{prompt_hash}"
            started, action = engine.begin_action(
                workflow_id, submit_action, action_type="kiro_submit")
            if started:
                self._publish(workflow_id, EV_PROMPT_SUBMITTING, {})
                self.kiro.focus_composer(hwnd, avoid_hwnds=act_avoid)
                self.kiro.paste_prompt(hwnd, prompt, avoid_hwnds=act_avoid)
                self.kiro.submit_prompt(hwnd, avoid_hwnds=act_avoid)
                # 7) POSITIVE submission verification (never Enter alone).
                verify_sub = self.kiro.verify_submission(hwnd, prompt)
                self._publish(workflow_id, EV_PROMPT_SUBMITTED, verify_sub)
                # CONSUME the manual handoff now that submission is positively
                # verified. A second submission requires a NEW handoff; a failure
                # before this point leaves the handoff intact (never consumed on
                # failed pre-submit validation).
                if handoff:
                    self._consume_handoff(workflow_id)
                engine.complete_action(workflow_id, submit_action,
                                       result_ref="submitted+verified")
            else:
                # RECOVERY D: already submitted in a prior run → do NOT resend.
                logger.info("Kiro prompt already submitted for %s; not resending",
                            workflow_id)
                verify_sub = {"submitted": True, "recovered": True}

            # 8) KIRO_STARTING → KIRO_RUNNING.
            wf = engine.get_workflow(workflow_id)
            if wf.state == "KIRO_STARTING":
                engine.transition(workflow_id, "KIRO_RUNNING",
                                  message="Approved prompt submitted to Kiro")
            # Update kiro metadata status + durable SUBSTATE; detect (NOT decide)
            # permission. The substate is what Continue/restart resumes to
            # exactly, and it gates which (if any) action may run on resume.
            permission = bool(verify_sub.get("permission"))
            kmeta = dict((engine.get_workflow(workflow_id).metadata or {}).get("kiro") or {})
            kmeta["status"] = "waiting_permission" if permission else "running"
            kmeta["substate"] = "WAITING_PERMISSION" if permission else "KIRO_RUNNING"
            kmeta["last_verified_at"] = _now_iso()
            engine.set_metadata(workflow_id, "kiro", kmeta)
            if permission:
                self._publish(workflow_id, EV_WAITING_PERMISSION, {"hwnd": hwnd})
            self._publish(workflow_id, EV_RUNNING, {"hwnd": hwnd,
                          "permission": permission})
            logger.info("Kiro start complete for %s → KIRO_RUNNING (permission=%s)",
                        workflow_id, permission)
        except Exception as exc:
            code = type(exc).__name__
            msg = str(exc)
            logger.warning("Kiro start error for %s: %s (%s)", workflow_id, msg, code)
            self._publish(workflow_id, EV_SUBMISSION_FAILED, {"error": msg, "code": code})
            # Do NOT fake KIRO_RUNNING. Fail legally where allowed. KIRO_STARTING
            # → FAILED is legal; if still WAITING_FOR_APPROVAL, leave it (only
            # WAITING_FOR_APPROVAL→FAILED is not permitted, so record metadata).
            try:
                cur = engine.get_workflow(workflow_id)
                if cur is not None and cur.state == "KIRO_STARTING":
                    engine.fail(workflow_id, error=msg, error_code=code)
                else:
                    engine.set_metadata(workflow_id, "kiro_last_error",
                                        {"error": msg, "code": code, "at": _now_iso()})
            except Exception:
                logger.debug("kiro fail handling failed", exc_info=True)
        finally:
            # Release the lock on any terminal-for-this-attempt outcome EXCEPT
            # when we successfully reached KIRO_RUNNING and want to keep owning
            # it for Parts 8+. We release on failure; keep on success.
            try:
                cur = engine.get_workflow(workflow_id)
                keep = cur is not None and cur.state == "KIRO_RUNNING"
                if lock_acquired and not keep:
                    self.lock.release(workflow_id)
            except Exception:
                logger.debug("lock release check failed", exc_info=True)

    # ── Focus / verify (delegated helpers for the API) ────────────────────────

    def focus(self, workflow_id: str) -> dict:
        wf = self.engine.get_workflow(workflow_id)
        if wf is None:
            raise KiroServiceError("Workflow not found")
        kmeta = (wf.metadata or {}).get("kiro") or {}
        hwnd = kmeta.get("window_hwnd")
        if not hwnd:
            raise KiroServiceError("NO_KIRO_WINDOW")
        ok = self.kiro.focus_window(int(hwnd), avoid_hwnds=self._protected_hwnds)
        return {"focused": bool(ok), "hwnd": hwnd}

    def verify_project(self, workflow_id: str) -> dict:
        wf = self.engine.get_workflow(workflow_id)
        if wf is None:
            raise KiroServiceError("Workflow not found")
        kmeta = (wf.metadata or {}).get("kiro") or {}
        hwnd = kmeta.get("window_hwnd")
        if not hwnd:
            raise KiroServiceError("NO_KIRO_WINDOW")
        v = self.kiro.verify_project(int(hwnd), self._project_path)
        # refresh last_verified_at
        try:
            k = dict(kmeta); k["last_verified_at"] = _now_iso(); k["verified"] = v["verified"]
            self.engine.set_metadata(workflow_id, "kiro", k)
        except Exception:
            pass
        return v

    def get_status(self, workflow_id: str) -> dict:
        wf = self.engine.get_workflow(workflow_id)
        if wf is None:
            raise KiroServiceError("Workflow not found")
        kmeta = (wf.metadata or {}).get("kiro") or {}
        return {
            "state": wf.state,
            "kiro_session_id": wf.kiro_session_id,
            "kiro_project_path": wf.kiro_project_path,
            "kiro_window_hwnd": kmeta.get("window_hwnd"),
            "kiro_status": kmeta.get("status"),
            "lock_owner": self.lock.owner(),
            "last_verified_at": kmeta.get("last_verified_at"),
            "last_error": (wf.metadata or {}).get("kiro_last_error") or wf.last_error,
            "manual_handoff": (wf.metadata or {}).get("kiro_manual_handoff") or {},
            "session_binding": (wf.metadata or {}).get("kiro_binding") or {},
            "session_mismatch": (wf.metadata or {}).get("kiro_session_mismatch") or {},
            "active": self.is_active(workflow_id),
        }

    # ── Session-mismatch pause + confirmation decisions ────────────────────────

    def _enter_session_mismatch_pause(self, workflow_id: str, mismatch: dict) -> None:
        """Fail-closed pause when a human switched the active Kiro session.

        Persists the paused substate, releases the exclusive Kiro lock (so the
        human's session is never held hostage), publishes the mismatch, and
        sends the Telegram confirmation. Takes ZERO Kiro actions.
        """
        engine = self.engine
        try:
            k = dict((engine.get_workflow(workflow_id).metadata or {}).get("kiro") or {})
            k["status"] = "session_mismatch"
            k["substate"] = "KIRO_SESSION_MISMATCH"
            engine.set_metadata(workflow_id, "kiro", k)
        except Exception:
            logger.debug("mismatch meta update failed", exc_info=True)
        # Release the lock: never hold Kiro exclusive while paused on the human.
        try:
            if self.lock.owner() == workflow_id:
                self.lock.release(workflow_id)
        except Exception:
            logger.debug("mismatch lock release failed", exc_info=True)
        # Durably PAUSE so the exact resume_state (KIRO_STARTING/KIRO_RUNNING)
        # survives restart and the workflow can resume from where it stopped.
        try:
            self.engine.pause(workflow_id, reason="kiro_session_mismatch")
        except Exception:
            logger.debug("mismatch pause failed", exc_info=True)
        self._publish(workflow_id, EV_SESSION_MISMATCH, mismatch)
        # Notify via Telegram with the 4 decision buttons (Continue/New/Pause/
        # Cancel). Best-effort; the durable mismatch record drives resolution.
        try:
            from jarvis_core.kiro_mismatch_service import kiro_mismatch_service
            kiro_mismatch_service.notify(workflow_id, mismatch)
        except Exception:
            logger.debug("mismatch notify failed", exc_info=True)

    def resume_after_confirmation(self, workflow_id: str, decision: str) -> dict:
        """Apply a user's session-mismatch decision.

        - ``continue`` : re-select the ORIGINAL Jarvis session, verify it, then
          resume from the exact durable substate (NO prompt resend if the submit
          action already completed).
        - ``new``      : create a NEW Jarvis session (retain history) and resume;
          replay the current step only if it was never submitted.
        - ``pause``    : leave the human session untouched; stay paused.
        - ``cancel``   : cancel the workflow safely (human session untouched).
        """
        wf = self.engine.get_workflow(workflow_id)
        if wf is None:
            raise KiroServiceError("Workflow not found")
        decision = (decision or "").strip().lower()
        self.session_mgr.clear_mismatch(workflow_id, decision=decision)
        if decision == "pause":
            return {"decision": "pause", "resumed": False}
        if decision == "cancel":
            try:
                self.engine.cancel(workflow_id, reason="kiro_session_mismatch_cancel")
            except Exception:
                logger.debug("cancel failed", exc_info=True)
            return {"decision": "cancel", "resumed": False}
        if decision in ("continue", "new"):
            # 'new' → force a fresh Jarvis session next run (old binding is
            # retained in history by rebind_new_session). 'continue' → ACTIVELY
            # relocate + re-select + verify the ORIGINAL bound session before
            # resuming; fail closed if it cannot be located.
            if decision == "new":
                try:
                    k = dict((wf.metadata or {}).get("kiro") or {})
                    k["force_new_session"] = True
                    self.engine.set_metadata(workflow_id, "kiro", k)
                except Exception:
                    logger.debug("mark force_new_session failed", exc_info=True)
            elif decision == "continue":
                try:
                    self.session_mgr.reselect_bound_session(workflow_id)
                    self._publish(workflow_id, EV_SESSION_RESELECTED,
                                  {"reselected": True})
                except Exception as exc:
                    # Could not locate/select the original session → do NOT
                    # claim Continue worked. Re-record a mismatch and stay
                    # paused so the user can pick New/Pause/Cancel.
                    logger.warning("Continue could not reselect session for %s: %s",
                                   workflow_id, exc)
                    try:
                        self.session_mgr.detect_and_record_mismatch(
                            workflow_id, substate="CONTINUE_RESELECT_FAILED")
                    except Exception:
                        logger.debug("re-record mismatch failed", exc_info=True)
                    return {"decision": "continue", "resumed": False,
                            "error": f"KIRO_ORIGINAL_SESSION_NOT_FOUND: {exc}"}
            # Resume PAUSED → saved resume_state (KIRO_STARTING/KIRO_RUNNING) so
            # we re-drive from the exact substate. The idempotent submit action
            # guarantees an already-completed submission is NEVER resent.
            try:
                if wf.state == "PAUSED":
                    self.engine.resume(workflow_id)
            except Exception:
                logger.debug("resume transition failed", exc_info=True)
            # Re-drive the start sequence on the worker (or inline for tests).
            with self._active_lock:
                already = workflow_id in self._active
                if not already:
                    self._active.add(workflow_id)
            if self._run_inline:
                try:
                    self._run_start(workflow_id)
                finally:
                    with self._active_lock:
                        self._active.discard(workflow_id)
            elif not already:
                self.start_worker()
                self._queue.put({"workflow_id": workflow_id})
            return {"decision": decision, "resumed": True}
        raise KiroServiceError(f"UNKNOWN_DECISION {decision!r}")


    # ── Continuous ownership monitor ───────────────────────────────────────────

    def check_ownership_once(self, workflow_id: str) -> Optional[dict]:
        """One ownership verification for a workflow in an active Kiro state.

        If the active Kiro session no longer matches the bound session (human
        switched), pause fail-closed + raise the confirmation and return the
        mismatch record. Returns None when ownership is intact. Performs ZERO
        dangerous actions (no paste/Enter/permission/result-capture)."""
        wf = self.engine.get_workflow(workflow_id)
        if wf is None:
            return None
        if wf.state not in ("KIRO_RUNNING", "KIRO_WAITING_PERMISSION"):
            return None
        # Skip if already paused / mismatch already open.
        if (wf.metadata or {}).get("kiro_session_mismatch", {}).get("resolved") is False:
            return None
        mismatch = self.session_mgr.detect_and_record_mismatch(
            workflow_id, substate=(wf.metadata or {}).get("kiro", {}).get("substate")
            or wf.state)
        if mismatch is not None:
            self._enter_session_mismatch_pause(workflow_id, mismatch)
        return mismatch

    def start_ownership_monitor(self, *, interval: float = 8.0) -> None:
        """Start a low-frequency background monitor that verifies session
        ownership for all active Kiro workflows. Independent of Part 8 — it
        never clicks permissions or captures results."""
        if self._run_inline:
            return
        with self._start_lock:
            if getattr(self, "_monitor", None) is not None and self._monitor.is_alive():
                return
            self._monitor_stop = threading.Event()
            self._monitor_interval = interval

            def _loop():
                while not self._monitor_stop.is_set():
                    try:
                        for wf in self.engine.list_active_workflows():
                            if wf.state in ("KIRO_RUNNING", "KIRO_WAITING_PERMISSION"):
                                try:
                                    self.check_ownership_once(wf.workflow_id)
                                except Exception:
                                    logger.debug("ownership check failed for %s",
                                                 wf.workflow_id, exc_info=True)
                    except Exception:
                        logger.debug("ownership monitor sweep failed", exc_info=True)
                    self._monitor_stop.wait(self._monitor_interval)

            self._monitor = threading.Thread(target=_loop, name="kiro-ownership-monitor",
                                             daemon=True)
            self._monitor.start()
            logger.info("Kiro ownership monitor started (interval=%.1fs)", interval)

    def stop_ownership_monitor(self, timeout: float = 3.0) -> None:
        ev = getattr(self, "_monitor_stop", None)
        if ev is not None:
            ev.set()
        mon = getattr(self, "_monitor", None)
        if mon is not None and mon.is_alive():
            mon.join(timeout=timeout)
        self._monitor = None


# Module-level singleton, following the project convention.
workflow_kiro_service = WorkflowKiroService()
