"""Mesh node server (v2 Phase 1) — stdlib http.server REST API.

Run:  AGENT_MESH_ADMIN=<token> AGENT_MESH_MASTER=<passphrase> \
        python3 -m agent_mesh.node [--port 8790] [--data-dir ~/.agent-mesh-node]
Request bodies are never logged.
"""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from . import bus
from .registry import Registry
from .tasks import InvalidTransition, TaskStore
from .vault import Vault


class Node:
    def __init__(self, data_dir: str, admin_token: str,
                 master_passphrase: Optional[str] = None):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.admin_token = admin_token
        self.registry = Registry(os.path.join(data_dir, "agents.db"))
        self.tasks = TaskStore(os.path.join(data_dir, "tasks.db"))
        self.bus_db = os.path.join(data_dir, "bus.db")
        self.vault = Vault(os.path.join(data_dir, "vault.json"),
                           os.path.join(data_dir, "audit.log"),
                           passphrase=master_passphrase)

    # -- auth helpers ----------------------------------------------------
    def agent_from(self, headers) -> Optional[Dict[str, Any]]:
        auth = headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        return self.registry.authenticate(token)

    def is_admin(self, headers) -> bool:
        return bool(self.admin_token) and headers.get(
            "X-Admin-Token", "") == self.admin_token


def make_handler(node: Node):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep bodies/secrets out of logs
            pass

        def _send(self, code: int, obj: Any) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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

        # -- routing -----------------------------------------------------
        def do_GET(self):
            parsed = urlparse(self.path)
            path, qs = parsed.path.rstrip("/") or "/", parse_qs(parsed.query)
            try:
                if path == "/health":
                    return self._send(200, {"status": "ok"})
                if path == "/agents":
                    if not (node.agent_from(self.headers)
                            or node.is_admin(self.headers)):
                        return self._send(401, {"error": "unauthenticated"})
                    return self._send(200, {"agents": node.registry.list_agents()})
                if path == "/messages":
                    agent = self._require_agent()
                    if agent is None:
                        return
                    recipient = qs.get("recipient", [agent["id"]])[0]
                    msgs = bus.list_messages(
                        recipient=recipient, unread_only=False, limit=100,
                        db_path=node.bus_db)
                    return self._send(200, {"messages": msgs})
                if path == "/tasks":
                    agent = self._require_agent()
                    if agent is None:
                        return
                    tasks = node.tasks.list_tasks(
                        assignee_agent=qs.get("assignee", [None])[0],
                        requester=qs.get("requester", [None])[0],
                        status=qs.get("status", [None])[0])
                    return self._send(200, {"tasks": tasks})
                if path.startswith("/tasks/"):
                    agent = self._require_agent()
                    if agent is None:
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
                return self._send(404, {"error": "not found"})
            except Exception as exc:  # never leak internals beyond the message
                return self._send(500, {"error": str(exc)})

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            body = self._body()
            try:
                if path == "/agents/register":
                    if not node.is_admin(self.headers):
                        return self._send(401, {"error": "admin token required"})
                    agent, token = node.registry.register(
                        name=body.get("name", "unnamed"),
                        machine=body.get("machine", ""),
                        node=body.get("node", ""),
                        capabilities=body.get("capabilities", []),
                        role=body.get("role", "worker"))
                    # Token is returned exactly once, here.
                    return self._send(201, {"agent": agent, "token": token})
                if path == "/agents/heartbeat":
                    agent = self._require_agent()
                    if agent is None:
                        return
                    node.registry.heartbeat(agent["id"], body.get("status"))
                    return self._send(200, {"agent": node.registry.get(agent["id"])})
                if path == "/messages":
                    agent = self._require_agent()
                    if agent is None:
                        return
                    if not body.get("recipient") or not body.get("msg_type"):
                        return self._send(400, {"error": "recipient and "
                                                        "msg_type required"})
                    msg_id = bus.send_message(
                        sender=agent["id"], recipient=body["recipient"],
                        msg_type=body["msg_type"],
                        payload=body.get("payload", ""),
                        correlation_id=body.get("correlation_id"),
                        db_path=node.bus_db)
                    return self._send(201, {"id": msg_id})
                if path == "/tasks":
                    agent = self._require_agent()
                    if agent is None:
                        return
                    if not body.get("title"):
                        return self._send(400, {"error": "title required"})
                    task, duplicate = node.tasks.create(
                        title=body["title"], requester=agent["id"],
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
                    return self._send(200 if duplicate else 201,
                                      {"task": task, "duplicate": duplicate})
                if path.startswith("/tasks/") and path.endswith("/transition"):
                    agent = self._require_agent()
                    if agent is None:
                        return
                    try:
                        task = node.tasks.transition(
                            path.split("/")[2], body.get("to", ""),
                            result=body.get("result"))
                    except KeyError as exc:
                        return self._send(404, {"error": str(exc)})
                    except InvalidTransition as exc:
                        return self._send(400, {"error": str(exc)})
                    return self._send(200, {"task": task})
                if path.startswith("/tasks/") and path.endswith("/progress"):
                    agent = self._require_agent()
                    if agent is None:
                        return
                    try:
                        task = node.tasks.add_progress(
                            path.split("/")[2], agent["id"],
                            body.get("note", ""))
                    except KeyError as exc:
                        return self._send(404, {"error": str(exc)})
                    except InvalidTransition as exc:
                        return self._send(400, {"error": str(exc)})
                    return self._send(200, {"task": task})
                if path == "/vault/services":
                    if not node.is_admin(self.headers):
                        return self._send(401, {"error": "admin token required"})
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
                    agent = self._require_agent()
                    if agent is None:
                        return
                    outcome = node.vault.use_check(
                        agent["id"], body.get("service", ""),
                        body.get("action", "read"))
                    code = 403 if outcome == "deny" else 200
                    return self._send(code, {"outcome": outcome})
                return self._send(404, {"error": "not found"})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
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
