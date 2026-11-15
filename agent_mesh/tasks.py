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
            # Phase 4 federation columns (added by migration so Phase 1
            # databases keep working): origin_node/origin_task_id link a
            # home-side copy back to the task on its origin node;
            # home_node marks an origin-side task whose assignee lives
            # on a peer node.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            for col in ("origin_node", "origin_task_id", "home_node"):
                if col not in cols:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
        conn.close()
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass

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
               budget_money=None,
               origin_node: Optional[str] = None,
               origin_task_id: Optional[str] = None,
               home_node: Optional[str] = None):
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
                    "updated_at,origin_node,origin_task_id,home_node) "
                    "VALUES (?,?,?,?,?,?,'DRAFT',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (task_id, title, instructions, requester, assignee_agent,
                     assignee_capability, priority, parent_id, depends_on,
                     idempotency_key, budget_time_sec, budget_tokens,
                     budget_money, "[]", now, now, origin_node,
                     origin_task_id, home_node))
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

    # -- Phase 4 federation --------------------------------------------------
    # A task snapshot travels between nodes. Status merges monotonically
    # (rank never moves backwards, except that a terminal state wins over
    # a non-terminal one), progress notes union, and a set result wins.
    # This makes re-delivery and out-of-order delivery harmless.

    _RANK = {"DRAFT": 0, "SENT": 1, "ACCEPTED": 2, "IN_PROGRESS": 3,
             "IN_REVIEW": 4, "DONE": 5, "FAILED": 5, "REJECTED": 5}

    def get_by_origin(self, origin_node: str,
                      origin_task_id: str) -> Optional[Dict[str, Any]]:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM tasks WHERE origin_node=? AND origin_task_id=?",
            (origin_node, origin_task_id)).fetchone()
        conn.close()
        return self._row(row) if row else None

    def _merge_state(self, task_id: str, snap: Dict[str, Any]) -> Dict[str, Any]:
        task = self.get(task_id)
        cur_rank = self._RANK.get(task["status"], 0)
        new_rank = self._RANK.get(snap.get("status", task["status"]), 0)
        status = task["status"]
        if new_rank > cur_rank or (new_rank == 5 and cur_rank < 5):
            status = snap["status"]
        notes = list(task["progress_notes"])
        seen = {(n.get("agent"), n.get("at"), n.get("note")) for n in notes}
        for n in snap.get("progress_notes") or []:
            key = (n.get("agent"), n.get("at"), n.get("note"))
            if key not in seen:
                notes.append(n)
                seen.add(key)
        notes.sort(key=lambda n: n.get("at") or 0)
        result = snap.get("result") if snap.get("result") is not None \
            else task["result"]
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE tasks SET status=?, result=?, progress_notes=?, "
                "updated_at=? WHERE id=?",
                (status, result, json.dumps(notes),
                 max(task["updated_at"], snap.get("updated_at") or 0),
                 task_id))
        conn.close()
        return self.get(task_id)

    def upsert_federated(self, snap: Dict[str, Any],
                         origin_node: str) -> Dict[str, Any]:
        """Home side: create or update the local copy of a task whose
        origin is `origin_node` and whose origin id is snap['id'].
        Duplicate delivery returns the existing copy unchanged."""
        existing = self.get_by_origin(origin_node, snap["id"])
        if existing:
            return self._merge_state(existing["id"], snap)
        if snap.get("status") not in STATUSES:
            raise InvalidTransition(f"unknown status {snap.get('status')!r}")
        task_id = "tsk_" + uuid.uuid4().hex[:16]
        now = time.time()
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO tasks (id,title,instructions,requester,"
                "assignee_agent,assignee_capability,status,priority,"
                "parent_id,depends_on,idempotency_key,budget_time_sec,"
                "budget_tokens,budget_money,progress_notes,result,"
                "created_at,updated_at,origin_node,origin_task_id,home_node) "
                "VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?,?,?,?,?,?,NULL)",
                (task_id, snap.get("title", ""), snap.get("instructions", ""),
                 snap.get("requester", ""), snap.get("assignee_agent"),
                 snap.get("assignee_capability"), snap["status"],
                 snap.get("priority", "normal"),
                 snap.get("budget_time_sec"), snap.get("budget_tokens"),
                 snap.get("budget_money"),
                 json.dumps(snap.get("progress_notes") or []),
                 snap.get("result"), snap.get("created_at") or now,
                 snap.get("updated_at") or now, origin_node, snap["id"]))
        conn.close()
        return self.get(task_id)

    def apply_remote_state(self, snap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Origin side: merge a home node's snapshot into the local task
        it refers to via snap['origin_task_id']. None if unknown."""
        local_id = snap.get("origin_task_id")
        if not local_id or self.get(local_id) is None:
            return None
        return self._merge_state(local_id, snap)
