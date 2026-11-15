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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from . import bus
from .approvals import ApprovalStore
from .pack import build_pack
from .registry import Registry
from .tasks import InvalidTransition, TaskStore
from .vault import Vault

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
WEB2_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web2")
OWNER = {"id": "owner", "name": "Owner", "role": "owner",
         "capabilities": [], "status": "online", "machine": "", "node": ""}


class Node:
    def __init__(self, data_dir: str, admin_token: str,
                 master_passphrase: Optional[str] = None):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.admin_token = admin_token
        self.audit_path = os.path.join(data_dir, "audit.log")
        self.registry = Registry(os.path.join(data_dir, "agents.db"))
        self.tasks = TaskStore(os.path.join(data_dir, "tasks.db"))
        self.bus_db = os.path.join(data_dir, "bus.db")
        self.vault = Vault(os.path.join(data_dir, "vault.json"),
                           self.audit_path,
                           passphrase=master_passphrase)
        self.approvals = ApprovalStore(
            os.path.join(data_dir, "approvals.db"), audit_path=self.audit_path)
        self.vault.approvals = self.approvals

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

    # -- audit ------------------------------------------------------------
    def audit_event(self, entry: Dict[str, Any]) -> None:
        entry.setdefault("ts", time.time())
        with open(self.audit_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

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
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return {}

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
                if path == "/health":
                    return self._send(200, {"status": "ok"})
                if path == "/agents":
                    if not (node.agent_from(self.headers)
                            or node.is_admin(self.headers)):
                        return self._send(401, {"error": "unauthenticated"})
                    return self._send(200, {"agents": node.registry.list_agents()})
                if path == "/messages":
                    actor = self._require_actor()
                    if actor is None:
                        return
                    if "recipient" in qs:
                        recipient = qs["recipient"][0]
                    elif node.is_admin(self.headers):
                        recipient = None  # owner sees all recent traffic
                    else:
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
                if path == "/agents/register":
                    if not self._require_admin():
                        return
                    agent, token = node.registry.register(
                        name=body.get("name", "unnamed"),
                        machine=body.get("machine", ""),
                        node=body.get("node", ""),
                        capabilities=body.get("capabilities", []),
                        role=body.get("role", "worker"))
                    # Token is returned exactly once, here.
                    return self._send(201, {"agent": agent, "token": token})
                if path == "/agents/pack":
                    # Register a new agent and return its connection pack.
                    if not self._require_admin():
                        return
                    agent, token = node.registry.register(
                        name=body.get("name", "unnamed"),
                        machine=body.get("machine", ""),
                        node=body.get("node", ""),
                        capabilities=body.get("capabilities", []),
                        role=body.get("role", "worker"))
                    pack = build_pack(self._node_url(), agent, token)
                    return self._send(201, {"agent": agent, "pack": pack})
                if path.startswith("/agents/") and path.endswith("/pack"):
                    # New pack for an existing agent: rotates its token
                    # (the old token stops working).
                    if not self._require_admin():
                        return
                    agent_id = path.split("/")[2]
                    agent = node.registry.get(agent_id)
                    if agent is None:
                        return self._send(404, {"error": "agent not found"})
                    token = node.registry.rotate_token(agent_id)
                    pack = build_pack(self._node_url(), agent, token)
                    return self._send(200, {"agent": agent, "pack": pack})
                if path == "/agents/heartbeat":
                    agent = self._require_agent()
                    if agent is None:
                        return
                    node.registry.heartbeat(agent["id"], body.get("status"))
                    return self._send(200, {"agent": node.registry.get(agent["id"])})
                if path == "/messages":
                    actor = self._require_actor()
                    if actor is None:
                        return
                    if not body.get("recipient") or not body.get("msg_type"):
                        return self._send(400, {"error": "recipient and "
                                                        "msg_type required"})
                    msg_id = bus.send_message(
                        sender=actor["id"], recipient=body["recipient"],
                        msg_type=body["msg_type"],
                        payload=body.get("payload", ""),
                        correlation_id=body.get("correlation_id"),
                        db_path=node.bus_db)
                    return self._send(201, {"id": msg_id})
                if path == "/tasks":
                    actor = self._require_actor()
                    if actor is None:
                        return
                    if not body.get("title"):
                        return self._send(400, {"error": "title required"})
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
                        budget_money=body.get("budget_money"))
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
                    return self._send(200, {"task": task})
                if path.startswith("/tasks/") and path.endswith("/progress"):
                    actor = self._require_actor()
                    if actor is None:
                        return
                    try:
                        task = node.tasks.add_progress(
                            path.split("/")[2], actor["id"],
                            body.get("note", ""))
                    except KeyError as exc:
                        return self._send(404, {"error": str(exc)})
                    except InvalidTransition as exc:
                        return self._send(400, {"error": str(exc)})
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
                        requires_approval=bool(body.get("requires_approval")))
                    return self._send(201, {"services":
                                            node.vault.list_services_safe()})
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
                            upstream=body.get("upstream"))
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
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-mesh-node")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("AGENT_MESH_PORT", "8790")))
    parser.add_argument("--data-dir", default=os.environ.get(
        "AGENT_MESH_DATA", os.path.expanduser("~/.agent-mesh-node")))
    args = parser.parse_args(argv)
    admin = os.environ.get("AGENT_MESH_ADMIN")
    if not admin:
        raise SystemExit("AGENT_MESH_ADMIN must be set (node admin token)")
    node = Node(args.data_dir, admin_token=admin)
    server = serve(node, args.host, args.port)
    print(f"agent-mesh node on http://{args.host}:{args.port} "
          f"(data: {args.data_dir})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
