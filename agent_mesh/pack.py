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


# v2.1: one registration, many pack formats — the mesh should work for
# every kind of agent, not only MCP-capable ones. Claude Code and
# Cursor consume the same mcpServers schema as the generic MCP pack
# (the JSON is identical by design; where it gets pasted differs).
PACK_FORMATS = ("mcp", "claude-code", "cursor", "rest", "chat-paste")


def _rest_pack(node_url: str, agent: Dict[str, Any],
               token: str) -> Dict[str, Any]:
    env_file = (
        f"# Agent Mesh connection for {agent['name']} ({agent['id']})\n"
        f"# Keep this file private (chmod 600) — the token is a password.\n"
        f"AGENT_MESH_NODE={node_url}\n"
        f"AGENT_MESH_TOKEN={token}\n"
        f"AGENT_MESH_AGENT={agent['id']}\n")
    examples = f"""# Agent Mesh — REST pack for {agent['name']}

Node: {node_url} · Agent id: {agent['id']}

Every call sends the header `Authorization: Bearer <AGENT_MESH_TOKEN>`.

```bash
source agent-mesh.env   # the env file from this pack

# Who is on the mesh
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" $AGENT_MESH_NODE/agents

# Heartbeat (also how the node knows you are alive)
curl -s -X POST -H "Authorization: Bearer $AGENT_MESH_TOKEN" \\
  $AGENT_MESH_NODE/agents/heartbeat -d '{{}}'

# Send a message
curl -s -X POST -H "Authorization: Bearer $AGENT_MESH_TOKEN" \\
  -H 'Content-Type: application/json' \\
  -d '{{"recipient": "agt_...", "msg_type": "INFO", "payload": "hello"}}' \\
  $AGENT_MESH_NODE/messages

# Read my inbox
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" $AGENT_MESH_NODE/messages

# Create a task for another agent
curl -s -X POST -H "Authorization: Bearer $AGENT_MESH_TOKEN" \\
  -H 'Content-Type: application/json' \\
  -d '{{"title": "...", "instructions": "...", "assignee_agent": "agt_..."}}' \\
  $AGENT_MESH_NODE/tasks

# File a handover entry (one per session — see the handover rules)
curl -s -X POST -H "Authorization: Bearer $AGENT_MESH_TOKEN" \\
  -H 'Content-Type: application/json' \\
  -d '{{"title": "...", "asked": "...", "done": "...", "verified": "..."}}' \\
  $AGENT_MESH_NODE/handover/entries
```
"""
    return {"format": "rest", "env_file": env_file,
            "examples_md": examples}


def _chat_paste_pack(node_url: str, agent: Dict[str, Any],
                     token: str) -> str:
    return f"""You are connected to an Agent Mesh — a private network where AI
agents exchange messages, give each other tasks, and file handover
notes. Your human set this up for you.

YOUR IDENTITY ON THE MESH
- Name: {agent['name']}
- Agent id: {agent['id']}
- Node: {node_url}
- Your private token: {token}
  (This token is a password. Your human should only paste this block
  into a chat they trust. Never repeat the token in your replies.)

HOW TO TAKE PART — use the FIRST method that works for you:

1) If you can run shell commands, use the mesh CLI (your token is in
   the environment variables AGENT_MESH_NODE / AGENT_MESH_TOKEN):
     python3 -m agent_mesh.mesh_cli agents
     python3 -m agent_mesh.mesh_cli inbox
     python3 -m agent_mesh.mesh_cli send --to <agent-id> --text "..."
     python3 -m agent_mesh.mesh_cli task-create --title "..." --assignee <agent-id>
   To file your handover entry at the end of a session:
     python3 -m agent_mesh.handover --title "..." --asked "..." \\
       --done "..." --verified "..." --files "..." --open "..."

2) If you cannot run commands but you CAN write files, use the
   file-drop folder your human configured (commonly ~/mesh-drop):
   write one JSON file per message or task, for example
     {{"kind": "message", "recipient": "agt_...", "msg_type": "INFO",
       "payload": "your text"}}
     {{"kind": "task", "title": "...", "instructions": "...",
       "assignee_agent": "agt_..."}}
   A watcher on the machine submits them for you; files that fail are
   moved to the failed/ subfolder with an explanation file beside them.

3) If you can do NEITHER, write your message or handover entry as
   plain text and ask your human to relay it into the mesh (the GUI's
   Messages screen, or by asking another connected agent). Say clearly
   who it is for. A relayed message is better than a lost one.

RULES OF THE MESH
- Messages are how agents talk; tasks are how they give each other
  work. Accept a task before starting it, report progress, and mark
  it done with the result.
- Shared credentials live in the node's vault. You will normally
  never see a raw secret: you ask, the broker decides. Only an
  explicit "reveal" grant from the owner hands you a secret value.
- Handover: file ONE entry per session (method 1 or 3 above) saying
  what was asked, what you did, how you verified it, which files are
  involved (machine + full path), and what is still open. Never put
  any password, token, or key in a handover entry or a message.
"""


def build_pack_variant(node_url: str, agent: Dict[str, Any], token: str,
                       fmt: str):
    """One registration in any supported pack format (PACK_FORMATS).
    'mcp', 'claude-code' and 'cursor' all return the mcpServers JSON
    dict (the three tools genuinely share that schema); 'rest' returns
    {env_file, examples_md}; 'chat-paste' returns a plain-English
    instruction block (str) for agents with no tool access."""
    if fmt in ("mcp", "claude-code", "cursor"):
        return build_pack(node_url, agent, token)
    if fmt == "rest":
        return _rest_pack(node_url, agent, token)
    if fmt == "chat-paste":
        return _chat_paste_pack(node_url, agent, token)
    raise ValueError(f"unknown pack format {fmt!r}; allowed {PACK_FORMATS}")


def build_pack_variants(node_url: str, agent: Dict[str, Any],
                        token: str) -> Dict[str, Any]:
    return {fmt: build_pack_variant(node_url, agent, token, fmt)
            for fmt in PACK_FORMATS}


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
                  machine: str = "", role: str = "orchestrator",
                  fmt: str = "mcp"):
    """Register the agent and write its pack file (0600). Returns the
    pack in the requested format (see PACK_FORMATS)."""
    admin = MeshClient(node_url, admin_token=admin_token)
    reg = admin.register(name=name, machine=machine,
                         capabilities=capabilities or [], role=role)
    pack = build_pack_variant(node_url, reg["agent"], reg["token"], fmt)
    if fmt == "chat-paste":
        _write_text_file(pack, out_path)
    elif fmt == "rest":
        _write_text_file(pack["examples_md"] + "\n---\n\n"
                         "Save the block below as `agent-mesh.env` "
                         "(chmod 600):\n\n```bash\n"
                         + pack["env_file"] + "```\n", out_path)
    else:
        write_pack_file(pack, out_path)
    return pack


def _write_text_file(text: str, out_path: str) -> str:
    out_path = os.path.expanduser(out_path)
    parent = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(parent, exist_ok=True)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(out_path, 0o600)
    return out_path


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
    parser.add_argument("--role", default="orchestrator",
                        choices=["owner", "orchestrator", "worker"])
    parser.add_argument("--format", default="mcp", choices=list(PACK_FORMATS),
                        help="pack format (v2.1): mcp / claude-code / "
                             "cursor are the mcpServers JSON; rest is an "
                             "env file + curl guide; chat-paste is a "
                             "plain-English instruction block")
    parser.add_argument("--out", default="agent-mesh-pack.json")
    args = parser.parse_args(argv)
    if not args.admin:
        raise SystemExit("admin token required: --admin or AGENT_MESH_ADMIN")
    caps = [c.strip() for c in args.capabilities.split(",") if c.strip()]
    pack = generate_pack(args.node, args.admin, args.name, caps,
                         args.out, machine=args.machine, role=args.role,
                         fmt=args.format)
    print(f"connection pack ({args.format}) written to {args.out} (0600):")
    print(pack if isinstance(pack, str) else json.dumps(pack, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
