"""Handover entry store (v2.1).

SQLite-backed log of handover entries filed by agents (via the MCP
tool, the CLI, or the REST endpoint). Every new entry regenerates the
node's exported handover file, HANDOVER-<node-name>.md in the node
data dir, so the file on disk is always the full current log grouped
by agent. Also tracks which agents have already received the one-time
handover-rules starter message, per rules version.
"""
import os
import re
import secrets
import sqlite3
import time
from typing import Any, Dict, List, Optional

ENTRY_FIELDS = ("title", "asked", "done", "verified", "decisions",
                "files", "open")


def export_filename(node_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", node_name or "node").strip("-")
    return f"HANDOVER-{safe or 'node'}.md"


class HandoverStore:
    def __init__(self, db_path: str, data_dir: str, node_name: str):
        self.db_path = db_path
        self.data_dir = data_dir
        self.node_name = node_name or "unnamed node"
        os.makedirs(os.path.dirname(os.path.abspath(db_path)),
                    exist_ok=True)
        conn = self._conn()
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS handover_entries (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    asked TEXT DEFAULT '',
                    done TEXT DEFAULT '',
                    verified TEXT DEFAULT '',
                    decisions TEXT DEFAULT '',
                    files TEXT DEFAULT '',
                    open_items TEXT DEFAULT '',
                    created_at REAL NOT NULL
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS handover_starters (
                    agent_id TEXT NOT NULL,
                    rules_version TEXT NOT NULL,
                    sent_at REAL NOT NULL,
                    PRIMARY KEY (agent_id, rules_version)
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

    # -- starter delivery tracking ---------------------------------------
    def starter_needed(self, agent_id: str, rules_version: str) -> bool:
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM handover_starters WHERE agent_id=? "
            "AND rules_version=?", (agent_id, rules_version)).fetchone()
        conn.close()
        return row is None

    def mark_starter(self, agent_id: str, rules_version: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO handover_starters "
                "(agent_id, rules_version, sent_at) VALUES (?,?,?)",
                (agent_id, rules_version, time.time()))
        conn.close()

    # -- entries -----------------------------------------------------------
    def add_entry(self, agent_id: str, agent_name: str,
                  fields: Dict[str, Any]) -> Dict[str, Any]:
        entry = {"id": "hnd_" + secrets.token_hex(8),
                 "agent_id": agent_id, "agent_name": agent_name,
                 "title": str(fields.get("title", "")).strip(),
                 "asked": str(fields.get("asked", "") or ""),
                 "done": str(fields.get("done", "") or ""),
                 "verified": str(fields.get("verified", "") or ""),
                 "decisions": str(fields.get("decisions", "") or ""),
                 "files": str(fields.get("files", "") or ""),
                 "open_items": str(fields.get("open", "") or ""),
                 "created_at": time.time()}
        if not entry["title"]:
            raise ValueError("handover entry requires a title")
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO handover_entries (id,agent_id,agent_name,"
                "title,asked,done,verified,decisions,files,open_items,"
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (entry["id"], entry["agent_id"], entry["agent_name"],
                 entry["title"], entry["asked"], entry["done"],
                 entry["verified"], entry["decisions"], entry["files"],
                 entry["open_items"], entry["created_at"]))
        conn.close()
        self.export()
        return entry

    def list_entries(self, agent_id: Optional[str] = None,
                     limit: int = 200) -> List[Dict[str, Any]]:
        q = "SELECT * FROM handover_entries"
        params: List[Any] = []
        if agent_id:
            q += " WHERE agent_id=?"
            params.append(agent_id)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        conn = self._conn()
        rows = conn.execute(q, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # -- export -------------------------------------------------------------
    def export_path(self) -> str:
        return os.path.join(self.data_dir,
                            export_filename(self.node_name))

    def export(self) -> str:
        """Regenerate HANDOVER-<node>.md from all stored entries."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM handover_entries ORDER BY created_at ASC"
        ).fetchall()
        conn.close()
        lines = [f"# Handover — {self.node_name}",
                 "",
                 f"Exported by the mesh node from {len(rows)} filed "
                 f"entries. One entry per session, per agent; see the "
                 f"handover rules (GET /handover/rules).",
                 ""]
        by_agent: Dict[str, List[sqlite3.Row]] = {}
        for row in rows:
            by_agent.setdefault(row["agent_name"] or row["agent_id"],
                                []).append(row)
        for agent_name in sorted(by_agent):
            lines.append(f"## {agent_name}")
            lines.append("")
            for row in by_agent[agent_name]:
                stamp = time.strftime("%Y-%m-%d %H:%M UTC",
                                      time.gmtime(row["created_at"]))
                lines.append(f"### {stamp} — {agent_name} — {row['title']}")
                lines.append("")
                for label, key in (("Asked", "asked"), ("Done", "done"),
                                   ("Verified", "verified"),
                                   ("Decisions", "decisions"),
                                   ("Files", "files"),
                                   ("Open items", "open_items")):
                    value = (row[key] or "").strip()
                    lines.append(f"- **{label}:** {value or '—'}")
                lines.append("")
        path = self.export_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path
