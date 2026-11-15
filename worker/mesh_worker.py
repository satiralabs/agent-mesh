#!/usr/bin/env python3
"""Autonomous Agent Mesh worker for the "Antigravity" mesh agent.

Polls the local Agent Mesh node for tasks assigned to this agent and, for
tasks from TRUSTED requesters only, executes them headlessly with the
Antigravity CLI (`agy --print=...`) — no human relay, no pasted nudges.

Safety rails:
  * one task at a time (this loop is strictly sequential)
  * trusted-requester allowlist (config.json) — anything else is left
    untouched and logged once
  * pause file: ~/.mesh-worker/PAUSED  -> poller idles
  * rate limit: max_runs_per_hour (default 6) agy runs per rolling hour
  * agy only ever runs inside the configured workspace directory
  * the worker never deletes anything

Stdlib only. Config: ~/.mesh-worker/config.json (see README.md).
State:  ~/.mesh-worker/state.json   Logs: ~/.mesh-worker/worker.log
Per-task agy output: ~/.mesh-worker/logs/<task_id>.log
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, ".mesh-worker")
CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state.json")
LOG_PATH = os.path.join(BASE, "worker.log")
PAUSE_PATH = os.path.join(BASE, "PAUSED")
TASK_LOG_DIR = os.path.join(BASE, "logs")

DEFAULTS = {
    "node_url": "http://127.0.0.1:8790",
    "agent_id": "agt_6cf5363921dea539",
    "pack_path": os.path.join(HOME, ".agent-mesh-node", "packs",
                              "antigravity-pack.json"),
    # "owner" = tasks created with the node admin token (Susan herself).
    "trusted_requesters": ["agt_1760d0fed699f3c5", "owner"],
    "workspace": os.path.join(HOME, "mesh-workspace"),
    "agy_path": os.path.join(HOME, ".gemini", "bin", "agy"),
    "poll_interval_sec": 20,
    "task_timeout_sec": 900,
    "max_runs_per_hour": 6,
    "result_max_chars": 8000,
}


def log(msg):
    line = "%s [mesh-worker] %s" % (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except FileNotFoundError:
        log("no config.json found, using built-in defaults")
    except (OSError, ValueError) as exc:
        log("config.json unreadable (%s), using built-in defaults" % exc)
    return cfg


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"runs": [], "logged_untrusted": []}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_PATH)


def read_pack_token(pack_path):
    with open(pack_path, encoding="utf-8") as fh:
        pack = json.load(fh)
    env = pack["mcpServers"]["agent-mesh"]["env"]
    return env["AGENT_MESH_TOKEN"], env.get("AGENT_MESH_NODE")


class Mesh:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token, pack_node = read_pack_token(cfg["pack_path"])
        self.base = (cfg.get("node_url") or pack_node or
                     "http://127.0.0.1:8790").rstrip("/")

    def call(self, method, path, body=None):
        data = None
        headers = {"Authorization": "Bearer " + self.token}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"error": raw[:500]}

    def heartbeat(self):
        self.call("POST", "/agents/heartbeat", {})

    def sent_tasks(self):
        status, data = self.call(
            "GET", "/tasks?assignee=%s&status=SENT" % self.cfg["agent_id"])
        if status != 200:
            raise RuntimeError("task list failed: HTTP %s %s" %
                               (status, data.get("error", "")))
        tasks = data.get("tasks", [])
        tasks.sort(key=lambda t: t.get("created_at") or 0)
        return tasks

    def get_task(self, task_id):
        status, data = self.call("GET", "/tasks/" + task_id)
        if status != 200:
            return None
        return data.get("task")

    def transition(self, task_id, to, result=None):
        body = {"to": to}
        if result is not None:
            body["result"] = result
        return self.call("POST", "/tasks/%s/transition" % task_id, body)

    def send_message(self, recipient, payload):
        return self.call("POST", "/messages",
                         {"recipient": recipient, "payload": payload,
                          "msg_type": "INFO"})


PREAMBLE = """You are Antigravity, an agent on the Agent Mesh network \
(agent id agt_6cf5363921dea539, node arch-pc). This task was placed on \
the mesh by {requester} and is being run automatically by the mesh \
worker — no human is watching this session, so work autonomously and \
finish the job in this one run.

Rules for this run:
- Follow the task instructions below exactly. If they say read-only, \
change nothing on the system.
- You have agent-mesh tools (mesh_task_list, mesh_send, mesh_inbox, \
etc.) if you need to communicate; the requester is {requester}.
- Your final printed output is recorded as the task result, so end \
with the complete deliverable the task asks for (report, answer, or \
summary of what you did), in plain text.

TASK TITLE: {title}

TASK INSTRUCTIONS:
{instructions}
"""


def run_task(mesh, cfg, state, task):
    tid = task["id"]
    title = task.get("title") or "(untitled)"
    requester = task.get("requester") or ""
    log("starting task %s %r (requester %s)" % (tid, title, requester))

    status, data = mesh.transition(tid, "ACCEPTED")
    if status != 200:
        log("task %s: ACCEPT failed HTTP %s %s" %
            (tid, status, data.get("error", "")))
        return
    status, data = mesh.transition(tid, "IN_PROGRESS")
    if status != 200:
        log("task %s: IN_PROGRESS failed HTTP %s %s" %
            (tid, status, data.get("error", "")))
        return

    state["runs"] = [t for t in state.get("runs", [])
                     if t > time.time() - 3600]
    state["runs"].append(time.time())
    save_state(state)

    prompt = PREAMBLE.format(requester=requester, title=title,
                             instructions=task.get("instructions") or "")
    os.makedirs(cfg["workspace"], exist_ok=True)
    os.makedirs(TASK_LOG_DIR, exist_ok=True)
    task_log = os.path.join(TASK_LOG_DIR, tid + ".log")

    timed_out = False
    exit_code = None
    output = ""
    started = time.time()
    try:
        proc = subprocess.run(
            [cfg["agy_path"], "--print=" + prompt,
             "--dangerously-skip-permissions",
             "--add-dir", cfg["workspace"]],
            cwd=cfg["workspace"], capture_output=True, text=True,
            timeout=cfg["task_timeout_sec"])
        exit_code = proc.returncode
        output = (proc.stdout or "") + (
            "\n[stderr]\n" + proc.stderr if proc.stderr else "")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        output = out + ("\n[stderr]\n" + err if err else "")
    except OSError as exc:
        output = "worker could not launch agy: %s" % exc

    elapsed = int(time.time() - started)
    with open(task_log, "w", encoding="utf-8") as fh:
        fh.write("task %s %r\nexit=%s timeout=%s elapsed=%ss\n\n%s" %
                 (tid, title, exit_code, timed_out, elapsed, output))
    log("task %s: agy finished exit=%s timeout=%s elapsed=%ss log=%s" %
        (tid, exit_code, timed_out, elapsed, task_log))

    failed = timed_out or exit_code is None or exit_code != 0
    tail = output.strip()[-cfg["result_max_chars"]:]
    if failed:
        reason = ("timed out after %ss" % cfg["task_timeout_sec"]
                  if timed_out else "agy exit code %s" % exit_code)
        result = ("[autonomous worker: agy run FAILED (%s)]\n\n%s\n\n"
                  "[full log: %s]" % (reason, tail, task_log))
    else:
        result = tail

    # agy may have moved/completed the task itself via its mesh tools.
    fresh = mesh.get_task(tid)
    cur = (fresh or {}).get("status")
    if cur in ("DONE", "FAILED"):
        log("task %s: agy already set status %s; leaving as-is" %
            (tid, cur))
        outcome = cur
    elif cur in ("IN_PROGRESS", "IN_REVIEW"):
        if cur == "IN_PROGRESS":
            mesh.transition(tid, "IN_REVIEW")
        target = "FAILED" if failed else "DONE"
        status, data = mesh.transition(tid, target, result=result)
        if status == 400 and "scanner" in str(data.get("error", "")):
            log("task %s: result blocked by secret scanner; retrying "
                "with a pointer to the log file" % tid)
            safe = ("[result withheld: the secret scanner blocked the "
                    "raw output; full agy output is in the worker log "
                    "at %s]" % task_log)
            status, data = mesh.transition(tid, target, result=safe)
        if status != 200:
            log("task %s: final transition to %s failed HTTP %s %s" %
                (tid, target, status, data.get("error", "")))
            outcome = cur
        else:
            outcome = target
    else:
        log("task %s: unexpected status %r after run; not touching" %
            (tid, cur))
        outcome = str(cur)

    # Notify the requester (the "owner" pseudo-agent has no inbox).
    if requester and requester != "owner":
        excerpt = tail[:600]
        note = ("Task %r %s by Antigravity (autonomous mesh worker).\n\n"
                "%s" % (title, outcome.lower(), excerpt))
        mstatus, _ = mesh.send_message(requester, note)
        log("task %s: requester notified (HTTP %s)" % (tid, mstatus))
    log("task %s finished with status %s" % (tid, outcome))


def main():
    os.makedirs(BASE, exist_ok=True)
    os.makedirs(TASK_LOG_DIR, exist_ok=True)
    cfg = load_config()
    state = load_state()
    try:
        mesh = Mesh(cfg)
    except (OSError, ValueError, KeyError) as exc:
        log("FATAL: cannot read agent pack %s: %s" %
            (cfg["pack_path"], exc))
        return 1
    log("worker up: agent %s on %s, workspace %s, trusted %s" %
        (cfg["agent_id"], mesh.base, cfg["workspace"],
         ",".join(cfg["trusted_requesters"])))

    paused_logged = False
    while True:
        try:
            if os.path.exists(PAUSE_PATH):
                if not paused_logged:
                    log("PAUSED file present — idling")
                    paused_logged = True
                time.sleep(cfg["poll_interval_sec"])
                continue
            paused_logged = False

            try:
                mesh.heartbeat()
            except Exception as exc:  # node briefly down etc.
                log("heartbeat failed: %s" % exc)

            tasks = mesh.sent_tasks()
            now = time.time()
            state["runs"] = [t for t in state.get("runs", [])
                             if t > now - 3600]
            for task in tasks:
                requester = task.get("requester") or ""
                if requester not in cfg["trusted_requesters"]:
                    key = task["id"]
                    if key not in state["logged_untrusted"]:
                        state["logged_untrusted"].append(key)
                        save_state(state)
                        log("ignoring task %s %r: requester %s is not "
                            "trusted" % (key, task.get("title"),
                                         requester))
                    continue
                if len(state["runs"]) >= cfg["max_runs_per_hour"]:
                    log("rate limit reached (%s runs/hour); task %s "
                        "waits" % (cfg["max_runs_per_hour"], task["id"]))
                    break
                run_task(mesh, cfg, state, task)
                break  # re-poll after each task; strictly one at a time
        except Exception as exc:
            log("poll error: %s" % exc)
        time.sleep(cfg["poll_interval_sec"])


if __name__ == "__main__":
    sys.exit(main())
