"""Peer federation (v2 Phase 4) — node identity, pairing, signed
transport helpers, the persistent outbox, and the remote-agent directory.

Security model (honest): node-to-node traffic is plain HTTP authenticated
with HMAC-SHA256 (authentication + integrity, replay window 5 minutes).
HMAC does NOT encrypt the body — pairing and peer traffic must run over a
trusted channel (same machine, LAN you trust, or Tailscale). The per-peer
secret is generated during pairing, stored only in each node's own data
dir (this SQLite file, chmod 0600), and is never logged and never
returned by any list/info endpoint.
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

TS_WINDOW_SEC = 300  # reject peer requests whose timestamp is older
DEFAULT_CODE_TTL_SEC = 900  # pairing codes live at most 15 minutes
DEFAULT_RETRY_SCHEDULE = (1.0, 5.0, 15.0, 60.0)
DEFAULT_MAX_ATTEMPTS = 100


def parse_schedule(raw: Optional[str]) -> Tuple[float, ...]:
    if not raw:
        return DEFAULT_RETRY_SCHEDULE
    try:
        vals = tuple(float(x) for x in raw.split(",") if x.strip())
        return vals or DEFAULT_RETRY_SCHEDULE
    except ValueError:
        return DEFAULT_RETRY_SCHEDULE


def sign(secret: str, timestamp: str, method: str, path: str,
         body: bytes) -> str:
    msg = timestamp.encode() + b"." + method.upper().encode() + b"." \
        + path.encode() + b"." + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify(secret: str, signature: str, timestamp: str, method: str,
           path: str, body: bytes) -> bool:
    if not signature:
        return False
    expected = sign(secret, timestamp, method, path, body)
    return hmac.compare_digest(expected, signature)


def load_identity(data_dir: str, name: Optional[str] = None) -> Dict[str, str]:
    """Persistent node identity: {node_id, name} in <data_dir>/node.json."""
    path = os.path.join(data_dir, "node.json")
    if os.path.exists(path):
        with open(path) as f:
            ident = json.load(f)
        if name and ident.get("name") != name:
            ident["name"] = name
            _write_identity(path, ident)
        return ident
    ident = {"node_id": "nod_" + uuid.uuid4().hex[:16],
             "name": name or "unnamed node"}
    _write_identity(path, ident)
    return ident


def _write_identity(path: str, ident: Dict[str, str]) -> None:
    with open(path, "w") as f:
        json.dump(ident, f)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class PeerStore:
    """Peers, one-time pairing codes, the outbound delivery outbox, and
    the cached directory of remote agents — all in one SQLite file."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init()
        try:
            os.chmod(db_path, 0o600)
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
                CREATE TABLE IF NOT EXISTS peers (
                    peer_node_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    secret TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    paired_at REAL NOT NULL,
                    last_seen REAL
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pairing_codes (
                    code_hash TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_node_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    delivered_at REAL,
                    last_error TEXT,
                    UNIQUE(peer_node_id, dedupe_key)
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS remote_agents (
                    agent_id TEXT PRIMARY KEY,
                    home_node TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    capabilities TEXT NOT NULL DEFAULT '[]',
                    role TEXT NOT NULL DEFAULT 'worker',
                    status TEXT NOT NULL DEFAULT 'online',
                    machine TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                )""")
        conn.close()

    # -- pairing codes ----------------------------------------------------
    def create_code(self, ttl_sec: float = DEFAULT_CODE_TTL_SEC):
        code = secrets.token_hex(4).upper()  # 8 hex chars, e.g. 9F3AC21B
        expires = time.time() + ttl_sec
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO pairing_codes (code_hash, expires_at, used) "
                "VALUES (?,?,0)",
                (hashlib.sha256(code.encode()).hexdigest(), expires))
        conn.close()
        return code, expires

    def consume_code(self, code: str) -> bool:
        """True exactly once for a live code; False for wrong/expired/used."""
        if not code:
            return False
        digest = hashlib.sha256(code.strip().upper().encode()).hexdigest()
        conn = self._conn()
        with conn:
            row = conn.execute(
                "SELECT * FROM pairing_codes WHERE code_hash=?",
                (digest,)).fetchone()
            if row is None or row["used"] or row["expires_at"] < time.time():
                return False
            conn.execute("UPDATE pairing_codes SET used=1 WHERE code_hash=?",
                         (digest,))
        conn.close()
        return True

    # -- peers --------------------------------------------------------------
    def add_peer(self, peer_node_id: str, name: str, url: str,
                 secret: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO peers (peer_node_id,name,url,secret,status,"
                "paired_at,last_seen) VALUES (?,?,?,?,'paired',?,?) "
                "ON CONFLICT(peer_node_id) DO UPDATE SET name=excluded.name,"
                "url=excluded.url, secret=excluded.secret,"
                "status='paired', last_seen=excluded.last_seen",
                (peer_node_id, name, url.rstrip("/"), secret, time.time(),
                 time.time()))
        conn.close()

    def get_peer(self, peer_node_id: str) -> Optional[Dict[str, Any]]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM peers WHERE peer_node_id=?",
                           (peer_node_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def peer_secret(self, peer_node_id: str) -> Optional[str]:
        peer = self.get_peer(peer_node_id)
        return peer["secret"] if peer else None

    @staticmethod
    def _safe(row: sqlite3.Row) -> Dict[str, Any]:
        return {"node_id": row["peer_node_id"], "name": row["name"],
                "url": row["url"], "status": row["status"],
                "paired_at": row["paired_at"], "last_seen": row["last_seen"]}

    def list_peers_safe(self) -> List[Dict[str, Any]]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM peers ORDER BY paired_at").fetchall()
        conn.close()
        return [self._safe(r) for r in rows]

    def touch_peer(self, peer_node_id: str, status: str = "online") -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE peers SET last_seen=?, status=? WHERE peer_node_id=?",
                (time.time(), status, peer_node_id))
        conn.close()

    # -- outbox ---------------------------------------------------------------
    def enqueue(self, peer_node_id: str, kind: str, payload: Dict[str, Any],
                dedupe_key: str) -> Optional[int]:
        conn = self._conn()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO outbox (peer_node_id,kind,payload,dedupe_key,"
                    "state,attempts,next_retry,created_at) VALUES (?,?,?,?,"
                    "'pending',0,?,?)",
                    (peer_node_id, kind, json.dumps(payload), dedupe_key,
                     time.time(), time.time()))
                row_id = cur.lastrowid
        except sqlite3.IntegrityError:
            row_id = None  # same delivery already queued/done
        conn.close()
        return row_id

    def due_items(self, limit: int = 50) -> List[Dict[str, Any]]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM outbox WHERE state='pending' AND next_retry<=? "
            "ORDER BY id ASC LIMIT ?", (time.time(), limit)).fetchall()
        conn.close()
        items = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            items.append(d)
        return items

    def mark_delivered(self, item_id: int) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE outbox SET state='delivered', delivered_at=? "
                "WHERE id=?", (time.time(), item_id))
        conn.close()

    def mark_attempt(self, item_id: int, error: str,
                     schedule: Tuple[float, ...] = DEFAULT_RETRY_SCHEDULE,
                     max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
        conn = self._conn()
        row = conn.execute("SELECT attempts FROM outbox WHERE id=?",
                           (item_id,)).fetchone()
        if row is None:
            conn.close()
            return
        attempts = row["attempts"] + 1
        delay = schedule[min(attempts - 1, len(schedule) - 1)]
        state = "failed" if attempts >= max_attempts else "pending"
        with conn:
            conn.execute(
                "UPDATE outbox SET attempts=?, next_retry=?, last_error=?, "
                "state=? WHERE id=?",
                (attempts, time.time() + delay, str(error)[:200], state,
                 item_id))
        conn.close()

    def outbox_counts(self) -> Dict[str, int]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT state, COUNT(*) AS n FROM outbox GROUP BY state").fetchall()
        conn.close()
        counts = {"pending": 0, "delivered": 0, "failed": 0}
        for r in rows:
            counts[r["state"]] = r["n"]
        return counts

    def outbox_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT id,peer_node_id,kind,state,attempts,next_retry,"
            "created_at,delivered_at,last_error FROM outbox "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # -- remote agent directory ----------------------------------------------
    def set_remote_agents(self, home_node: str,
                          agents: List[Dict[str, Any]]) -> None:
        now = time.time()
        conn = self._conn()
        with conn:
            conn.execute("DELETE FROM remote_agents WHERE home_node=?",
                         (home_node,))
            for a in agents:
                conn.execute(
                    "INSERT INTO remote_agents (agent_id,home_node,name,"
                    "capabilities,role,status,machine,updated_at) VALUES "
                    "(?,?,?,?,?,?,?,?)",
                    (a["id"], home_node, a.get("name", ""),
                     json.dumps(a.get("capabilities", [])),
                     a.get("role", "worker"), a.get("status", "online"),
                     a.get("machine", ""), now))
        conn.close()

    def remote_agents(self) -> List[Dict[str, Any]]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM remote_agents ORDER BY name").fetchall()
        conn.close()
        return [{"id": r["agent_id"], "name": r["name"],
                 "capabilities": json.loads(r["capabilities"]),
                 "role": r["role"], "status": r["status"],
                 "machine": r["machine"], "node": r["home_node"],
                 "home_node": r["home_node"], "remote": True,
                 "last_seen": r["updated_at"]} for r in rows]

    def find_remote_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        for a in self.remote_agents():
            if a["id"] == agent_id:
                return a
        return None
