"""Connection pack generator (v2 Phase 2).

Registers a new agent on a running node (admin token required) and
writes a ready-to-paste MCP configuration snippet for it:

    {"mcpServers": {"agent-mesh": {
        "command": "python3",
        "args": ["-m", "agent_mesh.mcp_server"],
        "env": {"AGENT_MESH_NODE": ..., "AGENT_MESH_TOKEN": ...,
                "AGENT_MESH_AGENT": ...}}}}

The agent token appears ONLY in the generated pack file, which is
written with 0600 permissions. It is never logged anywhere else.

Run:  AGENT_MESH_ADMIN=<token> python3 -m agent_mesh.pack \
        --node http://127.0.0.1:8790 --name "My Agent" \
        --capabilities coding,research --out ~/agent-mesh-pack.json
"""
import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .client import MeshClient


def build_pack(node_url: str, agent: Dict[str, Any],
               token: str) -> Dict[str, Any]:
    return {
        "mcpServers": {
            "agent-mesh": {
                "command": "python3",
                "args": ["-m", "agent_mesh.mcp_server"],
                "env": {
                    "AGENT_MESH_NODE": node_url,
                    "AGENT_MESH_TOKEN": token,
                    "AGENT_MESH_AGENT": agent["id"],
                },
            }
        }
    }


def write_pack_file(pack: Dict[str, Any], out_path: str) -> str:
    out_path = os.path.expanduser(out_path)
    parent = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(parent, exist_ok=True)
    # O_CREAT with an explicit 0600 mode, then chmod in case the file
    # already existed with looser permissions.
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(pack, fh, indent=2)
        fh.write("\n")
    os.chmod(out_path, 0o600)
    return out_path


def generate_pack(node_url: str, admin_token: str, name: str,
                  capabilities: Optional[List[str]] = None,
                  out_path: str = "agent-mesh-pack.json",
                  machine: str = "", role: str = "worker") -> Dict[str, Any]:
    """Register the agent and write its pack file. Returns the pack."""
    admin = MeshClient(node_url, admin_token=admin_token)
    reg = admin.register(name=name, machine=machine,
                         capabilities=capabilities or [], role=role)
    pack = build_pack(node_url, reg["agent"], reg["token"])
    write_pack_file(pack, out_path)
    return pack


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-mesh-pack")
    parser.add_argument("--node", default=os.environ.get(
        "AGENT_MESH_NODE", "http://127.0.0.1:8790"))
    parser.add_argument("--admin", default=os.environ.get("AGENT_MESH_ADMIN"),
                        help="node admin token (or set AGENT_MESH_ADMIN)")
    parser.add_argument("--name", required=True)
    parser.add_argument("--capabilities", default="",
                        help="comma-separated, e.g. coding,research")
    parser.add_argument("--machine", default="")
    parser.add_argument("--role", default="worker",
                        choices=["owner", "orchestrator", "worker"])
    parser.add_argument("--out", default="agent-mesh-pack.json")
    args = parser.parse_args(argv)
    if not args.admin:
        raise SystemExit("admin token required: --admin or AGENT_MESH_ADMIN")
    caps = [c.strip() for c in args.capabilities.split(",") if c.strip()]
    pack = generate_pack(args.node, args.admin, args.name, caps,
                         args.out, machine=args.machine, role=args.role)
    print(f"connection pack written to {args.out} (0600):")
    print(json.dumps(pack, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
