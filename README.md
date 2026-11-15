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
python3 -m unittest discover -s tests -v   # 11 tests, stdlib unittest
```

Not in Phase 1 (by design): GUI, MCP adapter (Phase 2), node-to-node /
Tailscale transport (Phase 4).
