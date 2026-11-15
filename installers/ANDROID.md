# Agent Mesh on Android

Two ways to use Agent Mesh from an Android phone:

## Option 1 — Install the GUI as an app (PWA) — recommended

The node's web GUI is an installable web app. This is the normal way
to use the mesh from a phone: check agents, approve gated vault uses
and credential reveals, read handover entries.

1. Make sure the phone can reach the node: same Wi-Fi/LAN as the
   machine running the node, or Tailscale on both (recommended —
   mesh traffic is authenticated but not encrypted, so do not expose
   a node to the open internet).
2. In **Chrome** on the phone, open the node's GUI, e.g.
   `http://<pc-address>:8790/` or its Tailscale address.
3. Log in with the admin token.
4. Tap the ⋮ menu → **Add to Home screen** (or accept the
   **Install app** prompt).
5. The Agent Mesh icon now sits on the home screen and opens
   full-screen like a native app. Data is never cached on the phone —
   the app shell is cached, every screen's data comes live from your
   node.

## Option 2 — Run a full node on the phone (Termux)

For an always-on pocket node. Termux provides a real Linux shell;
the mesh node is pure Python, so it runs as-is.

```bash
pkg update && pkg install -y python git openssl
git clone https://github.com/satiralabs/agent-mesh.git
cd agent-mesh

# Your node secrets — pick your own values, keep them private.
export AGENT_MESH_ADMIN='choose-an-admin-token'
export AGENT_MESH_MASTER='choose-a-vault-passphrase'

# Optional but wise on battery: keep the CPU awake while it runs.
termux-wake-lock

python -m agent_mesh.node --port 8790 --name "Phone Node" \
  --data-dir ~/.agent-mesh-node
```

Then open `http://127.0.0.1:8790/` in the phone's browser for the
GUI, and pair this node with your other machines from
**Settings → Peers** (pair only over LAN/Tailscale you trust).

Notes:

- Android may kill Termux in the background; `termux-wake-lock` plus
  excluding Termux from battery optimisation keeps a node alive.
- Re-running the node with the same `--data-dir` keeps your agents,
  vault, tasks and handover entries.
