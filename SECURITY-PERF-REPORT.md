# Agent Mesh v2.1.0 — Independent Security & Performance Report

**Scope:** `repo-v2`, branch `v2-phase1`, HEAD at audit start `e6284c4`
(v2.1.0). Auditor role: independent checker, not the builder.
**Method:** all tests ran against throwaway nodes on spare localhost
ports (18901–18908) with temp data dirs under `/tmp`. No live node
(PC or VM) was touched. Evidence below is from live HTTP calls, raw
sockets, byte scans of responses/logs, and code inspection.
**Baseline:** existing suite before any change: **90/90 tests pass**
(38.7 s). Custom battery: **57 checks (round 1) + 7 checks (round 2),
all PASS**, before and after the fixes in §4.

---

## 1. Security results

### 1.1 Authentication & authorization — all PASS

| Check | Result | Evidence |
|---|---|---|
| No token on `/agents` | PASS | HTTP 401 |
| Wrong admin token / wrong agent token | PASS | HTTP 401 / 401 |
| Valid agent token on `/agents` | PASS | HTTP 200 |
| Agent token on admin routes (`/agents/register`, `/peers`, `/activity`) | PASS | HTTP 401 on all |
| Admin token presented to peer route (`/peer/ping`) | PASS | HTTP 403 (unknown peer) |
| Unknown peer node id + forged signature | PASS | HTTP 403 |
| Agent Bearer token on `/peer/*` route | PASS | HTTP 403 |
| Peer secret used as agent token / as admin token | PASS | HTTP 401 / 401 |
| Disconnect (revoke) an agent | PASS | agent listed `revoked` |
| Revoked token on `/agents`, heartbeat, `/messages`, handover, vault | PASS | HTTP 401 on all five (was 200 before revoke) |
| Reconnect via new pack: new token works, **old token stays dead** | PASS | new token HTTP 200, old token HTTP 401 |
| Agent cannot read another agent's inbox via `?recipient=` | PASS | other agent's message not visible; own inbox readable |
| Approval `decide` requires admin | PASS | HTTP 401 with agent token |

### 1.2 Vault credential sharing (new in v2.1) — all PASS

Test credentials were unique marker strings in `login`, `ssh`, and
`api_key` services. Responses, the audit log, `/activity`, and
`/vault/services` were **byte-scanned** for the marker values.

| Check | Result | Evidence |
|---|---|---|
| `credential-use` without grant | PASS | HTTP 403, outcome `deny` |
| `reveal` without grant | PASS | HTTP 403, outcome `deny` |
| Grant create/revoke endpoints require admin | PASS | HTTP 401 with agent token and with no token |
| `use` under a use-grant (login / ssh / api) | PASS | HTTP 200, outcome `allow`, connection details only |
| **`use` responses byte-scanned for the secret (all 3 types)** | PASS | secret absent from every response |
| Use-grant does **not** permit reveal | PASS | HTTP 403, outcome `deny` |
| Reveal without approval | PASS | outcome `needs_approval`, approval id issued |
| Approval is keyed to the requesting agent | PASS | a second agent with its own reveal grant got `needs_approval`, could not consume the first agent's approved approval; the owner's approval remained intact and consumable by its owner afterwards |
| Reveal after approval | PASS | outcome `allow`, secret returned to the grantee |
| **Reveal replay (one approval = one reveal)** | PASS | second reveal → `needs_approval`, no secret bytes |
| Reveal after grant revoked | PASS | outcome `deny` |
| Audit log + `/activity` + `/vault/services` byte scan | PASS | no marker bytes anywhere (audit log 4,965 B scanned) |

### 1.3 Secret scanner — canonical shapes PASS, obfuscation bypasses found (F2)

Blocked (HTTP 400) in messages, and also verified in task
instructions and handover entries: plain `ghp_…`, `sk-…`, `AKIA…`,
PEM private-key header, JWT shape, `password = <value>`,
`password:\n<value>`, `token = <value>`. Non-owner
`allow_secret_risk` override refused (403); owner override allowed
(201) and audited. File-drop behaves identically (plain shape →
`failed/` with a rule-only error file; clean file → `processed/`).

The following shapes **passed the scanner** (HTTP 201) — see F2:
token split by a space, token split by a newline, base64-encoded
token, `api key:` with a space instead of `_`, lowercase `akia…`,
`password = ` with a value under 8 chars, a zero-width character
inside the keyword (`pass\u200bword`), and an `sk-` key with dots
inserted between segments.

### 1.4 Peer transport — all PASS

Nodes paired through the real API for these tests.

| Check | Result | Evidence |
|---|---|---|
| Pairing with fresh code | PASS | HTTP 201 |
| Pairing code reuse | PASS | HTTP 400 on second use |
| Wrong pairing code | PASS | HTTP 400 |
| Expired pairing code (1 s TTL, waited 1.6 s) | PASS | HTTP 403 |
| Code consumed exactly once under 12-way concurrent reuse | PASS | exactly 1× HTTP 200, 11× 403 |
| Valid HMAC-signed `/peer/ping` | PASS | HTTP 200 |
| Correctly signed request replayed with a 400 s-old timestamp | PASS | HTTP 401 `stale or missing timestamp` |
| Body tampered after signing | PASS | HTTP 401 `bad peer signature` |
| Signature made with the wrong secret | PASS | HTTP 401 |
| Exact replay **within** the 5-minute window | accepted | by design — see F7 |

### 1.5 Injection & robustness — all PASS (one gap, F1)

| Check | Result | Evidence |
|---|---|---|
| Agent named `<script>alert(1)</script>'); DROP TABLE agents;--` + RTL override char | PASS | name round-trips verbatim via API, registry intact, subsequent auth fine |
| GUI XSS surface | PASS | `web2/app.js` `esc()` escapes `& < > " '` and is applied to agent names and all user-controlled fields; the two composite strings (`aclText`, `credMetaText`) are escaped as wholes at insertion (`app.js:378`); `toast()` uses `textContent`; confirm dialogs are text-only |
| Path traversal (`/ui/../`, `/icons/%2e%2e%2f…`, `/sw.js/../../etc/passwd`, 7 shapes) | PASS | HTTP 404 on all, no file bytes leaked (routing is exact-match; icons served by `basename` only) |
| Malformed JSON body | PASS | HTTP 400, server alive after |
| Non-numeric / negative `Content-Length` (raw socket) | PASS | HTTP 400, server alive after |
| **5 MB message payload** | accepted | HTTP 201 in 0.50 s — **no body-size limit existed** → F1, fixed |

### 1.6 Handover — all PASS

| Check | Result | Evidence |
|---|---|---|
| Starter rules message under 20 rapid re-heartbeats | PASS | exactly 1 `HANDOVER_RULES` message in the inbox |
| Handover entry by a revoked agent | PASS | HTTP 401 |
| Normal agent files an entry | PASS | HTTP 201 |

### 1.7 Extension & PWA — PASS with notes (F8, F9)

- **Service worker** (`web2/sw.js`): caches only the six app-shell
  paths; the fetch handler returns network-only for every other GET
  and for all non-GET requests. **API responses are never cached.**
  Verified in the served file over HTTP.
- **Extension manifest** (`extension/manifest.json`): `permissions`
  is exactly `["storage"]` — minimal. Host access is
  `optional_host_permissions` (`http://*/*`, `https://*/*`) and
  `options.js` requests permission for **only the configured node's
  origin** at save time. The admin token is sent only to that node.
  See F9 for the storage note.

---

## 2. Performance results (localhost, this VM, throwaway node)

| Measurement | Result |
|---|---|
| `GET /health` latency (n=50) | median **0.95 ms**, p95 1.80 ms |
| `GET /agents` latency (n=50) | median **2.06 ms**, p95 3.41 ms |
| `GET /messages` latency (n=50) | median **2.56 ms**, p95 3.86 ms |
| Message throughput, 200 sequential sends | **0.70 s total — 3.5 ms/msg, ~286 msg/s** |
| Inbox read after 200 sends | 3.6 ms (returns the 100-message cap — F10) |
| Task lifecycle, 100 tasks × (create + 5 transitions) = 600 calls | **1.91 s total — 3.2 ms/call** |
| `GET /agents` with 50 registered agents (n=30) | median **2.66 ms**, p95 3.16 ms — presence computation cost is negligible |
| Concurrency: 20 parallel inbox readers during 50 writes | **509 reads, 0 read errors, 0 write errors** — no SQLite lock failures observed |
| Federation: 50 cross-node messages, explicit flush after each send | **50/50 delivered; median 13 ms, p95 15 ms, max 16 ms** (send → visible in peer inbox). With the default background flush (2 s interval) instead of an explicit flush, add up to ~2 s |
| GUI payloads | `/` 13,699 B (3,629 B gzipped) · `app.js` 36,752 B (9,533 B) · `app.css` 11,230 B (3,266 B) · `sw.js` 1,726 B (818 B) |
| Gzip | node serves **no** `Content-Encoding: gzip` — raw bytes (F8) |

No performance problems found at this scale. Latency is dominated by
per-request SQLite open/close and JSON work, all in the low
milliseconds; nothing in the hot path does unbounded work except the
(formerly) unbounded body read in F1.

---

## 3. Findings, ranked by severity

### F1 — MEDIUM — No request-body size limit (memory DoS) — **FIXED**
`node.py` `_body()` read exactly `Content-Length` bytes with no cap,
on every POST/PUT route — including the unauthenticated-by-token
`/peer/pair` route. A single request could force the node to buffer
arbitrary bytes in memory (verified: a 5 MB message was accepted,
HTTP 201). **Fix (commit `cd27edf`):** 10 MiB cap; oversized requests
get HTTP 413 and the connection is closed without reading the body.
Verified on the wire with a raw socket (declared 20 MiB →
`413 Request Entity Too Large`), normal traffic unaffected.

### F2 — MEDIUM — Secret scanner is shape-based; obfuscated credentials pass
The scanner (scan.py) reliably blocks canonical credential shapes,
but eight obfuscations passed (§1.3): whitespace/newline-split
tokens, base64-encoded tokens, spaced keywords (`api key:`),
lowercase AWS keys, sub-8-char password values, zero-width
characters inside keywords, and separator-inserted keys. Impact is
bounded — the scanner is defense-in-depth; the vault itself never
leaked in any test, and content that passes lands only in
messages/tasks/handover read by other mesh agents, not in logs. But
a careless or malicious agent *can* smuggle a real credential past
it into a shared channel. **Recommendation (not fixed — this is a
design step, not a patch):** normalize before scanning (strip
zero-width characters, collapse whitespace inside token-shaped
runs), add a decode-and-rescan pass for base64-looking blobs, and
document the scanner as a guardrail, not a guarantee.

### F3 — LOW/MEDIUM — Pairing codes are 32-bit with no attempt throttling
`peers.py` `create_code` mints 8 hex chars (`secrets.token_hex(4)`)
with a 15-minute TTL, and `/peer/pair` has no attempt counter or
lockout — the code is the only authentication for pairing. On the
intended trusted LAN/Tailscale this is a modest risk; it is the
weakest link in the pairing story. **Recommendation (not fixed —
code format is a design/UX decision):** move to ≥64-bit codes, or
add a per-node failed-attempt counter that invalidates outstanding
codes after N failures.

### F4 — LOW — A malformed pairing attempt burned a valid code — **FIXED**
`/peer/pair` consumed the one-time code *before* validating that
`node_id`/`url` were present, so a malformed request permanently
burned a fresh code. **Fix (commit `cd27edf`):** validate the body
first, consume second. Verified: malformed attempt → 400, the same
code then pairs successfully (200), and reuse after real use → 403.

### F5 — LOW — Admin token compared with `==` (timing side channel) — **FIXED**
`Node.is_admin` used plain string equality. **Fix (commit
`cd27edf`):** `hmac.compare_digest`, matching the peer-signature and
vault-MAC code. Verified: correct token 200, same-length wrong
token 401.

### F6 — LOW — Directory updates are not pushed when agents register
After two nodes pair, registering a new agent on node Q does **not**
enqueue a directory update: node P still could not see the new agent
after 4.5 s (multiple background flush cycles), and a message sent
from P to that agent returned 201 but was stored only locally on P —
never queued for delivery — until an admin pressed sync
(`/peers/{id}/sync`), after which the agent appeared and delivery
worked (13 ms median). Senders get no signal that the recipient is
unknown to the mesh. **Recommendation (not fixed — behaviour
change):** enqueue a directory sync on register / pack / disconnect
(the existing content-hash dedupe makes this cheap), and consider a
warning when a message's recipient is neither local nor in the
remote directory.

### F7 — INFO — Within-window replay of signed peer requests is accepted
The HMAC scheme has a 5-minute timestamp window and no nonce, so an
exact captured request replays successfully inside the window
(verified). Impact is small: `/peer/message` dedupes by
correlation_id, task sync is idempotent by origin ids, and
ping/directory are idempotent. Accepted design trade-off; noted
for the record.

### F8 — INFO — No gzip from the node server
`app.js` is 36,752 B raw (9,533 B gzipped). Irrelevant on localhost,
minor on a LAN; the service worker caches the shell after first
load. Optional improvement only.

### F9 — INFO — Extension stores the node admin token in plaintext local storage
`chrome.storage.local` holds `nodeUrl` + `adminToken` unencrypted
(standard for Manifest V3 extensions without a keystore). The token
is sent only to the configured node's origin, and host permission
is requested per-origin. Acceptable; users should know the token
sits in their browser profile.

### F10 — INFO — `GET /messages` is capped at 100
The handler hardcodes `limit=100`; after 200 sends an inbox read
returned exactly 100. Agents polling an inbox with more than 100
unread messages will not see the rest through this route. Worth a
`limit`/pagination parameter in a future version.

---

## 4. Fixes applied

One commit, **`cd27edf`** — *"v2.1 sec/perf fix: cap request body at
10MiB (413), constant-time admin token compare, validate pair body
before consuming pairing code"* (`agent_mesh/node.py` only):

1. F1: `MAX_BODY_BYTES = 10 MiB` in `_body()`; 413 + connection
   close on POST and PUT.
2. F5: `is_admin` uses `hmac.compare_digest`.
3. F4: `/peer/pair` validates `node_id`/`url` before consuming the
   pairing code.

Post-fix verification: the 413 was observed on the wire via raw
socket; the pairing-code sequence (malformed → 400, same code →
200, reuse → 403) was exercised end-to-end; the full custom battery
was re-run — **57/57 + 7/7 PASS** — and the project suite is green
(below). Everything else in §3 is reported, not redesigned, per the
audit brief.

## 5. Final test-suite result

After all fixes, on HEAD `cd27edf`:

```
$ python3 -m unittest discover -s tests
Ran 90 tests in 36.822s
OK
```

**Bottom line:** the v2.1 security core — auth separation, vault
grants, one-shot approvals, revocation, peer signing — held up
under every direct attack in the battery, and performance is
comfortably in single-digit milliseconds at this scale. The real
residual risks are the shape-based scanner (F2) and the 32-bit
pairing code (F3); both are documented above with recommendations
for a future version.
