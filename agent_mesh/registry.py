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
STATUSES = ("online", "busy", "offline")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Registry:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init()

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
                    role TEXT NOT NULL DEFAULT 'worker',
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
                 role: str = "worker") -> Tuple[Dict[str, Any], str]:
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
        GUI — the pack is the only place the new token appears."""
        token = secrets.token_urlsafe(32)
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "UPDATE agents SET token_hash=?, last_seen=? WHERE id=?",
                (_hash(token), time.time(), agent_id))
            changed = cur.rowcount
        conn.close()
        return token if changed else None

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
        if status is not None and status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
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
