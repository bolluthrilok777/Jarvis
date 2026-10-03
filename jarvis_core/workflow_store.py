"""Durable SQLite persistence for the locked Jarvis workflow engine (Part 1).

Why SQLite (stdlib ``sqlite3``) rather than the existing JSON-per-record
pattern used by ``FeatureStore``:

- **Atomic writes / transactions** — a workflow state change often touches
  several tables (workflow row + event + action) and must be all-or-nothing.
- **Safe concurrent reads** — Jarvis runs Telegram/server/background threads.
- **Deterministic lookup / ordering** — indexed primary keys and a monotonic
  event sequence give a stable audit order.
- **No corruption from partial writes** — WAL journaling + transactions.
- **Migration / version support** — ``user_version`` pragma + an explicit
  ``schema_migrations`` table.

This store is a thin, thread-safe data layer. It performs *no* state-machine
validation — that is the engine's job. It never launches side effects.

Secrets are never expected here; callers redact before persisting. The store
adds a defensive guard so obviously sensitive event metadata keys are dropped.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from jarvis_core.config import BASE_DIR
from jarvis_core.workflow_models import (
    ActionStatus,
    Workflow,
    WorkflowAction,
    WorkflowEvent,
)
from jarvis_core.workflow_states import WorkflowState, TERMINAL_STATES

# On-disk schema version. Bump when the physical schema changes and add a
# corresponding migration step in ``_migrate``.
SCHEMA_VERSION = 1

DEFAULT_DB_PATH = BASE_DIR / "data" / "workflows.db"

# Event-metadata keys that must never be persisted even if a caller passes
# them. Defensive: the engine already avoids putting secrets in metadata.
_SENSITIVE_METADATA_KEYS = {
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "auth", "cookie", "cookies", "session", "credential",
    "credentials", "private_key", "access_token", "refresh_token", "bearer",
}


def _scrub_metadata(metadata: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Drop obviously sensitive keys from a metadata dict before persistence."""
    if not metadata:
        return {}
    clean: dict[str, Any] = {}
    for key, value in metadata.items():
        if str(key).strip().lower() in _SENSITIVE_METADATA_KEYS:
            continue
        clean[key] = value
    return clean


# Full ordered list of Workflow columns persisted as first-class SQL columns.
# Anything not listed lives in the JSON ``metadata`` column.
_WORKFLOW_COLUMNS = [
    "workflow_id", "project_id", "project_name", "project_path",
    "state", "previous_state", "resume_state",
    "created_at", "updated_at", "started_at", "completed_at",
    "iteration_number",
    "selected_chrome_profile_key", "selected_chrome_account_email",
    "master_gpt_prompt", "chatgpt_conversation_url", "chatgpt_tab_id",
    "chatgpt_window_hwnd",
    "current_user_command", "latest_gpt_output", "approved_gpt_prompt",
    "kiro_session_id", "kiro_project_path", "latest_kiro_result",
    "last_error", "last_error_code", "pause_reason", "cancel_reason",
    "current_action_id", "last_completed_action_id",
    "version",
]


class WorkflowStore:
    """Thread-safe SQLite persistence for workflows, events and actions.

    A single connection is shared across threads (``check_same_thread=False``)
    and guarded by a re-entrant lock, matching the project's lock-guarded
    singleton style. WAL mode allows concurrent readers with a single writer.
    """

    def __init__(self, db_path: "str | Path | None" = None) -> None:
        self._path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._migrate()

    # ── Setup / migration ───────────────────────────────────────────────────

    def _configure(self) -> None:
        with self._lock:
            # WAL: durable, concurrent readers with a single writer, and no
            # corruption from partial writes. FULL sync keeps commits durable.
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                # :memory: databases do not support WAL; that's fine for tests.
                pass
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")

    def _user_version(self) -> int:
        with self._lock:
            row = self._conn.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row else 0

    def _set_user_version(self, version: int) -> None:
        with self._lock:
            # PRAGMA doesn't accept bound params; version is an int we control.
            self._conn.execute(f"PRAGMA user_version={int(version)}")

    def _migrate(self) -> None:
        """Create/upgrade the schema. Idempotent and safe to call on startup."""
        with self._lock:
            current = self._user_version()
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            if current < 1:
                self._create_schema_v1()
                self._record_migration(1)
                self._set_user_version(1)
            # Future migrations: `if current < 2: self._migrate_to_v2()` ...
            self._conn.commit()

    def _record_migration(self, version: int) -> None:
        from jarvis_core.timezone_utils import jarvis_now

        self._conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, jarvis_now().isoformat()),
        )

    def _create_schema_v1(self) -> None:
        c = self._conn
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS workflows (
                workflow_id                   TEXT PRIMARY KEY,
                project_id                    TEXT,
                project_name                  TEXT,
                project_path                  TEXT,
                state                         TEXT NOT NULL,
                previous_state                TEXT,
                resume_state                  TEXT,
                created_at                    TEXT NOT NULL,
                updated_at                    TEXT NOT NULL,
                started_at                    TEXT,
                completed_at                  TEXT,
                iteration_number              INTEGER NOT NULL DEFAULT 0,
                selected_chrome_profile_key   TEXT,
                selected_chrome_account_email TEXT,
                master_gpt_prompt             TEXT,
                chatgpt_conversation_url      TEXT,
                chatgpt_tab_id                TEXT,
                chatgpt_window_hwnd           INTEGER,
                current_user_command          TEXT,
                latest_gpt_output             TEXT,
                approved_gpt_prompt           TEXT,
                kiro_session_id               TEXT,
                kiro_project_path             TEXT,
                latest_kiro_result            TEXT,
                last_error                    TEXT,
                last_error_code               TEXT,
                pause_reason                  TEXT,
                cancel_reason                 TEXT,
                current_action_id             TEXT,
                last_completed_action_id      TEXT,
                version                       INTEGER NOT NULL DEFAULT 0,
                metadata                      TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_workflows_state ON workflows(state)")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_workflows_project ON workflows(project_id)"
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_events (
                sequence         INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id         TEXT NOT NULL UNIQUE,
                workflow_id      TEXT NOT NULL,
                event_type       TEXT NOT NULL,
                from_state       TEXT,
                to_state         TEXT,
                timestamp        TEXT NOT NULL,
                iteration_number INTEGER NOT NULL DEFAULT 0,
                action_id        TEXT,
                message          TEXT,
                metadata         TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id)
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_workflow "
            "ON workflow_events(workflow_id, sequence)"
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_actions (
                action_id    TEXT PRIMARY KEY,
                workflow_id  TEXT NOT NULL,
                action_type  TEXT,
                status       TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                result_ref   TEXT,
                error        TEXT,
                error_code   TEXT,
                metadata     TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id)
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_actions_workflow "
            "ON workflow_actions(workflow_id)"
        )
        # Optional per-key metadata table (kept for future structured queries).
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_metadata (
                workflow_id TEXT NOT NULL,
                key         TEXT NOT NULL,
                value       TEXT,
                updated_at  TEXT NOT NULL,
                PRIMARY KEY (workflow_id, key),
                FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id)
            )
            """
        )

    # ── Row <-> model conversion ─────────────────────────────────────────────

    @staticmethod
    def _workflow_to_row(wf: Workflow) -> dict[str, Any]:
        row = {col: getattr(wf, col) for col in _WORKFLOW_COLUMNS}
        row["metadata"] = json.dumps(wf.metadata or {})
        return row

    @staticmethod
    def _row_to_workflow(row: sqlite3.Row) -> Workflow:
        data = {col: row[col] for col in _WORKFLOW_COLUMNS}
        raw_meta = row["metadata"]
        try:
            data["metadata"] = json.loads(raw_meta) if raw_meta else {}
        except (json.JSONDecodeError, TypeError):
            data["metadata"] = {}
        return Workflow(**data)

    # ── Workflow CRUD ─────────────────────────────────────────────────────────

    def insert_workflow(self, wf: Workflow) -> None:
        """Insert a brand-new workflow row. Fails if the id already exists."""
        row = self._workflow_to_row(wf)
        cols = list(row.keys())
        placeholders = ", ".join(["?"] * len(cols))
        colnames = ", ".join(cols)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO workflows ({colnames}) VALUES ({placeholders})",
                [row[c] for c in cols],
            )
            self._conn.commit()

    def update_workflow(self, wf: Workflow, *, expected_version: Optional[int] = None) -> bool:
        """Persist an existing workflow.

        When *expected_version* is provided the update only succeeds if the
        stored ``version`` still matches (optimistic concurrency). Returns True
        on success, False if the version guard failed (stale write).
        """
        row = self._workflow_to_row(wf)
        assignments = ", ".join(f"{c}=?" for c in _WORKFLOW_COLUMNS) + ", metadata=?"
        values = [row[c] for c in _WORKFLOW_COLUMNS] + [row["metadata"]]
        with self._lock:
            if expected_version is not None:
                cur = self._conn.execute(
                    f"UPDATE workflows SET {assignments} "
                    "WHERE workflow_id=? AND version=?",
                    values + [wf.workflow_id, expected_version],
                )
            else:
                cur = self._conn.execute(
                    f"UPDATE workflows SET {assignments} WHERE workflow_id=?",
                    values + [wf.workflow_id],
                )
            self._conn.commit()
            return cur.rowcount > 0

    def get_workflow(self, workflow_id: str) -> Optional[Workflow]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
        return self._row_to_workflow(row) if row else None

    def list_workflows(self) -> list[Workflow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflows ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_workflow(r) for r in rows]

    def list_active_workflows(self) -> list[Workflow]:
        """Return non-terminal workflows (COMPLETED/CANCELLED excluded)."""
        terminal = [s.value for s in TERMINAL_STATES]
        placeholders = ", ".join(["?"] * len(terminal))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM workflows WHERE state NOT IN ({placeholders}) "
                "ORDER BY created_at DESC",
                terminal,
            ).fetchall()
        return [self._row_to_workflow(r) for r in rows]

    def delete_workflow(self, workflow_id: str) -> bool:
        with self._lock:
            self._conn.execute(
                "DELETE FROM workflow_events WHERE workflow_id=?", (workflow_id,)
            )
            self._conn.execute(
                "DELETE FROM workflow_actions WHERE workflow_id=?", (workflow_id,)
            )
            self._conn.execute(
                "DELETE FROM workflow_metadata WHERE workflow_id=?", (workflow_id,)
            )
            cur = self._conn.execute(
                "DELETE FROM workflows WHERE workflow_id=?", (workflow_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ── Atomic transactional mutation ────────────────────────────────────────

    def commit_transition(
        self,
        wf: Workflow,
        event: WorkflowEvent,
        *,
        expected_version: int,
    ) -> bool:
        """Atomically persist a workflow update *and* append an event.

        Either both the workflow row and the event are written, or neither is.
        Uses optimistic concurrency on ``version``. Returns False (rolled back)
        if the version guard fails, so a stale read-modify-write cannot corrupt
        state.
        """
        row = self._workflow_to_row(wf)
        assignments = ", ".join(f"{c}=?" for c in _WORKFLOW_COLUMNS) + ", metadata=?"
        values = [row[c] for c in _WORKFLOW_COLUMNS] + [row["metadata"]]
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    f"UPDATE workflows SET {assignments} "
                    "WHERE workflow_id=? AND version=?",
                    values + [wf.workflow_id, expected_version],
                )
                if cur.rowcount == 0:
                    self._conn.rollback()
                    return False
                self._insert_event_locked(event)
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    # ── Events ────────────────────────────────────────────────────────────────

    def _insert_event_locked(self, event: WorkflowEvent) -> None:
        """Insert an event assuming the caller already holds the lock/txn."""
        self._conn.execute(
            """
            INSERT INTO workflow_events
                (event_id, workflow_id, event_type, from_state, to_state,
                 timestamp, iteration_number, action_id, message, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.workflow_id,
                event.event_type,
                event.from_state,
                event.to_state,
                event.timestamp,
                event.iteration_number,
                event.action_id,
                event.message,
                json.dumps(_scrub_metadata(event.metadata)),
            ),
        )

    def append_event(self, event: WorkflowEvent) -> None:
        """Append a standalone audit event (own transaction)."""
        with self._lock:
            self._insert_event_locked(event)
            self._conn.commit()

    def get_events(self, workflow_id: str, *, limit: Optional[int] = None) -> list[WorkflowEvent]:
        query = (
            "SELECT * FROM workflow_events WHERE workflow_id=? ORDER BY sequence ASC"
        )
        params: list[Any] = [workflow_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> WorkflowEvent:
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        return WorkflowEvent(
            event_id=row["event_id"],
            workflow_id=row["workflow_id"],
            event_type=row["event_type"],
            from_state=row["from_state"],
            to_state=row["to_state"],
            timestamp=row["timestamp"],
            iteration_number=row["iteration_number"],
            action_id=row["action_id"],
            message=row["message"] or "",
            metadata=meta,
            sequence=row["sequence"],
        )

    # ── Actions (idempotency) ─────────────────────────────────────────────────

    def get_action(self, action_id: str) -> Optional[WorkflowAction]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_actions WHERE action_id=?", (action_id,)
            ).fetchone()
        return self._row_to_action(row) if row else None

    def list_actions(self, workflow_id: str) -> list[WorkflowAction]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_actions WHERE workflow_id=? ORDER BY created_at ASC",
                (workflow_id,),
            ).fetchall()
        return [self._row_to_action(r) for r in rows]

    def try_begin_action(self, action: WorkflowAction) -> tuple[bool, Optional[WorkflowAction]]:
        """Atomically register a RUNNING action iff no record already exists.

        Returns ``(True, action)`` when this call created the RUNNING record,
        or ``(False, existing)`` when a record with the same ``action_id``
        already exists (running or completed) — the caller must NOT duplicate.
        This is the core idempotency primitive and is safe across restarts and
        concurrent callers because it relies on the PRIMARY KEY constraint.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute(
                    "SELECT * FROM workflow_actions WHERE action_id=?",
                    (action.action_id,),
                ).fetchone()
                if existing is not None:
                    self._conn.rollback()
                    return False, self._row_to_action(existing)
                self._conn.execute(
                    """
                    INSERT INTO workflow_actions
                        (action_id, workflow_id, action_type, status,
                         created_at, updated_at, result_ref, error, error_code, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action.action_id,
                        action.workflow_id,
                        action.action_type,
                        ActionStatus.RUNNING.value,
                        action.created_at,
                        action.updated_at,
                        action.result_ref,
                        action.error,
                        action.error_code,
                        json.dumps(_scrub_metadata(action.metadata)),
                    ),
                )
                self._conn.commit()
                return True, action
            except Exception:
                self._conn.rollback()
                raise

    def update_action(self, action: WorkflowAction) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE workflow_actions
                SET action_type=?, status=?, updated_at=?, result_ref=?,
                    error=?, error_code=?, metadata=?
                WHERE action_id=?
                """,
                (
                    action.action_type,
                    action.status,
                    action.updated_at,
                    action.result_ref,
                    action.error,
                    action.error_code,
                    json.dumps(_scrub_metadata(action.metadata)),
                    action.action_id,
                ),
            )
            self._conn.commit()

    @staticmethod
    def _row_to_action(row: sqlite3.Row) -> WorkflowAction:
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        return WorkflowAction(
            action_id=row["action_id"],
            workflow_id=row["workflow_id"],
            action_type=row["action_type"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            result_ref=row["result_ref"],
            error=row["error"],
            error_code=row["error_code"],
            metadata=meta,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    @property
    def db_path(self) -> Path:
        return self._path
