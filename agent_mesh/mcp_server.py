"""MCP adapter (v2 Phase 2) — Model Context Protocol server over stdio.

The `mcp` Python package is not installable in this environment
(externally-managed Python), so the minimal MCP protocol is implemented
by hand with stdlib only: newline-delimited JSON-RPC 2.0 on stdin/stdout.

Handled methods: initialize, ping, tools/list, tools/call, plus the
notifications/initialized notification (no reply). Unknown methods get a
JSON-RPC -32601 error. Tool failures are returned as tool results with
isError=true; they never crash the server.

Configuration (environment):
  AGENT_MESH_NODE   base URL of a running mesh node (default :8790)
  AGENT_MESH_TOKEN  this agent's bearer token (from registration / pack)

Run:  python3 -m agent_mesh.mcp_server
"""
import json
import os
import sys
from typing import Any, Callable, Dict, Optional

from .client import MeshClient, MeshError

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "agent-mesh", "version": "2.1.1"}

# Friendly task-update actions -> node lifecycle states (PROTOCOL.md §3).
TASK_ACTIONS = {
    "send": "SENT",
    "accept": "ACCEPTED",
    "reject": "REJECTED",
    "start": "IN_PROGRESS",
    "review": "IN_REVIEW",
    "done": "DONE",
    "fail": "FAILED",
}


def _schema(properties: Dict[str, Any], required=None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        out["required"] = required
    return out


_STR = {"type": "string"}
TOOLS = [
    {
        "name": "mesh_agents_list",
        "description": "List all agents registered on the mesh node "
                       "(id, name, machine, capabilities, role, status).",
        "inputSchema": _schema({}),
    },
    {
        "name": "mesh_send",
        "description": "Send a message to another agent on the mesh.",
        "inputSchema": _schema(
            {"to": _STR, "type": _STR, "text": _STR,
             "payload": {"description": "Alternative structured payload"}},
            required=["to"]),
    },
    {
        "name": "mesh_inbox",
        "description": "List my incoming messages plus tasks assigned to "
                       "me and tasks I requested.",
        "inputSchema": _schema({}),
    },
    {
        "name": "mesh_task_create",
        "description": "Create a task and send it (DRAFT -> SENT). Assign "
                       "to an agent id (assignee) or a capability; an "
                       "assignee starting with 'agt_' is treated as an "
                       "agent id, anything else as a capability. The "
                       "assignee is the LEAD; optional participants "
                       "(agent ids) join as collaborators who can see "
                       "the task, comment on its thread and add progress "
                       "notes — several agents working one task together.",
        "inputSchema": _schema(
            {"title": _STR, "instructions": _STR, "assignee": _STR,
             "assignee_agent": _STR, "assignee_capability": _STR,
             "participants": {"type": "array", "items": _STR,
                              "description": "Collaborator agent ids"},
             "priority": _STR, "idempotency_key": _STR},
            required=["title"]),
    },
    {
        "name": "mesh_task_update",
        "description": "Move a task through its lifecycle. action is one "
                       "of send|accept|reject|start|review|done|fail|"
                       "progress, or a raw target status (SENT, ACCEPTED, "
                       "REJECTED, IN_PROGRESS, IN_REVIEW, DONE, FAILED). "
                       "An optional note is appended as a progress note; "
                       "result is stored when the task completes.",
        "inputSchema": _schema(
            {"task_id": _STR, "action": _STR, "note": _STR,
             "result": _STR},
            required=["task_id"]),
    },
    {
        "name": "mesh_task_list",
        "description": "List tasks, optionally filtered by assignee, "
                       "requester, participant or status. An agent's "
                       "list only contains tasks it is involved in.",
        "inputSchema": _schema(
            {"assignee": _STR, "requester": _STR, "participant": _STR,
             "status": _STR}),
    },
    {
        "name": "mesh_task_get",
        "description": "Get one task in full (status, participants, "
                       "progress notes, the shared comment thread, "
                       "result).",
        "inputSchema": _schema({"task_id": _STR}, required=["task_id"]),
    },
    {
        "name": "mesh_task_comment",
        "description": "Add a comment to a task's shared thread. The "
                       "requester, the lead and any participant may "
                       "comment; the thread is visible to all of them "
                       "and syncs across nodes with the task.",
        "inputSchema": _schema(
            {"task_id": _STR, "text": _STR},
            required=["task_id", "text"]),
    },
    {
        "name": "mesh_use_service",
        "description": "Ask the vault broker whether I may use a shared "
                       "service for an action (read|write|spend). Returns "
                       "the decision only — allow, deny or "
                       "needs_approval. Never returns a secret.",
        "inputSchema": _schema(
            {"service": _STR, "action": _STR}, required=["service"]),
    },
    {
        "name": "mesh_handover_log",
        "description": "File one handover entry for the session just "
                       "worked (mesh handover rules: one independent "
                       "dated entry per session; asked / done / how "
                       "verified / decisions / end state / open items; "
                       "all related files with machine + full path; "
                       "failures recorded plainly; NEVER any credential "
                       "values — locations only). The node stores it and "
                       "updates its HANDOVER-<node>.md export.",
        "inputSchema": _schema(
            {"title": _STR, "asked": _STR, "done": _STR,
             "verified": _STR, "decisions": _STR, "files": _STR,
             "open": _STR},
            required=["title"]),
    },
]


class MeshTools:
    """The mesh tools, backed by a MeshClient for one agent."""

    def __init__(self, client: MeshClient):
        self.client = client
        self._my_id: Optional[str] = None
        self.handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {
            "mesh_agents_list": self.agents_list,
            "mesh_send": self.send,
            "mesh_inbox": self.inbox,
            "mesh_task_create": self.task_create,
            "mesh_task_update": self.task_update,
            "mesh_task_list": self.task_list,
            "mesh_task_get": self.task_get,
            "mesh_task_comment": self.task_comment,
            "mesh_use_service": self.use_service,
            "mesh_handover_log": self.handover_log,
        }

    def my_id(self) -> str:
        if self._my_id is None:
            # Heartbeat returns the caller's own public agent record.
            self._my_id = self.client.heartbeat()["agent"]["id"]
        return self._my_id

    # -- tools ------------------------------------------------------------
    def agents_list(self, args):
        return self.client.list_agents()

    def send(self, args):
        payload = args.get("text", args.get("payload", ""))
        return self.client.send_message(
            args["to"], args.get("type", "INFO"), payload)

    def inbox(self, args):
        me = self.my_id()
        participating = [
            t for t in self.client.list_tasks(participant=me)["tasks"]
            if t.get("assignee_agent") != me and t.get("requester") != me]
        return {
            "agent": me,
            "messages": self.client.list_messages()["messages"],
            "tasks_assigned": self.client.list_tasks(assignee=me)["tasks"],
            "tasks_requested": self.client.list_tasks(requester=me)["tasks"],
            "tasks_participating": participating,
        }

    def task_create(self, args):
        kw: Dict[str, Any] = {"title": args["title"],
                              "instructions": args.get("instructions", "")}
        assignee_agent = args.get("assignee_agent")
        assignee_cap = args.get("assignee_capability")
        generic = args.get("assignee")
        if generic and not assignee_agent and not assignee_cap:
            if generic.startswith("agt_"):
                assignee_agent = generic
            else:
                assignee_cap = generic
        if assignee_agent:
            kw["assignee_agent"] = assignee_agent
        if assignee_cap:
            kw["assignee_capability"] = assignee_cap
        if args.get("participants"):
            kw["participants"] = list(args["participants"])
        if args.get("priority"):
            kw["priority"] = args["priority"]
        if args.get("idempotency_key"):
            kw["idempotency_key"] = args["idempotency_key"]
        out = self.client.create_task(**kw)
        task = out["task"]
        if task["status"] == "DRAFT":
            # A created task is an order: move it to SENT for the assignee.
            out = self.client.transition(task["id"], "SENT")
        return out

    def task_update(self, args):
        task_id = args["task_id"]
        action = (args.get("action") or "").strip()
        note = args.get("note")
        result = args.get("result")
        task = None
        if note and action.lower() != "progress":
            task = self.client.progress(task_id, note)["task"]
        if not action or action.lower() == "progress":
            if task is None:
                if note:
                    task = self.client.progress(task_id, note)["task"]
                else:
                    task = self.client.get_task(task_id)["task"]
            return {"task": task}
        target = TASK_ACTIONS.get(action.lower(), action.upper())
        return self.client.transition(task_id, target, result=result)

    def task_list(self, args):
        return self.client.list_tasks(
            assignee=args.get("assignee"), requester=args.get("requester"),
            participant=args.get("participant"), status=args.get("status"))

    def task_get(self, args):
        return self.client.get_task(args["task_id"])

    def task_comment(self, args):
        return self.client.add_comment(args["task_id"], args["text"])

    def handover_log(self, args):
        kw = {"title": args["title"]}
        for key in ("asked", "done", "verified", "decisions", "files",
                    "open"):
            if args.get(key):
                kw[key] = args[key]
        return self.client.handover_log(**kw)

    def use_service(self, args):
        try:
            return self.client.vault_use_check(
                args["service"], args.get("action", "read"))
        except MeshError as exc:
            if exc.status == 403 and isinstance(exc.body, dict) \
                    and "outcome" in exc.body:
                # A denial is a broker decision, not a tool failure.
                return {"outcome": exc.body["outcome"],
                        "service": args["service"],
                        "action": args.get("action", "read")}
            raise


def _result_text(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


def tool_result(obj: Any, is_error: bool = False) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": _result_text(obj)}],
            "isError": is_error}


class McpServer:
    """Newline-delimited JSON-RPC 2.0 loop for the mesh tools."""

    def __init__(self, tools: MeshTools, instream=None, outstream=None):
        self.tools = tools
        self.instream = instream or sys.stdin
        self.outstream = outstream or sys.stdout

    def _write(self, obj: Dict[str, Any]) -> None:
        self.outstream.write(json.dumps(obj) + "\n")
        self.outstream.flush()

    def _error(self, req_id, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": req_id,
                     "error": {"code": code, "message": message}})

    def handle(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method", "")
        req_id = msg.get("id")
        params = msg.get("params") or {}
        if req_id is None:
            return  # notification (e.g. notifications/initialized): no reply
        if method == "initialize":
            version = params.get("protocolVersion") or PROTOCOL_VERSION
            return self._write({"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO}})
        if method == "ping":
            return self._write({"jsonrpc": "2.0", "id": req_id, "result": {}})
        if method == "tools/list":
            return self._write({"jsonrpc": "2.0", "id": req_id,
                                "result": {"tools": TOOLS}})
        if method == "tools/call":
            return self._write({"jsonrpc": "2.0", "id": req_id,
                                "result": self.call_tool(params)})
        self._error(req_id, -32601, f"Method not found: {method}")

    def call_tool(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name", "")
        args = params.get("arguments") or {}
        handler = self.tools.handlers.get(name)
        if handler is None:
            return tool_result({"error": f"unknown tool: {name}"},
                               is_error=True)
        try:
            return tool_result(handler(args))
        except MeshError as exc:
            return tool_result(
                {"error": f"node returned HTTP {exc.status}",
                 "detail": exc.body}, is_error=True)
        except Exception as exc:  # a tool error must never kill the server
            return tool_result({"error": f"{type(exc).__name__}: {exc}"},
                               is_error=True)

    def serve(self) -> int:
        for line in self.instream:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                self._error(None, -32700, f"Parse error: {exc}")
                continue
            if not isinstance(msg, dict) or "method" not in msg:
                self._error(msg.get("id") if isinstance(msg, dict) else None,
                            -32600, "Invalid Request")
                continue
            try:
                self.handle(msg)
            except Exception as exc:  # keep serving after any one failure
                self._error(msg.get("id"), -32603,
                            f"Internal error: {exc}")
        return 0


def main(argv=None) -> int:
    node_url = os.environ.get("AGENT_MESH_NODE", "http://127.0.0.1:8790")
    token = os.environ.get("AGENT_MESH_TOKEN")
    tools = MeshTools(MeshClient(node_url, token=token))
    return McpServer(tools).serve()


if __name__ == "__main__":
    raise SystemExit(main())
