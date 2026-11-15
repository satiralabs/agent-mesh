#!/usr/bin/env python3
"""Inbox watcher (v2.1) — the mesh side of inbox notification.

Polls one agent's mesh inbox with that agent's own credentials and,
for every NEW message, appends it to a local file and prints it.
Dedupe is by message id, persisted in a small state file beside the
output file, so restarts never re-report old messages.

Honest scope: this cannot push a message INTO a host application's
live chat — no mesh component can. Live push is the host's job (a
Claude/Codex/Antigravity session sees mesh messages when it checks
its inbox). What the mesh provides is this watcher, the GUI's badges
and auto-refresh, and the audit/activity trail. Run the watcher in
any terminal (or under tmux/systemd) next to the agent it serves:

  AGENT_MESH_NODE=http://127.0.0.1:8790 AGENT_MESH_TOKEN=<agent token> \
    python3 scripts/inbox_watch.py --out ~/mesh-inbox.log

Options: --interval SECONDS (default 15), --once (single poll, for
cron), --state PATH (default: <out>.seen.json).
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from agent_mesh.client import MeshClient, MeshError  # noqa: E402


def load_seen(state_path):
    try:
        with open(state_path) as fh:
            return set(json.load(fh))
    except Exception:
        return set()


def save_seen(state_path, seen):
    tmp = state_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(sorted(seen), fh)
    os.replace(tmp, state_path)


def poll_once(client, out_path, state_path):
    """One poll. Returns the number of NEW messages written."""
    seen = load_seen(state_path)
    messages = client.list_messages()["messages"]
    fresh = [m for m in messages if m["id"] not in seen]
    if fresh:
        with open(out_path, "a", encoding="utf-8") as fh:
            for m in fresh:
                line = json.dumps(m, default=str)
                fh.write(line + "\n")
                print(line, flush=True)
                seen.add(m["id"])
        save_seen(state_path, seen)
    return len(fresh)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="inbox-watch")
    parser.add_argument("--out", required=True,
                        help="file to append new messages to")
    parser.add_argument("--state", default=None,
                        help="dedupe state file (default <out>.seen.json)")
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--once", action="store_true",
                        help="poll once and exit (for cron)")
    args = parser.parse_args(argv)
    node = os.environ.get("AGENT_MESH_NODE", "http://127.0.0.1:8790")
    token = os.environ.get("AGENT_MESH_TOKEN")
    if not token:
        print("AGENT_MESH_TOKEN must be set (the agent's pack token)",
              file=sys.stderr)
        return 2
    state_path = args.state or (args.out + ".seen.json")
    client = MeshClient(node, token=token)
    while True:
        try:
            poll_once(client, args.out, state_path)
        except MeshError as exc:
            print(f"inbox watch: node returned HTTP {exc.status}",
                  file=sys.stderr)
            if args.once:
                return 1
        except Exception as exc:  # node down etc. — keep watching
            print(f"inbox watch: {exc}", file=sys.stderr)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
