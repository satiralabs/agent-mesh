import sqlite3
import json
import time
import os
from typing import Optional, List, Dict, Any, Generator

DEFAULT_DB_PATH = os.path.expanduser("~/.agent-mesh/bus.db")

def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn

def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                msg_type TEXT NOT NULL,
                correlation_id TEXT,
                payload TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_processed INTEGER DEFAULT 0,
                processed_at DATETIME
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox ON messages(recipient, is_processed);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_corr ON messages(correlation_id);")
    conn.close()

def send_message(sender: str, recipient: str, msg_type: str, payload: Any, correlation_id: Optional[str] = None, db_path: str = DEFAULT_DB_PATH) -> int:
    init_db(db_path)
    conn = get_connection(db_path)
    payload_str = json.dumps(payload) if not isinstance(payload, str) else payload
    with conn:
        cur = conn.execute(
            """
            INSERT INTO messages (sender, recipient, msg_type, correlation_id, payload)
            VALUES (?, ?, ?, ?, ?);
            """,
            (sender, recipient, msg_type, correlation_id, payload_str)
        )
        msg_id = cur.lastrowid
    conn.close()
    return msg_id

def list_messages(recipient: Optional[str] = None, sender: Optional[str] = None, unread_only: bool = True, limit: int = 50, db_path: str = DEFAULT_DB_PATH) -> List[Dict[str, Any]]:
    init_db(db_path)
    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    query = "SELECT id, sender, recipient, msg_type, correlation_id, payload, created_at, is_processed, processed_at FROM messages WHERE 1=1"
    params = []
    
    if recipient:
        query += " AND recipient = ?"
        params.append(recipient)
    if sender:
        query += " AND sender = ?"
        params.append(sender)
    if unread_only:
        query += " AND is_processed = 0"
        
    query += " ORDER BY id ASC LIMIT ?"
    params.append(limit)
    
    with conn:
        rows = conn.execute(query, params).fetchall()
        
    results = []
    for r in rows:
        item = dict(r)
        try:
            item["payload"] = json.loads(item["payload"])
        except Exception:
            pass
        results.append(item)
    conn.close()
    return results

def ack_message(message_id: int, db_path: str = DEFAULT_DB_PATH) -> bool:
    init_db(db_path)
    conn = get_connection(db_path)
    with conn:
        cur = conn.execute(
            """
            UPDATE messages
            SET is_processed = 1, processed_at = CURRENT_TIMESTAMP
            WHERE id = ?;
            """,
            (message_id,)
        )
        affected = cur.rowcount
    conn.close()
    return affected > 0

def listen_messages(recipient: str, poll_interval: float = 1.0, db_path: str = DEFAULT_DB_PATH) -> Generator[Dict[str, Any], None, None]:
    init_db(db_path)
    while True:
        unread = list_messages(recipient=recipient, unread_only=True, limit=20, db_path=db_path)
        for msg in unread:
            yield msg
        time.sleep(poll_interval)
