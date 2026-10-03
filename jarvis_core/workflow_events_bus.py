"""In-process pub/sub bus for realtime workflow updates (Part 2).

Part 1 (the workflow engine + store) is LOCKED and knows nothing about the UI.
To deliver realtime updates to the Architecture frontend without modifying any
Part 1 file, the Part 2 API layer publishes lightweight messages onto this bus
immediately after it calls a ``WorkflowEngine`` method. WebSocket connections
subscribe per ``workflow_id`` and receive those messages.

Design:
- Publishers are synchronous (the REST handlers run in a threadpool). Publishing
  never blocks on slow/absent subscribers and never raises into the caller.
- Subscribers are asyncio consumers (the WebSocket endpoint). Each subscriber
  gets its own bounded ``asyncio.Queue``; if a client is too slow and its queue
  fills, the oldest message is dropped rather than stalling the whole system.
- A subscriber is bound to the event loop that created it; publishing hops onto
  that loop via ``call_soon_threadsafe`` so cross-thread publishing is safe.

No secrets are placed on the bus — messages carry the same safe public workflow
view / event data that the REST API already exposes.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger("jarvis.workflow.bus")

# Message "type" values broadcast to the frontend. These mirror the meaningful
# lifecycle moments; the frontend switches on ``type``.
MSG_WORKFLOW_CREATED = "workflow_created"
MSG_WORKFLOW_UPDATED = "workflow_updated"
MSG_STATE_TRANSITION = "state_transition"
MSG_EVENT_ADDED = "event_added"
MSG_WORKFLOW_PAUSED = "workflow_paused"
MSG_WORKFLOW_RESUMED = "workflow_resumed"
MSG_WORKFLOW_FAILED = "workflow_failed"
MSG_WORKFLOW_CANCELLED = "workflow_cancelled"
MSG_WORKFLOW_COMPLETED = "workflow_completed"


class _Subscriber:
    __slots__ = ("loop", "queue")

    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int = 200) -> None:
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)


class WorkflowEventBus:
    """Thread-safe fan-out of workflow messages to per-workflow subscribers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # workflow_id -> set of subscribers. A special key "*" receives every
        # message regardless of workflow (used by the multi-workflow list view).
        self._subs: dict[str, set[_Subscriber]] = {}

    # ── Subscription (async side) ─────────────────────────────────────────────

    def subscribe(self, workflow_id: str) -> _Subscriber:
        """Register a subscriber for *workflow_id* (or "*" for all). Async loop
        must be running when this is called (it is, inside a WS handler)."""
        loop = asyncio.get_event_loop()
        sub = _Subscriber(loop)
        with self._lock:
            self._subs.setdefault(workflow_id, set()).add(sub)
        return sub

    def unsubscribe(self, workflow_id: str, sub: _Subscriber) -> None:
        with self._lock:
            subs = self._subs.get(workflow_id)
            if subs is not None:
                subs.discard(sub)
                if not subs:
                    self._subs.pop(workflow_id, None)

    # ── Publishing (sync side, any thread) ────────────────────────────────────

    def publish(self, workflow_id: str, message: dict[str, Any]) -> None:
        """Broadcast *message* to subscribers of *workflow_id* and to "*".

        Safe to call from any thread. Never raises; slow subscribers drop their
        oldest queued message instead of blocking publishers.
        """
        targets: list[_Subscriber] = []
        with self._lock:
            for key in (workflow_id, "*"):
                subs = self._subs.get(key)
                if subs:
                    targets.extend(subs)
        for sub in targets:
            self._deliver(sub, message)

    def _deliver(self, sub: _Subscriber, message: dict[str, Any]) -> None:
        def _put() -> None:
            q = sub.queue
            if q.full():
                # Drop oldest to make room — realtime freshness over completeness.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass

        try:
            sub.loop.call_soon_threadsafe(_put)
        except RuntimeError:
            # Loop already closed (client gone). Ignore — cleanup happens on
            # unsubscribe from the WS handler.
            logger.debug("Dropping message for closed loop subscriber")

    def subscriber_count(self, workflow_id: Optional[str] = None) -> int:
        with self._lock:
            if workflow_id is None:
                return sum(len(s) for s in self._subs.values())
            return len(self._subs.get(workflow_id, set()))


# Module-level singleton, following the project's convention.
workflow_event_bus = WorkflowEventBus()
