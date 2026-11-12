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
