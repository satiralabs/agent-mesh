"""Mesh node server (v2) — stdlib http.server REST API + GUI.

Run:  AGENT_MESH_ADMIN=<token> AGENT_MESH_MASTER=<passphrase> \
        python3 -m agent_mesh.node [--port 8790] [--data-dir ~/.agent-mesh-node]

Open http://127.0.0.1:8790/ for the Phase 3 GUI (enter the admin token
there; it is kept only in the browser's sessionStorage). The v1 page is
served at /v1. Request bodies are never logged.

Phase 3 additions: approvals queue (a gated vault use enqueues a pending
approval; an admin approval lets exactly one use through), /activity
audit readback, vault ACL edit + delete, connection-pack endpoints, and
the owner (admin token) may act directly as the reserved agent "owner"
so the GUI needs no separate agent token.
"""
import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from . import bus, peers as peers_mod
from .approvals import ApprovalStore
from .handover_log import HandoverStore
from .handover_rules import rules_text, rules_version, starter_message
from .pack import PACK_FORMATS, build_pack, build_pack_variant
from .registry import DEFAULT_ROLE, Registry, order_denial
from .scan import scan_fields
from .tasks import InvalidTransition, TaskStore
from .vault import Vault

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
WEB2_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web2")
OWNER = {"id": "owner", "name": "Owner", "role": "owner",
         "capabilities": [], "status": "online", "machine": "", "node": ""}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


class Node:
    def __init__(self, data_dir: str, admin_token: str,
                 master_passphrase: Optional[str] = None,
                 node_name: Optional[str] = None,
                 public_url: Optional[str] = None,
                 flush_interval: Optional[float] = None,
                 retry_schedule=None, code_ttl: Optional[float] = None,
                 max_attempts: Optional[int] = None):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        try:
            os.chmod(data_dir, 0o700)  # Phase 5: node data is owner-only
        except OSError:
            pass
        self.admin_token = admin_token
        self.audit_path = os.path.join(data_dir, "audit.log")
        # Create the audit log up front at 0600; later appends (node,
        # vault, approvals) inherit the existing file's permissions.
        with open(self.audit_path, "a"):
            pass
        try:
            os.chmod(self.audit_path, 0o600)
        except OSError:
            pass
        self.registry = Registry(os.path.join(data_dir, "agents.db"))
        self.tasks = TaskStore(os.path.join(data_dir, "tasks.db"))
        self.bus_db = os.path.join(data_dir, "bus.db")
        self.vault = Vault(os.path.join(data_dir, "vault.json"),
                           self.audit_path,
                           passphrase=master_passphrase)
        self.approvals = ApprovalStore(
            os.path.join(data_dir, "approvals.db"), audit_path=self.audit_path)
        self.vault.approvals = self.approvals
        # Phase 4 federation.
        self.identity = peers_mod.load_identity(
            data_dir, name=node_name or os.environ.get("AGENT_MESH_NODE_NAME"))
        self.node_id = self.identity["node_id"]
        # v2.1 handover automation: entry store + exported handover file.
        self.handover = HandoverStore(
            os.path.join(data_dir, "handover.db"), data_dir,
            self.identity["name"])
        self.peers = peers_mod.PeerStore(os.path.join(data_dir, "peers.db"))
        self.public_url = (public_url
                           or os.environ.get("AGENT_MESH_PUBLIC_URL") or "")
        self.flush_interval = flush_interval if flush_interval is not None \
            else _env_float("AGENT_MESH_FLUSH_SEC", 2.0)
        self.retry_schedule = retry_schedule if retry_schedule is not None \
            else peers_mod.parse_schedule(
                os.environ.get("AGENT_MESH_RETRY_SCHEDULE"))
        self.code_ttl = code_ttl if code_ttl is not None else _env_float(
            "AGENT_MESH_PAIR_TTL_SEC", peers_mod.DEFAULT_CODE_TTL_SEC)
        self.max_attempts = max_attempts if max_attempts is not None \
            else int(_env_float("AGENT_MESH_OUTBOX_MAX_ATTEMPTS",
                                peers_mod.DEFAULT_MAX_ATTEMPTS))
        self._flush_stop = threading.Event()

    # -- auth helpers ----------------------------------------------------
    def agent_from(self, headers) -> Optional[Dict[str, Any]]:
        auth = headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        return self.registry.authenticate(token)

    def is_admin(self, headers) -> bool:
        return bool(self.admin_token) and headers.get(
            "X-Admin-Token", "") == self.admin_token

    def actor_from(self, headers) -> Optional[Dict[str, Any]]:
        """An authenticated agent, or the owner pseudo-agent for admin."""
        agent = self.agent_from(headers)
        if agent is not None:
            return agent
        if self.is_admin(headers):
            return dict(OWNER)
        return None

    # -- handover starter (v2.1) -------------------------------------------
    def deliver_handover_starter(self, agent: Dict[str, Any]) -> bool:
        """Send the agent its ONE handover-rules starter message for the
        current rules version. Idempotent per agent per version: safe
        to call on registration and on every heartbeat — only the first
        call per version actually sends. Never raises: a starter
        failure must not break registration or heartbeats."""
        try:
            version, text = starter_message()
            if not self.handover.starter_needed(agent["id"], version):
                return False
            bus.send_message(sender="owner", recipient=agent["id"],
                             msg_type="HANDOVER_RULES", payload=text,
                             db_path=self.bus_db)
            self.handover.mark_starter(agent["id"], version)
            self.audit_event({"event": "handover_starter_sent",
                              "agent": agent["id"], "service": "",
                              "action": "handover_rules",
                              "outcome": f"v{version}"})
            return True
        except Exception:
            return False

    # -- audit ------------------------------------------------------------
    def audit_event(self, entry: Dict[str, Any]) -> None:
        entry.setdefault("ts", time.time())
        with open(self.audit_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    # -- Phase 5 ordering matrix -------------------------------------------
    def _target_role(self, assignee_agent: Optional[str]) -> Optional[str]:
        if not assignee_agent:
            return None
        local = self.registry.get(assignee_agent)
        if local:
            return local["role"]
        remote = self.peers.find_remote_agent(assignee_agent)
        return remote.get("role") if remote else None

    def check_order(self, actor: Dict[str, Any],
                    assignee_agent: Optional[str],
                    assignee_capability: Optional[str]) -> Optional[str]:
        """None if `actor` may create this task, else the denial reason."""
        return order_denial(actor["id"], actor.get("role") or "worker",
                            assignee_agent,
                            self._target_role(assignee_agent),
                            assignee_capability)

    def check_remote_order(self, sender_node: str, snap: Dict[str, Any],
                           ) -> Optional[str]:
        """Matrix check for a peer-originated task snapshot about to be
        created on THIS node. The requester's role comes from the
        sender's synced directory (the reserved id 'owner' is the human
        on the origin node and keeps owner rights). Unknown requesters
        are denied: the directory sync precedes task sync in the outbox,
        so a legit requester is known by the time its task arrives, and
        the sender's outbox retries while it is not."""
        requester = snap.get("requester") or ""
        if requester == "owner":
            requester_role = "owner"
        else:
            known = [a for a in self.peers.remote_agents()
                     if a["id"] == requester
                     and a["home_node"] == sender_node]
            if not known:
                return (f"remote requester {requester} is not in "
                        f"{sender_node}'s synced directory")
            requester_role = known[0].get("role") or "worker"
        return order_denial(requester, requester_role,
                            snap.get("assignee_agent"),
                            self._target_role(snap.get("assignee_agent")),
                            snap.get("assignee_capability"))

    def read_activity(self, limit: int = 100):
        entries = []
        if os.path.exists(self.audit_path):
            with open(self.audit_path) as f:
                for line in f:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        continue
        return entries[-limit:][::-1]

    # -- Phase 4 federation -------------------------------------------------
    def all_agents(self):
        local = [dict(a, home_node=self.node_id, remote=False)
                 for a in self.registry.list_agents()]
        return local + self.peers.remote_agents()

    def enqueue_peer(self, peer_node_id: str, kind: str,
                     payload: Dict[str, Any], dedupe_key: str) -> None:
        self.peers.enqueue(peer_node_id, kind, payload, dedupe_key)

    def enqueue_directory(self, peer_node_id: str) -> None:
        # Dedupe by content hash: re-syncing an unchanged directory is a
        # no-op, a changed one queues a fresh delivery.
        import hashlib
        agents = self.registry.list_agents()
        digest = hashlib.sha256(json.dumps(
            agents, sort_keys=True).encode()).hexdigest()[:16]
        self.enqueue_peer(peer_node_id, "directory",
                          {"node_id": self.node_id,
                           "name": self.identity["name"], "agents": agents},
                          f"directory:{digest}")

    def sync_task_to_peer(self, task: Dict[str, Any]) -> None:
        """Queue a snapshot of a federated task for its other node."""
        target = task.get("home_node") or task.get("origin_node")
        if not target or target == self.node_id:
            return
        self.enqueue_peer(
            target, "task_sync", {"task": task},
            f"task:{task['id']}:{task['status']}:{task['updated_at']}")

    def _signed_post(self, peer: Dict[str, Any], path: str,
                     payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        ts = str(int(time.time()))
        sig = peers_mod.sign(peer["secret"], ts, "POST", path, body)
        req = urllib.request.Request(peer["url"] + path, data=body,
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Mesh-Node", self.node_id)
        req.add_header("X-Mesh-Timestamp", ts)
        req.add_header("X-Mesh-Signature", sig)
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"peer returned HTTP {resp.status}")

    def flush_outbox_once(self) -> Dict[str, int]:
        delivered = failed = 0
        for item in self.peers.due_items():
            peer = self.peers.get_peer(item["peer_node_id"])
            if peer is None:
                self.peers.mark_attempt(item["id"], "peer record missing",
                                        self.retry_schedule,
                                        self.max_attempts)
                failed += 1
                continue
            path = {"directory": "/peer/directory",
                    "task_sync": "/peer/task_sync",
                    "message": "/peer/message"}.get(item["kind"])
            try:
                if path is None:
                    raise RuntimeError(f"unknown outbox kind {item['kind']}")
                self._signed_post(peer, path, item["payload"])
                self.peers.mark_delivered(item["id"])
                self.peers.touch_peer(peer["peer_node_id"])
                delivered += 1
            except Exception as exc:
                self.peers.mark_attempt(item["id"], str(exc),
                                        self.retry_schedule,
                                        self.max_attempts)
                failed += 1
        return {"delivered": delivered, "failed": failed,
                "counts": self.peers.outbox_counts()}

    def flush_loop(self) -> None:
        while not self._flush_stop.wait(self.flush_interval):
            try:
                self.flush_outbox_once()
            except Exception:
                pass  # the loop must never die on one bad pass

    def pair_with(self, peer_url: str, code: str) -> Dict[str, Any]:
        """Initiator side of pairing: call the peer's /peer/pair with our
        identity and the one-time code its admin generated. On success
        both nodes hold the same per-peer secret."""
        peer_url = peer_url.rstrip("/")
        body = json.dumps({"node_id": self.node_id,
                           "name": self.identity["name"],
                           "url": self.public_url,
                           "code": code}).encode()
        req = urllib.request.Request(peer_url + "/peer/pair", data=body,
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                out = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise ValueError(
                f"pairing refused by peer (HTTP {exc.code}): {detail}")
        self.peers.add_peer(out["node_id"], out.get("name", ""), peer_url,
                            out["peer_secret"])
        self.enqueue_directory(out["node_id"])
        self.audit_event({"event": "peer_paired", "agent": "owner",
                          "service": out["node_id"], "action": "pair",
                          "outcome": "ok"})
        return {"node_id": out["node_id"], "name": out.get("name", ""),
                "url": peer_url, "status": "paired"}


def _static_file(directory: str, name: str):
    path = os.path.join(directory, name)
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def make_handler(node: Node):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep bodies/secrets out of logs
            pass

        def _send(self, code: int, obj: Any) -> None:
            self._send_bytes(code, json.dumps(obj).encode(),
                             "application/json")

        def _send_bytes(self, code: int, body: bytes,
                        content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_file(self, directory: str, name: str,
                        content_type: str) -> None:
            data = _static_file(directory, name)
            if data is None:
                return self._send(404, {"error": "not found"})
            return self._send_bytes(200, data, content_type)

        def _body(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length <= 0:
                self._raw = b""
                return {}
            self._raw = self.rfile.read(length)
            try:
                return json.loads(self._raw or b"{}")
            except Exception:
                return {}

        def _verify_peer(self, path: str):
            """HMAC peer auth for /peer/* (except /peer/pair, which is
            authenticated by the one-time pairing code). Returns the peer
            record, or None after sending 401/403. Peer credentials are
            valid ONLY here — they never satisfy agent/admin auth."""
            sender = self.headers.get("X-Mesh-Node", "")
            peer = node.peers.get_peer(sender) if sender else None
            if peer is None:
                self._send(403, {"error": "unknown peer node"})
                return None
            ts = self.headers.get("X-Mesh-Timestamp", "")
            try:
                fresh = abs(time.time() - int(ts)) \
                    <= peers_mod.TS_WINDOW_SEC
            except ValueError:
                fresh = False
            if not fresh:
                self._send(401, {"error": "stale or missing timestamp"})
                return None
            sig = self.headers.get("X-Mesh-Signature", "")
            if not peers_mod.verify(peer["secret"], sig, ts, "POST", path,
                                    getattr(self, "_raw", b"")):
                self._send(401, {"error": "bad peer signature"})
                return None
            node.peers.touch_peer(sender)
            return peer

        def _require_agent(self):
            agent = node.agent_from(self.headers)
            if agent is None:
                self._send(401, {"error": "unauthenticated: missing or "
                                          "invalid agent token"})
            return agent

        def _require_actor(self):
            actor = node.actor_from(self.headers)
            if actor is None:
                self._send(401, {"error": "unauthenticated: missing or "
                                          "invalid token"})
            return actor

        def _scan_gate(self, actor_id: str, actor_role: str,
                       fields: Dict[str, Any], body: Dict[str, Any],
                       action: str) -> bool:
            """Phase 5 secret scanner. True = content may proceed.
            On a hit: audits the block (rule name + actor only — never
            the matched text) and sends 400, or 403 when a non-owner
            tried to use the owner override. The owner (admin actor or
            an owner-role agent) may resend with allow_secret_risk:
            true; that override is audited too."""
            hits = scan_fields(fields)
            if not hits:
                return True
            rule = hits[0]
            if body.get("allow_secret_risk"):
                if actor_id == "owner" or actor_role == "owner":
                    node.audit_event({
                        "event": "secret_scan_override", "agent": actor_id,
                        "service": "", "action": action, "rule": rule,
                        "outcome": "override_allowed"})
                    return True
                node.audit_event({
                    "event": "secret_scan_blocked", "agent": actor_id,
                    "service": "", "action": action, "rule": rule,
                    "outcome": "override_denied"})
                self._send(403, {
                    "error": f"blocked by secret scanner (rule: {rule}); "
                             f"only the owner may override with "
                             f"allow_secret_risk", "rule": rule})
                return False
            node.audit_event({
                "event": "secret_scan_blocked", "agent": actor_id,
                "service": "", "action": action, "rule": rule,
                "outcome": "blocked"})
            self._send(400, {
                "error": f"blocked by secret scanner: rule '{rule}' "
                         f"matched credential-shaped content; nothing "
                         f"was stored. If this is a false positive, the "
                         f"owner may resend with allow_secret_risk: true.",
                "rule": rule, "rules": hits})
            return False

        def _require_admin(self) -> bool:
            if not node.is_admin(self.headers):
                self._send(401, {"error": "admin token required"})
                return False
            return True

        def _node_url(self) -> str:
            host = self.headers.get("Host") or "127.0.0.1:8790"
            return f"http://{host}"

        # -- routing -----------------------------------------------------
        def do_GET(self):
            parsed = urlparse(self.path)
            path, qs = parsed.path.rstrip("/") or "/", parse_qs(parsed.query)
            try:
                # GUI (Phase 3) — public shell; all data stays behind auth.
                if path == "/":
                    return self._serve_file(WEB2_DIR, "index.html",
                                            "text/html; charset=utf-8")
                if path == "/v1":
                    return self._serve_file(WEB_DIR, "index.html",
                                            "text/html; charset=utf-8")
                if path == "/ui/app.js":
                    return self._serve_file(WEB2_DIR, "app.js",
                                            "text/javascript; charset=utf-8")
                if path == "/ui/app.css":
                    return self._serve_file(WEB2_DIR, "app.css",
                                            "text/css; charset=utf-8")
                # PWA (v2.1): installable web app — also the Android path.
                if path == "/manifest.webmanifest":
                    return self._serve_file(
                        WEB2_DIR, "manifest.webmanifest",
                        "application/manifest+json; charset=utf-8")
                if path == "/sw.js":
                    return self._serve_file(WEB2_DIR, "sw.js",
                                            "text/javascript; charset=utf-8")
                if path.startswith("/icons/") and path.endswith(".png"):
                    return self._serve_file(
                        os.path.join(WEB2_DIR, "icons"),
                        os.path.basename(path), "image/png")
                if path == "/health":
                    return self._send(200, {"status": "ok"})
                if path == "/agents":
                    if not (node.agent_from(self.headers)
                            or node.is_admin(self.headers)):
                        return self._send(401, {"error": "unauthenticated"})
                    return self._send(200, {"agents": node.all_agents()})
                if path == "/node/info":
                    if not (node.agent_from(self.headers)
                            or node.is_admin(self.headers)):
                        return self._send(401, {"error": "unauthenticated"})
                    return self._send(200, {
                        "node_id": node.node_id,
                        "name": node.identity["name"],
                        "url": node.public_url})
                if path == "/peers":
                    if not self._require_admin():
                        return
                    return self._send(
                        200, {"peers": node.peers.list_peers_safe(),
                              "outbox": node.peers.outbox_counts()})
                if path == "/peers/outbox":
                    if not self._require_admin():
                        return
                    return self._send(200, {
                        "counts": node.peers.outbox_counts(),
                        "items": node.peers.outbox_recent(50)})
                if path == "/messages":
                    actor = self._require_actor()
                    if actor is None:
                        return
                    if node.is_admin(self.headers):
                        # Owner may inspect any inbox (or all traffic).
                        recipient = qs.get("recipient", [None])[0]
                    else:
                        # Phase 5 fix: an agent reads ONLY its own
                        # inbox — a ?recipient= naming someone else is
                        # ignored, not honoured.
                        recipient = actor["id"]
                    msgs = bus.list_messages(
                        recipient=recipient, unread_only=False, limit=100,
                        db_path=node.bus_db)
                    return self._send(200, {"messages": msgs})
                if path == "/tasks":
                    actor = self._require_actor()
                    if actor is None:
                        return
                    tasks = node.tasks.list_tasks(
                        assignee_agent=qs.get("assignee", [None])[0],
                        requester=qs.get("requester", [None])[0],
                        status=qs.get("status", [None])[0])
                    return self._send(200, {"tasks": tasks})
                if path.startswith("/tasks/"):
                    actor = self._require_actor()
                    if actor is None:
                        return
                    task = node.tasks.get(path.split("/")[2])
                    if task is None:
                        return self._send(404, {"error": "task not found"})
                    return self._send(200, {"task": task})
                if path == "/vault/services":
                    if not (node.agent_from(self.headers)
                            or node.is_admin(self.headers)):
                        return self._send(401, {"error": "unauthenticated"})
                    return self._send(
                        200, {"services": node.vault.list_services_safe()})
                if path == "/approvals":
                    if not self._require_admin():
                        return
                    status = qs.get("status", [None])[0]
                    return self._send(
                        200, {"approvals": node.approvals.list(status)})
                if path == "/handover/rules":
                    actor = self._require_actor()
                    if actor is None:
                        return
                    return self._send(200, {
                        "version": rules_version(), "rules": rules_text()})
                if path == "/handover/entries":
                    actor = self._require_actor()
                    if actor is None:
                        return
                    if node.is_admin(self.headers):
                        entries = node.handover.list_entries()
                    else:
                        entries = node.handover.list_entries(actor["id"])
                    return self._send(200, {"entries": entries})
                if path == "/activity":
                    if not self._require_admin():
                        return
                    try:
                        limit = int(qs.get("limit", ["100"])[0])
                    except ValueError:
                        limit = 100
                    return self._send(
                        200, {"entries": node.read_activity(limit)})
                return self._send(404, {"error": "not found"})
            except Exception as exc:  # never leak internals beyond the message
                return self._send(500, {"error": str(exc)})

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            body = self._body()
            try:
                # -- Phase 4 peer endpoints -------------------------------
                if path == "/peer/pair":
                    # Authenticated by the one-time pairing code itself.
                    if not node.peers.consume_code(body.get("code", "")):
                        return self._send(
                            403, {"error": "invalid, expired or already "
                                           "used pairing code"})
                    if not body.get("node_id") or not body.get("url"):
                        return self._send(
                            400, {"error": "node_id and url required"})
                    import secrets as _secrets
                    peer_secret = _secrets.token_urlsafe(32)
                    node.peers.add_peer(body["node_id"],
                                        body.get("name", ""), body["url"],
                                        peer_secret)
                    node.enqueue_directory(body["node_id"])
                    node.audit_event({
                        "event": "peer_paired", "agent": "owner",
                        "service": body["node_id"], "action": "pair",
                        "outcome": "ok"})
                    # The secret is returned exactly once, over this
                    # pairing response, to the initiating node only.
                    return self._send(200, {
                        "node_id": node.node_id,
                        "name": node.identity["name"],
                        "peer_secret": peer_secret})
                if path.startswith("/peer/"):
                    peer = self._verify_peer(path)
                    if peer is None:
                        return
                    sender = peer["peer_node_id"]
                    if path == "/peer/ping":
                        return self._send(
                            200, {"ok": True, "node_id": node.node_id})
                    if path == "/peer/directory":
                        node.peers.set_remote_agents(
                            sender, body.get("agents", []))
                        node.audit_event({
                            "event": "peer_directory", "agent": sender,
                            "service": "", "action": "sync",
                            "outcome": f"{len(body.get('agents', []))} agents"})
                        return self._send(200, {"ok": True})
                    if path == "/peer/task_sync":
                        snap = body.get("task") or {}
                        if snap.get("origin_node") == node.node_id \
                                and snap.get("origin_task_id"):
                            task = node.tasks.apply_remote_state(snap)
                        else:
                            # Phase 5: a remote task is CREATED here —
                            # scan it and enforce the ordering matrix
                            # against the origin requester before it
                            # exists on this node. A denial returns 403,
                            # so the sender's outbox keeps it retrying /
                            # failed rather than delivered.
                            if not self._scan_gate(
                                    snap.get("requester", sender),
                                    "peer",
                                    {"title": snap.get("title", ""),
                                     "instructions":
                                         snap.get("instructions", ""),
                                     "result": snap.get("result")},
                                    {}, "peer_task"):
                                return
                            existing = node.tasks.get_by_origin(
                                sender, snap.get("id", ""))
                            if existing is None:
                                denial = node.check_remote_order(
                                    sender, snap)
                                if denial:
                                    node.audit_event({
                                        "event": "task_order_denied",
                                        "agent":
                                            snap.get("requester", ""),
                                        "service": "", "action": "create",
                                        "outcome": "denied_remote",
                                        "reason": denial})
                                    return self._send(
                                        403, {"error": denial})
                            task = node.tasks.upsert_federated(snap, sender)
                        return self._send(
                            200, {"ok": True,
                                  "task_id": task["id"] if task else None})
                    if path == "/peer/message":
                        msg = body.get("message") or {}
                        if not self._scan_gate(
                                msg.get("sender", sender), "peer",
                                {"payload": msg.get("payload", "")},
                                {}, "peer_message"):
                            return
                        dedupe = msg.get("correlation_id") or ""
                        if dedupe and bus.message_exists(
                                dedupe, db_path=node.bus_db):
                            return self._send(
                                200, {"ok": True, "duplicate": True})
                        msg_id = bus.send_message(
                            sender=msg.get("sender", sender),
                            recipient=msg.get("recipient", ""),
                            msg_type=msg.get("msg_type", "INFO"),
                            payload=msg.get("payload", ""),
                            correlation_id=dedupe or None,
                            db_path=node.bus_db)
                        return self._send(200, {"ok": True, "id": msg_id})
                    return self._send(404, {"error": "not found"})
                # -- Phase 4 admin peer endpoints -------------------------
                if path == "/peers/pairing-code":
                    if not self._require_admin():
                        return
                    code, expires = node.peers.create_code(node.code_ttl)
                    return self._send(201, {"code": code,
                                            "expires_at": expires})
                if path == "/peers/pair":
                    if not self._require_admin():
                        return
                    if not body.get("peer_url") or not body.get("code"):
                        return self._send(
                            400, {"error": "peer_url and code required"})
                    try:
                        peer = node.pair_with(body["peer_url"], body["code"])
                    except Exception as exc:
                        return self._send(400, {"error": str(exc)})
                    return self._send(201, {"peer": peer})
                if path == "/peers/flush":
                    if not self._require_admin():
                        return
                    return self._send(200, node.flush_outbox_once())
                if path.startswith("/peers/") and path.endswith("/sync"):
                    if not self._require_admin():
                        return
                    peer_id = path.split("/")[2]
                    if node.peers.get_peer(peer_id) is None:
                        return self._send(404, {"error": "peer not found"})
                    node.enqueue_directory(peer_id)
                    return self._send(200, {"ok": True})
                if path == "/agents/register":
                    if not self._require_admin():
                        return
                    agent, token = node.registry.register(
                        name=body.get("name", "unnamed"),
                        machine=body.get("machine", ""),
                        node=body.get("node", ""),
                        capabilities=body.get("capabilities", []),
                        role=body.get("role") or DEFAULT_ROLE)
                    node.deliver_handover_starter(agent)
                    # Token is returned exactly once, here.
                    return self._send(201, {"agent": agent, "token": token})
                if path == "/agents/pack":
                    # Register a new agent and return its connection pack.
                    if not self._require_admin():
                        return
                    fmt = body.get("format", "mcp")
                    if fmt not in PACK_FORMATS:
                        return self._send(
                            400, {"error": f"unknown pack format {fmt!r}; "
                                           f"allowed {PACK_FORMATS}"})
                    agent, token = node.registry.register(
                        name=body.get("name", "unnamed"),
                        machine=body.get("machine", ""),
                        node=body.get("node", ""),
                        capabilities=body.get("capabilities", []),
                        role=body.get("role") or DEFAULT_ROLE)
                    node.deliver_handover_starter(agent)
                    pack = build_pack_variant(self._node_url(), agent,
                                              token, fmt)
                    return self._send(201, {"agent": agent, "pack": pack,
                                            "format": fmt})
                if path.startswith("/agents/") and path.endswith("/pack"):
                    # New pack for an existing agent: rotates its token
                    # (the old token stops working).
                    if not self._require_admin():
                        return
                    agent_id = path.split("/")[2]
                    agent = node.registry.get(agent_id)
                    if agent is None:
                        return self._send(404, {"error": "agent not found"})
                    fmt = body.get("format", "mcp")
                    if fmt not in PACK_FORMATS:
                        return self._send(
                            400, {"error": f"unknown pack format {fmt!r}; "
                                           f"allowed {PACK_FORMATS}"})
                    token = node.registry.rotate_token(agent_id)
                    pack = build_pack_variant(self._node_url(), agent,
                                              token, fmt)
                    return self._send(200, {"agent": agent, "pack": pack,
                                            "format": fmt})
                if path.startswith("/agents/") and \
                        path.endswith("/disconnect"):
                    # Phase 6: admin disconnects (revokes) an agent.
                    # Its token stops working at once; the agent stays
                    # listed as 'revoked' and its tasks/messages remain
                    # readable by admin. Reconnect = a fresh pack.
                    if not self._require_admin():
                        return
                    agent_id = path.split("/")[2]
                    agent = node.registry.get(agent_id)
                    if agent is None:
                        return self._send(404, {"error": "agent not found"})
                    node.registry.revoke(agent_id)
                    node.audit_event({
                        "event": "agent_disconnected", "agent": agent_id,
                        "outcome": "revoked"})
                    return self._send(
                        200, {"agent": node.registry.get(agent_id)})
                if path == "/agents/heartbeat":
                    agent = self._require_agent()
                    if agent is None:
                        return
                    node.registry.heartbeat(agent["id"], body.get("status"))
                    # First heartbeat also delivers the handover
                    # starter (idempotent — registration usually beat
                    # it, and re-heartbeats never resend).
                    node.deliver_handover_starter(agent)
                    return self._send(200, {"agent": node.registry.get(agent["id"])})
                if path == "/handover/entries":
                    actor = self._require_actor()
                    if actor is None:
                        return
                    if not body.get("title"):
                        return self._send(400, {"error": "title required"})
                    if not self._scan_gate(
                            actor["id"], actor.get("role") or "",
                            {"title": body.get("title", ""),
                             "asked": body.get("asked", ""),
                             "done": body.get("done", ""),
                             "verified": body.get("verified", ""),
                             "decisions": body.get("decisions", ""),
                             "files": body.get("files", ""),
                             "open": body.get("open", "")},
                            body, "handover_entry"):
                        return
                    entry = node.handover.add_entry(
                        actor["id"], actor.get("name", ""), body)
                    node.audit_event({
                        "event": "handover_entry", "agent": actor["id"],
                        "service": "", "action": "handover_log",
                        "outcome": entry["id"]})
                    return self._send(201, {"entry": entry})
                if path == "/messages":
                    actor = self._require_actor()
                    if actor is None:
                        return
                    if not body.get("recipient") or not body.get("msg_type"):
                        return self._send(400, {"error": "recipient and "
                                                        "msg_type required"})
                    if not self._scan_gate(
                            actor["id"], actor.get("role") or "",
                            {"payload": body.get("payload", "")}, body,
                            "message"):
                        return
                    corr = body.get("correlation_id") \
                        or f"{node.node_id}:msg:{int(time.time()*1000)}:" \
                           f"{actor['id']}"
                    msg_id = bus.send_message(
                        sender=actor["id"], recipient=body["recipient"],
                        msg_type=body["msg_type"],
                        payload=body.get("payload", ""),
                        correlation_id=corr,
                        db_path=node.bus_db)
                    remote = node.peers.find_remote_agent(body["recipient"])
                    if remote:
                        node.enqueue_peer(
                            remote["home_node"], "message",
                            {"message": {
                                "sender": actor["id"],
                                "recipient": body["recipient"],
                                "msg_type": body["msg_type"],
                                "payload": body.get("payload", ""),
                                "correlation_id": corr}},
                            f"message:{corr}")
                    return self._send(201, {"id": msg_id})
                if path == "/tasks":
                    actor = self._require_actor()
                    if actor is None:
                        return
                    if not body.get("title"):
                        return self._send(400, {"error": "title required"})
                    # Phase 5: ordering matrix (hard-enforced) + scan.
                    denial = node.check_order(
                        actor, body.get("assignee_agent"),
                        body.get("assignee_capability"))
                    if denial:
                        node.audit_event({
                            "event": "task_order_denied",
                            "agent": actor["id"], "service": "",
                            "action": "create", "outcome": "denied",
                            "reason": denial})
                        return self._send(403, {"error": denial})
                    if not self._scan_gate(
                            actor["id"], actor.get("role") or "",
                            {"title": body.get("title", ""),
                             "instructions": body.get("instructions", "")},
                            body, "task_create"):
                        return
                    home_node = None
                    if body.get("assignee_agent"):
                        remote = node.peers.find_remote_agent(
                            body["assignee_agent"])
                        if remote:
                            home_node = remote["home_node"]
                    task, duplicate = node.tasks.create(
                        title=body["title"], requester=actor["id"],
                        instructions=body.get("instructions", ""),
                        assignee_agent=body.get("assignee_agent"),
                        assignee_capability=body.get("assignee_capability"),
                        priority=body.get("priority", "normal"),
                        parent_id=body.get("parent_id"),
                        depends_on=body.get("depends_on"),
                        idempotency_key=body.get("idempotency_key"),
                        budget_time_sec=body.get("budget_time_sec"),
                        budget_tokens=body.get("budget_tokens"),
                        budget_money=body.get("budget_money"),
                        home_node=home_node)
                    if not duplicate:
                        node.sync_task_to_peer(task)
                    node.audit_event({
                        "event": "task_created", "agent": actor["id"],
                        "task": task["id"], "outcome": task["status"],
                        "service": "", "action": "create"})
                    return self._send(200 if duplicate else 201,
                                      {"task": task, "duplicate": duplicate})
                if path.startswith("/tasks/") and path.endswith("/transition"):
                    actor = self._require_actor()
                    if actor is None:
                        return
                    if body.get("result") and not self._scan_gate(
                            actor["id"], actor.get("role") or "",
                            {"result": body.get("result")}, body,
                            "task_result"):
                        return
                    try:
                        task = node.tasks.transition(
                            path.split("/")[2], body.get("to", ""),
                            result=body.get("result"))
                    except KeyError as exc:
                        return self._send(404, {"error": str(exc)})
                    except InvalidTransition as exc:
                        return self._send(400, {"error": str(exc)})
                    node.audit_event({
                        "event": "task_transition", "agent": actor["id"],
                        "task": task["id"], "outcome": task["status"],
                        "service": "", "action": body.get("to", "")})
                    node.sync_task_to_peer(task)
                    return self._send(200, {"task": task})
                if path.startswith("/tasks/") and path.endswith("/progress"):
                    actor = self._require_actor()
                    if actor is None:
                        return
                    if not self._scan_gate(
                            actor["id"], actor.get("role") or "",
                            {"note": body.get("note", "")}, body,
                            "task_progress"):
                        return
                    try:
                        task = node.tasks.add_progress(
                            path.split("/")[2], actor["id"],
                            body.get("note", ""))
                    except KeyError as exc:
                        return self._send(404, {"error": str(exc)})
                    except InvalidTransition as exc:
                        return self._send(400, {"error": str(exc)})
                    node.sync_task_to_peer(task)
                    return self._send(200, {"task": task})
                if path == "/vault/services":
                    if not self._require_admin():
                        return
                    if not body.get("service") or not body.get("secret"):
                        return self._send(400, {"error": "service and secret "
                                                        "required"})
                    node.vault.set_service(
                        body["service"], body["secret"],
                        upstream=body.get("upstream", ""),
                        acl=body.get("acl", {}),
                        requires_approval=bool(body.get("requires_approval")),
                        cred_type=body.get("type", "api_key"),
                        meta=body.get("meta", {}))
                    return self._send(201, {"services":
                                            node.vault.list_services_safe()})
                if path.startswith("/vault/services/") and \
                        path.endswith("/grants"):
                    # v2.1: owner shares one credential with one agent.
                    if not self._require_admin():
                        return
                    service = path.split("/")[3]
                    if not body.get("agent_id") or not body.get("mode"):
                        return self._send(
                            400, {"error": "agent_id and mode required"})
                    try:
                        grant = node.vault.set_grant(
                            service, body["agent_id"], body["mode"],
                            standing_reveal=bool(
                                body.get("standing_reveal")))
                    except KeyError as exc:
                        return self._send(404, {"error": str(exc)})
                    node.audit_event({
                        "event": "vault_grant_set", "agent": "owner",
                        "service": service, "action": "grant",
                        "outcome": body["mode"],
                        "grantee": body["agent_id"]})
                    return self._send(200, {
                        "grant": grant,
                        "services": node.vault.list_services_safe()})
                if path == "/vault/credential-use":
                    # v2.1 brokered use: connection details only, the
                    # secret never leaves the node.
                    agent = self._require_agent()
                    if agent is None:
                        return
                    if not body.get("service"):
                        return self._send(400, {"error": "service required"})
                    out = node.vault.credential_use(agent["id"],
                                                    body["service"])
                    code = 403 if out["outcome"] == "deny" else 200
                    return self._send(code, out)
                if path == "/vault/reveal":
                    # v2.1 reveal: the ONLY endpoint that returns a
                    # secret value, and only under a reveal grant —
                    # per-use approved, or standing. All audited.
                    agent = self._require_agent()
                    if agent is None:
                        return
                    if not body.get("service"):
                        return self._send(400, {"error": "service required"})
                    out = node.vault.reveal(agent["id"], body["service"])
                    if out["outcome"] == "needs_approval":
                        approval, _created = node.approvals.request(
                            agent["id"], body["service"], "reveal")
                        out["approval_id"] = approval["id"]
                    code = 403 if out["outcome"] == "deny" else 200
                    return self._send(code, out)
                if path == "/vault/use-check":
                    agent = node.agent_from(self.headers)
                    if agent is not None:
                        agent_id = agent["id"]
                    elif node.is_admin(self.headers) and body.get("agent_id"):
                        # GUI test path: owner checks on behalf of an agent.
                        agent_id = body["agent_id"]
                    else:
                        return self._send(401, {"error": "unauthenticated: "
                                                         "missing or invalid "
                                                         "agent token"})
                    outcome = node.vault.use_check(
                        agent_id, body.get("service", ""),
                        body.get("action", "read"))
                    reply: Dict[str, Any] = {"outcome": outcome}
                    if outcome == "needs_approval":
                        approval, _created = node.approvals.request(
                            agent_id, body.get("service", ""),
                            body.get("action", "read"))
                        reply["approval_id"] = approval["id"]
                    code = 403 if outcome == "deny" else 200
                    return self._send(code, reply)
                if path == "/approvals":
                    agent = self._require_agent()
                    if agent is None:
                        return
                    if not body.get("service"):
                        return self._send(400, {"error": "service required"})
                    action = body.get("action", "read")
                    outcome = node.vault.use_check(
                        agent["id"], body["service"], action)
                    reply = {"outcome": outcome}
                    if outcome == "needs_approval":
                        approval, created = node.approvals.request(
                            agent["id"], body["service"], action)
                        reply["approval"] = approval
                        reply["created"] = created
                    code = 403 if outcome == "deny" else 200
                    return self._send(code, reply)
                if path.startswith("/approvals/") and path.endswith("/decide"):
                    if not self._require_admin():
                        return
                    decision = body.get("decision")
                    if decision is not None:
                        approve = decision == "approve"
                    else:
                        approve = bool(body.get("approved"))
                    try:
                        approval = node.approvals.decide(
                            path.split("/")[2], approve,
                            decided_by=body.get("decided_by", "owner"))
                    except KeyError as exc:
                        return self._send(404, {"error": str(exc)})
                    except ValueError as exc:
                        return self._send(400, {"error": str(exc)})
                    return self._send(200, {"approval": approval})
                return self._send(404, {"error": "not found"})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            except Exception as exc:
                return self._send(500, {"error": str(exc)})

        def do_PUT(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            body = self._body()
            try:
                if path.startswith("/vault/services/"):
                    if not self._require_admin():
                        return
                    service = path.split("/")[3]
                    try:
                        node.vault.update_service(
                            service, acl=body.get("acl"),
                            requires_approval=body.get("requires_approval"),
                            upstream=body.get("upstream"),
                            meta=body.get("meta"))
                    except KeyError as exc:
                        return self._send(404, {"error": str(exc)})
                    node.audit_event({
                        "event": "vault_acl_updated", "agent": "owner",
                        "service": service, "action": "update",
                        "outcome": "ok"})
                    return self._send(
                        200, {"services": node.vault.list_services_safe()})
                return self._send(404, {"error": "not found"})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            except Exception as exc:
                return self._send(500, {"error": str(exc)})

        def do_DELETE(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            try:
                if path.startswith("/vault/services/") and \
                        "/grants/" in path:
                    # v2.1: owner revokes one sharing grant.
                    if not self._require_admin():
                        return
                    parts = path.split("/")
                    service, agent_id = parts[3], parts[5]
                    try:
                        removed = node.vault.remove_grant(service, agent_id)
                    except KeyError as exc:
                        return self._send(404, {"error": str(exc)})
                    if not removed:
                        return self._send(404, {"error": "grant not found"})
                    node.audit_event({
                        "event": "vault_grant_removed", "agent": "owner",
                        "service": service, "action": "grant",
                        "outcome": "removed", "grantee": agent_id})
                    return self._send(
                        200, {"services": node.vault.list_services_safe()})
                if path.startswith("/vault/services/"):
                    if not self._require_admin():
                        return
                    service = path.split("/")[3]
                    if not node.vault.delete_service(service):
                        return self._send(404, {"error": "service not found"})
                    node.audit_event({
                        "event": "vault_service_deleted", "agent": "owner",
                        "service": service, "action": "delete",
                        "outcome": "ok"})
                    return self._send(
                        200, {"services": node.vault.list_services_safe()})
                return self._send(404, {"error": "not found"})
            except Exception as exc:
                return self._send(500, {"error": str(exc)})

    return Handler


def serve(node: Node, host: str = "127.0.0.1", port: int = 8790):
    server = ThreadingHTTPServer((host, port), make_handler(node))
    server.daemon_threads = True
    if not node.public_url:
        node.public_url = f"http://{host}:{server.server_address[1]}"
    threading.Thread(target=node.flush_loop, daemon=True,
                     name="mesh-outbox-flush").start()
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-mesh-node")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("AGENT_MESH_PORT", "8790")))
    parser.add_argument("--data-dir", default=os.environ.get(
        "AGENT_MESH_DATA", os.path.expanduser("~/.agent-mesh-node")))
    parser.add_argument("--name", default=None,
                        help="this node's display name (default: "
                             "AGENT_MESH_NODE_NAME or 'unnamed node')")
    parser.add_argument("--url", default=None,
                        help="public URL peers should use to reach this "
                             "node (default: http://<host>:<port>)")
    args = parser.parse_args(argv)
    admin = os.environ.get("AGENT_MESH_ADMIN")
    if not admin:
        raise SystemExit("AGENT_MESH_ADMIN must be set (node admin token)")
    node = Node(args.data_dir, admin_token=admin, node_name=args.name,
                public_url=args.url)
    server = serve(node, args.host, args.port)
    print(f"agent-mesh node {node.node_id} ({node.identity['name']}) on "
          f"{node.public_url} (data: {args.data_dir})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
