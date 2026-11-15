# Agent Mesh Handover Rules

Version: 1.0

These rules are the handover standard for every agent on this mesh.
The node delivers them to each agent once, when it connects. Follow
them for every work session, without exception.

## The standard

1. **Use your one unique, stable agent name everywhere.** Your mesh
   agent name is your identity. Use it at the top of your handover and
   in every session heading and session list. Never write entries
   under a generic label ("the agent", "Muse", "the assistant") and
   never claim another agent's sessions as yours.

2. **One independent entry per session — never merged.** Every chat /
   session gets its own dated handover entry under its own heading.
   Do not merge sessions into vague summaries; a later reader must be
   able to reconstruct one session without reading the others.

3. **Every entry records, in this order:**
   - what the user asked for;
   - what was actually done;
   - how it was verified (the check that was run, not just "it works");
   - decisions taken, and who took them;
   - the end state (what is live, what is stopped, what is where);
   - open items still outstanding.

4. **List ALL related files** for the session: machine + full path for
   each one (and a link where the file lives in shared storage).

5. **Record failures and corrections plainly.** If something broke, was
   done wrong, or had to be redone, say so in the entry. Do not smooth
   it over and do not silently drop it.

6. **NEVER write credential values** — no passwords, tokens, API keys,
   or private keys — in a handover entry, a message, or any file.
   Locations only ("the token is in the 0600 file at <path>"), never
   the value itself.

7. **Update the handover in the same session** the work happened in,
   not later from memory.

The test of a good entry: a brand-new agent can pick up the session
cold, from the handover alone, and continue correctly.

## How to file an entry on this mesh

Use whichever of these works for you — the node stores the entry and
updates the node's handover file either way:

- MCP tool: `mesh_handover_log` with fields `title`, `asked`, `done`,
  `verified`, `decisions`, `files`, `open`.
- CLI: `python3 -m agent_mesh.handover --title "..." --asked "..."
  --done "..." --verified "..." --decisions "..." --files "..."
  --open "..."` (with your pack environment: AGENT_MESH_NODE and
  AGENT_MESH_TOKEN set).
- If you cannot run either, tell the owner what the entry should say
  and ask them (or another agent) to file it for you — an unfiled
  session is a lost session.
