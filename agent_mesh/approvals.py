"""Approvals queue (v2 Phase 3).

When a vault use-check returns ``needs_approval``, the node enqueues a
persistent approval request here (SQLite). The owner (admin) approves or
denies it from the GUI / API. An approved request is consumed exactly
once: the agent's next use-check for that service+action is allowed,
after which the service returns to ``needs_approval`` again.

Every step writes a sanitised line to the node's audit log (agent,
service, action, event — never any secret).
"""
import json
import os
import secrets
import sqlite3
import time
from typing import Any, Dict, List, Optional

STATUSES = ("pending", "approved", "denied")


class ApprovalStore:
    def __init__(self, db_path: str, audit_path: Optional[str] = None):
        self.db_path = db_path
        self.audit_path = audit_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        conn = self._conn()
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    service TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    decided_by TEXT,
                    decided_at REAL,
                    consumed_at REAL
                )""")
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
        d["consumed"] = bool(d.get("consumed_at"))
        return d

    def _audit(self, event: str, approval: Dict[str, Any],
               outcome: str = "") -> None:
        if not self.audit_path:
            return
        entry = {"ts": time.time(), "event": event,
                 "agent": approval["agent_id"], "service": approval["service"],
                 "action": approval["action"], "outcome": outcome or event,
                 "approval_id": approval["id"]}
        with open(self.audit_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    # -- lifecycle -------------------------------------------------------
    def request(self, agent_id: str, service: str, action: str):
        """Create a pending request, de-duplicated: if one is already
        pending for this agent+service+action, return it unchanged.
        Returns (approval, created)."""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM approvals WHERE agent_id=? AND service=? "
            "AND action=? AND status='pending' ORDER BY created_at LIMIT 1",
            (agent_id, service, action)).fetchone()
        if row:
            conn.close()
            return self._row(row), False
        approval = {"id": "apr_" + secrets.token_hex(8),
                    "agent_id": agent_id, "service": service,
                    "action": action, "status": "pending",
                    "created_at": time.time(), "decided_by": None,
                    "decided_at": None, "consumed_at": None}
        with conn:
            conn.execute(
                "INSERT INTO approvals (id,agent_id,service,action,status,"
                "created_at) VALUES (?,?,?,?,'pending',?)",
                (approval["id"], agent_id, service, action,
                 approval["created_at"]))
        conn.close()
        self._audit("approval_requested", approval)
        return {**approval, "consumed": False}, True

    def get(self, approval_id: str) -> Optional[Dict[str, Any]]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM approvals WHERE id=?",
                           (approval_id,)).fetchone()
        conn.close()
        return self._row(row) if row else None

    def list(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        q = "SELECT * FROM approvals"
        params: List[Any] = []
        if status:
            q += " WHERE status=?"
            params.append(status)
        q += " ORDER BY created_at"
        conn = self._conn()
        rows = conn.execute(q, params).fetchall()
        conn.close()
        return [self._row(r) for r in rows]

    def decide(self, approval_id: str, approve: bool,
               decided_by: str = "owner") -> Dict[str, Any]:
        approval = self.get(approval_id)
        if approval is None:
            raise KeyError(f"approval not found: {approval_id}")
        if approval["status"] != "pending":
            raise ValueError(
                f"approval {approval_id} is already {approval['status']}")
        new_status = "approved" if approve else "denied"
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE approvals SET status=?, decided_by=?, decided_at=? "
                "WHERE id=?",
                (new_status, decided_by, time.time(), approval_id))
        conn.close()
        updated = self.get(approval_id)
        self._audit("approval_approved" if approve else "approval_denied",
                    updated)
        return updated

    def consume_approved(self, agent_id: str, service: str,
                         action: str) -> bool:
        """Mark the oldest unconsumed approved request for this
        agent+service+action as consumed. Returns True if one existed —
        the caller may allow exactly one use."""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM approvals WHERE agent_id=? AND service=? "
            "AND action=? AND status='approved' AND consumed_at IS NULL "
            "ORDER BY created_at LIMIT 1",
            (agent_id, service, action)).fetchone()
        if not row:
            conn.close()
            return False
        approval = self._row(row)
        with conn:
            conn.execute("UPDATE approvals SET consumed_at=? WHERE id=?",
                         (time.time(), approval["id"]))
        conn.close()
        self._audit("approval_consumed", approval)
        return True
