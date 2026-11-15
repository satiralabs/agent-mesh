# Agent Mesh browser extension

A status glance for your Agent Mesh node, in any Chromium browser
(Chrome, Brave, Edge, Chromium): is the node reachable, how many
agents are online / idle / offline (presence computed exactly like
the node computes it), and how many approvals are waiting for you —
plus one-click buttons into the GUI and the Approvals screen.

The extension talks to **no server except the node you configure**.
Your admin token is kept in `chrome.storage.local` and is sent only
to your own node, as a header, on the same read paths the GUI uses.

## Install (unpacked)

1. Open `chrome://extensions` (Brave: `brave://extensions`,
   Edge: `edge://extensions`).
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and choose this `extension/` folder.
4. Click the Agent Mesh toolbar icon, then **Settings**:
   - **Node URL** — e.g. `http://127.0.0.1:8790` (or your node's
     Tailscale/LAN address).
   - **Admin token** — the same token you use to log into the GUI.
5. Click **Save** and accept the permission prompt — it grants the
   extension access to *that node's address only*.

## Use

Click the toolbar icon any time: a green dot means the node answered;
the counts update on open. **Open GUI** opens the full control
centre; **Open Approvals** opens it straight on the Approvals screen
(the place where gated vault uses and credential reveals wait for
your decision).

## Files

- `manifest.json` — Manifest V3; permissions are only `storage` plus
  *optional* host access, requested per-node at save time.
- `popup.html` / `popup.js` / `popup.css` — the status popup.
- `options.html` / `options.js` — node URL + admin token settings.
