"""
FORGE Core — Persistent Task Queue
====================================
A durable, priority-ordered task queue that survives process restarts.
Prevents two concurrent FORGE agents from processing the same document.
Supports timeouts, retries, and priority levels.

Backed by a simple SQLite database — zero infrastructure required,
works on any machine, ITAR-safe (fully local).
"""

import sqlite3
import json
import uuid
import time
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from contextlib import contextmanager
from enum import IntEnum

from .logger import get_logger

log = get_logger("task_queue")


class Priority(IntEnum):
    CRITICAL = 0    # ITAR docs, safety failures — process immediately
    HIGH     = 1    # Errors (not warnings), newly staged files
    MEDIUM   = 2    # Changed files, low-score docs
    LOW      = 3    # Periodic re-verification of clean docs


class TaskStatus(str):
    PENDING    = "pending"
    LOCKED     = "locked"      # Being processed right now
    DONE       = "done"
    FAILED     = "failed"
    CANCELLED  = "cancelled"


@dataclass
class Task:
    id:           str
    doc_path:     str
    task_type:    str          # "verify" | "repair" | "reindex" | "cross_check"
    priority:     int          = Priority.MEDIUM
    status:       str          = TaskStatus.PENDING
    created_at:   str          = field(default_factory=lambda: datetime.now().isoformat())
    scheduled_at: str          = field(default_factory=lambda: datetime.now().isoformat())
    locked_at:    Optional[str]= None
    locked_by:    Optional[str]= None  # Process ID of the agent holding the lock
    completed_at: Optional[str]= None
    attempt:      int          = 0
    max_attempts: int          = 3
    timeout_seconds: int       = 300   # 5 min default
    metadata:     dict         = field(default_factory=dict)
    result:       Optional[dict]= None
    error:        Optional[str]= None

    @property
    def is_expired(self) -> bool:
        if self.locked_at and self.status == TaskStatus.LOCKED:
            locked = datetime.fromisoformat(self.locked_at)
            return (datetime.now() - locked).total_seconds() > self.timeout_seconds
        return False

    @property
    def can_retry(self) -> bool:
        return self.attempt < self.max_attempts and self.status in (
            TaskStatus.FAILED, TaskStatus.LOCKED
        )


class TaskQueue:
    """
    SQLite-backed persistent task queue.
    Thread-safe and process-safe via SQLite's write-ahead logging.
    """

    def __init__(self, db_path: str = "./forge_tasks.db", worker_id: str = None):
        self.db_path   = Path(db_path)
        self.worker_id = worker_id or f"forge_{uuid.uuid4().hex[:8]}"
        self._init_db()
        log.info(f"Task queue: {db_path} (worker: {self.worker_id})")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")  # Better concurrent access
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id              TEXT PRIMARY KEY,
                    doc_path        TEXT NOT NULL,
                    task_type       TEXT NOT NULL,
                    priority        INTEGER NOT NULL DEFAULT 2,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    created_at      TEXT NOT NULL,
                    scheduled_at    TEXT NOT NULL,
                    locked_at       TEXT,
                    locked_by       TEXT,
                    completed_at    TEXT,
                    attempt         INTEGER NOT NULL DEFAULT 0,
                    max_attempts    INTEGER NOT NULL DEFAULT 3,
                    timeout_seconds INTEGER NOT NULL DEFAULT 300,
                    metadata        TEXT DEFAULT '{}',
                    result          TEXT,
                    error           TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_status_priority
                    ON tasks(status, priority, scheduled_at);

                CREATE INDEX IF NOT EXISTS idx_doc_path
                    ON tasks(doc_path, status);

                CREATE TABLE IF NOT EXISTS task_log (
                    id          TEXT PRIMARY KEY,
                    task_id     TEXT NOT NULL,
                    timestamp   TEXT NOT NULL,
                    event       TEXT NOT NULL,
                    detail      TEXT,
                    worker_id   TEXT
                );
            """)

    # ── Enqueue ───────────────────────────────────────────────────────────

    def enqueue(self, doc_path: str, task_type: str = "verify",
                priority: Priority = Priority.MEDIUM,
                metadata: dict = None, delay_seconds: int = 0,
                deduplicate: bool = True) -> Optional[Task]:
        """
        Add a task to the queue. Returns None if deduplicated.
        deduplicate=True: skip if same doc+type already pending/locked.
        """
        if deduplicate:
            existing = self.get_pending_for_doc(doc_path, task_type)
            if existing:
                log.debug(f"Deduplicated: {doc_path} ({task_type})")
                return None

        scheduled = (
            (datetime.now() + timedelta(seconds=delay_seconds)).isoformat()
            if delay_seconds > 0
            else datetime.now().isoformat()
        )

        task = Task(
            id=uuid.uuid4().hex[:16],
            doc_path=doc_path,
            task_type=task_type,
            priority=int(priority),
            scheduled_at=scheduled,
            metadata=metadata or {},
        )

        with self._conn() as conn:
            conn.execute(
                """INSERT INTO tasks
                   (id, doc_path, task_type, priority, status, created_at,
                    scheduled_at, attempt, max_attempts, timeout_seconds, metadata)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (task.id, task.doc_path, task.task_type, task.priority,
                 task.status, task.created_at, task.scheduled_at,
                 task.attempt, task.max_attempts, task.timeout_seconds,
                 json.dumps(task.metadata))
            )
            self._log_event(conn, task.id, "enqueued",
                            f"priority={priority.name}", self.worker_id)

        log.debug(f"Queued [{priority.name}]: {doc_path} ({task_type})")
        return task

    def enqueue_many(self, items: list[dict]) -> int:
        """Bulk enqueue. items = [{doc_path, task_type, priority, metadata}]"""
        count = 0
        for item in items:
            result = self.enqueue(
                doc_path=item["doc_path"],
                task_type=item.get("task_type", "verify"),
                priority=item.get("priority", Priority.MEDIUM),
                metadata=item.get("metadata", {}),
            )
            if result:
                count += 1
        return count

    # ── Claim ─────────────────────────────────────────────────────────────

    def claim_next(self, task_types: list[str] = None) -> Optional[Task]:
        """
        Atomically claim the highest-priority pending task.
        Returns None if queue is empty.
        """
        # First: expire any stale locks
        self._expire_locks()

        type_filter = ""
        params = [self.worker_id, datetime.now().isoformat()]
        if task_types:
            placeholders = ",".join("?" * len(task_types))
            type_filter = f"AND task_type IN ({placeholders})"
            params = [self.worker_id, datetime.now().isoformat()] + task_types

        with self._conn() as conn:
            # Find next claimable task
            row = conn.execute(f"""
                SELECT * FROM tasks
                WHERE status = 'pending'
                  AND scheduled_at <= datetime('now')
                  {type_filter}
                ORDER BY priority ASC, scheduled_at ASC
                LIMIT 1
            """, params[2:] if task_types else []).fetchone()

            if not row:
                return None

            task_id = row["id"]
            now = datetime.now().isoformat()

            # Atomically lock it
            updated = conn.execute(
                """UPDATE tasks
                   SET status='locked', locked_at=?, locked_by=?, attempt=attempt+1
                   WHERE id=? AND status='pending'""",
                (now, self.worker_id, task_id)
            ).rowcount

            if updated == 0:
                return None  # Race condition — another worker claimed it

            self._log_event(conn, task_id, "claimed", f"worker={self.worker_id}")
            return self._row_to_task(conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone())

    # ── Complete / fail ───────────────────────────────────────────────────

    def complete(self, task_id: str, result: dict = None):
        with self._conn() as conn:
            conn.execute(
                """UPDATE tasks SET status='done', completed_at=?, result=?,
                   locked_at=NULL, locked_by=NULL
                   WHERE id=? AND locked_by=?""",
                (datetime.now().isoformat(), json.dumps(result or {}),
                 task_id, self.worker_id)
            )
            self._log_event(conn, task_id, "completed",
                            f"result_keys={list((result or {}).keys())}")

    def fail(self, task_id: str, error: str = "", retry: bool = True):
        with self._conn() as conn:
            row = conn.execute("SELECT attempt, max_attempts FROM tasks WHERE id=?",
                               (task_id,)).fetchone()
            if not row:
                return

            if retry and row["attempt"] < row["max_attempts"]:
                # Retry with backoff
                delay = 60 * (2 ** row["attempt"])  # exponential: 60s, 120s, 240s
                scheduled = (datetime.now() + timedelta(seconds=delay)).isoformat()
                conn.execute(
                    """UPDATE tasks SET status='pending', locked_at=NULL, locked_by=NULL,
                       scheduled_at=?, error=? WHERE id=?""",
                    (scheduled, error[:500], task_id)
                )
                self._log_event(conn, task_id, "retry_scheduled",
                                f"delay={delay}s error={error[:80]}")
            else:
                conn.execute(
                    """UPDATE tasks SET status='failed', completed_at=?,
                       locked_at=NULL, locked_by=NULL, error=? WHERE id=?""",
                    (datetime.now().isoformat(), error[:500], task_id)
                )
                self._log_event(conn, task_id, "failed", error[:80])

    def cancel(self, doc_path: str, task_type: str = None):
        """Cancel pending tasks for a document."""
        with self._conn() as conn:
            if task_type:
                conn.execute(
                    "UPDATE tasks SET status='cancelled' WHERE doc_path=? AND task_type=? AND status='pending'",
                    (doc_path, task_type)
                )
            else:
                conn.execute(
                    "UPDATE tasks SET status='cancelled' WHERE doc_path=? AND status='pending'",
                    (doc_path,)
                )

    # ── Query ─────────────────────────────────────────────────────────────

    def get_pending_for_doc(self, doc_path: str,
                             task_type: str = None) -> Optional[Task]:
        with self._conn() as conn:
            if task_type:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE doc_path=? AND task_type=? AND status IN ('pending','locked')",
                    (doc_path, task_type)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE doc_path=? AND status IN ('pending','locked')",
                    (doc_path,)
                ).fetchone()
            return self._row_to_task(row) if row else None

    def stats(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
            ).fetchall()
            by_status = {row["status"]: row["cnt"] for row in rows}

            pending_by_prio = conn.execute(
                "SELECT priority, COUNT(*) as cnt FROM tasks WHERE status='pending' GROUP BY priority"
            ).fetchall()
            by_priority = {row["priority"]: row["cnt"] for row in pending_by_prio}

        return {
            "by_status":   by_status,
            "by_priority": by_priority,
            "worker_id":   self.worker_id,
            "db_path":     str(self.db_path),
        }

    # ── Internals ─────────────────────────────────────────────────────────

    def _expire_locks(self):
        """Release locks held by crashed workers."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, locked_at, timeout_seconds FROM tasks WHERE status='locked'"
            ).fetchall()
            for row in rows:
                if row["locked_at"]:
                    locked = datetime.fromisoformat(row["locked_at"])
                    if (datetime.now() - locked).total_seconds() > row["timeout_seconds"]:
                        conn.execute(
                            "UPDATE tasks SET status='pending', locked_at=NULL, locked_by=NULL WHERE id=?",
                            (row["id"],)
                        )
                        self._log_event(conn, row["id"], "lock_expired",
                                        "Expired stale lock — requeueing")
                        log.warning(f"Expired stale lock: task {row['id']}")

    def _log_event(self, conn, task_id: str, event: str,
                    detail: str = "", worker_id: str = None):
        conn.execute(
            "INSERT INTO task_log (id, task_id, timestamp, event, detail, worker_id) VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex[:12], task_id, datetime.now().isoformat(),
             event, detail, worker_id or self.worker_id)
        )

    def _row_to_task(self, row) -> Optional[Task]:
        if not row:
            return None
        d = dict(row)
        d["metadata"] = json.loads(d.get("metadata") or "{}")
        d["result"]   = json.loads(d.get("result") or "null")
        return Task(**{k: v for k, v in d.items() if k in Task.__dataclass_fields__})
