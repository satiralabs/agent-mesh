# agent-mesh — v2.0

**Agent Mesh connects your AI agents — on one machine or several —
into one mesh.** Agents register with an identity and capabilities,
exchange messages, and *order each other tasks* under a permission
matrix (owner → orchestrator → worker). Shared account credentials
and APIs live in an encrypted vault: an agent asks the broker, the
broker decides (allow / deny / needs your approval), and **no agent
ever sees a raw secret**. Everything is driven from a GUI — a web
control centre served by the node, with a Tauri desktop scaffold —
and every security-relevant event lands in an audit log.

Cross-platform by construction: the node is pure Python 3.11+
(stdlib-first; the `cryptography` package is used when present, with
a documented OpenSSL fallback), so the same code runs on Windows,
macOS and Linux. See [PROTOCOL.md](PROTOCOL.md) for the spec,
[CHANGELOG.md](CHANGELOG.md) for how v2 was built phase by phase,
and [SECURITY-REVIEW.md](SECURITY-REVIEW.md) for the Phase 5 audit,
including what v2.0 deliberately does **not** do.

## Quickstart

### 1. Install

```bash
git clone https://github.com/satiralabs/agent-mesh.git
cd agent-mesh
pip install .            # or just run with PYTHONPATH=. from the repo root
```

### 2. Start a node

```bash
export AGENT_MESH_ADMIN='choose-a-node-admin-token'      # you, the owner
export AGENT_MESH_MASTER='choose-a-vault-passphrase'     # encrypts the vault
python3 -m agent_mesh.node --port 8790 --data-dir ~/.agent-mesh-node
```

### 3. Open the GUI

Open **http://127.0.0.1:8790/** and enter the admin token. From here
everything is clicks: add vault credentials, set who may use them,
create tasks, approve gated uses, watch activity. (Details: the
Phase 3 section below.)

### 4. Connect an agent

In the GUI: **Agents → add an agent** — you get a *connection pack*
(a ready-to-paste MCP config; the agent's token exists only inside
it). Paste it into the agent tool's MCP configuration, or generate
one from the terminal:

```bash
export AGENT_MESH_ADMIN='your-node-admin-token'
python3 -m agent_mesh.pack --node http://127.0.0.1:8790 \
  --name "My Agent" --capabilities coding,research \
  --out ~/agent-mesh-pack.json
```

Shell-only agents use the CLI shim, file-only agents the file-drop
folder (Phase 2 section below).

### 5. Pair a second machine

Run a node on the second machine too (steps 1-2, its own admin token
and data dir; the machines must reach each other — same LAN or
Tailscale). Then, in the GUI on both sides under **Settings → Peers**:

1. On machine B: **Generate a pairing code** (shown once, 15 minutes).
2. On machine A: enter B's node URL + the code, click **Pair**.
3. Done — each GUI now lists the other machine's agents as *remote*,
   and ordering one works exactly like ordering a local agent,
   including while it is offline (deliveries queue and land once).

Full detail, the ordering matrix, and the vault model are in
[PROTOCOL.md](PROTOCOL.md). Run the whole test suite (60 tests,
stdlib unittest) with:

```bash
python3 -m unittest discover -s tests
```

---

## v1 (still included, unchanged)

An ultra-lightweight, zero-cost, secure agent communication toolkit, handover synchronization engine, and zero-trust secret broker.

Built entirely with standard library tools (`python3`, `sqlite3`). Requires **no pip packages**, **no external daemons**, and consumes **< 15 MB RAM**.

---

## Capabilities

1. **Agent Message Bus (`agent-mesh msg`)**: SQLite WAL-backed inter-agent mailbox with pub/sub and ACK.
2. **Synchronized Handover (`agent-mesh handover`)**: Compiles 4-tier memory packs (like Muse) and syncs to Google Drive.
3. **Zero-Trust Secret Broker (`agent-mesh secret`, `agent-mesh broker`)**: Injects real API keys/tokens into upstream requests via a local reverse proxy. Agents never see or handle raw secret strings.
4. **Human-in-the-Loop Approval Gate**: Configurable per-service gating (e.g., wallet spending requires explicit confirmation before execution).
5. **Immutable Audit Trail**: Logs every proxied request with sanitized headers to `~/.agent-mesh/audit.log`.

---

## Quick Reference Commands

### 1. Inter-Agent Communication
```bash
# Send a message to another agent
agent-mesh msg send --from muse --to successor --type HANDOVER_OFFER --text '{"version":"1.0","state_hash":"98fbc1"}'

# List unread messages for an agent
agent-mesh msg list --to successor

# Acknowledge a processed message
agent-mesh msg ack --msg-id 1

# Stream/listen for new messages live
agent-mesh msg listen --to successor
```

### 2. Secret Management (0600 Permissions)
```bash
# Register an API key (e.g., Shipd)
agent-mesh secret set shipd --upstream "https://api.shipd.com/v1" --token "YOUR_KEY" --type bearer

# Register a sensitive spend endpoint requiring human approval
agent-mesh secret set reseller_wallet --upstream "https://supplier.example/api" --token "WALLET_KEY" --type bearer --approval

# List protected services (tokens are automatically masked)
agent-mesh secret list
```

### 3. Running the Secret Broker Proxy
```bash
# Start the broker proxy on localhost:8088
agent-mesh broker start --port 8088

# Approve a pending gated request
agent-mesh broker approve <REQUEST_ID>
```

### 4. Compiling & Synchronizing Handovers
```bash
# Compile local memory into a handover pack
agent-mesh handover compile --memory-dir ~/.agent-mesh/memory --out-dir ~/.agent-mesh/handover-pack

# Compile and sync to Google Drive
agent-mesh handover sync

# Run continuous 5-minute sync loop
agent-mesh handover watch --interval 300
```

### 5. Check System & Service Health
```bash
agent-mesh status
```

---

## v2 Phase 1 — core engine (branch `v2-phase1`)

What exists now, on top of (and without changing) the v1 modules above.
Full spec: [PROTOCOL.md](PROTOCOL.md).

- **Agent registry** (`agent_mesh/registry.py`) — agents with id, name,
  machine/node, capabilities, role (`owner|orchestrator|worker`), status
  and heartbeat/last-seen. Auth tokens are shown once at registration and
  stored only as SHA-256 hashes.
- **Task store** (`agent_mesh/tasks.py`) — SQLite tasks with a strict
  lifecycle `DRAFT -> SENT -> ACCEPTED/REJECTED -> IN_PROGRESS ->
  IN_REVIEW -> DONE/FAILED`, progress notes, results, named-agent or
  capability assignment, priority, budgets, subtask parent, dependencies
  (a blocked task cannot be accepted/started until its dependency is
  DONE), and idempotency keys (a duplicate submission returns the
  existing task).
- **Encrypted vault v2** (`agent_mesh/vault.py`) — credentials encrypted
  at rest with a per-service ACL (which agents, which of
  `read/write/spend`) and a `requires_approval` flag. The broker check
  returns `allow` / `deny` / `needs_approval` and writes a sanitised
  audit line — the secret itself never appears on disk in plaintext, in
  logs, or in any API response. Crypto: Fernet (PBKDF2-HMAC-SHA256,
  per-store salt) when the `cryptography` package is installed;
  otherwise a documented fallback using the OpenSSL CLI
  (AES-256-CBC encrypt-then-MAC). Master passphrase from
  `AGENT_MESH_MASTER` or a `0600` key file (`AGENT_MESH_MASTER_FILE`).
- **Node server** (`agent_mesh/node.py`) — stdlib REST API for all of
  the above. Agent calls need `Authorization: Bearer <token>` (401 if
  missing/wrong, 403 on ACL denial); registration and vault writes need
  the node admin token (`X-Admin-Token: $AGENT_MESH_ADMIN`).
- **Client** (`agent_mesh/client.py`) — `MeshClient`, so agents can
  drive the whole flow from Python without curl.

### Run the node

```bash
export AGENT_MESH_ADMIN='choose-a-node-admin-token'
export AGENT_MESH_MASTER='choose-a-master-passphrase'
python3 -m agent_mesh.node --port 8790 --data-dir ~/.agent-mesh-node
```

### Run the tests

```bash
python3 -m unittest discover -s tests -v   # Phase 1 + Phase 2, stdlib unittest
```

Not in Phase 1 (by design): GUI, MCP adapter (Phase 2), node-to-node /
Tailscale transport (Phase 4).

---

## v2 Phase 2 — adapters: how an agent connects

Phase 2 gives every agent four ways into the same node. Start a node
first (see Phase 1 above), then pick one:

### 1. Connection pack (do this first — it registers the agent)

```bash
export AGENT_MESH_ADMIN='your-node-admin-token'
python3 -m agent_mesh.pack --node http://127.0.0.1:8790 \
  --name "My Agent" --capabilities coding,research \
  --out ~/agent-mesh-pack.json
```

This registers the agent and writes a ready-to-paste MCP config
(`mcpServers` snippet) with file permissions `0600`. **The pack file is
the only place the agent's token ever appears** — treat it like a
password. Paste the snippet into your agent tool's MCP configuration
(Claude-style `mcpServers` JSON), and the agent gets the mesh tools
below on its next start.

### 2. MCP server (`agent_mesh/mcp_server.py`)

A Model Context Protocol server over stdio (newline-delimited JSON-RPC
2.0). The `mcp` Python package is not installable in every environment,
so the minimal protocol (`initialize`, `ping`, `tools/list`,
`tools/call`) is implemented by hand with stdlib only. It reads
`AGENT_MESH_NODE` and `AGENT_MESH_TOKEN` from its environment — exactly
what the connection pack sets. Tools:

| Tool | What it does |
|---|---|
| `mesh_agents_list` | List registered agents |
| `mesh_send` | Send a message to another agent |
| `mesh_inbox` | My messages + tasks assigned to me / requested by me |
| `mesh_task_create` | Create a task and send it to an agent or capability |
| `mesh_task_update` | accept / start / review / done / fail / progress a task |
| `mesh_task_list` / `mesh_task_get` | List / inspect tasks |
| `mesh_use_service` | Broker decision for a vault service: `allow` / `deny` / `needs_approval` — never a secret |

Tool errors come back as MCP results with `isError: true`; the server
keeps running.

### 3. CLI shim (`agent_mesh/mesh_cli.py`)

For agents and scripts that can run a shell but not MCP:

```bash
export AGENT_MESH_NODE=http://127.0.0.1:8790
export AGENT_MESH_TOKEN='<agent token from the pack>'
python3 -m agent_mesh.mesh_cli agents
python3 -m agent_mesh.mesh_cli task-create --title "Check the logs" \
  --assignee agt_0123abcd4567ef89
python3 -m agent_mesh.mesh_cli inbox
python3 -m agent_mesh.mesh_cli task-update --id tsk_... --action accept
python3 -m agent_mesh.mesh_cli task-list
python3 -m agent_mesh.mesh_cli use-service --service shipd --action read
```

Every subcommand prints JSON.

### 4. File-drop (`agent_mesh/filedrop.py`)

For agents that can only write files. Drop a `.json` file in a watched
folder:

```json
{"kind": "task", "title": "Nightly check", "assignee_agent": "agt_..."}
{"kind": "message", "recipient": "agt_...", "msg_type": "INFO", "payload": "hi"}
```

```bash
python3 -m agent_mesh.filedrop --dir ~/mesh-drop          # watch (polls)
python3 -m agent_mesh.filedrop --dir ~/mesh-drop --once   # one pass
```

Submitted files move to `processed/`; bad files move to `failed/` with
a `<name>.error.txt` explaining why.

Phase 2 tests (`tests/test_phase2.py`) drive the whole pilot flow —
task create → accept → progress → review → done, plus messages and a
vault decision — through two real MCP server subprocesses, and cover
the file-drop adapter, the CLI shim and the pack generator, including a
scan proving no secret appears in any MCP response.

## v2 Phase 3 — the GUI: no terminal needed

Phase 3 turns the node into a fully GUI tool, and adds the approvals
queue that Phase 1 deferred.

### Run it (the only terminal step left — starting the node)

```bash
export AGENT_MESH_ADMIN='your-node-admin-token'
export AGENT_MESH_MASTER='your-vault-master-passphrase'
python3 -m agent_mesh.node --port 8790
```

Then open **http://127.0.0.1:8790/** in a browser and enter the admin
token. The token is kept only in the browser tab's session storage —
never saved to disk, never in the page. Everything after this is
clicks. (The v1 broker page is still served, at `/v1`.)

### The 8 screens

| Screen | What it does |
|---|---|
| **Dashboard** | Agents online / busy / offline, open tasks, pending approvals, tasks by status, recent activity |
| **Agents** | Who is registered, their status, capabilities, role and last-seen. Add an agent and get its **connection pack** (the MCP config snippet, with a copy button). Making a new pack for an existing agent rotates its token — the old one stops working |
| **Tasks** | Create and send a task to an agent or a capability; board grouped by status; click a task for its progress notes and result, add notes, and use the owner actions (approve a review → Done, Fail) |
| **Messages** | Send a message as owner to any agent; read recent traffic across the mesh |
| **Vault** | Add a credential (write-only — it is encrypted by the node and never shown again), set **who may use it** (per agent: read / write / spend) and whether every use needs approval, test a use as a chosen agent, delete a service |
| **Approvals** | When an agent tries to use a gated service, its request waits here. **Approve once** lets exactly that one use through — the next use asks again. Deny grants nothing. Decided history below |
| **Activity** | The audit trail: vault use-checks, approval requests / decisions / consumption, and task events. Secrets never appear here |
| **Settings** | Node info, lock (forget the token on this tab), and Peers — pair other machines into your mesh (Phase 4, below) |

### Approvals, in the API

`POST /vault/use-check` for a gated service now also creates a pending
approval (`approval_id` in the reply). `GET /approvals` (admin) lists
them, `POST /approvals/{id}/decide` with `{"decision": "approve"|"deny"}`
decides, and `GET /activity` returns recent audit entries as JSON. An
approval is consumed by the first allowed use — it is not a standing
permission.

### Desktop app (Tauri)

A Tauri 2 scaffold that spawns this node and opens this same GUI in a
desktop window lives in **`desktop/`** — see
[`desktop/DESKTOP.md`](desktop/DESKTOP.md) for prerequisites and the
exact build commands per OS. It is a scaffold: it has **not** been
compiled yet (the Phase 3 build machine has no Rust toolchain).

Phase 3 tests (`tests/test_phase3.py`) cover GUI serving, the full
approval flow (deny grants nothing; approve grants exactly one use),
the activity endpoint, the vault admin endpoints and the pack endpoint,
with the dummy secret scanned out of every response.

## v2 Phase 4 — node-to-node: pairing, transport, offline queue

Phase 4 turns separate nodes into one mesh. Each machine runs its own
node with its own persistent identity (`node.json` in its data dir);
paired nodes sync their agent directories, exchange tasks and messages,
and queue deliveries while a peer is offline.

### Pair two machines (GUI, Settings → Peers)

1. Start the node on both machines (Phase 3 instructions). Note each
   node's URL as shown in **Settings → Peers** ("Node URL") — on
   Tailscale this is the machine's `100.x.y.z` address, port 8790.
2. On machine B: **Settings → Peers → Generate a pairing code.** The
   code is shown once, works once, and expires in 15 minutes.
3. On machine A: enter B's node URL and the code in the pair form and
   click **Pair with that machine**.
4. Both machines now list each other under "Paired machines", and each
   node's **Agents** screen shows the other machine's agents marked
   *remote*. From then on, ordering a remote agent works exactly like
   ordering a local one — the task is forwarded, worked on its home
   node, and its status, progress and result flow back.

Pairing can equally be driven from the API (`POST /peers/pairing-code`,
`POST /peers/pair`, admin token on your own node only).

### Security model (5 lines)

- Pairing is authenticated by the one-time code; during pairing the two
  nodes establish a shared per-peer secret, stored only in each node's
  own data dir — never logged, never returned by any list endpoint.
- Every node-to-node request is signed: HMAC-SHA256 over
  timestamp + method + path + body, with a 5-minute replay window.
- Unknown nodes, bad signatures and stale timestamps are rejected.
- A peer credential works **only** on `/peer/*` endpoints — it can
  never call agent or admin endpoints (and vice versa).
- HMAC gives authentication and integrity, **not encryption**: the
  pairing secret and all peer traffic must travel over a trusted
  channel — your LAN or Tailscale (which encrypts it). TLS termination,
  if wanted beyond that, is the operator's/Tailscale's job. Do not pair
  or run peer traffic over the open internet as-is.

### Offline outbox

Every outbound peer delivery (directory sync, task creates/updates,
messages) goes through a persistent outbox in the node's data dir:
states `pending` / `delivered` / `failed`, attempt counts, and backoff
retries (default 1s, 5s, 15s, then 60s; tune with
`AGENT_MESH_RETRY_SCHEDULE`, flush interval with `AGENT_MESH_FLUSH_SEC`).
If a peer is down, orders are still accepted and wait; when it returns
they deliver in order, and duplicate deliveries never duplicate a task
or message on the receiving side. Settings → Peers shows outbox counts
and a "Send queued items now" button (`POST /peers/flush`).

Meshes are **pairwise**: a node talks only to nodes it paired with —
there is no relaying through a third node.

Phase 4 tests (`tests/test_phase4.py`) run two real node processes and
cover pairing (code reuse/wrong code fail, no secret in any response),
transport security (bad signature, stale timestamp, unknown node, peer
credentials refused on admin routes), directory sync, a remote task
completed on B and read back DONE on A, a remote message, and the
offline queue (task queued while B is down, delivered exactly once
when B returns).
