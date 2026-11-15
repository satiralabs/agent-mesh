"""Task store (v2 Phase 1) — strict lifecycle, dependencies, idempotency."""
import json
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

PRIORITIES = ("low", "normal", "high", "urgent")
STATUSES = ("DRAFT", "SENT", "ACCEPTED", "REJECTED", "IN_PROGRESS",
            "IN_REVIEW", "DONE", "FAILED")
TRANSITIONS = {
    "DRAFT": {"SENT"},
    "SENT": {"ACCEPTED", "REJECTED"},
    "ACCEPTED": {"IN_PROGRESS"},
    "REJECTED": set(),
    "IN_PROGRESS": {"IN_REVIEW", "FAILED"},
    "IN_REVIEW": {"DONE", "FAILED", "IN_PROGRESS"},
    "DONE": set(),
    "FAILED": set(),
}
# Moving into these states requires any depends_on task to be DONE.
_DEPENDENCY_GATED = {"ACCEPTED", "IN_PROGRESS"}


class InvalidTransition(ValueError):
    pass


class TaskStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        conn = self._conn()
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    instructions TEXT DEFAULT '',
                    requester TEXT NOT NULL,
                    assignee_agent TEXT,
                    assignee_capability TEXT,
                    status TEXT NOT NULL DEFAULT 'DRAFT',
                    priority TEXT NOT NULL DEFAULT 'normal',
                    parent_id TEXT,
                    depends_on TEXT,
                    idempotency_key TEXT UNIQUE,
                    budget_time_sec REAL,
                    budget_tokens INTEGER,
                    budget_money REAL,
                    progress_notes TEXT DEFAULT '[]',
                    result TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )""")
        conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["progress_notes"] = json.loads(d.get("progress_notes") or "[]")
        return d

    def create(self, title: str, requester: str, instructions: str = "",
               assignee_agent: Optional[str] = None,
               assignee_capability: Optional[str] = None,
               priority: str = "normal", parent_id: Optional[str] = None,
               depends_on: Optional[str] = None,
               idempotency_key: Optional[str] = None,
               budget_time_sec=None, budget_tokens=None,
               budget_money=None):
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}")
        if idempotency_key:
            existing = self.get_by_idempotency_key(idempotency_key)
            if existing:
                return existing, True
        task_id = "tsk_" + uuid.uuid4().hex[:16]
        now = time.time()
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO tasks (id,title,instructions,requester,"
                    "assignee_agent,assignee_capability,status,priority,"
                    "parent_id,depends_on,idempotency_key,budget_time_sec,"
                    "budget_tokens,budget_money,progress_notes,created_at,"
                    "updated_at) VALUES (?,?,?,?,?,?,'DRAFT',?,?,?,?,?,?,?,?,?,?)",
                    (task_id, title, instructions, requester, assignee_agent,
                     assignee_capability, priority, parent_id, depends_on,
                     idempotency_key, budget_time_sec, budget_tokens,
                     budget_money, "[]", now, now))
        except sqlite3.IntegrityError:
            # Lost a race on the idempotency key: return the winner.
            existing = self.get_by_idempotency_key(idempotency_key or "")
            if existing:
                return existing, True
            raise
        finally:
            conn.close()
        return self.get(task_id), False

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM tasks WHERE id=?",
                           (task_id,)).fetchone()
        conn.close()
        return self._row(row) if row else None

    def get_by_idempotency_key(self, key: str) -> Optional[Dict[str, Any]]:
        if not key:
            return None
        conn = self._conn()
        row = conn.execute("SELECT * FROM tasks WHERE idempotency_key=?",
                           (key,)).fetchone()
        conn.close()
        return self._row(row) if row else None

    def list_tasks(self, assignee_agent: Optional[str] = None,
                   requester: Optional[str] = None,
                   status: Optional[str] = None) -> List[Dict[str, Any]]:
        q = "SELECT * FROM tasks WHERE 1=1"
        params: List[Any] = []
        if assignee_agent:
            q += " AND assignee_agent=?"
            params.append(assignee_agent)
        if requester:
            q += " AND requester=?"
            params.append(requester)
        if status:
            q += " AND status=?"
            params.append(status)
        q += " ORDER BY created_at"
        conn = self._conn()
        rows = conn.execute(q, params).fetchall()
        conn.close()
        return [self._row(r) for r in rows]

    def transition(self, task_id: str, to_status: str,
                   result: Optional[str] = None) -> Dict[str, Any]:
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        if to_status not in STATUSES:
            raise InvalidTransition(f"unknown status {to_status!r}")
        current = task["status"]
        if to_status not in TRANSITIONS[current]:
            allowed = sorted(TRANSITIONS[current]) or ["(none — terminal)"]
            raise InvalidTransition(
                f"invalid transition {current} -> {to_status}; "
                f"allowed from {current}: {', '.join(allowed)}")
        if to_status in _DEPENDENCY_GATED and task.get("depends_on"):
            dep = self.get(task["depends_on"])
            if dep is None:
                raise InvalidTransition(
                    f"dependency task {task['depends_on']} not found")
            if dep["status"] != "DONE":
                raise InvalidTransition(
                    f"dependency {dep['id']} is {dep['status']}, "
                    f"must be DONE before {to_status}")
        conn = self._conn()
        with conn:
            if result is not None:
                conn.execute(
                    "UPDATE tasks SET status=?, result=?, updated_at=? "
                    "WHERE id=?", (to_status, result, time.time(), task_id))
            else:
                conn.execute(
                    "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                    (to_status, time.time(), task_id))
        conn.close()
        return self.get(task_id)

    def add_progress(self, task_id: str, agent: str, note: str) -> Dict[str, Any]:
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        if task["status"] in ("DONE", "FAILED", "REJECTED"):
            raise InvalidTransition(
                f"cannot add progress to a {task['status']} task")
        notes = task["progress_notes"]
        notes.append({"agent": agent, "note": note, "at": time.time()})
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE tasks SET progress_notes=?, updated_at=? WHERE id=?",
                (json.dumps(notes), time.time(), task_id))
        conn.close()
        return self.get(task_id)
