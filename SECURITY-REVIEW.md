# Agent Mesh v2.0 — Security self-review (Phase 5, 2026-11-15)

Scope: the v2 node (`agent_mesh/node.py` and the stores it wires up),
reviewed endpoint by endpoint against the code as shipped in v2.0.
This is a self-audit by the builder, not an external review. Method:
read every route handler, trace what each response is built from, and
byte-scan test data dirs / responses for dummy secrets (the Phase 1-5
test suites do this mechanically; results quoted below).

Auth classes: **public** (no credential), **agent** (Bearer token,
stored on the node only as a SHA-256 hash), **admin** (`X-Admin-Token`
= the owner; the GUI's reserved actor is `owner`), **peer** (HMAC with
the per-peer secret, valid on `/peer/*` only).

## Endpoint inventory

| Route | Who | What it can return / do | Secret exposure check |
|---|---|---|---|
| `GET /`, `/v1`, `/ui/app.js`, `/ui/app.css` | public | Static GUI shell only. The page contains no token and no data; the admin token lives in the tab's sessionStorage | Files are literals in the repo; nothing dynamic is interpolated |
| `GET /health` | public | `{"status": "ok"}` | No state disclosed |
| `GET /agents` | agent/admin | Public agent records (id, name, machine, capabilities, role, status, timestamps) + cached remote agents | Built by `Registry._public`, which has no token field; Phase 1 test asserts `token_hash` absent |
| `GET /node/info` | agent/admin | node_id, name, configured URL | No credentials |
| `GET /peers`, `GET /peers/outbox` | admin | Peer id/name/url/status/timestamps; outbox counts + recent items | `list_peers_safe` / `outbox_recent` select no secret and no payload columns (read in `peers.py`); Phase 4 test asserts `"secret"` absent from the JSON |
| `GET /messages` | agent (own inbox only — **fixed in Phase 5**, see F1) / admin (any) | Message rows for the caller's inbox | Messages are mesh content, scanned on entry (§ scanner) |
| `GET /tasks`, `GET /tasks/{id}` | agent/admin | Full task records | Tasks are shared work objects by design; see "accepted model limits" |
| `GET /vault/services` | agent/admin | Service name, upstream, ACL, `requires_approval`, `has_secret: true` | No ciphertext, no secret, not even a masked fragment; byte-scanned in Phase 1/3 tests |
| `GET /approvals` | admin | Approval queue (agent, service, action, status) | No secret material exists in approval records |
| `GET /activity` | admin | Audit entries | Entries are built from fixed fields everywhere (see "audit" below); dummy secrets byte-scanned out in Phase 1/3 tests |
| `POST /agents/register`, `POST /agents/pack`, `POST /agents/{id}/pack` | admin | Creates an agent; returns its token / connection pack **once** | By design — registration is the token's one legitimate appearance. Re-packing rotates the token (old one dies; Phase 3 test) |
| `POST /agents/heartbeat` | agent | Updates the *authenticated* agent's own status/last-seen | Cannot touch another agent: the id comes from the token, not the body |
| `POST /messages` | agent/admin actor | Stores a message; sender is forced to the authenticated actor (a body-supplied sender is ignored) | Scanner gate before storage (Phase 5) |
| `POST /tasks` | agent/admin actor | Creates a task; requester = authenticated actor | Ordering matrix (403, audited) + scanner gate (Phase 5) |
| `POST /tasks/{id}/transition`, `/progress` | agent/admin actor | Lifecycle moves, result, progress notes | Scanner gate on result / note |
| `POST /vault/services` | admin | Stores a credential **encrypted**; response is the safe listing | Secret arrives in the request body only; bodies are never logged (below); never in the response |
| `PUT /vault/services/{name}` | admin | Edits ACL / approval flag / upstream | `update_service` cannot read or replace the secret — there is no code path from this route to the ciphertext |
| `DELETE /vault/services/{name}` | admin | Deletes a service | — |
| `POST /vault/use-check` | agent (own id from token); admin may name `agent_id` (GUI test path) | `allow` / `deny` / `needs_approval` (+ approval id) | Decision only. `Vault.get_secret` has **no HTTP caller** — grep-verified: it is defined for the node-internal broker path and no route calls it |
| `POST /approvals` | agent | Asks the vault decision for itself; enqueues an approval when gated | Returns the approval record, no secret |
| `POST /approvals/{id}/decide` | admin | Approve/deny | — |
| `POST /peers/pairing-code`, `/peers/pair`, `/peers/flush`, `/peers/{id}/sync` | admin (on the caller's own node) | Pairing + outbox control | The pairing code is shown once to the owner who asked for it |
| `POST /peer/pair` | pairing code (one-time, ≤15 min, hashed at rest) | Establishes a peer; returns the new per-peer secret **once**, to the initiating node | By design, over the trusted channel pairing requires (PROTOCOL §6). The secret is never returned again by any route |
| `POST /peer/ping`, `/peer/directory`, `/peer/task_sync`, `/peer/message` | peer (HMAC + 5-min timestamp window) | Federation sync | Peer credentials satisfy *only* these routes — Phase 4 test posts a valid peer signature at `/agents/register` and gets 401. Phase 5: task creation here is matrix-checked and scanned before it exists locally |

## Cross-cutting checks

- **Request bodies are never logged.** The handler's `log_message` is
  a no-op; the only stdout line is the startup banner (node id, name,
  URL, data dir — no tokens). Audit writers (node, vault, approvals)
  emit fixed-field JSON: timestamps, ids, service/task names, actions,
  outcomes, rule names. The vault secret, agent tokens, peer secrets
  and pairing codes are never written by any audit path — verified by
  the byte-scans in the Phase 1, 3 and 5 tests, which walk *every*
  file in a live node's data dir.
- **Error messages don't echo secrets.** Scanner errors name the rule
  only (Phase 5 test asserts the fake token is absent from the error
  body). Vault decrypt failures say "integrity check failed", never
  content. Pairing failure detail relays the peer's *error body*
  (≤200 chars) — peer error bodies are built from the same fixed
  strings, so this cannot carry a credential; noted, accepted.
- **Data-file permissions (F2, fixed).** The node data dir is forced
  to `0700` at startup and `audit.log` is created `0600`. Store files:
  `vault.json` (ciphertext), `peers.db` (peer secrets) and `node.json`
  were already `0600`; Phase 5 added the same to `agents.db` (token
  hashes), `tasks.db`, `approvals.db` and `bus.db`. A Phase 5 test
  builds a fresh node in a `0755` dir and asserts the dir becomes
  `0700` and all eight files are `0600`. SQLite `-wal`/`-shm`
  sidecars live inside the `0700` dir.
- **Ordering matrix (F3, fixed).** Phase 1 recorded roles without
  enforcing them. Phase 5 enforces the PROTOCOL §1 matrix on local
  and remote task creation; denials are `403` + audited. Messages are
  deliberately not orders and are not matrix-checked.
- **Secret scanner (F4, new).** See PROTOCOL §4b. Blocks are audited
  with rule + actor only; the owner override is audited too, and a
  non-owner override attempt is `403`.
- **Robustness (F5, fixed).** `_body()` crashed the handler thread on
  a malformed `Content-Length`; it now treats it as an empty body.

## Accepted model limits (not fixed, on purpose)

- **Task transitions:** any authenticated agent can transition any
  task (Phases 1-4 semantics — e.g. the requester marks a review
  DONE). Transitions are audit-logged with the actor. Per-task actor
  restrictions are a policy decision for a later version, not a leak.
- **Plain HTTP:** the node speaks HTTP. HMAC gives peer traffic
  authentication + integrity, **not confidentiality** — run nodes on
  localhost / LAN / Tailscale as PROTOCOL §6 states. There is no TLS
  in v2.0.
- **Admin token = owner.** Whoever holds `AGENT_MESH_ADMIN` is the
  owner, including the scanner override and vault writes. There is
  one human seat in v2.0 by design.

## Could not verify

- The Tauri desktop bundle (no Rust toolchain on the build machine) —
  the icon set and config were produced and checked as files, but no
  compiled app was run, so nothing here covers the desktop shell's
  runtime behaviour (e.g. how it stores its generated admin token —
  see `desktop/DESKTOP.md`).
- Behaviour on Windows / macOS (the whole suite ran on Linux):
  `chmod`-based permissions are POSIX semantics; on Windows the
  `0700`/`0600` guarantees degrade to "best effort" and the data dir
  relies on the user profile's ACLs.
- The Fernet crypto path (taken only when `cryptography` is
  installed) is code-reviewed but not exercised on this machine,
  which lacks the package; the OpenSSL fallback path is the one the
  tests actually run.
- Load / abuse resistance (rate limits, body-size caps) — out of
  scope for a single-owner LAN tool in v2.0; there is no rate
  limiting, and a paired peer or a token holder can spam the node.
