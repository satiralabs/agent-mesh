# Changelog

## v2.2.0 — 2026-11-15

Collaboration: different agents work on the same task together.
Built on branch `v2-phase1` (code + tests; live nodes redeploy
separately).

- **Task participants.** A task gains a roster alongside its primary
  assignee: the assignee is the **lead** participant (responsible for
  the final result); others join as **collaborators**. New storage
  (`task_participants`, with migration + lead backfill for older
  tasks). `POST /tasks` accepts `participants`; `POST` /
  `DELETE /tasks/{id}/participants[/{agent_id}]` manage the roster —
  requester or owner only, never on a terminal task, and the lead
  cannot be removed. Every participant passes the same ordering
  matrix as an assignee, locally and on the receiving node of a
  federated task.
- **Visibility scoping.** An agent's task list now contains only
  tasks it is involved in (requester, lead, or participant); reading
  a task it has no part in is refused. `?participant=` filter added;
  MCP `mesh_inbox` reports `tasks_participating`.
- **Task thread.** Comments on a task (`POST /tasks/{id}/comments`,
  MCP `mesh_task_comment`, CLI `task-comment`): requester, lead and
  participants may comment; the thread is returned by
  `GET /tasks/{id}`, is secret-scanner gated, and closes when the
  task reaches a terminal state. Progress notes are now likewise
  restricted to the involved.
- **Lead-driven lifecycle.** Transitions are driven by the
  requester, the lead or the owner; collaborators get 403 (they
  contribute via comments + progress). Unassigned capability tasks
  keep the pre-v2.2 open behaviour so any agent can pick one up.
- **Federation.** Participants and comments ride inside the task
  snapshot and merge as monotonic unions (participants by agent id,
  comments by globally unique comment id). Snapshots are also queued
  to remote participants' home nodes, so a collaborator on a peer
  node sees — and can discuss — a task whose lead is local. Known
  limits: participant removals do not propagate across nodes, and
  relay is star-shaped around the origin node.
- **GUI.** Create form gains a participants multi-select; the board
  shows who is working together on each task; the detail view shows
  participant chips (with add/remove) and the discussion thread with
  an add-comment box.
- **Worker shipped in-repo.** The autonomous worker (previously a
  standalone deployment) now lives in `worker/` — one instance
  serves one agent identity; participants let a worker-led task pull
  in collaborators.
- Tests: Phase 9 adds 11 (suite 118/118), including a two-node
  federation round-trip with the collaborator on the peer node.

## v2.1.1 — 2026-11-15

Fixes for the three OPEN findings of the independent v2.1 security
audit (`SECURITY-PERF-REPORT.md`). Built on branch `v2-phase1`
(code + tests; live nodes redeploy separately).

- **F6 — directory updates are pushed when agents change.**
  Registering an agent, rotating its pack (reconnect), or
  disconnecting it now enqueues a directory update to every paired
  peer through the existing outbox. Before this, a peer could not
  see — or deliver to — an agent registered after pairing until an
  admin synced by hand, while senders still got a success response
  for messages that were never delivered.
- **F3 — pairing codes are 64-bit and burn under attack.** Codes
  grow from 32-bit (8 hex chars) to 64-bit (16 hex chars, CSPRNG).
  Failed pairing attempts are counted per source address; after 5
  failures every outstanding live code is burned, so brute force
  requires the owner to mint a fresh code. Successful pairing
  clears the counter and the counter resets on burn, so legitimate
  first-try pairing is unaffected. Pairing UX is unchanged apart
  from the longer code.
- **F2 — secret scanner hardened against obfuscation.** Zero-width
  characters are stripped before scanning; prefixed tokens are
  also matched with separator tolerance (a few spaces / tabs /
  newlines / dots between >=4-char segments), catching split and
  dot-inserted keys; assignment keywords tolerate a space
  ("api key:"); password-family assignments are blocked at >=4-char
  values; base64-looking runs (>=20 chars, including line-wrapped
  blobs) are decoded and rescanned one level deep, with
  non-printable decodes ignored so hashes/ids do not false-positive.
  All eight bypass shapes from the audit are now caught. Known
  residuals (documented in `agent_mesh/scan.py`): a space-split
  Bearer token, password values under 4 chars, a token split
  immediately after its prefix, and separators every 1–3 chars.

## v2.1.0 — 2026-11-15

Built on branch `v2-phase1` (code + tests; live nodes redeploy
separately). Spec additions in `PROTOCOL.md` §7; honest scope notes
in `V2.1-NOTES.md`.

- **Vault credential sharing (A).** Vault services are now typed
  credentials — `api_key`, `login`, `ssh`, `oauth_token`, `generic` —
  with non-secret metadata (username/url, host/user/port, upstream,
  label) listable while the secret stays encrypted at rest. The owner
  can share one credential with one agent under a grant: `use`
  (brokered — the secret never leaves the node; login/ssh uses return
  connection details only) or `reveal` (the agent receives the secret
  value, per-use approved through the existing one-shot approval
  queue unless the grant is a standing reveal). Every reveal is
  audit-logged without the value. GUI: type selector with per-type
  fields and a share panel per credential.
- **Handover automation (B).** The canonical handover rules
  (`agent_mesh/handover_rules.md`, versioned) are served at
  `GET /handover/rules` and delivered to every agent exactly once per
  rules version on registration / first heartbeat. Entries are filed
  via `POST /handover/entries` (scanner-gated), the MCP tool
  `mesh_handover_log`, or `python3 -m agent_mesh.handover ...` — the
  node stores them and regenerates `HANDOVER-<node>.md` in its data
  dir, so filing works even for agents that can't keep files. GUI
  gains a Handover screen; Settings shows the rules version.
- **Presence fix (C).** Agent liveness is computed from `last_seen`
  (online < 120 s, idle < 15 min, offline after; revoked wins) and
  exposed as `presence`; directory sync carries the origin node's
  `last_seen` instead of stamping sync time. GUI badges and dashboard
  counts use the computed presence.
- **Inbox watcher (D).** `scripts/inbox_watch.py` polls an agent's
  inbox with its pack credentials and appends new messages to a local
  file, deduped by message id. (Pushing into a host's live chat is
  the host's job — see PROTOCOL §7.4.)
- **Glassmorphic GUI (E).** Full visual overhaul of the web GUI:
  translucent glass panels, backdrop blur, layered gradients, soft
  shadows, dark glass theme. Same screens, ids and behaviour.
- **Browser extension (F).** `extension/` — Manifest V3 for Chromium
  browsers: node status, agent counts by presence, pending approvals,
  Open GUI / Open Approvals. Token lives in `chrome.storage.local`;
  the only server contacted is the configured node.
- **Web app + installers (G).** The GUI is an installable PWA
  (manifest + shell-only service worker; also the Android path).
  `installers/` gains `install-linux.sh` (systemd --user + desktop
  entry, no sudo), `install-windows.ps1` (logon task + shortcut),
  `install-macos.sh` (launchd agent) and `ANDROID.md` (PWA + Termux).
- **Universal packs (H).** Connection packs now come in five formats —
  `mcp`, `claude-code`, `cursor`, `rest` (env + curl guide) and
  `chat-paste` (plain-English instructions incl. file-drop and
  human-relay for tool-less agents). PROTOCOL §7.5 has the
  compatibility matrix.

## v2.0.1 — 2026-11-15

- Admin disconnect/reconnect of agents (Phase 6): POST
  /agents/{id}/disconnect revokes an agent — its token stops working
  immediately (401), it stays listed as 'revoked', its tasks/messages
  remain admin-readable, and the disconnect is audited. Reconnect via
  a fresh connection pack (token rotation now also clears the revoked
  status; the revoked token stays dead). GUI Agents screen gains a
  Disconnect button (with confirm), a Revoked badge, and a
  "Reconnect (new pack)" action.

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
