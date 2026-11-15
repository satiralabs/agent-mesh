# Autonomous Mesh Worker (shipped component)

This directory ships with agent-mesh (since v2.2) — it is the
autonomous worker component, copied from the live deployment where it
serves the mesh agent **Antigravity** (`agt_6cf5363921dea539`) on the
Arch PC. The text below describes that deployment; the mechanics are
generic.

**One instance serves ONE agent identity.** A worker instance is
bound to a single agent (its `agent_id` + connection pack in
`config.json`). Running several autonomous agents means running
several worker instances — one per agent, each with its own config,
state and log directory. There is no multi-agent mode inside one
instance, by design: identities, tokens and trust lists stay separate.

**Workers + participants (v2.2):** a task the worker's agent leads
can name other agents as participants. Participants see the task,
comment in its shared thread and add progress notes while the worker
drives the lifecycle — that is how a worker-led task pulls in
collaborators (another worker's agent, or a human-driven session)
without hand-offs.

## Install (per agent)

```bash
mkdir -p ~/.mesh-worker ~/.config/systemd/user
cp mesh_worker.py ~/.mesh-worker/
cp mesh-worker.service ~/.config/systemd/user/
# write ~/.mesh-worker/config.json (agent_id, pack_path,
# trusted_requesters, workspace, ...) — see mesh_worker.py DEFAULTS
systemctl --user daemon-reload
systemctl --user enable --now mesh-worker
```

For a second agent, repeat with a different home for its files
(config/state/logs) and a unit of its own.

---

`mesh_worker.py` makes the mesh agent **Antigravity**
(`agt_6cf5363921dea539`) work on its own. Nobody has to paste anything
into a chat window: the worker watches the mesh, picks up tasks, runs
them, and reports back.

## What it does

Every 20 seconds the worker asks the local Agent Mesh node
(`http://127.0.0.1:8790`) for tasks assigned to Antigravity in status
`SENT`. For each such task **from a trusted requester** it:

1. Moves the task `SENT → ACCEPTED → IN_PROGRESS`.
2. Runs it headlessly with the Antigravity CLI in `~/mesh-workspace`:
   `agy --print=<preamble + task instructions> --dangerously-skip-permissions`
   (900 s timeout; full output saved to `~/.mesh-worker/logs/<task-id>.log`).
3. Moves the task to `DONE` with agy's output as the result (or `FAILED`
   with the error tail if agy failed or timed out). If agy completed the
   task itself through its own mesh tools, the worker leaves it alone.
4. Sends the requester a mesh message saying the task finished, with a
   short excerpt of the result.

## Safeguards

- **Trusted requesters only** (in `~/.mesh-worker/config.json`):
  Muse-Hatch (`agt_1760d0fed699f3c5`) and `owner` (tasks Susan creates
  with the admin token). Tasks from anyone else are left untouched and
  logged once.
- **One task at a time** — never parallel agy runs.
- **Rate limit:** at most 6 automatic runs per hour; extra tasks wait.
- **Workspace jail:** agy only runs inside `~/mesh-workspace`.
- The worker never deletes anything. Its token comes from the existing
  connection pack `~/.agent-mesh-node/packs/antigravity-pack.json`.

## How Susan pauses / stops it

Any one of these:

```bash
systemctl --user stop mesh-worker        # stop until started again
touch ~/.mesh-worker/PAUSED              # soft-pause (worker stays up, idles)
rm ~/.mesh-worker/PAUSED                 # resume after a soft-pause
```

Restart / status / logs:

```bash
systemctl --user restart mesh-worker
systemctl --user status mesh-worker
tail -f ~/.mesh-worker/worker.log        # worker's own log
ls ~/.mesh-worker/logs/                  # per-task agy output
```

## Files

| Path | What |
|---|---|
| `~/.mesh-worker/mesh_worker.py` | the poller |
| `~/.mesh-worker/config.json` | trusted requesters, timeouts, paths |
| `~/.mesh-worker/state.json` | rate-limit + dedup state (managed by the worker) |
| `~/.mesh-worker/worker.log` | timestamped worker log |
| `~/.mesh-worker/logs/<task-id>.log` | full agy output per task |
| `~/.config/systemd/user/mesh-worker.service` | the systemd --user unit |

The unit sets `DISPLAY` / `DBUS_SESSION_BUS_ADDRESS` / `XDG_RUNTIME_DIR`
explicitly because agy's sign-in lives in Susan's desktop session (the
Antigravity hub process runs there).

## Note on the VS Code agent

This worker serves the **CLI** identity (Antigravity). The separate
Antigravity-VSCode agent only acts while Susan has VS Code open — an
interactive chat session cannot be woken by any tool. Tasks meant to
run unattended should be assigned to Antigravity (CLI).
