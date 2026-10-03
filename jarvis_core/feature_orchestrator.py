"""Jarvis Autonomous Feature Orchestrator V1.

Central coordinator for feature-development requests arriving from the Web UI
or Telegram/Optimus: detection → free auto research (repo + docs + local
Ollama) → user-assisted ChatGPT review → final plan → mandatory Telegram
approval (with high-risk second confirmation) → Cline-ready handoff.

Design rules:
- Reuses existing Jarvis services (task_manager, model routing, Ollama).
- Runs stages on a dedicated background worker; never blocks commands,
  Telegram polling, Gmail or LIVE TASKS.
- Only one stage of the same feature runs concurrently.
- Persists safe structured state under data/features/ and restores it on
  restart so pending approvals never disappear.
- Cline handoff happens only after explicit Telegram approval.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Optional

from jarvis_core.cline_bridge import cline_bridge
from jarvis_core.feature_detector import detect_feature_request
from jarvis_core.feature_models import (
    FeatureRequest,
    FeatureSource,
    FeatureStatus,
)
from jarvis_core.feature_store import FeatureStore
from jarvis_core.feature_utils import redact_sensitive, sanitize_review_text
from jarvis_core.feature_progress import build_progress
from jarvis_core.timezone_utils import jarvis_now

logger = logging.getLogger("jarvis.feature")

# Worker actions
ACTION_RESEARCH = "research"
ACTION_ANALYZE_REVIEW = "analyze_review"
ACTION_BUILD_PLAN = "build_plan"


_REPO_PATH_RE = None  # compiled lazily


def _referenced_repo_paths(text: str) -> list[str]:
    """Extract jarvis_core/... style paths mentioned in a prompt (Section G).

    Used to validate that a candidate Cline prompt only references real files.
    """
    import re as _re

    return list(dict.fromkeys(
        m.group(0).rstrip(".,:;)")
        for m in _re.finditer(
            r"(?:jarvis_core|tests|config)/[A-Za-z0-9_/\.\-]+", str(text or "")
        )
    ))[:20]


def _repo_path_exists(path: str) -> bool:
    """True when *path* (relative to BASE_DIR) really exists on disk."""
    if not path or not str(path).strip():
        return False
    from pathlib import Path

    from jarvis_core.config import BASE_DIR

    candidate = (
        Path(str(path))
        if Path(str(path)).is_absolute()
        else BASE_DIR / str(path)
    )
    try:
        return bool(candidate.exists())
    except OSError:
        return False


class FeatureOrchestrator:
    """Coordinates the whole autonomous feature workflow."""

    def __init__(
        self,
        store: Optional[FeatureStore] = None,
        task_manager_instance=None,
    ) -> None:
        self._store = store or FeatureStore()
        self._task_manager_ref = task_manager_instance
        self._lock = threading.Lock()
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._start_lock = threading.RLock()  # reentrant — _restore_pending may call start_worker
        self._active_features: set[str] = set()
        self._active_lock = threading.Lock()

        # Follow-up stages requested from INSIDE a running stage. They cannot be
        # queued directly (the feature is still "active"), so the worker drains
        # them right after the current stage finishes.
        self._chained: dict[str, str] = {}

        # Cooperative cancellation flags, one per feature.
        self._cancel_events: dict[str, threading.Event] = {}

        # Actions replayed after a restart so interrupted pipelines resume.
        self._resume_actions: list[tuple[str, str]] = []

        # Hard wall-clock budget for the whole research stage. External calls
        # inside it are individually bounded (web fetch 8s, Ollama <=180s);
        # this watchdog guarantees the stage itself can never hang forever.
        self.RESEARCH_TIMEOUT_SECONDS = 240
        self.OLLAMA_STAGE_TIMEOUT_SECONDS = 120

        # Telegram hooks are registered later via set_telegram_channels().
        # Initialising them here keeps _send_telegram/_authorize safe when a
        # fresh instance resumes persisted features before wiring happens.
        self._telegram_send = None
        self._telegram_auth = None

    # ── Wiring ────────────────────────────────────────────────────────────

    def set_telegram_channels(
        self,
        send: Callable[[int, str, Optional[dict]], None],
        is_authorized: Callable[[int], bool],
    ) -> None:
        """Register the Telegram send/authorization callbacks."""
        self._telegram_send = send
        self._telegram_auth = is_authorized

    @property
    def task_manager(self):
        if self._task_manager_ref is not None:
            return self._task_manager_ref
        from jarvis_core.task_manager import task_manager

        return task_manager

    def _send_telegram(self, chat_id: int, text: str, markup: Optional[dict] = None) -> None:
        if chat_id is None or self._telegram_send is None:
            return
        try:
            self._telegram_send(chat_id, text, markup)
        except Exception:
            logger.exception("Feature orchestrator Telegram delivery failed")

    def _restore_task_card(self, feature: FeatureRequest, status: str) -> None:
        """Recreate the in-memory LIVE TASKS card for a restored feature.

        Tasks are per-process; after a restart a waiting feature would vanish
        from LIVE TASKS without this, even though its state is persisted.
        """
        try:
            existing = (
                self.task_manager.get_task(feature.task_id)
                if feature.task_id else None
            )
            if existing is None:
                task = self.task_manager.create_task(
                    feature.original_request,
                    source=feature.source,
                    task_type="feature",
                )
                feature.task_id = task.task_id
            self.task_manager.set_status(feature.task_id, status)
            self._store.save(feature)
        except Exception:
            logger.exception(
                "Could not restore LIVE TASK card for feature %s",
                feature.feature_id[:8],
            )

    def _restore_pending(self) -> None:
        """Re-enqueue features in-progress when the process died.

        Called from start_worker() before the worker loop begins.
        The _restoring flag prevents recursive start_worker() calls.

        Iterates over *all* persisted features (not just pending()) so that
        features interrupted mid-stage — at received / analyzing / researching /
        planning — are also resumed after a restart, while truly terminal
        states (completed, cancelled, failed, cline_ready, approved) are left
        alone.
        """
        if getattr(self, "_restoring", False):
            return
        self._restoring = True
        try:
            # Terminal states: never resume.
            terminal = {
                FeatureStatus.completed.value,
                FeatureStatus.cancelled.value,
                FeatureStatus.failed.value,
                FeatureStatus.cline_ready.value,
                FeatureStatus.approved.value,
                FeatureStatus.implementation_in_progress.value,
                FeatureStatus.resource_review_complete.value,  # phase-1 stop
            }
            restored: list[str] = []
            for feature in self._store.all():
                # Part 9 crash-window recovery: a feature whose ▶️ Continue
                # already enqueued RESEARCH (research_enqueued=True) but crashed
                # before the research stage set ANALYZING must resume research
                # exactly once. The persisted flag stays True, so the
                # analyzing/researching resume branch below never re-enqueues.
                if (
                    feature.status == FeatureStatus.resource_review_complete.value
                    and getattr(feature, "research_enqueued", False)
                ):
                    self._cancel_events.pop(feature.feature_id, None)
                    self._resume_actions.append(
                        (ACTION_RESEARCH, feature.feature_id)
                    )
                    restored.append(feature.feature_id)
                    continue
                if feature.status in terminal:
                    continue
                self._cancel_events.pop(feature.feature_id, None)
                if feature.status in (
                    FeatureStatus.resource_review_pending.value,
                    FeatureStatus.resource_review.value,
                    FeatureStatus.resource_scanning.value,
                ):
                    # Restart: re-scan and re-send a fresh RESOURCE_REVIEW menu.
                    # The feature parks at RESOURCE_REVIEW until the user presses
                    # ▶️ Continue; it never auto-advances past the review.
                    self._restore_task_card(feature, "waiting_for_confirmation")
                    if feature.telegram_chat_id is not None:
                        self._schedule_resource_review(feature)
                elif feature.status in (
                    FeatureStatus.received.value,
                    FeatureStatus.analyzing.value,
                    FeatureStatus.researching.value,
                ):
                    self._resume_actions.append((ACTION_RESEARCH, feature.feature_id))
                elif feature.status == FeatureStatus.chatgpt_review_pending.value:
                    # Stay waiting for the user's review/skip; only an explicit
                    # skip recorded before the restart continues automatically.
                    if feature.chatgpt_skipped:
                        self._resume_actions.append((ACTION_BUILD_PLAN, feature.feature_id))
                    else:
                        self._restore_task_card(feature, "waiting_for_confirmation")
                        # Re-send the ChatGPT Review Ready message (with the
                        # review-capture buttons) so the user can act on the
                        # waiting feature after a restart.
                        if feature.telegram_chat_id is not None:
                            from jarvis_core.feature_approval import (
                                chatgpt_review_buttons,
                                chatgpt_review_ready_message,
                            )

                            self._send_telegram(
                                feature.telegram_chat_id,
                                chatgpt_review_ready_message(feature),
                                markup=chatgpt_review_buttons(feature),
                            )
                elif feature.status == FeatureStatus.planning.value:
                    self._resume_actions.append((ACTION_BUILD_PLAN, feature.feature_id))
                elif feature.status == FeatureStatus.approval_pending.value:
                    self._restore_task_card(feature, "waiting_for_confirmation")
                restored.append(feature.feature_id[:8])
                logger.info(
                    "Resumed feature %s at status '%s'",
                    feature.feature_id[:8], feature.status,
                )
            if restored:
                logger.info(
                    "Restored %d pending/interrupted feature request(s) after restart",
                    len(restored),
                )
        finally:
            self._restoring = False

    # ── Detection & submission ────────────────────────────────────────────

    def is_feature_request(self, text: str) -> bool:
        return bool(detect_feature_request(text).get("is_feature"))

    def submit_feature_request(
        self,
        text: str,
        source: str,
        *,
        telegram_user_id: Optional[int] = None,
        telegram_chat_id: Optional[int] = None,
    ) -> dict:
        """Create a tracked feature request and schedule research."""
        detection = detect_feature_request(text)
        if not detection.get("is_feature"):
            return {"status": "not_a_feature", "message": "Not a feature request."}

        feature = FeatureRequest(
            original_request=redact_sensitive(str(text).strip()),
            source=source,
            interpreted_objective=detection["objective"],
            status=FeatureStatus.received.value,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
        )
        feature.record("received", f"Feature request received via {source}")
        self._store.save(feature)

        task = self.task_manager.create_task(
            feature.original_request,
            source=feature.source,
            task_type="feature",
        )
        feature.task_id = task.task_id

        # ── Resource Review gate (Telegram Feature Development) ──────────
        # Phase 1 state machine: after the user picks "Feature Development",
        # the feature enters RESOURCE_SCANNING, a read-only resource scan runs,
        # and only THEN does it move to RESOURCE_REVIEW. Nothing (research /
        # Ollama / ChatGPT / Cline) may start automatically after this point;
        # only the explicit ▶️ Continue button may advance the phase.
        if source == FeatureSource.telegram.value and feature.telegram_chat_id is not None:
            feature.resource_review_started_at = jarvis_now().isoformat()
            self._set_state(
                feature, FeatureStatus.resource_scanning,
                "running",
                "Scanning laptop resources (apps / Chrome tabs)",
            )
            self._schedule_resource_review(feature)
            logger.info(
                "Feature %s scanning resources before any research/planning",
                feature.feature_id[:8],
            )
            return {
                "status": "ok",
                "action": "feature_created",
                "feature_id": feature.feature_id,
                "task_id": task.task_id,
                "objective": feature.interpreted_objective,
                "message": (
                    "\U0001f9e9 FEATURE DEVELOPMENT\n\n"
                    f"Feature: {feature.interpreted_objective}\n\n"
                    "Scanning your current laptop environment..."
                ),
            }

        # Web / non-chat features keep the original immediate-research flow.
        self.task_manager.set_status(task.task_id, "interpreting")
        self._store.save(feature)

        logger.info(
            "Feature request %s accepted (%s): %s",
            feature.feature_id[:8], source, feature.interpreted_objective,
        )
        self.start_worker()
        self.enqueue(ACTION_RESEARCH, feature.feature_id)

        return {
            "status": "ok",
            "action": "feature_created",
            "feature_id": feature.feature_id,
            "task_id": task.task_id,
            "objective": feature.interpreted_objective,
            "message": (
                "\U0001f9e0 FEATURE REQUEST RECEIVED\n\n"
                f"{feature.interpreted_objective}\n\nStatus: Analyzing..."
            ),
        }

    # Hard deadline for the resource scan. A hung UIA/CDP scan must never
    # park a feature at the resource-review stage forever.
    RESOURCE_SCAN_TIMEOUT_SECONDS: float = 120.0

    def _schedule_resource_review(self, feature: FeatureRequest) -> None:
        """Scan the REAL laptop, then present the RESOURCE_REVIEW panel.

        Runs on a background thread so the Telegram poller is never blocked.
        READ-ONLY discovery only (windows + Chrome tabs metadata) - it never
        focuses, closes, types or activates anything.

        Phase 1 state machine:
          RESOURCE_SCANNING (already set by submit_feature_request)
            → scan completes → RESOURCE_REVIEW  (set here)
            → only the explicit ▶️ Continue button → RESOURCE_REVIEW_COMPLETE

        A scan failure still sends an honest RESOURCE_REVIEW panel with an
        empty snapshot. No stage past the review is ever enqueued from here:
        nothing (research / Ollama / ChatGPT / Cline) starts automatically.
        """
        def _work() -> None:
            from jarvis_core.resource_manager import resource_registry
            from jarvis_core.resource_menus import build_resource_review_home

            logger.info(
                "Resource review worker entered for feature %s",
                feature.feature_id[:8],
            )
            try:
                scan, timed_out = self._run_with_timeout(
                    lambda: resource_registry.scan(
                        feature_id=feature.feature_id, include_identity=True
                    ),
                    self.RESOURCE_SCAN_TIMEOUT_SECONDS,
                    feature.feature_id,
                    "resource_scan",
                )
            except Exception:
                logger.exception(
                    "Resource scan failed for feature %s", feature.feature_id[:8],
                )
                scan, timed_out = None, False
            logger.info(
                "Resource scan finished for feature %s (timed_out=%s, "
                "apps=%s, tabs=%s)",
                feature.feature_id[:8], timed_out,
                len((scan or {}).get("applications") or []),
                len((scan or {}).get("tabs") or []),
            )
            if timed_out or scan is None:
                scan = {
                    "generation": 0,
                    "feature_id": feature.feature_id,
                    "applications": [],
                    "tabs": [],
                    "chrome_detected": False,
                    "chrome_controllable": False,
                    "chrome_reason": (
                        "Resource scan timed out" if timed_out
                        else "Resource scan failed"
                    ),
                    "scanned_at": jarvis_now().isoformat(),
                }

            # ── Phase transition: RESOURCE_SCANNING → RESOURCE_REVIEW ──────
            # The feature parks at RESOURCE_REVIEW until the user presses the
            # ▶️ Continue button. Nothing progresses on its own.
            try:
                current = self._store.get(feature.feature_id)
                if current is not None and current.status not in (
                    FeatureStatus.cancelled.value, FeatureStatus.failed.value,
                ):
                    if current.status != FeatureStatus.resource_review.value:
                        self._set_state(
                            current, FeatureStatus.resource_review,
                            "waiting_for_confirmation",
                            "Resource review ready (apps / Chrome tabs)",
                        )
                    current.resource_snapshot = scan
                    self._store.save(current)
            except Exception:
                logger.exception(
                    "Could not persist resource snapshot for feature %s",
                    feature.feature_id[:8],
                )

            # Present the RESOURCE_REVIEW panel (SAME message stays the
            # single control panel for expand / collapse / detail / Back /
            # Refresh / Close later on).
            try:
                text, rows = build_resource_review_home(
                    feature.feature_id, scan
                )
                self._send_telegram(
                    feature.telegram_chat_id,
                    text,
                    markup={"inline_keyboard": rows},
                )
            except Exception:
                logger.exception("Could not send resource review menu")

            logger.info(
                "Resource review presented for feature %s; waiting for user Continue",
                feature.feature_id[:8],
            )

        try:
            threading.Thread(
                target=_work,
                name=f"resource-review-{feature.feature_id[:8]}",
                daemon=True,
            ).start()
        except Exception:
            # The background worker could not be started. Run the scan inline
            # (it is fully exception-guarded) rather than auto-advancing the
            # feature, so the user STILL gets a review panel to decide on.
            logger.exception(
                "Resource review worker could not start for feature %s; "
                "scanning inline",
                feature.feature_id[:8],
            )
            _work()

    def continue_after_resource_review(
        self, feature_id: str, *, telegram_user_id=None,
    ) -> dict:
        """▶️ Continue: final scan → persist → RESOURCE_REVIEW_COMPLETE →
        enqueue RESEARCH once → existing pipeline.

        This is the ONLY transition out of RESOURCE_REVIEW. It performs one
        final read-only resource scan, persists the approved snapshot, marks
        the review complete, moves the feature to RESOURCE_REVIEW_COMPLETE and
        then (Part 9 bridge) enqueues the RESEARCH stage EXACTLY ONCE so the
        existing Research → ChatGPT Review → Plan/Approval pipeline resumes.

        Idempotency (persisted ``research_enqueued`` flag is the source of
        truth): a double Continue tap, a duplicate Telegram callback, a retry
        of this method, or a restart after a successful Continue all result in
        RESEARCH being enqueued at most once.
        """
        if telegram_user_id is not None and not self._authorize(telegram_user_id):
            return {"status": "error",
                    "message": "You are not authorized to control Jarvis."}
        feature = self._store.get(feature_id)
        if feature is None:
            return {"status": "error", "message": "Feature not found."}
        if feature.status not in (
            FeatureStatus.resource_review.value,
            FeatureStatus.resource_review_pending.value,
        ):
            return {
                "status": "error",
                "message": "Feature is not waiting for resource review.",
            }

        # 1) One final read-only scan; fall back to the last valid snapshot
        #    when the environment cannot be rescanned right now.
        final_snapshot = None
        timed_out = False
        try:
            from jarvis_core.resource_manager import resource_registry

            scan, timedout = self._run_with_timeout(
                lambda: resource_registry.scan(
                    feature_id=feature_id, include_identity=True
                ),
                self.RESOURCE_SCAN_TIMEOUT_SECONDS,
                feature_id,
                "final_resource_scan",
            )
            if not timedout and scan is not None:
                final_snapshot = scan
            timed_out = bool(timedout)
        except Exception:
            logger.exception(
                "Final resource scan failed for feature %s", feature_id[:8]
            )
            timed_out = True
        if final_snapshot is None:
            final_snapshot = feature.resource_snapshot or {
                "generation": 0,
                "feature_id": feature_id,
                "applications": [],
                "tabs": [],
                "chrome_detected": False,
                "chrome_controllable": False,
                "chrome_reason": (
                    "Final resource scan was unavailable; "
                    "showing the last valid snapshot"
                    if (feature.resource_snapshot is not None or timed_out)
                    else "Resource scan failed"
                ),
                "scanned_at": jarvis_now().isoformat(),
            }

        # 2) Persist the final approved snapshot + completion flags.
        feature.resource_snapshot = final_snapshot
        feature.resource_review_completed = True
        feature.record(
            "resource_review_completed", "Resource review completed by user",
        )

        # 3) State = RESOURCE_REVIEW_COMPLETE (Part 8 contract preserved).
        self._set_state(
            feature, FeatureStatus.resource_review_complete,
            "running",
            "Resource review complete - continuing feature",
        )

        # 4) Part 9 bridge: resume the existing pipeline by enqueuing RESEARCH
        #    exactly once. ``research_enqueued`` is persisted BEFORE the enqueue
        #    so the flag is the source of truth for idempotency:
        #      • double ▶️ Continue tap        -> flag already set -> no re-enqueue
        #      • duplicate Telegram callback    -> flag already set -> no re-enqueue
        #      • retry of continue_after_...     -> flag already set -> no re-enqueue
        #      • restart after a successful tap  -> feature already progressed to
        #        analyzing/researching (or flag set) -> resume path handles it,
        #        continue_after_... is not called again for a completed review.
        already_enqueued = bool(getattr(feature, "research_enqueued", False))
        if not already_enqueued:
            feature.research_enqueued = True
            feature.record(
                "research_enqueued",
                "Research stage queued after resource review continue",
            )
            self._store.save(feature)
            self.start_worker()
            self.enqueue(ACTION_RESEARCH, feature.feature_id)
            logger.info(
                "Feature %s continued past RESOURCE_REVIEW_COMPLETE; "
                "RESEARCH enqueued once (Part 9 bridge).",
                feature.feature_id[:8],
            )
        else:
            logger.info(
                "Feature %s Continue received again; RESEARCH already enqueued "
                "- ignoring duplicate (Part 9 idempotency).",
                feature.feature_id[:8],
            )

        # 5) Telegram acknowledgement (same-message control preserved).
        if feature.telegram_chat_id is not None:
            self._send_telegram(
                feature.telegram_chat_id,
                (
                    "\u2705 Resource Review Complete\n\n"
                    "\u25b6\ufe0f Continuing Feature...\n\n"
                    "Phase: Research"
                ),
            )
        return {
            "status": "ok",
            "action": "feature_continued",
            "feature_id": feature.feature_id,
            "phase": "research",
            "research_enqueued": not already_enqueued,
            "message": (
                "\u2705 Resource Review Complete\n\n"
                "\u25b6\ufe0f Continuing Feature...\n\n"
                "Phase: Research"
            ),
        }

    # Friendly alias: ``complete_resource_review`` == the explicit ▶️ Continue.
    complete_resource_review = continue_after_resource_review

    @staticmethod
    def _resource_review_summary(feature: FeatureRequest) -> str:
        """``✅ RESOURCE REVIEW COMPLETED`` panel with the approved counts."""
        from jarvis_core.resource_menus import count_chrome_accounts

        snapshot = feature.resource_snapshot or {}
        applications = snapshot.get("applications") or []
        tabs = snapshot.get("tabs") or []
        return (
            "\u2705 RESOURCE REVIEW COMPLETED\n\n"
            f"Feature: {feature.interpreted_objective}\n\n"
            f"Applications reviewed: {len(applications)}\n"
            f"Chrome accounts/profiles: {count_chrome_accounts(snapshot)}\n"
            f"Chrome tabs: {len(tabs)}\n\n"
            "Environment is ready for the next phase."
        )

    def list_resource_review_features(
        self, telegram_user_id: Optional[int] = None,
    ) -> list[dict]:
        """Features currently paused at the resource-review gate."""
        out = []
        for f in self._store.all():
            if f.status in (
                FeatureStatus.resource_review_pending.value,
                FeatureStatus.resource_review.value,
            ):
                if telegram_user_id is None or f.telegram_user_id == telegram_user_id:
                    out.append(f.public())
        return out

    def update_resource_snapshot(self, feature_id: str, snapshot: dict) -> dict:
        """Persist a fresh READ-ONLY resource snapshot on the feature record.

        Used by the Apps/Tabs Refresh action. Informational only: the feature
        status / stage / research progress are NEVER touched, so a refresh can
        never restart research or reset the workflow.
        """
        feature = self._store.get(feature_id)
        if feature is None:
            return {"status": "error", "message": "Feature not found."}
        feature.resource_snapshot = dict(snapshot or {})
        self._store.save(feature)
        logger.info("Feature %s resource snapshot updated", feature_id[:8])
        return {"status": "ok"}

    def enqueue(self, action: str, feature_id: str) -> None:
        """Queue a stage action. If the feature is currently active, store as a
        chained follow-up so the worker drains it after the current stage
        completes. This prevents the silent-drop deadlock where a stage
        chains its successor from inside an already-active feature."""
        with self._active_lock:
            if feature_id in self._active_features:
                chained = self._chained.setdefault(feature_id, [])
                if action not in chained:
                    chained.append(action)
                    logger.debug(
                        "Stage %s chained for %s (waiting for current stage)",
                        action, feature_id[:8],
                    )
                return
            self._active_features.add(feature_id)
        self._queue.put((action, feature_id))

    def _run_with_timeout(self, func, timeout: float, feature_id: str, stage_name: str):
        """Execute *func* with a hard wall-clock timeout.

        Returns (result, timed_out). The underlying thread is daemon and
        abandoned if it overruns; it never blocks the caller beyond *timeout*.
        On timeout the caller must handle the fallback explicitly -- the
        method never fabricates success.
        """
        holder = {"value": None, "exc": None, "done": threading.Event()}

        def _target():
            try:
                holder["value"] = func()
            except Exception as exc:
                holder["exc"] = exc
            finally:
                holder["done"].set()

        t = threading.Thread(target=_target, daemon=True,
                             name=f"feature-{stage_name}-{feature_id[:8]}")
        t.start()
        holder["done"].wait(timeout=timeout)
        # Tolerate test doubles that only implement start() (no is_alive).
        is_alive = getattr(t, "is_alive", None)
        if callable(is_alive) and is_alive():
            logger.warning("Stage %s timed out for feature %s after %ss",
                           stage_name, feature_id[:8], timeout)
            return None, True
        if holder["exc"] is not None:
            raise holder["exc"]
        return holder["value"], False

    def _check_cancelled(self, feature_id: str) -> bool:
        """True when a cooperative cancellation has been requested."""
        event = self._cancel_events.get(feature_id)
        return event is not None and event.is_set()

    def _restore_actions(self) -> None:
        """Replay queued resume actions into the worker queue."""
        with self._active_lock:
            actions = list(self._resume_actions)
            self._resume_actions.clear()
        for action, feature_id in actions:
            self._queue.put((action, feature_id))

    def start_worker(self) -> None:
        with self._start_lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._stop_event.clear()
            self._restore_pending()
            self._restore_actions()
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="feature-orchestrator",
                daemon=True,
            )
            self._worker_thread.start()
            logger.info("Feature orchestrator worker started")

    def stop_worker(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)
        self._worker_thread = None

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                action, feature_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            with self._active_lock:
                self._active_features.add(feature_id)
            try:
                logger.info("Stage %s started for feature %s", action, feature_id[:8])
                self._run_action(action, feature_id)
                logger.info("Stage %s completed for feature %s", action, feature_id[:8])
            except Exception:
                logger.exception("Feature stage %s failed for %s", action, feature_id[:8])
                self._fail_feature(feature_id, f"{action} stage failed")
            finally:
                with self._active_lock:
                    self._active_features.discard(feature_id)
                    # Drain any follow-up stages enqueued during this stage.
                    chained = self._chained.pop(feature_id, [])
                for chained_action in chained:
                    self._queue.put((chained_action, feature_id))

    def _run_action(self, action: str, feature_id: str) -> None:
        feature = self._store.get(feature_id)
        if feature is None:
            return
        # Honour cooperative cancellation at the start of every stage.
        if self._check_cancelled(feature_id):
            logger.info("Stage %s skipped for feature %s (cancelled)", action, feature_id[:8])
            return
        if action == ACTION_RESEARCH:
            self._run_research_stage(feature)
        elif action == ACTION_ANALYZE_REVIEW:
            self._run_review_analysis_stage(feature)
        elif action == ACTION_BUILD_PLAN:
            self._run_plan_stage(feature)

    # ── LIVE TASKS integration ────────────────────────────────────────────

    def _update_task(self, feature: FeatureRequest, status: Optional[str], message: str) -> None:
        """Mirror feature progress into the existing LIVE TASKS panel."""
        if not feature.task_id:
            return
        try:
            if status is not None:
                self.task_manager.set_status(feature.task_id, status)
            self.task_manager.add_event(feature.task_id, "feature", redact_sensitive(message))
        except Exception:
            logger.exception("LIVE TASKS update failed for feature %s", feature.feature_id[:8])

    def _set_state(
        self,
        feature: FeatureRequest,
        status: FeatureStatus,
        task_status: Optional[str],
        stage_message: str,
    ) -> None:
        feature.status = status.value
        feature.current_stage_started_at = jarvis_now().isoformat()
        feature.record(status.value, stage_message)
        self._store.save(feature)
        self._update_task(feature, task_status, stage_message)

    # ── Stage: free auto research ─────────────────────────────────────────

    def _run_research_stage(self, feature: FeatureRequest) -> None:
        logger.info("Research stage started for feature %s", feature.feature_id[:8])
        from jarvis_core.feature_research import run_research, _scan_repository

        self._set_state(
            feature, FeatureStatus.analyzing, "running",
            "Analyzing feature request",
        )
        if self._check_cancelled(feature.feature_id):
            logger.info("Research cancelled (pre-start) for feature %s", feature.feature_id[:8])
            return

        self._set_state(
            feature, FeatureStatus.researching, "running",
            "Researching Jarvis architecture",
        )

        objective = feature.interpreted_objective or feature.original_request
        # Research-required revision: incorporate the change-request instructions
        # so the repository re-scan targets the specific areas the user asked to
        # re-inspect (e.g. "find Notes implementation"). The original feature
        # identity (original_request / interpreted_objective) is NEVER mutated.
        revision_text = ""
        if feature.change_requests:
            revision_text = " ".join(
                c.get("text", "") for c in feature.change_requests
            )
        research_target = f"{objective} {revision_text}".strip()
        findings, timed_out = self._run_with_timeout(
            lambda: run_research(research_target),
            self.RESEARCH_TIMEOUT_SECONDS,
            feature.feature_id,
            "research",
        )

        if self._check_cancelled(feature.feature_id):
            logger.info("Research cancelled during execution for feature %s", feature.feature_id[:8])
            return

        if timed_out or findings is None:
            logger.warning("Research timed out for feature %s", feature.feature_id[:8])
            feature.research_incomplete = True
            feature.research_error = (
                "Research timed out after {0}s; continuing with repository "
                "facts only.".format(self.RESEARCH_TIMEOUT_SECONDS)
            )
            feature.record("researching", "Research timed out (partial)")
            affected = _scan_repository(research_target)
            feature.observed_facts = [
                redact_sensitive(f"{hit['plain']} - present in this repository")
                for hit in affected
            ]
            feature.affected_modules = [hit["plain"] for hit in affected]
            feature.model_recommendations = []
        else:
            feature.observed_facts = [redact_sensitive(f) for f in findings.observed_facts]
            feature.model_recommendations = [
                redact_sensitive(r) for r in findings.model_recommendations
            ]
            feature.affected_modules = list(findings.affected_modules)
            feature.dependencies = list(findings.dependencies)
            feature.research_incomplete = findings.incomplete
            feature.research_error = findings.error
            feature.record("researching", "Research complete")

        # Persist findings before building the review prompt.
        self._store.save(feature)
        logger.info("Research stage completed for feature %s", feature.feature_id[:8])

        # ── Verified evidence model (Phase 1) ─────────────────────────────
        # verified_existing_files are REAL paths (confirmed by the scanner's
        # existence checks). Proposed = new files; never presented as verified.
        from jarvis_core.feature_utils import propose_new_files

        # A fresh research pass supersedes any previous "more research" request.
        feature.needs_more_research = False
        feature.missing_evidence = []

        verified = [
            p for p in feature.affected_modules if _repo_path_exists(p)
        ]
        feature.verified_existing_files = verified
        feature.proposed_new_files = [
            redact_sensitive(x) for x in propose_new_files(
                feature.interpreted_objective or feature.original_request
            )
        ]
        if feature.research_incomplete:
            feature.unverified_assumptions = [
                "Repository/evidence coverage was incomplete; some reuse "
                "assumptions remain unverified.",
            ]
        else:
            feature.unverified_assumptions = [
                x for x in feature.observed_facts if str(x).startswith("[WEB-UNTRUSTED]")
            ]
        self._store.save(feature)

        # Research-required revision path: evidence has been refreshed. The
        # ChatGPT step was already completed earlier, so skip the second review
        # gate and continue straight to rebuilding the plan.
        if feature.rerun_research and (feature.chatgpt_reviewed or feature.chatgpt_skipped):
            feature.rerun_research = False
            feature.needs_more_research = False
            self._store.save(feature)
            self._set_state(
                feature, FeatureStatus.planning, "running",
                "Re-building plan from refreshed repository evidence",
            )
            if feature.telegram_chat_id is not None:
                self._send_telegram(
                    feature.telegram_chat_id,
                    "\U0001f50d Repository re-analysis complete.\n"
                    "Verified evidence refreshed; rebuilding the plan...",
                )
            self.enqueue(ACTION_BUILD_PLAN, feature.feature_id)
            return

        # ChatGPT consultation prompt is deterministic from existing structured
        # research (no model call) -- it cannot stall.
        self._set_state(
            feature, FeatureStatus.chatgpt_review_pending, "running",
            "Preparing ChatGPT review",
        )
        from jarvis_core.chatgpt_review import build_review_prompt

        # Section T: ChatGPT request versioning (G1, G2, ...)
        feature.chatgpt_request_revision += 1
        feature.chatgpt_prompt = build_review_prompt(feature)

        # Section K: Phase 1 -> Phase 2 transition proof snapshot.
        try:
            from jarvis_core.feature_proof import capture_proof

            capture_proof(
                feature,
                phase="Analyze & Verify",
                stage="Research complete - ChatGPT request prepared",
                description=(
                    "Repository evidence refreshed "
                    f"({len(feature.verified_existing_files)} verified files); "
                    f"request {feature.request_label} generated."
                ),
                related_paths=feature.verified_existing_files,
            )
        except Exception:  # pragma: no cover - proof must never break flow
            logger.exception("Proof capture failed (Phase 1->2)")

        self._store.save(feature)

        if feature.telegram_chat_id is not None:
            from jarvis_core.feature_approval import (
                chatgpt_review_buttons,
                chatgpt_review_ready_message,
                research_complete_message,
            )

            self._send_telegram(
                feature.telegram_chat_id, research_complete_message(feature),
            )
            self._send_telegram(
                feature.telegram_chat_id,
                chatgpt_review_ready_message(feature),
                markup=chatgpt_review_buttons(feature),
            )

        # V1 workflow: EVERY feature waits here for the user's optional
        # ChatGPT review (Telegram buttons or Web UI controls) or an explicit
        # skip. Waiting for the user is not active work — never auto-advance.
        logger.info("Feature %s waiting for ChatGPT review/skip", feature.feature_id[:8])
        self._update_task(
            feature, "waiting_for_confirmation",
            "Waiting for ChatGPT review feedback",
        )

    # ── Stage: ChatGPT review ─────────────────────────────────────────────

    def skip_chatgpt(self, feature_id: str) -> dict:
        feature = self._store.get(feature_id)
        if feature is None or feature.status != FeatureStatus.chatgpt_review_pending.value:
            return {"status": "error", "message": "Feature not awaiting ChatGPT review."}
        feature.chatgpt_skipped = True
        feature.record("chatgpt_skipped", "ChatGPT review skipped by user")
        self._store.save(feature)
        if feature.telegram_chat_id is not None:
            self._send_telegram(
                feature.telegram_chat_id,
                "ChatGPT review skipped. Continuing with Jarvis local research...",
            )
        self.enqueue(ACTION_BUILD_PLAN, feature.feature_id)
        return {"status": "ok", "message": "Continuing with local plan."}

    def submit_chatgpt_review(self, feature_id: str, review_text: str) -> dict:
        """Accept the user-pasted ChatGPT output (Section F/I).

        Provenance: Jarvis never claims it fetched anything from ChatGPT — the
        authorized user submitted this exact text. The SHA-256 of the exact
        submitted content and source + timestamp are stored as the proof chain;
        the text is preserved verbatim (never rewritten by Ollama).

        If ChatGPT returned ``NEED_MORE_RESEARCH`` the feature does NOT advance
        to plan/Cline-ready; it returns to Phase 1 after showing the missing
        evidence.
        """
        import hashlib

        feature = self._store.get(feature_id)
        if feature is None or feature.status != FeatureStatus.chatgpt_review_pending.value:
            return {"status": "error", "message": "Feature not awaiting a ChatGPT review."}
        cleaned = sanitize_review_text(review_text)
        if not cleaned:
            return {"status": "error", "message": "Review text is empty."}
        feature.chatgpt_review = cleaned

        # ── Proof chain / provenance (F) ───────────────────────────────────
        feature.chatgpt_provenance = {
            "source": "user_submitted_chatgpt_output",
            "feature_id": feature.feature_id,
            "submitted_at": jarvis_now().isoformat(),
            "text_length": len(cleaned),
            "sha256": hashlib.sha256(cleaned.encode("utf-8")).hexdigest(),
        }
        feature.record("chatgpt_review", "User submitted ChatGPT output (exact, hashed)")

        # ── Section G: submitted output IS the candidate Cline prompt ──────
        # Preserved verbatim; validated against repository evidence but never
        # silently rewritten by Ollama.
        missing_paths = [
            p for p in _referenced_repo_paths(cleaned)
            if not _repo_path_exists(p)
        ]
        unsafe_markers = ("rm -rf", "format c:", "git push --force")
        unsafe = [m for m in unsafe_markers if m in cleaned.lower()]
        issues = []
        if missing_paths:
            issues.append(f"references non-existent paths: {missing_paths[:5]}")
        if unsafe:
            issues.append(f"requests unsafe actions: {unsafe}")
        if "commit" in cleaned.lower() and "push" in cleaned.lower():
            pass  # instruction NOT to commit/push is expected wording
        feature.prompt_validation = {
            "valid": not issues,
            "issues": issues,
            "missing_paths": missing_paths[:10],
            "unsafe": unsafe,
        }
        feature.cline_candidate_prompt = cleaned
        feature.prompt_source = "ChatGPT"
        feature.prompt_version += 1
        feature.prompt_versions.append({
            "version": feature.prompt_version,
            "source": "ChatGPT",
            "at": jarvis_now().isoformat(),
        })
        self._store.save(feature)

        # ── NEED_MORE_RESEARCH path (I) ───────────────────────────────────
        if "NEED_MORE_RESEARCH" in cleaned.upper():
            feature.needs_more_research = True
            marker = cleaned.upper().find("NEED_MORE_RESEARCH")
            rest = cleaned[marker + len("NEED_MORE_RESEARCH"):]
            facts = [ln.strip() for ln in rest.splitlines() if ln.strip()]
            feature.missing_evidence = facts[:10] or [
                "Repository evidence was insufficient. Re-run Phase 1 research."
            ]
            self._store.save(feature)
            if feature.telegram_chat_id is not None:
                missing = "\n".join(f"- {f[:200]}" for f in feature.missing_evidence)
                self._send_telegram(
                    feature.telegram_chat_id,
                    "\U0001f50d CHATGPT REQUESTED MORE RESEARCH\n\n"
                    "Missing evidence:\n"
                    f"{missing}\n\n"
                    "Returning to Phase 1 (Analyze & Verify) to refresh repository evidence...",
                )
            self._update_task(feature, "running", "More research requested by ChatGPT")
            self.start_worker()
            self.enqueue(ACTION_RESEARCH, feature.feature_id)
            return {
                "status": "ok",
                "action": "more_research",
                "message": "ChatGPT requested more research. Returning to Phase 1.",
            }

        self._update_task(feature, "running", "Analyzing ChatGPT review")
        self.start_worker()
        self.enqueue(ACTION_ANALYZE_REVIEW, feature.feature_id)
        return {"status": "ok", "message": "Review received. Jarvis is analyzing it..."}

    def _run_review_analysis_stage(self, feature: FeatureRequest) -> None:
        logger.info("Review analysis started for feature %s", feature.feature_id[:8])
        from jarvis_core.chatgpt_review import analyze_review

        analysis, timed_out = self._run_with_timeout(
            lambda: analyze_review(feature, feature.chatgpt_review),
            self.OLLAMA_STAGE_TIMEOUT_SECONDS,
            feature.feature_id,
            "analyze_review",
        )

        if self._check_cancelled(feature.feature_id):
            logger.info("Review analysis cancelled for feature %s", feature.feature_id[:8])
            return

        if timed_out or analysis is None:
            # Local reasoning unavailable → continue with research-only plan.
            feature.chatgpt_analysis = None
            feature.record("chatgpt_analysis_failed", "Local analysis unavailable; using research only")
            self._store.save(feature)
            if feature.telegram_chat_id is not None:
                self._send_telegram(
                    feature.telegram_chat_id,
                    "\u26a0\ufe0f Local reasoning was unavailable for the review.\n"
                    "Continuing with the local research plan...",
                )
        else:
            feature.chatgpt_analysis = analysis
            feature.chatgpt_reviewed = True
            feature.record("chatgpt_analyzed", "ChatGPT review analyzed locally")
            self._store.save(feature)
            if feature.telegram_chat_id is not None:
                from jarvis_core.feature_approval import review_analyzed_message

                self._send_telegram(
                    feature.telegram_chat_id, review_analyzed_message(feature),
                )
        self.enqueue(ACTION_BUILD_PLAN, feature.feature_id)

    # ── Stage: final plan + approval gate ─────────────────────────────────

    def _run_plan_stage(self, feature: FeatureRequest) -> None:
        logger.info("Plan stage started for feature %s", feature.feature_id[:8])
        from jarvis_core.feature_plan import build_final_plan

        self._set_state(
            feature, FeatureStatus.planning, "running",
            "Creating implementation plan",
        )
        plan = build_final_plan(feature)
        feature.plan = plan
        feature.risk = plan.get("Estimated risk") or "Medium"
        feature.record("planning", f"Plan ready (risk {feature.risk})")

        # Mandatory Telegram approval gate.
        self._set_state(
            feature, FeatureStatus.approval_pending, "waiting_for_confirmation",
            "Waiting for approval - check Telegram",
        )

        # Section K: Phase 2 -> Phase 3 transition proof snapshot.
        try:
            from jarvis_core.feature_proof import capture_proof

            capture_proof(
                feature,
                phase="Approve & Handoff",
                stage="Final Cline prompt ready",
                description=(
                    f"Candidate prompt {feature.prompt_label} "
                    f"(source: {feature.prompt_source or 'Jarvis Local'}); "
                    f"risk {feature.risk}."
                ),
                related_paths=feature.verified_existing_files,
            )
        except Exception:  # pragma: no cover - proof must never break flow
            logger.exception("Proof capture failed (Phase 2->3)")

        self._store.save(feature)

        from jarvis_core.feature_approval import plan_buttons, plan_ready_message

        if feature.risk == "High":
            from jarvis_core.feature_approval import (
                high_risk_buttons,
                high_risk_message,
            )

            self._send_telegram(
                feature.telegram_chat_id,
                plan_ready_message(feature),
                markup=plan_buttons(feature),
            )
            self._send_telegram(
                feature.telegram_chat_id,
                high_risk_message(feature),
                markup=high_risk_buttons(feature),
            )
        elif feature.telegram_chat_id is not None:
            if feature.cline_candidate_prompt:
                # Section J: show the EXACT prompt that Cline will receive.
                from jarvis_core.feature_approval import (
                    ready_for_cline_buttons,
                    ready_for_cline_message,
                )

                self._send_telegram(
                    feature.telegram_chat_id,
                    ready_for_cline_message(feature) + "\n\n"
                    + f"{feature.cline_candidate_prompt[:2500]}",
                    markup=ready_for_cline_buttons(feature),
                )
            else:
                self._send_telegram(
                    feature.telegram_chat_id,
                    plan_ready_message(feature),
                    markup=plan_buttons(feature),
                )
        else:
            logger.info(
                "Feature %s awaits approval (web source); visible via 'show pending features'.",
                feature.feature_id[:8],
            )
        logger.info("Plan stage completed for feature %s", feature.feature_id[:8])

    # ── Approval gate (Telegram-only, allowlist enforced) ─────────────────

    def _authorize(self, telegram_user_id) -> bool:
        if self._telegram_auth is None:
            return False
        try:
            return bool(self._telegram_auth(telegram_user_id))
        except Exception:
            logger.exception("Feature authorization check failed")
            return False

    def approve_cline(self, feature_id: str, telegram_user_id) -> dict:
        """First approval. High-risk features still need a second confirmation."""
        if not self._authorize(telegram_user_id):
            return {"status": "error", "message": "You are not authorized to control Jarvis."}
        feature = self._store.get(feature_id)
        if feature is None or feature.status != FeatureStatus.approval_pending.value:
            return {"status": "error", "message": "Feature is not awaiting approval."}

        if feature.risk == "High" and not feature.high_risk_confirmed:
            from jarvis_core.feature_approval import high_risk_buttons, high_risk_message

            self._send_telegram(
                feature.telegram_chat_id,
                high_risk_message(feature),
                markup=high_risk_buttons(feature),
            )
            return {
                "status": "high_risk_confirmation_required",
                "message": "High-risk feature: confirm implementation to continue.",
            }

        return self._finalize_approval(feature, telegram_user_id)

    def confirm_high_risk(self, feature_id: str, telegram_user_id) -> dict:
        """Second (stronger) confirmation for high-risk features."""
        if not self._authorize(telegram_user_id):
            return {"status": "error", "message": "You are not authorized to control Jarvis."}
        feature = self._store.get(feature_id)
        if feature is None or feature.status != FeatureStatus.approval_pending.value:
            return {"status": "error", "message": "Feature is not awaiting confirmation."}
        if feature.risk != "High":
            return self._finalize_approval(feature, telegram_user_id)
        feature.high_risk_confirmed = True
        return self._finalize_approval(feature, telegram_user_id)

    def _finalize_approval(self, feature: FeatureRequest, telegram_user_id: int) -> dict:
        from jarvis_core.timezone_utils import jarvis_now

        feature.approval_decision = "approved"
        feature.approved_at = jarvis_now().isoformat()
        feature.record("approved", f"Approved by Telegram user {redact_sensitive(str(telegram_user_id))}")
        cline_task = cline_bridge.build_cline_task(feature)
        feature.cline_task = cline_task
        feature.status = FeatureStatus.cline_ready.value
        feature.record("cline_ready", "Cline task generated")
        self._store.save(feature)
        self._update_task(
            feature, "completed",
            f"Cline task ready for '{feature.interpreted_objective}'",
        )
        self.task_manager.set_result(feature.task_id, {
            "summary": f"Cline task ready for '{feature.interpreted_objective}'.",
            "feature_id": feature.feature_id,
        })
        if feature.telegram_chat_id is not None:
            from jarvis_core.feature_approval import cline_ready_message

            self._send_telegram(feature.telegram_chat_id, cline_ready_message(feature))
        logger.info("Feature %s approved; Cline task ready", feature.feature_id[:8])
        return {
            "status": "ok",
            "action": "cline_ready",
            "feature_id": feature.feature_id,
            "message": "Approved. Cline task is ready.",
        }

    # ── Part 11: Approval → Implementation / Execution bridge ─────────────
    #
    # Approval finalises to CLINE_READY (unchanged Part 8-10 contract). Part 11
    # adds the missing step that hands the approved task off to the EXISTING
    # ClineBridge and moves the feature CLINE_READY → IMPLEMENTATION_IN_PROGRESS
    # → COMPLETED/FAILED. It never fabricates that Cline finished the work: the
    # real implementation run reports its outcome back via
    # ``record_implementation_result``. We use Kiro only to build Jarvis; the
    # Jarvis runtime Cline integration (cline_bridge) is left in place.

    def start_implementation(self, feature_id: str, telegram_user_id=None) -> dict:
        """Start the Cline implementation EXACTLY ONCE for an approved feature.

        Idempotency (persisted ``implementation_started`` flag is the source of
        truth): a double approval-then-start, a duplicate Telegram callback, a
        retry, or a restart after a successful start can never launch a second
        implementation job. Only allowed from CLINE_READY.
        """
        if telegram_user_id is not None and not self._authorize(telegram_user_id):
            return {"status": "error", "message": "You are not authorized to control Jarvis."}
        feature = self._store.get(feature_id)
        if feature is None:
            return {"status": "error", "message": "Feature not found."}

        # Already running / done: never start a duplicate implementation.
        if feature.implementation_started or feature.status in (
            FeatureStatus.implementation_in_progress.value,
            FeatureStatus.completed.value,
        ):
            return {
                "status": "already_running",
                "action": "implementation_in_progress",
                "feature_id": feature.feature_id,
                "message": "Implementation already started for this feature.",
            }
        if feature.status != FeatureStatus.cline_ready.value:
            return {
                "status": "error",
                "message": "Feature is not ready for implementation.",
            }
        if not feature.cline_task:
            return {"status": "error", "message": "No approved Cline task to implement."}

        # Hand the approved prompt to the EXISTING Cline bridge and mark the
        # persisted idempotency flag BEFORE moving state.
        cline_bridge.mark_handoff(feature)
        feature.implementation_started = True
        feature.implementation_started_at = jarvis_now().isoformat()
        feature.record("implementation_started", "Cline implementation handed off")
        self._set_state(
            feature, FeatureStatus.implementation_in_progress,
            "running",
            "Implementation in progress (Cline)",
        )
        if feature.telegram_chat_id is not None:
            self._send_telegram(
                feature.telegram_chat_id,
                "\U0001f6e0\ufe0f IMPLEMENTATION STARTED\n\n"
                f"Feature: {feature.interpreted_objective}\n"
                "Status: Cline is implementing the approved plan.",
            )
        logger.info(
            "Feature %s implementation started (handed to Cline)",
            feature.feature_id[:8],
        )
        return {
            "status": "ok",
            "action": "implementation_started",
            "feature_id": feature.feature_id,
            "message": "Implementation started.",
        }

    def record_implementation_result(
        self, feature_id: str, *,
        success: bool,
        summary: str = "",
        error: str = "",
        verified: Optional[bool] = None,
    ) -> dict:
        """Persist the outcome reported by the Cline implementation run.

        Success → verification/proof snapshot → COMPLETED.
        Failure → error persisted → FAILED (never a fabricated success).
        Only valid while IMPLEMENTATION_IN_PROGRESS; idempotent on terminal
        states.
        """
        feature = self._store.get(feature_id)
        if feature is None:
            return {"status": "error", "message": "Feature not found."}
        if feature.status in (
            FeatureStatus.completed.value, FeatureStatus.failed.value,
        ):
            return {
                "status": "already_recorded",
                "feature_id": feature.feature_id,
                "message": f"Result already recorded ({feature.status}).",
            }
        if feature.status != FeatureStatus.implementation_in_progress.value:
            return {
                "status": "error",
                "message": "Feature is not being implemented.",
            }

        feature.implementation_result = {
            "success": bool(success),
            "summary": redact_sensitive(str(summary))[:2000],
            "error": redact_sensitive(str(error))[:2000],
            "verified": bool(verified) if verified is not None else None,
            "at": jarvis_now().isoformat(),
        }

        if not success:
            feature.record(
                "implementation_failed",
                redact_sensitive(str(error) or "Implementation failed")[:200],
            )
            self._set_state(
                feature, FeatureStatus.failed, "failed",
                "Implementation failed - see reported error",
            )
            if feature.task_id:
                self.task_manager.set_error(
                    feature.task_id,
                    redact_sensitive(str(error) or "Implementation failed")[:300],
                )
            if feature.telegram_chat_id is not None:
                self._send_telegram(
                    feature.telegram_chat_id,
                    "\u274c IMPLEMENTATION FAILED\n\n"
                    f"Feature: {feature.interpreted_objective}\n"
                    f"Reason: {redact_sensitive(str(error) or 'unknown')[:300]}",
                )
            logger.info("Feature %s implementation FAILED", feature.feature_id[:8])
            return {
                "status": "ok",
                "action": "implementation_failed",
                "feature_id": feature.feature_id,
                "message": "Implementation failure recorded.",
            }

        # Success: capture a verification/proof snapshot, then COMPLETE.
        feature.cline_handed_off = True
        feature.record("implementation_completed", "Implementation completed and verified")
        try:
            from jarvis_core.feature_proof import capture_proof

            capture_proof(
                feature,
                phase="Implement & Verify",
                stage="Implementation completed",
                description=(
                    (redact_sensitive(str(summary))[:200] or "Implementation completed")
                    + (" (verified)" if verified else "")
                ),
                related_paths=feature.verified_existing_files,
            )
        except Exception:  # pragma: no cover - proof must never break flow
            logger.exception("Proof capture failed (implementation complete)")
        self._set_state(
            feature, FeatureStatus.completed, "completed",
            "Feature implemented and verified",
        )
        if feature.task_id:
            self.task_manager.set_result(feature.task_id, {
                "summary": (redact_sensitive(str(summary))[:300]
                            or f"Feature '{feature.interpreted_objective}' implemented."),
                "feature_id": feature.feature_id,
            })
        if feature.telegram_chat_id is not None:
            self._send_telegram(
                feature.telegram_chat_id,
                "\u2705 IMPLEMENTATION COMPLETE\n\n"
                f"Feature: {feature.interpreted_objective}\n"
                f"{redact_sensitive(str(summary))[:300]}".rstrip(),
            )
        logger.info("Feature %s implementation COMPLETED", feature.feature_id[:8])
        return {
            "status": "ok",
            "action": "implementation_completed",
            "feature_id": feature.feature_id,
            "message": "Implementation completed.",
        }

    def cancel(self, feature_id: str, *, telegram_user_id=None) -> dict:
        """Cancel a feature. Nothing is ever sent to Cline after cancellation."""
        if telegram_user_id is not None and not self._authorize(telegram_user_id):
            return {"status": "error", "message": "You are not authorized to control Jarvis."}
        feature = self._store.get(feature_id)
        if feature is None:
            return {"status": "error", "message": "Feature not found."}
        if feature.status in (FeatureStatus.completed.value, FeatureStatus.cancelled.value):
            return {"status": "error", "message": f"Feature already {feature.status}."}

        # Signal any in-progress stage to stop cooperatively.
        self._cancel_events.setdefault(feature_id, threading.Event()).set()

        feature.approval_decision = feature.approval_decision or "cancelled"
        self._set_state(feature, FeatureStatus.cancelled, "cancelled", "Feature cancelled by user")
        if feature.task_id:
            # Terminal on the LIVE TASKS side too so the card/quantum core
            # stop counting this feature as active.
            self.task_manager.set_status(feature.task_id, "cancelled")
            self.task_manager.set_error(feature.task_id, "Cancelled before Cline handoff.")
        if feature.telegram_chat_id is not None:
            from jarvis_core.feature_approval import cancelled_message

            self._send_telegram(feature.telegram_chat_id, cancelled_message(feature))
        logger.info("Feature %s cancelled", feature.feature_id[:8])
        return {
            "status": "ok",
            "action": "cancelled",
            "feature_id": feature.feature_id,
            "message": "Feature cancelled.",
        }

    def request_changes(
        self,
        feature_id: str,
        instructions: str,
        *,
        telegram_user_id=None,
        kind: Optional[str] = None,
    ) -> dict:
        """Regenerate the plan after user feedback.

        A revision NEVER mutates original identity fields (original_request /
        interpreted_objective / the feature title). The user's instructions are
        stored separately in ``change_requests``. Depending on their intent the
        plan is rebuilt either from existing evidence (plan-only) or after a
        full re-inspection of the repository (research-required).
        """
        from jarvis_core.feature_utils import detect_revision_kind

        cleaned = sanitize_review_text(instructions)
        if not cleaned:
            return {"status": "error", "message": "Change instructions are empty."}
        if telegram_user_id is not None and not self._authorize(telegram_user_id):
            return {"status": "error", "message": "You are not authorized to control Jarvis."}

        feature = self._store.get(feature_id)
        if feature is None:
            return {"status": "error", "message": "Feature not found."}
        allowed_states = (
            FeatureStatus.approval_pending.value,
            FeatureStatus.chatgpt_review_pending.value,
            FeatureStatus.planning.value,
        )
        if feature.status not in allowed_states:
            return {"status": "error", "message": f"Feature cannot be changed in state '{feature.status}'."}

        revision_kind = kind or detect_revision_kind(cleaned)
        feature.change_requests.append({
            "at": jarvis_now().isoformat(),
            "kind": revision_kind,
            "text": cleaned,
        })
        # Record BEFORE any in-place mutation so the original request is never
        # touched; identity fields stay immutable.
        feature.record(
            "changes_requested",
            f"[{revision_kind}] {cleaned[:120]}",
        )
        if revision_kind == "plan_only":
            self._store.save(feature)
            self._update_task(feature, "running", "Re-planning with your changes")
            self.start_worker()
            self.enqueue(ACTION_BUILD_PLAN, feature.feature_id)
            return {
                "status": "ok",
                "action": "replanning",
                "message": "Change request accepted. Regenerating the plan from existing evidence...",
            }

        # ── RESEARCH-REQUIRED revision ──────────────────────────────────────
        # Re-inspect the repository, refresh verified evidence, then rebuild the
        # ChatGPT request and plan. Never rebuild from stale findings.
        feature.rerun_research = True
        feature.research_revision = (feature.research_revision or 0) + 1
        # Drop resolved evidence that we must not reuse; raw history is kept.
        feature.affected_modules = []
        feature.verified_existing_files = []
        feature.proposed_new_files = []
        self._store.save(feature)
        self._update_task(
            feature, "running",
            "Re-inspecting repository per change request",
        )
        self.start_worker()
        self.enqueue(ACTION_RESEARCH, feature.feature_id)
        return {
            "status": "ok",
            "action": "reresearching",
            "research_revision": feature.research_revision,
            "message": (
                "Change request accepted. Re-inspecting the repository and "
                "refreshing verified evidence before rebuilding the plan..."
            ),
        }

    # ── Queries & history ─────────────────────────────────────────────────

    def get_feature(self, feature_id: str) -> Optional[dict]:
        feature = self._store.get(feature_id)
        if feature is None:
            return None
        data = feature.public()
        data.update({
            "observed_facts": list(feature.observed_facts),
            "model_recommendations": list(feature.model_recommendations),
            "dependencies": list(feature.dependencies),
            "security_implications": list(feature.security_implications),
            "failure_risks": list(feature.failure_risks),
            "testing_requirements": list(feature.testing_requirements),
            "change_requests": list(feature.change_requests),
            "high_risk_confirmed": feature.high_risk_confirmed,
        })
        data["plan"] = feature.plan
        data["resource_snapshot"] = feature.resource_snapshot
        data["cline_task"] = feature.cline_task
        data["chatgpt_prompt"] = feature.chatgpt_prompt or None
        data["cline_candidate_prompt"] = feature.cline_candidate_prompt or None
        data["chatgpt_analysis"] = feature.chatgpt_analysis
        data["history"] = [
            {"at": e.at, "stage": e.stage, "message": e.message} for e in feature.history
        ]
        data["feature_progress"] = build_progress(feature)
        return data

    def list_features(self, status: Optional[str] = None) -> list[dict]:
        features = (
            self._store.list_status(status)
            if status else self._store.all()
        )
        return [f.public() for f in features]

    def pending_features(self) -> list[dict]:
        return [f.public() for f in self._store.pending()]

    def format_history_line(self, feature: FeatureRequest) -> str:
        review = "reviewed" if feature.chatgpt_reviewed else (
            "skipped" if feature.chatgpt_skipped else "not consulted"
        )
        approval = feature.approval_decision or "-"
        cline = "ready" if feature.cline_task else "-"
        created = str(feature.created_at)[:19]
        return (
            f"[{created}] {feature.feature_id[:8]} | {feature.source} | "
            f"{feature.status} | risk={feature.risk} | ChatGPT={review} | "
            f"approval={approval} | cline={cline}\n"
            f"  {feature.original_request[:100]}"
        )

    def _fail_feature(self, feature_id: str, message: str) -> None:
        feature = self._store.get(feature_id)
        if feature is None or feature.status in (
            FeatureStatus.completed.value, FeatureStatus.cancelled.value,
        ):
            return
        self._set_state(feature, FeatureStatus.failed, "failed", redact_sensitive(message))
        if feature.telegram_chat_id is not None:
            self._send_telegram(
                feature.telegram_chat_id,
                "\u26a0\ufe0f Feature workflow failed.\n"
                f"{redact_sensitive(message)}\n\n"
                "The approved plan (if any) is preserved. Say 'show pending features' to retry.",
            )


# Module-level singleton
feature_orchestrator = FeatureOrchestrator()