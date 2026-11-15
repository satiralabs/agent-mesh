"""File-drop adapter (v2 Phase 2) — for agents that can only write files.

Watch a folder (stdlib polling). Drop a .json file in:

    {"kind": "task", "title": "...", "instructions": "...",
     "assignee_agent": "agt_...", "priority": "normal"}
    {"kind": "message", "recipient": "agt_...", "msg_type": "INFO",
     "payload": "hello"}

A task may use "assignee_agent" (agt_...) or "assignee_capability".
Submitted files are moved to <dir>/processed/; anything that fails
(bad JSON, missing fields, node error) is moved to <dir>/failed/ with a
sibling <name>.error.txt explaining why.

Run:  AGENT_MESH_NODE=... AGENT_MESH_TOKEN=... \
        python3 -m agent_mesh.filedrop --dir ~/mesh-drop [--once]
Without --once it polls every --interval seconds (default 2).
"""
import argparse
import json
import os
import shutil
import time
from typing import Any, Dict

from .client import MeshClient, MeshError


def _submit(client: MeshClient, item: Dict[str, Any]) -> Dict[str, Any]:
    kind = item.get("kind")
    if kind == "task":
        if not item.get("title"):
            raise ValueError("task file requires a 'title'")
        kw: Dict[str, Any] = {"title": item["title"],
                              "instructions": item.get("instructions", "")}
        if item.get("assignee_agent"):
            kw["assignee_agent"] = item["assignee_agent"]
        if item.get("assignee_capability"):
            kw["assignee_capability"] = item["assignee_capability"]
        if item.get("priority"):
            kw["priority"] = item["priority"]
        if item.get("idempotency_key"):
            kw["idempotency_key"] = item["idempotency_key"]
        out = client.create_task(**kw)
        if out["task"]["status"] == "DRAFT":
            out = client.transition(out["task"]["id"], "SENT")
        return out
    if kind == "message":
        if not item.get("recipient"):
            raise ValueError("message file requires a 'recipient'")
        return client.send_message(item["recipient"],
                                   item.get("msg_type", "INFO"),
                                   item.get("payload", ""))
    raise ValueError(f"unknown kind {kind!r} (expected 'task' or 'message')")


def _move(src: str, dest_dir: str) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(src))
    shutil.move(src, dest)
    return dest


def process_once(drop_dir: str, client: MeshClient) -> Dict[str, int]:
    """Submit every *.json file currently in drop_dir. Returns counts."""
    counts = {"processed": 0, "failed": 0}
    names = sorted(n for n in os.listdir(drop_dir) if n.endswith(".json"))
    for name in names:
        path = os.path.join(drop_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as fh:
                item = json.load(fh)
            if not isinstance(item, dict):
                raise ValueError("top-level JSON must be an object")
            _submit(client, item)
        except Exception as exc:
            dest = _move(path, os.path.join(drop_dir, "failed"))
            with open(dest + ".error.txt", "w") as fh:
                fh.write(f"{type(exc).__name__}: {exc}\n")
            counts["failed"] += 1
        else:
            _move(path, os.path.join(drop_dir, "processed"))
            counts["processed"] += 1
    return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-mesh-filedrop")
    parser.add_argument("--dir", required=True, help="drop folder to watch")
    parser.add_argument("--once", action="store_true",
                        help="process current files once and exit")
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args(argv)
    os.makedirs(args.dir, exist_ok=True)
    client = MeshClient(os.environ.get("AGENT_MESH_NODE",
                                       "http://127.0.0.1:8790"),
                        token=os.environ.get("AGENT_MESH_TOKEN"))
    if args.once:
        counts = process_once(args.dir, client)
        print(json.dumps(counts))
        return 0
    print(f"watching {args.dir} every {args.interval}s (Ctrl-C to stop)")
    try:
        while True:
            process_once(args.dir, client)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
