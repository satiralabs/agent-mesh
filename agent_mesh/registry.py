"""Agent identity registry (v2 Phase 1).

SQLite-backed. Auth tokens are generated at registration, returned to the
caller once, and stored ONLY as a SHA-256 hash.
"""
import hashlib
import json
import os
import secrets
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

ROLES = ("owner", "orchestrator", "worker")
STATUSES = ("online", "busy", "offline", "revoked")

# Registration without an explicit role defaults to "orchestrator"
# (Phase 5): in Phases 1-4 ordering was not enforced, so existing
# agents were registered role-less and must keep working. The ordering
# matrix below bites as soon as an agent is EXPLICITLY a worker.
DEFAULT_ROLE = "orchestrator"


def order_denial(requester_id: str, requester_role: str,
                 assignee_agent: str = None,
                 assignee_role: str = None,
                 assignee_capability: str = None):
    """Ordering-permission matrix (Phase 5, hard-enforced).

    Returns None if the order is allowed, else a human-readable reason
    (safe to return to the caller and to audit — it contains ids and
    roles only, never content).

    - owner: may order anyone (named agent, or by capability).
    - orchestrator: may order workers and orchestrators (named or by
      capability); may NOT order an owner-role agent.
    - worker: may NOT order other agents at all. It may create a task
      assigned to itself, and it may send messages/questions to anyone
      (messages are not orders and are not checked here).
    An assignee whose role is unknown (not registered locally and not
    in the remote directory) is treated as a non-owner target.
    """
    if requester_role == "owner":
        return None
    if requester_role == "orchestrator":
        if assignee_agent and assignee_role == "owner":
            return (f"orchestrator {requester_id} may not order "
                    f"owner-role agent {assignee_agent}")
        return None
    if requester_role == "worker":
        if assignee_agent and assignee_agent == requester_id \
                and not assignee_capability:
            return None
        return (f"worker {requester_id} may not order other agents "
                f"(it may only create tasks assigned to itself)")
    return f"unknown requester role {requester_role!r}"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Registry:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init()
        try:
            os.chmod(db_path, 0o600)  # token hashes live here (Phase 5)
        except OSError:
            pass

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    def _init(self) -> None:
        conn = self._conn()
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    machine TEXT DEFAULT '',
                    node TEXT DEFAULT '',
                    capabilities TEXT DEFAULT '[]',
                    role TEXT NOT NULL DEFAULT 'orchestrator',
                    status TEXT NOT NULL DEFAULT 'online',
                    token_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen REAL NOT NULL
                )""")
        conn.close()

    @staticmethod
    def _public(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"], "name": row["name"], "machine": row["machine"],
            "node": row["node"], "capabilities": json.loads(row["capabilities"]),
            "role": row["role"], "status": row["status"],
            "created_at": row["created_at"], "last_seen": row["last_seen"],
        }

    def register(self, name: str, machine: str = "", node: str = "",
                 capabilities: Optional[List[str]] = None,
                 role: str = DEFAULT_ROLE) -> Tuple[Dict[str, Any], str]:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}, got {role!r}")
        agent_id = "agt_" + secrets.token_hex(8)
        token = secrets.token_urlsafe(32)
        now = time.time()
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO agents (id,name,machine,node,capabilities,role,"
                "status,token_hash,created_at,last_seen) VALUES (?,?,?,?,?,?,"
                "'online',?,?,?)",
                (agent_id, name, machine, node,
                 json.dumps(capabilities or []), role, _hash(token), now, now))
            row = conn.execute("SELECT * FROM agents WHERE id=?",
                               (agent_id,)).fetchone()
        conn.close()
        return self._public(row), token

    def rotate_token(self, agent_id: str) -> Optional[str]:
        """Issue a fresh token for an existing agent (old token stops
        working). Used when generating a new connection pack from the
        GUI — the pack is the only place the new token appears.
        Rotating also clears a revoked status (Phase 6 reconnect path):
        a disconnected agent comes back 'online' with the new token,
        and the token it was revoked with stays dead."""
        token = secrets.token_urlsafe(32)
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "UPDATE agents SET token_hash=?, status='online', "
                "last_seen=? WHERE id=?",
                (_hash(token), time.time(), agent_id))
            changed = cur.rowcount
        conn.close()
        return token if changed else None

    def revoke(self, agent_id: str) -> bool:
        """Disconnect an agent (Phase 6): invalidate its token so every
        subsequent call with it fails authentication, and mark it
        'revoked'. The agent row, its tasks and its messages are kept —
        only its access is cut. Returns False if no such agent."""
        dead = "revoked:" + _hash(secrets.token_urlsafe(32))
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "UPDATE agents SET token_hash=?, status='revoked' "
                "WHERE id=?",
                (dead, agent_id))
            changed = cur.rowcount
        conn.close()
        return changed > 0

    def authenticate(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        conn = self._conn()
        row = conn.execute("SELECT * FROM agents WHERE token_hash=?",
                           (_hash(token),)).fetchone()
        conn.close()
        return self._public(row) if row else None

    def get(self, agent_id: str) -> Optional[Dict[str, Any]]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM agents WHERE id=?",
                           (agent_id,)).fetchone()
        conn.close()
        return self._public(row) if row else None

    def list_agents(self) -> List[Dict[str, Any]]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM agents ORDER BY created_at").fetchall()
        conn.close()
        return [self._public(r) for r in rows]

    def heartbeat(self, agent_id: str, status: Optional[str] = None) -> bool:
        if status is not None and (status not in STATUSES
                                   or status == "revoked"):
            raise ValueError(
                "heartbeat status must be online, busy or offline "
                "('revoked' is set only by an admin disconnect)")
        conn = self._conn()
        with conn:
            if status:
                cur = conn.execute(
                    "UPDATE agents SET last_seen=?, status=? WHERE id=?",
                    (time.time(), status, agent_id))
            else:
                cur = conn.execute(
                    "UPDATE agents SET last_seen=? WHERE id=?",
                    (time.time(), agent_id))
            changed = cur.rowcount
        conn.close()
        return changed > 0
