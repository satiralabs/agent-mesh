# Changelog

## v2.0.0 — 2026-11-15

v2 turns the v1 toolkit into a multi-agent mesh: agents on one or
several machines register with identities, exchange messages, order
each other tasks under a permission matrix, and use shared credentials
through a broker that never hands a raw secret to an agent — all
drivable from a GUI. Built in five phases (branch `v2-phase1`,
spec in `PROTOCOL.md`, security pass in `SECURITY-REVIEW.md`).

### Phase 1 — core engine
- Agent registry: id, name, machine, capabilities, role, heartbeat;
  auth tokens shown once, stored only as SHA-256 hashes.
- Task store: strict lifecycle DRAFT → SENT → ACCEPTED/REJECTED →
  IN_PROGRESS → IN_REVIEW → DONE/FAILED, dependencies, idempotency
  keys, budgets, progress notes, results.
- Encrypted vault v2 with per-service, per-agent ACL
  (read/write/spend) and a `requires_approval` flag; broker use-check
  returns allow / deny / needs_approval, always audited.
- Node REST API (stdlib `http.server`) + a small Python client.

### Phase 2 — adapters
- MCP server (hand-rolled stdio JSON-RPC; the `mcp` package is not
  installable everywhere) with 8 tools: agents list, send, inbox,
  task create/update/list/get, use-service.
- Connection packs: one admin action registers an agent and emits a
  ready-to-paste MCP config (pack file is 0600; the only place the
  token ever appears).
- CLI shim for shell-only agents; file-drop adapter for file-only
  agents (processed/failed folders with error notes).

### Phase 3 — GUI + approvals
- Web control centre served by the node (8 screens: Dashboard,
  Agents, Tasks, Messages, Vault, Approvals, Activity, Settings).
  Admin token kept in sessionStorage only.
- Approvals queue: a gated vault use parks here; one approval lets
  exactly one use through, then the service is gated again.
- Vault ACL editing + service delete from the GUI; activity/audit
  readback; Tauri 2 desktop scaffold (not compiled — see
  `desktop/DESKTOP.md`).

### Phase 4 — node-to-node federation
- Persistent node identity; one-time pairing codes (≤15 min, hashed,
  single use) establish a per-peer secret stored only in each node's
  own data dir.
- Signed transport: HMAC-SHA256 over timestamp + method + path +
  body, 5-minute replay window; peer credentials work on `/peer/*`
  only.
- Directory sync (remote agents listed like local ones), federated
  tasks (created on the origin, worked on the home node, status /
  progress / result flow back), remote messages with dedupe.
- Persistent offline outbox with backoff; deliveries are idempotent
  (a queued task lands exactly once). Pairwise meshes only — no
  relaying through third nodes.

### Phase 5 — hardening + release prep
- Ordering matrix hard-enforced (PROTOCOL §1): owner orders anyone;
  orchestrator orders workers/orchestrators, never an owner; a
  worker may only create tasks for itself (messages are not orders).
  Enforced for local **and** remote-originated tasks; denials are
  403 and audited. Registration without an explicit role defaults
  to orchestrator, so Phase 1-4 agents keep working.
- Secret scanner on all content paths (messages, task
  title/instructions/result, progress notes, file-drop, peer
  payloads): credential shapes are blocked before storage, errors
  name only the rule, the owner may override with
  `allow_secret_risk: true` (audited), nobody else can.
- Security self-audit (`SECURITY-REVIEW.md`) with fixes: agents can
  no longer read another agent's inbox via `GET /messages`; node
  data dir forced to 0700 and every store file to 0600; malformed
  `Content-Length` no longer crashes a handler thread.
- GUI auto-refresh: the current screen re-polls every 10 s, pausing
  when the tab is hidden or a form is being edited, with an
  "Auto-refresh: on/paused" indicator.
- Real app icon set (SVG source + PNG 32/128/256/512 + ICO) wired
  into the Tauri config; version unified at 2.0.0.

## v1.0.0

The original toolkit: SQLite message bus, handover compiler with
Google Drive sync, localhost secret-broker proxy with an approval
gate and audit log, CLI-first, plus a Chrome `--app` launcher page.
v1 modules are unchanged and still shipped in v2.0.
