# agent-mesh

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
