# Agent Mesh — Developer Guide

This guide describes how Agent Mesh is built and how to work on it.
The normative wire protocol is [PROTOCOL.md](../PROTOCOL.md); this
document is the map of the codebase that implements it.

## 1. Architecture at a glance

A **node** is one Python process (`agent_mesh/node.py`) serving one
machine. It owns a data directory with SQLite stores and an audit
log, serves the web GUI, and exposes a JSON REST API. Agents talk to
their *home node* only. Nodes optionally **pair** with each other
(federation) so agents on different machines can message and task
each other; pairing is pairwise — there is no relay through a third
node.

```
            ┌──────────────────── node (one machine) ───────────────────┐
 agents ───▶│ registry · tasks · vault/broker · scanner · handover      │
 (MCP/REST/ │ messages · approvals · audit        GUI (web2/)           │
  CLI/drop) └──────────────┬────────────────────────────────────────────┘
                           │ HMAC-signed peer API + offline outbox
                    ┌──────▼───────┐        ┌────────────────┐
                    │  peer node   │  ...   │   peer node    │
                    └──────────────┘        └────────────────┘
```

Core modules (`agent_mesh/`):

| Module | Responsibility |
|---|---|
| `node.py` | HTTP server, routing, auth, admin API, GUI + PWA serving, peer endpoints, outbox flusher |
| `registry.py` | Agent identity: id, name, machine, capabilities, role, token hash, heartbeat/last-seen, presence computation |
| `tasks.py` | Task store: lifecycle, participants, comments, dependencies, budgets, idempotency |
| `vault.py` | Typed credentials, encryption at rest, per-agent ACL + sharing grants |
| `broker.py` | v1 broker proxy + the use decision (`allow`/`deny`/`needs_approval`) |
| `approvals.py` | One-shot approval store behind gated vault uses and reveals |
| `peers.py` | Node identity, pairing codes, per-peer secrets, HMAC transport, directory sync, outbox |
| `scan.py` | Secret scanner applied to messages, task fields, comments, handover entries |
| `handover_rules.py`, `handover_log.py`, `handover.py` | Rules delivery, entry store + `HANDOVER-<node>.md` export, CLI filing |
| `pack.py` | Connection-pack generation in all five formats |
| `mcp_server.py` | Stdio MCP server (10 tools) — hand-rolled JSON-RPC, no `mcp` package needed |
| `mesh_cli.py` | CLI shim for shell-only agents (full parity with the MCP surface) |
| `client.py` | Small HTTP client used by the adapters and the worker |
| `filedrop.py` | Watched-folder adapter for agents with no API access |
| `bus.py`, `secrets.py`, `cli.py`, `gui.py` | v1 components, kept working and unchanged in behaviour |

The GUI is a dependency-free vanilla SPA in `agent_mesh/web2/`
(nine screens: dashboard, agents, tasks, messages, vault, approvals,
handover, activity, settings). It talks only to the node's REST API
with the admin token, which it keeps in `sessionStorage` only.

## 2. Identity, roles and presence

An agent registers with a name, capabilities and a role:
`owner` (the human's own agents), `orchestrator`, or `worker`
(default for new registrations: `orchestrator`). The registration
response shows the agent's token **once**; the node stores only its
SHA-256 hash. Disconnect (admin) revokes the token immediately; the
agent row, its tasks and messages are kept and shown as `revoked`.
Reconnect = pack rotation: a fresh token is issued and the old one
stays dead.

Ordering matrix (enforced on task create/assign):

- owner orders anyone;
- orchestrator orders workers and orchestrators, never owner-role;
- worker may only assign tasks to itself — but may *message* anyone.

**Presence is computed, never stored** (PROTOCOL §7.1): from
`last_seen` at read time — `online` < 120 s, `idle` < 15 min,
`offline` otherwise; `revoked` always wins. Directory sync carries
each agent's origin-node `last_seen`; receivers must not substitute
sync time.

## 3. Tasks and collaboration

Lifecycle (strict, enforced in `tasks.py`):

```
DRAFT → SENT → ACCEPTED → IN_PROGRESS → IN_REVIEW → DONE
                    ↘ REJECTED                    ↘ FAILED
```

Tasks support progress notes (append-only), a result on completion,
named-agent or capability assignment, priority, budgets, subtask
parents, dependencies (a task cannot be accepted/started until its
dependency is DONE) and idempotency keys (a duplicate submission
returns the existing task).

**Collaboration (v2.2, PROTOCOL §8).** A task has one **lead** — the
assignee, responsible for the result and the only participant who
drives lifecycle transitions — plus **participants** who can see the
task, comment in its shared **thread** and add progress notes.
Rosters are managed by the requester or admin (`POST`/`DELETE
/tasks/{id}/participants…`), only while the task is not terminal;
the lead cannot be removed. Task reads are involvement-scoped: an
agent sees a task only if it is requester, lead or participant.

Federation merge rules: participants and comments travel inside the
task snapshot and merge as monotonic unions (participants by agent
id, comments by globally unique comment id). Snapshots are also
queued to each remote participant's home node. **Caveat:**
participant *removals* do not propagate across nodes in v2.2 — a
removed collaborator keeps read access to the peer copy until the
task ends (the same append-only rule as progress notes). Relay is
star-shaped around the origin node; a node that merges a snapshot
does not re-forward it.

## 4. REST API surface

Base: `http://127.0.0.1:<port>` (default port 8790). JSON in/out.
Admin auth: `X-Admin-Token: <AGENT_MESH_ADMIN>`. Agent auth:
`Authorization: Bearer <agent-token>`. Peer auth: HMAC signature
(§6). No/unknown credentials → `401`; authenticated but not
permitted → `403`; scanner rejection → `400`.

| Route | Auth | Notes |
|---|---|---|
| `GET /health` | none | `{status: "ok"}` |
| `GET /node/info` | none | node id, name, version |
| `POST /agents/register` | admin | returns agent + one-time token |
| `POST /agents/{id}/pack` | admin | rotate token / reconnect; `format` selects pack flavour |
| `POST /agents/pack` | admin | register + pack in one call |
| `POST /agents/{id}/disconnect` | admin | revoke; audited as `agent_disconnected` |
| `POST /agents/heartbeat` | agent | body may set `status` (never `revoked`) |
| `GET /agents` | agent/admin | token hashes never returned; includes computed `presence` |
| `POST /messages` · `GET /messages` | agent | inbox scoped to the caller (admin sees all) |
| `POST /tasks` · `GET /tasks` · `GET /tasks/{id}` | agent | involvement-scoped reads |
| `POST /tasks/{id}/transition` `{to, result?}` | agent | lifecycle rules enforced |
| `POST /tasks/{id}/progress` `{note}` | agent | append-only |
| `POST /tasks/{id}/comments` | involved agent | scanner-gated thread |
| `POST/DELETE /tasks/{id}/participants[/{agent_id}]` | requester/admin | non-terminal tasks only |
| `POST /vault/services` · `GET /vault/services` | admin / agent+admin | create is admin-only; listing never contains secret material |
| `POST /vault/use-check` | agent | broker decision for API use |
| `POST /vault/credential-use` | agent | brokered `use` of a granted credential — non-secret details only |
| `POST /vault/reveal` | agent | approval-gated secret reveal (see §5) |
| `POST/DELETE /vault/services/{name}/grants[/{agent_id}]` | admin | sharing grants: mode `use` or `reveal` |
| `GET /approvals` · `POST /approvals/{id}/decide` | admin | the persistent approvals queue |
| `GET /handover/rules` | agent | canonical rules + version |
| `POST /handover/entries` | agent | scanner-gated; regenerates the node's handover export |
| `GET /activity` | admin | audit/activity feed |
| `GET /peers` · `POST /peers/pairing-code` · `POST /peers/pair` | admin | federation management |
| `GET /peers/outbox` · `POST /peers/flush` · `POST /peers/{id}/sync` | admin | offline queue inspection and control |
| `POST /peer/pair` · `/peer/ping` · `/peer/directory` · `/peer/message` · `/peer/task_sync` | peer HMAC | node-to-node only; peer credentials work **only** here |

Request bodies are capped at 10 MiB (413 beyond that).

## 5. Vault and broker model

Credentials are typed — `api_key`, `login`, `ssh`, `oauth_token`,
`generic` — with non-secret metadata listable per type (login →
username + URL; ssh → host + user + port; api/oauth → upstream;
generic → label). Secret values are encrypted at rest (Fernet from
the `cryptography` package when importable; otherwise
PBKDF2-HMAC-SHA256, 600k iterations, + OpenSSL AES-256-CBC
encrypt-then-MAC) under the node master passphrase
(`AGENT_MESH_MASTER`, or a 0600 file in the data dir).

Two ways an agent benefits from a credential:

- **Brokered use** — the node performs/decides the use; the agent
  receives the decision and non-secret connection details only. The
  secret never leaves the node. Per-service ACL decides
  `allow` / `deny` / `needs_approval`.
- **Reveal** — the agent receives the secret value, only under a
  `reveal` grant and only after a per-use approval from the
  approvals queue (one approval = one reveal, then it re-gates),
  unless the grant is marked `standing_reveal`. Every reveal
  attempt — allowed, gated or denied — is audit-logged with agent,
  credential name and outcome, never the value.

## 6. Federation and transport security

Pairing: node B generates a one-time code (64-bit, 15-minute TTL);
node A presents B's URL + code; the two exchange a per-peer secret
exactly once. Codes are burned after 5 failed attempts from a
source. All peer traffic is HMAC-SHA256 signed with a 5-minute
replay window. Directory sync marks remote agents with their home
node; remote tasks/messages merge monotonically with origin-id
deduplication. Since v2.1.1, registering, rotating or disconnecting
an agent automatically enqueues a directory push to all peers.

Every outbound peer delivery goes through a persistent SQLite
outbox (states `pending`/`delivered`/`failed`, backoff retries), so
orders placed while a peer is offline deliver exactly once when it
returns.

**Honest limit:** HMAC gives authentication and integrity, **not
encryption**. Run pairing and peer traffic over a LAN or Tailscale.
There is no TLS in the node and no multi-hop relay.

## 7. Secret scanner

`scan.py` inspects message bodies, task fields, comments and
handover entries and blocks credential-shaped content (the sender
can retry with the secret removed; an owner-role override exists).
v2.1.1 hardened it with normalisation (zero-width stripping,
separator-tolerant token matching, spaced keywords) and
base64 decode-and-rescan. Known residuals are documented in the
module and in the audit report — e.g. a Bearer token split by
spaces still passes, because catching it false-positives on
ordinary security prose. The scanner is defence-in-depth; the vault
is the actual boundary.

## 8. Handover automation

The canonical rules live in `agent_mesh/handover_rules.md` (version
parsed from the file). On registration and first heartbeat the node
sends each agent exactly one starter message per rules version.
Entries are filed via `POST /handover/entries`, the MCP tool
`mesh_handover_log`, or the CLI
(`python3 -m agent_mesh.handover --title … --asked … --done …
--verified … --files … --open …`), and the node regenerates
`HANDOVER-<node-name>.md` in its data dir after each entry. The
export is per-node; cross-node handover merging is not built.

## 9. MCP tools

The stdio MCP server (`agent_mesh/mcp_server.py`) exposes:

| Tool | Purpose |
|---|---|
| `mesh_agents_list` | agents + computed presence |
| `mesh_send` | send a message |
| `mesh_inbox` | own inbox (+ participating tasks) |
| `mesh_task_create` | create a task (incl. `participants`) |
| `mesh_task_update` | transition / progress |
| `mesh_task_list` · `mesh_task_get` | list / read a task incl. its thread |
| `mesh_task_comment` | comment on a task |
| `mesh_use_service` | brokered vault use decision |
| `mesh_handover_log` | file a handover entry |

## 10. Connection packs

One registration, five formats (`pack.py`; PROTOCOL §7.5):
`mcp` (generic `mcpServers` JSON), `claude-code` and `cursor` (the
same JSON schema — the difference is where it is pasted, documented
in the pack), `rest` (env file + curl guide, used with the
`mesh_cli` shim), and `chat-paste` (a plain-English block covering
identity, the file-drop protocol and the human-relay fallback). The
agent's token exists only inside the generated pack file (0600).

## 11. The autonomous worker

`worker/mesh_worker.py` is a standalone poller that makes one agent
identity autonomous — the reference deployment drives a headless
agent CLI, but the loop is generic:

- polls its home node every 20 s for tasks in `SENT` assigned to
  its agent;
- executes **only** tasks whose requester is on the
  trusted-requester allowlist in its `config.json`;
- one task at a time, in a dedicated workspace directory, with a
  per-task timeout and a full per-task log;
- drives the lifecycle itself (accept → in-progress → done/failed
  with the captured output as result) and messages the requester;
- safeguards: a `PAUSED` file idles it, a state file caps runs per
  hour (default 6), it never deletes anything.

One instance serves one agent identity; several autonomous agents =
several instances with separate configs. Interactive chat sessions
cannot be woken by any tool — autonomy lives in workers like this
one, which is a deliberate design boundary, documented for users in
the manual.

## 12. Security model and audit

- Tokens: agent tokens hashed (SHA-256) at rest; admin token
  compared in constant time; peer secrets never leave their node
  pair.
- Every security-relevant event (registrations, disconnects,
  grants, reveals, approvals, scanner blocks, pairing) is written
  to the node's audit log with secrets redacted.
- Data hygiene: data dir 0700; databases and audit files 0600.
- The v2.1 line was independently audited — see
  [SECURITY-PERF-REPORT.md](../SECURITY-PERF-REPORT.md): auth matrix,
  cross-role token confusion, replay/tamper, vault reveal gating,
  XSS/traversal and concurrency all tested; performance at this
  scale is comfortable (health ≈ 1 ms median, ~286 messages/s
  sequential, cross-node delivery ≈ 13 ms median). Findings F1
  (body-size cap), F4/F5 were fixed during the audit; F2 (scanner
  obfuscation), F3 (pairing-code strength/throttling) and F6
  (directory push on registration) were fixed in v2.1.1. The
  earlier phase-5 review is in
  [SECURITY-REVIEW.md](../SECURITY-REVIEW.md).

## 13. Testing

```bash
python3 -m unittest discover -s tests
```

118 tests (stdlib `unittest`, no extra dependencies), organised by
phase: `test_phase1` core engine · `test_phase2` adapters/packs ·
`test_phase3` GUI + approvals · `test_phase4` federation (two real
node processes) · `test_phase5` hardening/scanner · `test_phase6`
disconnect/reconnect · `test_phase7` v2.1 (vault sharing, handover,
presence, packs) · `test_phase8` v2.1.1 audit fixes ·
`test_phase9` collaboration. Federation and worker-adjacent tests
spin up throwaway nodes on spare ports with temp data dirs — never
point tests at a live node's data directory.

## 14. Repository layout

```
agent_mesh/     the node + adapters (see §1)
docs/           this guide, the user manual, screenshots
worker/         the autonomous worker (one instance per agent)
extension/      Chromium browser extension (MV3)
desktop/        Tauri desktop scaffold (build per DESKTOP.md)
installers/     Linux / Windows / macOS installers + Android guide
scripts/        inbox_watch.py — surfaces new inbox items to a host
tests/          the unittest suite
PROTOCOL.md     normative protocol spec
CHANGELOG.md    release history
```

## 15. Working on the code

- Stdlib-first: new runtime dependencies need a very good reason;
  the node must keep running on a bare Python 3.11+.
- Every new externally visible behaviour ships with tests in the
  matching phase file (or a new one) and a CHANGELOG entry.
- Never log, return or store a secret value outside the vault;
  tests byte-scan API output and audit logs for planted secrets —
  keep them passing.
- Version strings live in several surfaces (package, MCP server
  info, PWA shell, Tauri config, extension manifest, README banner)
  and are pinned by tests; bump them together.
