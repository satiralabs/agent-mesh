"""CLI shim (v2 Phase 2) — drive the mesh without MCP or curl.

Thin wrapper over MeshClient; every subcommand prints JSON.
Config: AGENT_MESH_NODE (default http://127.0.0.1:8790),
        AGENT_MESH_TOKEN (this agent's token).

Run:  python3 -m agent_mesh.mesh_cli <subcommand> ...
  agents                                   list registered agents
  send --to AGENT_ID [--type INFO] --text "..."
  inbox                                    my messages + my tasks
  task-create --title "..." [--instructions ...] [--assignee ID|capability]
              [--priority normal] [--idempotency-key KEY]
  task-update --id TASK_ID --action accept|start|review|done|...
              [--note ...] [--result ...]
  task-list [--assignee ID] [--requester ID] [--status STATUS]
  use-service --service NAME [--action read]
"""
import argparse
import json
import os
import sys

from .client import MeshClient, MeshError
from .mcp_server import TASK_ACTIONS


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _my_id(client: MeshClient) -> str:
    return client.heartbeat()["agent"]["id"]


def build_client() -> MeshClient:
    return MeshClient(os.environ.get("AGENT_MESH_NODE",
                                     "http://127.0.0.1:8790"),
                      token=os.environ.get("AGENT_MESH_TOKEN"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-mesh")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("agents")

    p = sub.add_parser("send")
    p.add_argument("--to", required=True)
    p.add_argument("--type", default="INFO")
    p.add_argument("--text", default="")

    sub.add_parser("inbox")

    p = sub.add_parser("task-create")
    p.add_argument("--title", required=True)
    p.add_argument("--instructions", default="")
    p.add_argument("--assignee", help="agent id (agt_...) or capability")
    p.add_argument("--priority", default="normal")
    p.add_argument("--idempotency-key")

    p = sub.add_parser("task-update")
    p.add_argument("--id", required=True)
    p.add_argument("--action", required=True,
                   help="send|accept|reject|start|review|done|fail|"
                        "progress, or a raw status")
    p.add_argument("--note")
    p.add_argument("--result")

    p = sub.add_parser("task-list")
    p.add_argument("--assignee")
    p.add_argument("--requester")
    p.add_argument("--status")

    p = sub.add_parser("use-service")
    p.add_argument("--service", required=True)
    p.add_argument("--action", default="read")

    args = parser.parse_args(argv)
    client = build_client()
    try:
        if args.cmd == "agents":
            _out(client.list_agents())
        elif args.cmd == "send":
            _out(client.send_message(args.to, args.type, args.text))
        elif args.cmd == "inbox":
            me = _my_id(client)
            _out({"agent": me,
                  "messages": client.list_messages()["messages"],
                  "tasks_assigned": client.list_tasks(assignee=me)["tasks"],
                  "tasks_requested":
                      client.list_tasks(requester=me)["tasks"]})
        elif args.cmd == "task-create":
            kw = {"title": args.title, "instructions": args.instructions,
                  "priority": args.priority}
            if args.assignee:
                if args.assignee.startswith("agt_"):
                    kw["assignee_agent"] = args.assignee
                else:
                    kw["assignee_capability"] = args.assignee
            if args.idempotency_key:
                kw["idempotency_key"] = args.idempotency_key
            out = client.create_task(**kw)
            if out["task"]["status"] == "DRAFT":
                out = client.transition(out["task"]["id"], "SENT")
            _out(out)
        elif args.cmd == "task-update":
            if args.note and args.action.lower() != "progress":
                client.progress(args.id, args.note)
            if args.action.lower() == "progress":
                _out(client.progress(args.id, args.note or ""))
            else:
                target = TASK_ACTIONS.get(args.action.lower(),
                                          args.action.upper())
                _out(client.transition(args.id, target,
                                       result=args.result))
        elif args.cmd == "task-list":
            _out(client.list_tasks(assignee=args.assignee,
                                   requester=args.requester,
                                   status=args.status))
        elif args.cmd == "use-service":
            try:
                _out(client.vault_use_check(args.service, args.action))
            except MeshError as exc:
                if exc.status == 403 and isinstance(exc.body, dict) \
                        and "outcome" in exc.body:
                    _out({"outcome": exc.body["outcome"],
                          "service": args.service, "action": args.action})
                else:
                    raise
    except MeshError as exc:
        print(json.dumps({"error": f"HTTP {exc.status}",
                          "detail": exc.body}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
