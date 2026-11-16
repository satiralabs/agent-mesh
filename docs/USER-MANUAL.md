# Agent Mesh — User Manual

Agent Mesh lets your AI agents work together. You give the orders
from one screen; the agents message each other, share tasks, and use
shared logins and API keys safely — without you copying and pasting
between chat windows.

This manual uses the web app (the GUI) for everything. No terminal
needed, except where it says so.

## 1. Open the app and log in

1. Open the address your installer gave you — usually
   **http://127.0.0.1:8790/** on the computer where the node runs.
2. Type your **admin token** (the installer printed it, and it is
   saved in a file next to your node — keep it private, it is the
   master key to the mesh).
3. Click **Log in**. The token is remembered only for this browser
   session.

On a phone on the same network/Tailscale, the same address works —
see §11 for installing it like an app.

## 2. Read the dashboard

The **Dashboard** is your overview:

- **Agents** — how many you have, and how many are online right now.
- **Tasks** — what is waiting, running, and done.
- **Approvals waiting** — things an agent wants to do that need your
  yes (see §8). If this number is not zero, an agent is paused
  waiting for you.
- **Peers** — other computers connected to this one.

Agent dots mean: **green** = seen in the last 2 minutes,
**amber** = idle (seen in the last 15 minutes), **grey** = offline,
**red badge** = disconnected by you.

## 3. Add an agent

1. Go to **Agents** → **Add agent**.
2. Give it a name you will recognise (e.g. `Antigravity`), pick a
   role, and list what it can do (`coding, research, browser`).
   - **Orchestrator** — can give tasks to other agents.
   - **Worker** — only receives tasks and reports back.
3. Click **Add**. The agent appears in the list, offline for now.
4. Click **Connection pack** next to it and copy the pack. The pack
   contains the agent's private token — treat it like a password.
5. Give the pack to the agent (table below), then restart that
   agent or start a fresh session so it loads the pack.

| Your agent is… | Use this pack | Where it goes |
|---|---|---|
| An MCP tool (Claude, Cursor-like tools, Antigravity) | MCP pack | The tool's MCP config file |
| Claude Code | Claude Code pack | `.mcp.json` / Claude Code settings |
| Cursor | Cursor pack | `.cursor/mcp.json` |
| A script or shell-only agent | REST pack | Save the env file; it uses the mesh CLI commands in the pack notes |
| A chat agent with no tools | Chat-paste pack | Paste the whole text block into its chat and follow it together |

To check it worked, ask the agent in its chat:
*"Use your mesh tools: check your inbox and tell me your agent name."*
If it answers with the name you registered, it is connected. It will
also turn green in your Agents list.

**One agent, one identity.** If the same program runs in two places
(for example a CLI and an editor extension), register it twice with
two names — each gets its own inbox and its own tasks.

## 4. Give a task

1. Go to **Tasks** → **New task**.
2. Write a clear title and instructions: what to do, what "done"
   looks like, and any limits (for example *"read-only — do not
   change anything"*).
3. Pick the **lead** agent — the one responsible for the result.
4. Optionally add **teammates** (participants): tick other agents
   who should see the task, discuss it and help.
5. Click **Create**, then **Send**.

The task moves through: **Sent → Accepted → In progress → In review
→ Done** (or Failed). You can watch it live on the Tasks screen.

If the agent is an interactive chat (an editor assistant, a chat
window), it picks the task up when its session is next active. If
the agent has the autonomous worker (§10), it starts by itself.

## 5. Watch and discuss a task

Click any task to open it. You can see:

- the instructions, the lead, and all teammates;
- **progress notes** the agents add while working;
- the **discussion thread** — every participant (and you) can post
  comments here; agents read them too;
- the **result** when it finishes.

Use the thread instead of chasing agents in separate chats: post a
comment like *"change of plan: do the disk check first"* and every
agent on the task sees it.

You can add or remove teammates while the task is still open (the
lead cannot be removed). Removing a teammate on another computer
stops them seeing new updates there, but they keep what they already
received until the task ends.

## 6. Messages

The **Messages** screen shows what agents are sending each other and
lets you send a message to any agent yourself. Messages to agents on
other computers are delivered automatically — if that computer is
off, the message waits and delivers when it returns.

## 7. The vault: shared logins and keys, safely

The **Vault** holds credentials (API keys, website logins, SSH
details) so agents can *use* them without *seeing* them.

**Add a credential**

1. Go to **Vault** → **Add credential**.
2. Pick the type (API key, Login, SSH, OAuth token, Other), give it
   a name, and fill in the details. The secret part is encrypted
   when saved and is never shown again in the app.

**Share it with an agent**

On the credential, click **Share**, pick the agent, and choose a
mode:

- **Use** — the safe default. The agent can *use* the credential
  (for example, the mesh makes the API call, or gives the agent the
  server address and username for an SSH login), but the password or
  key itself never leaves your node.
- **Reveal** — the agent actually receives the secret value. This
  always asks your approval first (see §8), one approval lets it
  reveal exactly once, and every reveal is written into the
  activity log (without the value). Only choose this when an agent
  truly cannot work any other way. You can also mark a grant
  "standing reveal" for an agent you fully trust — then it no
  longer asks each time.

Remove a share at any time from the same panel; access stops
immediately.

## 8. Approvals

When an agent wants to do something gated — use a protected
credential, or reveal a secret — it pauses and the request appears
under **Approvals**. Check who is asking and what for, then
**Approve** (allows it once) or **Deny**. Nothing gated happens
without your yes, and every decision is logged under **Activity**.

## 9. Handover

Agents are expected to leave a written handover: what they were
asked, what they did, how they checked it, and what is still open.
The rules are sent to every agent automatically when it connects.

The **Handover** screen shows all entries filed on this computer.
Agents file them with their mesh tools; an agent that cannot file
its own can be covered by the handover script (ask your
administrator, or see the developer guide). The node also keeps a
`HANDOVER-<computer>.md` file next to its data that collects every
entry.

## 10. The autonomous worker (agents that work on their own)

An interactive chat agent can only act while its session is open —
no tool can wake it. The **worker** fixes that for agents that have
a command-line mode: it is a small helper that watches the mesh,
picks up new tasks for its agent, runs them, and posts the results
back. One worker serves one agent.

- It only runs tasks from people/agents on its trust list.
- **Pause it:** your administrator can stop the worker service, or
  place a `PAUSED` file in its folder — it then idles until removed.
- Everything it runs is logged, one log file per task.

If a task for a chat agent seems to sit at **Sent**, this is
usually why: the agent's session is not active, and it has no
worker. Open its session (or ask for a worker to be installed for
it).

## 11. Browser extension and Android

**Browser extension** (Chrome/Edge and other Chromium browsers):
your administrator loads the `extension/` folder as an unpacked
extension once. After that, its toolbar button shows whether your
node is up, how many agents are online, and whether approvals are
waiting — and opens the GUI in one click. Set your node address and
admin token in the extension's options page.

**Android:** open the GUI address in Chrome on your phone (your
phone must reach the node — same network or Tailscale), tap the
browser menu, and choose **Add to Home screen / Install app**. You
get an app icon with the full GUI. (A full node can also run on
Android under Termux — see `installers/ANDROID.md`.)

## 12. Connect a second computer (pairing)

1. Install Agent Mesh on the second computer and open its GUI.
2. There, go to **Settings → Peers** and click **Generate pairing
   code**. The code is shown once and lasts 15 minutes.
3. On your first computer, **Settings → Peers → Pair**: enter the
   second computer's node address and the code.
4. Done. Each side now lists the other's agents as **remote**, and
   you can task them exactly like local ones.

Pair only over your home network or Tailscale — the connection
between computers is authenticated but not encrypted by Agent Mesh
itself, and Tailscale encrypts it for you.

## 13. Disconnect an agent

**Agents** screen → **Disconnect** next to the agent. Its token
stops working immediately and it shows as **Revoked**. Its old tasks
and messages stay visible for your records. To bring it back, click
**Reconnect (new pack)** and give the agent the fresh pack — the old
one stays dead.

## 14. Troubleshooting

| Problem | What it usually means | What to do |
|---|---|---|
| Agent shows **offline** | Its session is closed, or it has not checked in for 15+ minutes | Open the agent's session, or start its worker. A grey dot is not an error by itself |
| Agent shows **online** but has not read my task | "Online" only means its connector checked in | Interactive agents read tasks when their session is active; give it a nudge in its chat, or use a worker |
| Message has not arrived | The other computer is off, or the nodes lost contact | Check **Settings → Peers** and the outbox count; messages deliver automatically when the peer returns |
| GUI does not load | The node service is stopped, or a stale app window points at an old address | Open the address in a normal browser tab and hard-refresh (Ctrl+Shift+R). If still nothing, restart the node service (or ask your administrator) |
| Login token rejected | Wrong token, or you are on the wrong computer's GUI | Use the admin token saved by that node's installer; each computer has its own |
| Worker is not picking up tasks | It is paused, stopped, or the task is from someone not on its trust list | Check for a `PAUSED` file, check the worker service is running, check its log folder |
| An agent says it has no mesh tools | Its session started before the pack was added | Restart the agent / start a fresh session so it loads the pack |
| Approval never appears | The agent has not asked yet, or it is on the other computer's node | Approvals live on the agent's home node — check that node's GUI |

## 15. Three habits that keep the mesh healthy

1. **One identity per agent seat.** CLI, editor, and chat versions
   of the "same" assistant get separate names.
2. **Write tasks like orders:** what to do, what done looks like,
   what not to touch.
3. **Prefer "Use" over "Reveal"** in the vault, and disconnect
   agents you no longer use.
