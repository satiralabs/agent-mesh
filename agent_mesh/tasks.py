"""Task store (v2 Phase 1) — strict lifecycle, dependencies, idempotency.

v2.2 adds collaboration: a task has PARTICIPANTS (the primary assignee
is the lead participant; others are collaborators who can see the task,
comment on its shared thread and add progress notes, but cannot drive
its lifecycle) and a comment THREAD that travels with the task across
nodes exactly like its progress notes do.
"""
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
# Terminal states: no transitions out, and (v2.2) no new participants,
# comments or progress notes in.
TERMINAL_STATUSES = ("DONE", "FAILED", "REJECTED")
PARTICIPANT_ROLES = ("lead", "collaborator")


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
            # v2.2 collaboration tables (new tables, so Phase 1-4
            # databases gain them by creation; existing tasks are
            # backfilled with a lead row for their assignee below).
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_participants (
                    task_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'collaborator',
                    added_by TEXT DEFAULT '',
                    added_at REAL NOT NULL,
                    PRIMARY KEY (task_id, agent_id)
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_comments (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    author TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at REAL NOT NULL
                )""")
            conn.execute(
                "INSERT OR IGNORE INTO task_participants "
                "(task_id, agent_id, role, added_by, added_at) "
                "SELECT id, assignee_agent, 'lead', requester, created_at "
                "FROM tasks WHERE assignee_agent IS NOT NULL")
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

    # -- v2.2 collaboration: participants + comment thread ------------------
    @staticmethod
    def _participant_rows(conn, task_id: str) -> List[Dict[str, Any]]:
        rows = conn.execute(
            "SELECT agent_id, role, added_by, added_at "
            "FROM task_participants WHERE task_id=? "
            "ORDER BY added_at, agent_id", (task_id,)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _comment_rows(conn, task_id: str) -> List[Dict[str, Any]]:
        rows = conn.execute(
            "SELECT id, author, body, created_at FROM task_comments "
            "WHERE task_id=? ORDER BY created_at, id",
            (task_id,)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _insert_participant(conn, task_id: str, agent_id: str, role: str,
                            added_by: str, added_at: float) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO task_participants "
            "(task_id, agent_id, role, added_by, added_at) "
            "VALUES (?,?,?,?,?)",
            (task_id, agent_id, role, added_by, added_at))

    def _attach(self, task: Dict[str, Any],
                comments: bool = True) -> Dict[str, Any]:
        conn = self._conn()
        task["participants"] = self._participant_rows(conn, task["id"])
        if comments:
            task["comments"] = self._comment_rows(conn, task["id"])
        conn.close()
        return task

    def participant_ids(self, task_id: str) -> List[str]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT agent_id FROM task_participants WHERE task_id=?",
            (task_id,)).fetchall()
        conn.close()
        return [r["agent_id"] for r in rows]

    def is_participant(self, task_id: str, agent_id: str) -> bool:
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM task_participants WHERE task_id=? AND agent_id=?",
            (task_id, agent_id)).fetchone()
        conn.close()
        return row is not None

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
               home_node: Optional[str] = None,
               participants: Optional[List[str]] = None):
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
                # v2.2: the assignee is the lead participant; any
                # extra participants join as collaborators.
                if assignee_agent:
                    self._insert_participant(
                        conn, task_id, assignee_agent, "lead",
                        requester, now)
                for pid in participants or []:
                    if pid and pid != assignee_agent:
                        self._insert_participant(
                            conn, task_id, pid, "collaborator",
                            requester, now)
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
        if row is None:
            return None
        return self._attach(self._row(row))

    def get_by_idempotency_key(self, key: str) -> Optional[Dict[str, Any]]:
        if not key:
            return None
        conn = self._conn()
        row = conn.execute("SELECT * FROM tasks WHERE idempotency_key=?",
                           (key,)).fetchone()
        conn.close()
        return self._attach(self._row(row)) if row else None

    def list_tasks(self, assignee_agent: Optional[str] = None,
                   requester: Optional[str] = None,
                   status: Optional[str] = None,
                   participant_agent: Optional[str] = None,
                   visible_to: Optional[str] = None) -> List[Dict[str, Any]]:
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
        if participant_agent:
            q += " AND id IN (SELECT task_id FROM task_participants " \
                 "WHERE agent_id=?)"
            params.append(participant_agent)
        if visible_to:
            # v2.2 visibility scoping: an agent's list contains only
            # tasks it is involved in — as requester, as lead, or as a
            # participant. (The owner/admin view passes visible_to=None.)
            q += " AND (requester=? OR assignee_agent=? OR id IN " \
                 "(SELECT task_id FROM task_participants WHERE agent_id=?))"
            params += [visible_to, visible_to, visible_to]
        q += " ORDER BY created_at"
        conn = self._conn()
        rows = conn.execute(q, params).fetchall()
        conn.close()
        # Lists carry participants (the board shows them) but not the
        # comment thread — that lives on the single-task view.
        return [self._attach(self._row(r), comments=False) for r in rows]

    # -- v2.2 participants + comments ---------------------------------------
    def add_participant(self, task_id: str, agent_id: str,
                        added_by: str) -> Dict[str, Any]:
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        if task["status"] in TERMINAL_STATUSES:
            raise InvalidTransition(
                f"cannot add a participant to a {task['status']} task")
        role = "lead" if agent_id == task.get("assignee_agent") \
            else "collaborator"
        conn = self._conn()
        with conn:
            self._insert_participant(conn, task_id, agent_id, role,
                                     added_by, time.time())
            conn.execute("UPDATE tasks SET updated_at=? WHERE id=?",
                         (time.time(), task_id))
        conn.close()
        return self.get(task_id)

    def remove_participant(self, task_id: str,
                           agent_id: str) -> Dict[str, Any]:
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        if task["status"] in TERMINAL_STATUSES:
            raise InvalidTransition(
                f"cannot remove a participant from a {task['status']} task")
        if agent_id == task.get("assignee_agent"):
            raise ValueError(
                "the lead (primary assignee) cannot be removed from "
                "its own task")
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "DELETE FROM task_participants WHERE task_id=? "
                "AND agent_id=?", (task_id, agent_id))
            if cur.rowcount == 0:
                raise KeyError(
                    f"agent {agent_id} is not a participant of {task_id}")
            conn.execute("UPDATE tasks SET updated_at=? WHERE id=?",
                         (time.time(), task_id))
        conn.close()
        return self.get(task_id)

    def add_comment(self, task_id: str, author: str,
                    body: str) -> Dict[str, Any]:
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        if task["status"] in TERMINAL_STATUSES:
            raise InvalidTransition(
                f"cannot comment on a {task['status']} task")
        comment = {"id": "cmt_" + uuid.uuid4().hex[:16],
                   "author": author, "body": body,
                   "created_at": time.time()}
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO task_comments (id, task_id, author, body, "
                "created_at) VALUES (?,?,?,?,?)",
                (comment["id"], task_id, author, body,
                 comment["created_at"]))
            # Bumping updated_at also moves the federation sync dedupe
            # key, so the new comment is queued for the peer nodes.
            conn.execute("UPDATE tasks SET updated_at=? WHERE id=?",
                         (time.time(), task_id))
        conn.close()
        return self.get(task_id)

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
            # v2.2: participants merge as a union by agent id, comments
            # as a union by comment id (comments are append-only, and
            # their ids are globally unique, so re-delivery and
            # out-of-order delivery are harmless). Removals do not
            # propagate — the same monotonic rule as progress notes.
            have = {p["agent_id"] for p in task.get("participants") or []}
            for p in snap.get("participants") or []:
                if p.get("agent_id") and p["agent_id"] not in have:
                    self._insert_participant(
                        conn, task_id, p["agent_id"],
                        p.get("role") or "collaborator",
                        p.get("added_by") or "",
                        p.get("added_at") or time.time())
                    have.add(p["agent_id"])
            if snap.get("assignee_agent") \
                    and snap["assignee_agent"] not in have:
                self._insert_participant(
                    conn, task_id, snap["assignee_agent"], "lead",
                    snap.get("requester") or "", time.time())
            seen_comments = {c["id"] for c in task.get("comments") or []}
            for c in snap.get("comments") or []:
                if c.get("id") and c["id"] not in seen_comments:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_comments "
                        "(id, task_id, author, body, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (c["id"], task_id, c.get("author", ""),
                         c.get("body", ""), c.get("created_at") or 0))
                    seen_comments.add(c["id"])
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
            # v2.2: the snapshot's participants + comment thread are
            # stored against the NEW local copy id (comment ids stay
            # the sender's — they are the cross-node dedupe key).
            if snap.get("assignee_agent"):
                self._insert_participant(
                    conn, task_id, snap["assignee_agent"], "lead",
                    snap.get("requester") or "",
                    snap.get("created_at") or now)
            for p in snap.get("participants") or []:
                if p.get("agent_id"):
                    self._insert_participant(
                        conn, task_id, p["agent_id"],
                        p.get("role") or "collaborator",
                        p.get("added_by") or "",
                        p.get("added_at") or now)
            for c in snap.get("comments") or []:
                if c.get("id"):
                    conn.execute(
                        "INSERT OR IGNORE INTO task_comments "
                        "(id, task_id, author, body, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (c["id"], task_id, c.get("author", ""),
                         c.get("body", ""), c.get("created_at") or now))
        conn.close()
        return self.get(task_id)

    def apply_remote_state(self, snap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Origin side: merge a home node's snapshot into the local task
        it refers to via snap['origin_task_id']. None if unknown."""
        local_id = snap.get("origin_task_id")
        if not local_id or self.get(local_id) is None:
            return None
        return self._merge_state(local_id, snap)
