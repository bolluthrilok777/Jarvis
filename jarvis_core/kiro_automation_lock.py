"""KiroAutomationLock — exclusive, single-owner Kiro automation lock (Part 7).

Only ONE workflow may automate the real Kiro app at a time. The user may be
using Kiro manually or another workflow may be mid-automation; this lock makes
Jarvis refuse to type/click into Kiro unless it holds exclusive ownership.

Durability: ownership is persisted to a small JSON file (``data/kiro_lock.json``)
so a Jarvis restart does not immediately create competing automation. A crashed
owner's lock is reclaimed ONLY after it is proven stale (heartbeat age exceeds
the TTL) — never blindly.

Thread-safe via an in-process RLock; the persisted file is the cross-restart
source of truth. This lock guards automation intent; it does not itself touch
Kiro.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional

from jarvis_core.timezone_utils import jarvis_now

logger = logging.getLogger("jarvis.kirolock")

_DEFAULT_LOCK_PATH = os.path.join("data", "kiro_lock.json")
# A heartbeat older than this many seconds marks the lock stale (reclaimable).
DEFAULT_STALE_SECONDS = 120.0


def _now_ts() -> float:
    return jarvis_now().timestamp()


def _now_iso() -> str:
    return jarvis_now().isoformat()


class KiroLockError(Exception):
    """Raised when the lock is held by another owner."""


class KiroAutomationLock:
    """Exclusive Kiro automation lock with durable, heartbeat-based ownership."""

    def __init__(self, *, lock_path: Optional[str] = None,
                 stale_seconds: float = DEFAULT_STALE_SECONDS,
                 process_alive_fn=None) -> None:
        self._path = str(lock_path or _DEFAULT_LOCK_PATH)
        self._stale_seconds = float(stale_seconds)
        self._lock = threading.RLock()
        # Injectable "is this pid still alive" check (default uses psutil).
        self._process_alive_fn = process_alive_fn or self._default_process_alive

    # ── Persistence ────────────────────────────────────────────────────────

    def _read(self) -> Optional[dict]:
        try:
            if not os.path.isfile(self._path):
                return None
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) and data.get("owner") else None
        except Exception:
            logger.debug("lock read failed", exc_info=True)
            return None

    def _write(self, data: Optional[dict]) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            if data is None:
                if os.path.isfile(self._path):
                    os.remove(self._path)
                return
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self._path)
        except Exception:
            logger.debug("lock write failed", exc_info=True)

    @staticmethod
    def _default_process_alive(pid) -> bool:
        if not pid:
            return False
        try:
            import psutil
            return psutil.pid_exists(int(pid))
        except Exception:
            # If we can't tell, assume alive (safer: don't reclaim blindly).
            return True

    # ── Staleness ────────────────────────────────────────────────────────────

    def is_stale(self, record: Optional[dict] = None) -> bool:
        """True when the current (or given) lock record is reclaimable.

        Stale = heartbeat age exceeds the TTL OR the recorded process is proven
        gone. A missing lock is not "stale" (it's simply free).
        """
        rec = record if record is not None else self._read()
        if not rec:
            return False
        hb = rec.get("heartbeat_ts")
        pid = rec.get("pid")
        # Proven dead process → stale.
        if pid is not None and not self._process_alive_fn(pid):
            return True
        if hb is None:
            return True
        try:
            return (_now_ts() - float(hb)) > self._stale_seconds
        except Exception:
            return True

    # ── Public API ─────────────────────────────────────────────────────────

    def owner(self) -> Optional[str]:
        rec = self._read()
        return rec.get("owner") if rec else None

    def owner_record(self) -> Optional[dict]:
        return self._read()

    def acquire(self, workflow_id: str, *, pid: Optional[int] = None) -> bool:
        """Acquire exclusive ownership for *workflow_id*.

        Returns True on success. Idempotent: re-acquiring while already the owner
        succeeds and refreshes the heartbeat. Fails (False) when another live,
        non-stale owner holds the lock. A stale/dead owner's lock is reclaimed.
        """
        with self._lock:
            rec = self._read()
            if rec and rec.get("owner") and rec["owner"] != workflow_id:
                if not self.is_stale(rec):
                    return False
                logger.info("Reclaiming stale Kiro lock from %s", rec.get("owner"))
            new = {
                "owner": workflow_id,
                "pid": int(pid) if pid is not None else os.getpid(),
                "acquired_at": rec.get("acquired_at") if (rec and rec.get("owner") == workflow_id) else _now_iso(),
                "heartbeat_ts": _now_ts(),
                "heartbeat_at": _now_iso(),
            }
            self._write(new)
            return True

    def heartbeat(self, workflow_id: str) -> bool:
        """Refresh the heartbeat if *workflow_id* owns the lock. Returns success."""
        with self._lock:
            rec = self._read()
            if not rec or rec.get("owner") != workflow_id:
                return False
            rec["heartbeat_ts"] = _now_ts()
            rec["heartbeat_at"] = _now_iso()
            self._write(rec)
            return True

    def release(self, workflow_id: str) -> bool:
        """Release ownership if *workflow_id* owns it. Returns success.

        A non-owner release is a no-op (returns False) so one workflow can never
        release another's lock.
        """
        with self._lock:
            rec = self._read()
            if not rec:
                return True
            if rec.get("owner") != workflow_id:
                return False
            self._write(None)
            return True

    def force_clear(self) -> None:
        """Administrative clear (used by tests/recovery tooling)."""
        with self._lock:
            self._write(None)


# Module-level singleton, following the project convention.
kiro_automation_lock = KiroAutomationLock()
