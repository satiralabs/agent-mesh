"use strict";
/* Agent Mesh GUI (Phase 3). All logic lives in the node API; this file
   only renders and calls it. The admin token lives in sessionStorage
   only — never localStorage, never in this file. */
const TOKEN_KEY = "agentMeshAdmin";
const TRANSITIONS = {
  DRAFT: ["SENT"], SENT: ["ACCEPTED", "REJECTED"],
  ACCEPTED: ["IN_PROGRESS"], REJECTED: [],
  IN_PROGRESS: ["IN_REVIEW", "FAILED"],
  IN_REVIEW: ["DONE", "FAILED", "IN_PROGRESS"], DONE: [], FAILED: [],
};
const state = { agents: [], tasks: [], services: {}, approvals: [],
                entries: [], messages: [], peers: [], outbox: null,
                nodeInfo: null, screen: "dashboard" };
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v == null ? "" : v)
  .replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;",
                               '"': "&quot;", "'": "&#39;"}[c]));
const fmtTime = (ts) => {
  if (ts == null || ts === "") return "";
  if (typeof ts === "number") return new Date(ts * 1000).toLocaleString();
  return String(ts);
};
const agentName = (id) => {
  if (id === "owner") return "Owner (you)";
  const a = state.agents.find((x) => x.id === id);
  return a ? a.name : id;
};

function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  setTimeout(() => t.classList.add("hidden"), 4200);
}

async function api(path, opts) {
  opts = opts || {};
  const headers = { "X-Admin-Token": sessionStorage.getItem(TOKEN_KEY) || "" };
  if (opts.body) headers["Content-Type"] = "application/json";
  const resp = await fetch(path, {
    method: opts.method || "GET", headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(data.error || ("HTTP " + resp.status));
    err.data = data; err.status = resp.status;
    throw err;
  }
  return data;
}

/* ---------- data loading ---------- */
async function loadAll() {
  const [agents, tasks, services, approvals, activity, messages,
         peers, nodeInfo] =
    await Promise.all([
      api("/agents"), api("/tasks"), api("/vault/services"),
      api("/approvals"), api("/activity?limit=100"), api("/messages"),
      api("/peers"), api("/node/info"),
    ]);
  state.agents = agents.agents;
  state.tasks = tasks.tasks;
  state.services = services.services;
  state.approvals = approvals.approvals;
  state.entries = activity.entries;
  state.messages = messages.messages;
  state.peers = peers.peers;
  state.outbox = peers.outbox;
  state.nodeInfo = nodeInfo;
  $("node-status").innerHTML = '<span class="dot"></span>node online';
  renderAll();
}

function updateApprovalBadge() {
  const pending = state.approvals.filter((a) => a.status === "pending").length;
  const badge = $("nav-approval-badge");
  badge.textContent = pending;
  badge.classList.toggle("hidden", pending === 0);
}

function renderAll() {
  renderDashboard(); renderAgents(); renderTasks();
  renderMessages(); renderVault(); renderApprovals(); renderActivity();
  renderPeers();
  updateApprovalBadge();
}

/* ---------- dashboard ---------- */
function renderDashboard() {
  const by = (s) => state.agents.filter((a) => a.status === s).length;
  $("stat-agents-online").textContent = by("online");
  $("stat-agents-busy").textContent = by("busy");
  $("stat-agents-offline").textContent = by("offline");
  const open = state.tasks.filter(
    (t) => !["DONE", "FAILED", "REJECTED"].includes(t.status)).length;
  $("stat-tasks-open").textContent = open;
  $("stat-approvals").textContent =
    state.approvals.filter((a) => a.status === "pending").length;
  const counts = {};
  state.tasks.forEach((t) => { counts[t.status] = (counts[t.status] || 0) + 1; });
  $("dash-task-counts").innerHTML = Object.keys(counts).length
    ? Object.entries(counts).map(([s, n]) =>
        `<span class="chip">${esc(s)} <b>${n}</b></span>`).join("")
    : '<span class="muted">No tasks yet.</span>';
  $("dash-activity").innerHTML = state.entries.slice(0, 8).map((e) =>
    `<div class="line"><span>${esc(fmtTime(e.ts))}</span><b>${esc(e.event || "vault_use")}</b>` +
    `<span>${esc(agentName(e.agent))}</span><span>${esc(e.service || "")}</span>` +
    `<span>${esc(e.outcome || "")}</span></div>`).join("")
    || '<span class="muted">No activity yet.</span>';
}

/* ---------- agents ---------- */
function renderAgents() {
  $("agents-list").innerHTML = state.agents.length ? state.agents.map((a) =>
    `<div class="row-item"><div class="row-top">
       <b>${esc(a.name)}</b><span class="pill ${esc(a.status)}">${esc(a.status)}</span>
       <span class="chip">${esc(a.role)}</span>
       ${a.remote ? `<span class="chip">remote · home ${esc(a.home_node)}</span>` : ""}
       <span class="spacer"></span>
       ${a.remote ? "" : `<button class="btn small" data-pack="${esc(a.id)}">Connection pack</button>`}
     </div>
     <div class="muted">${esc(a.id)} · ${esc(a.machine || "no machine")} · ` +
     `capabilities: ${esc((a.capabilities || []).join(", ") || "none")} · ` +
     `last seen ${esc(fmtTime(a.last_seen))}</div></div>`).join("")
    : '<p class="muted">No agents yet — add the first one below.</p>';
  document.querySelectorAll("[data-pack]").forEach((btn) =>
    btn.addEventListener("click", () => showPack(btn.dataset.pack)));
  const opts = state.agents.map((a) =>
    `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("");
  $("task-assignee").innerHTML =
    '<option value="">— choose —</option>' + opts;
  $("message-recipient").innerHTML = opts;
}

async function showPack(agentId) {
  if (!confirm("Make a NEW connection pack for this agent? Its old token " +
               "stops working — the agent must be restarted with the new pack."))
    return;
  try {
    const out = await api(`/agents/${agentId}/pack`, { method: "POST", body: {} });
    displayPack(out.pack);
    toast("New pack made. The agent's old token no longer works.");
  } catch (e) { toast(e.message, true); }
}

function displayPack(pack) {
  $("pack-panel").classList.remove("hidden");
  $("pack-text").textContent = JSON.stringify(pack, null, 2);
  $("pack-panel").scrollIntoView({ behavior: "smooth" });
}

$("agent-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const caps = $("agent-caps").value.split(",").map((s) => s.trim()).filter(Boolean);
  try {
    const out = await api("/agents/pack", { method: "POST", body: {
      name: $("agent-name").value, machine: $("agent-machine").value,
      capabilities: caps, role: $("agent-role").value } });
    displayPack(out.pack);
    ev.target.reset();
    await loadAll();
    toast("Agent added — its connection pack is shown below the form.");
  } catch (e) { toast(e.message, true); }
});

$("pack-copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("pack-text").textContent);
        toast("Pack copied."); }
  catch (e) { toast("Copy failed — select the text and copy manually.", true); }
});

/* ---------- tasks ---------- */
function renderTasks() {
  const groups = {};
  state.tasks.forEach((t) => { (groups[t.status] = groups[t.status] || []).push(t); });
  const order = ["SENT", "ACCEPTED", "IN_PROGRESS", "IN_REVIEW", "DRAFT",
                 "DONE", "FAILED", "REJECTED"];
  $("tasks-board").innerHTML = order.filter((s) => groups[s]).map((s) =>
    `<div class="board-group"><h4>${esc(s)} (${groups[s].length})</h4>` +
    groups[s].map((t) =>
      `<div class="row-item task-item" data-task="${esc(t.id)}">
         <div class="row-top"><b>${esc(t.title)}</b>
           <span class="pill ${esc(t.status)}">${esc(t.status)}</span>
           <span class="chip">${esc(t.priority)}</span><span class="spacer"></span>
           <span class="muted">${esc(agentName(t.requester))} → ` +
           `${esc(t.assignee_agent ? agentName(t.assignee_agent)
                                   : "capability: " + (t.assignee_capability || "?"))}</span>
         </div></div>`).join("") + "</div>").join("")
    || '<p class="muted">No tasks yet.</p>';
  document.querySelectorAll("[data-task]").forEach((el) =>
    el.addEventListener("click", () => showTask(el.dataset.task)));
}

async function showTask(taskId) {
  try {
    const out = await api(`/tasks/${taskId}`);
    const t = out.task;
    const notes = (t.progress_notes || []).map((n) =>
      `<div class="line"><span>${esc(fmtTime(n.at))}</span><b>${esc(agentName(n.agent))}</b><span>${esc(n.note)}</span></div>`).join("");
    const buttons = (TRANSITIONS[t.status] || []).map((to) => {
      const label = to === "DONE" ? "Approve review → Done" : "Move to " + to;
      const cls = to === "DONE" ? "btn good small" :
                  (to === "FAILED" || to === "REJECTED") ? "btn danger small" : "btn small";
      return `<button class="${cls}" data-to="${to}">${label}</button>`;
    }).join(" ");
    $("task-detail").classList.remove("hidden");
    $("task-detail").innerHTML =
      `<div class="row-top"><b>${esc(t.title)}</b>
         <span class="pill ${esc(t.status)}">${esc(t.status)}</span></div>
       <p>${esc(t.instructions || "")}</p>
       <div class="muted">From ${esc(agentName(t.requester))} · to ` +
       `${esc(t.assignee_agent ? agentName(t.assignee_agent)
                               : "capability: " + (t.assignee_capability || "?"))} · ` +
       `priority ${esc(t.priority)} · created ${esc(fmtTime(t.created_at))}</div>
       ${t.result ? `<div><h4>Result</h4><p>${esc(t.result)}</p></div>` : ""}
       <div><h4>Progress notes</h4><div class="mini-list">${notes || '<span class="muted">None yet.</span>'}</div></div>
       <form id="task-note-form" class="inline-form">
         <label>Add a note <input id="task-note-text"></label>
         <button class="btn small" type="submit">Add note</button></form>
       <div class="inline-form">${buttons}</div>`;
    $("task-detail").scrollIntoView({ behavior: "smooth" });
    $("task-note-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        await api(`/tasks/${t.id}/progress`, { method: "POST",
          body: { note: $("task-note-text").value } });
        await loadAll(); showTask(t.id);
      } catch (e) { toast(e.message, true); }
    });
    $("task-detail").querySelectorAll("[data-to]").forEach((btn) =>
      btn.addEventListener("click", async () => {
        let result;
        if (btn.dataset.to === "DONE")
          result = prompt("Result / completion note (optional):") || undefined;
        try {
          await api(`/tasks/${t.id}/transition`, { method: "POST",
            body: { to: btn.dataset.to, result } });
          toast("Task is now " + btn.dataset.to);
          await loadAll(); showTask(t.id);
        } catch (e) { toast(e.message, true); }
      }));
  } catch (e) { toast(e.message, true); }
}

$("task-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = { title: $("task-title").value,
                 instructions: $("task-instructions").value,
                 priority: $("task-priority").value };
  if ($("task-assignee").value) body.assignee_agent = $("task-assignee").value;
  if ($("task-capability").value) body.assignee_capability = $("task-capability").value;
  try {
    const out = await api("/tasks", { method: "POST", body });
    await api(`/tasks/${out.task.id}/transition`,
              { method: "POST", body: { to: "SENT" } });
    ev.target.reset();
    await loadAll();
    toast("Task created and sent.");
  } catch (e) { toast(e.message, true); }
});

/* ---------- messages ---------- */
function renderMessages() {
  const items = [...state.messages].reverse().slice(0, 50);
  $("messages-list").innerHTML = items.length ? items.map((m) =>
    `<div class="row-item"><div class="row-top">
       <b>${esc(agentName(m.sender))}</b><span>→</span>
       <b>${esc(agentName(m.recipient))}</b>
       <span class="chip">${esc(m.msg_type)}</span><span class="spacer"></span>
       <span class="muted">${esc(fmtTime(m.created_at))}</span></div>
     <div>${esc(typeof m.payload === "string" ? m.payload
                                              : JSON.stringify(m.payload))}</div></div>`).join("")
    : '<p class="muted">No messages yet.</p>';
}

$("message-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("/messages", { method: "POST", body: {
      recipient: $("message-recipient").value,
      msg_type: $("message-type").value,
      payload: $("message-text").value } });
    $("message-text").value = "";
    await loadAll();
    toast("Message sent.");
  } catch (e) { toast(e.message, true); }
});

/* ---------- vault ---------- */
function renderVault() {
  const names = Object.keys(state.services);
  $("vault-list").innerHTML = names.length ? names.map((name) => {
    const s = state.services[name];
    const aclText = Object.entries(s.acl || {}).map(([a, acts]) =>
      `${agentName(a)}: ${acts.join("/")}`).join(" · ") || "no agent allowed yet";
    const agentOpts = state.agents.map((a) =>
      `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("");
    return `<div class="row-item"><div class="row-top">
        <b>${esc(name)}</b>
        <span class="chip">secret stored ✓ (never shown)</span>
        ${s.requires_approval ? '<span class="pill needs_approval">approval needed</span>' : ""}
        <span class="spacer"></span>
        <button class="btn small danger" data-vdel="${esc(name)}">Delete</button>
      </div>
      <div class="muted">Upstream: ${esc(s.upstream || "—")} · Who may use it: ${esc(aclText)}</div>
      <form class="inline-form" data-acl="${esc(name)}">
        <label>Agent <select name="agent">${agentOpts}</select></label>
        <span class="checks">
          <label><input type="checkbox" name="read"> read</label>
          <label><input type="checkbox" name="write"> write</label>
          <label><input type="checkbox" name="spend"> spend</label>
        </span>
        <label class="check"><input type="checkbox" name="gated" ${s.requires_approval ? "checked" : ""}> needs approval</label>
        <button class="btn small" type="submit">Save access</button>
      </form>
      <form class="inline-form" data-check="${esc(name)}">
        <label>Test as agent <select name="agent">${agentOpts}</select></label>
        <label>Action <select name="action">
          <option>read</option><option>write</option><option>spend</option>
        </select></label>
        <button class="btn small" type="submit">Test use</button>
        <span class="muted" data-check-out></span>
      </form>
    </div>`;
  }).join("") : '<p class="muted">No services yet — add the first credential above.</p>';

  document.querySelectorAll("[data-vdel]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm(`Delete the credential for "${btn.dataset.vdel}"? ` +
                   "Agents will no longer be able to use it.")) return;
      try {
        await api(`/vault/services/${btn.dataset.vdel}`, { method: "DELETE" });
        await loadAll(); toast("Service deleted.");
      } catch (e) { toast(e.message, true); }
    }));

  document.querySelectorAll("[data-acl]").forEach((form) =>
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const name = form.dataset.acl;
      const current = JSON.parse(JSON.stringify(state.services[name].acl || {}));
      const agent = form.agent.value;
      const acts = ["read", "write", "spend"].filter((a) => form[a].checked);
      if (acts.length) current[agent] = acts; else delete current[agent];
      try {
        await api(`/vault/services/${name}`, { method: "PUT", body: {
          acl: current, requires_approval: form.gated.checked } });
        await loadAll(); toast("Access saved for " + name + ".");
      } catch (e) { toast(e.message, true); }
    }));

  document.querySelectorAll("[data-check]").forEach((form) =>
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const out = form.querySelector("[data-check-out]");
      try {
        const r = await api("/vault/use-check", { method: "POST", body: {
          agent_id: form.agent.value, service: form.dataset.check,
          action: form.action.value } });
        out.textContent = "Result: " + r.outcome +
          (r.approval_id ? " (approval request " + r.approval_id + " is waiting)" : "");
        await loadAll();
      } catch (e) {
        // A denial comes back as HTTP 403 with {"outcome": "deny"}.
        out.textContent = "Result: " + ((e.data && e.data.outcome) || "deny");
        await loadAll();
      }
    }));
}

$("vault-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("/vault/services", { method: "POST", body: {
      service: $("vault-name").value, upstream: $("vault-upstream").value,
      secret: $("vault-secret").value,
      requires_approval: $("vault-gated").checked, acl: {} } });
    ev.target.reset();
    await loadAll();
    toast("Credential saved (encrypted). Now set who may use it, below.");
  } catch (e) { toast(e.message, true); }
});

/* ---------- approvals ---------- */
function approvalCard(a, withButtons) {
  return `<div class="row-item"><div class="row-top">
      <b>${esc(agentName(a.agent_id))}</b><span>wants</span>
      <span class="chip">${esc(a.action)}</span><span>on</span>
      <b>${esc(a.service)}</b>
      <span class="pill ${esc(a.status)}">${esc(a.status)}${a.consumed ? " · used" : ""}</span>
      <span class="spacer"></span><span class="muted">${esc(fmtTime(a.created_at))}</span>
      ${withButtons ? `<button class="btn small good" data-approve="${esc(a.id)}">Approve once</button>
                       <button class="btn small danger" data-deny="${esc(a.id)}">Deny</button>` : ""}
    </div></div>`;
}

function renderApprovals() {
  const pending = state.approvals.filter((a) => a.status === "pending");
  const history = state.approvals.filter((a) => a.status !== "pending").reverse();
  $("approvals-pending").innerHTML = pending.length
    ? pending.map((a) => approvalCard(a, true)).join("")
    : '<p class="muted">Nothing waiting. When an agent tries to use a gated service, its request lands here.</p>';
  $("approvals-history").innerHTML = history.length
    ? history.map((a) => approvalCard(a, false)).join("")
    : '<p class="muted">No decisions yet.</p>';
  const decide = async (id, decision) => {
    try {
      await api(`/approvals/${id}/decide`, { method: "POST", body: { decision } });
      await loadAll();
      toast("Approval " + (decision === "approve" ? "granted — one use only." : "denied."));
    } catch (e) { toast(e.message, true); }
  };
  document.querySelectorAll("[data-approve]").forEach((b) =>
    b.addEventListener("click", () => decide(b.dataset.approve, "approve")));
  document.querySelectorAll("[data-deny]").forEach((b) =>
    b.addEventListener("click", () => decide(b.dataset.deny, "deny")));
}

/* ---------- activity ---------- */
function renderActivity() {
  $("activity-list").innerHTML = state.entries.length
    ? `<table><thead><tr><th>When</th><th>Event</th><th>Agent</th><th>Service / task</th><th>Action</th><th>Outcome</th></tr></thead><tbody>` +
      state.entries.map((e) =>
        `<tr><td>${esc(fmtTime(e.ts))}</td><td>${esc(e.event || "vault_use")}</td>` +
        `<td>${esc(agentName(e.agent))}</td><td>${esc(e.service || e.task || "")}</td>` +
        `<td>${esc(e.action || "")}</td><td>${esc(e.outcome || "")}</td></tr>`).join("") +
      "</tbody></table>"
    : '<p class="muted">No activity yet.</p>';
  $("settings-node").innerHTML =
    `<div class="line"><span>Node URL</span><b>${esc(location.origin)}</b></div>
     <div class="line"><span>Agents registered</span><b>${state.agents.length}</b></div>
     <div class="line"><span>Tasks total</span><b>${state.tasks.length}</b></div>
     <div class="line"><span>Vault services</span><b>${Object.keys(state.services).length}</b></div>`;
}

/* ---------- peers (Phase 4) ---------- */
function renderPeers() {
  const info = state.nodeInfo || {};
  $("peers-this-node").innerHTML =
    `<div class="line"><span>This node</span><b>${esc(info.name || "")}</b></div>
     <div class="line"><span>Node ID</span><b>${esc(info.node_id || "")}</b></div>
     <div class="line"><span>Node URL (give this to the other machine)</span><b>${esc(info.url || location.origin)}</b></div>`;
  $("peers-list").innerHTML = state.peers.length ? state.peers.map((p) =>
    `<div class="row-item"><div class="row-top">
       <b>${esc(p.name || p.node_id)}</b>
       <span class="pill ${esc(p.status)}">${esc(p.status)}</span>
       <span class="spacer"></span>
       <button class="btn small" data-psync="${esc(p.node_id)}">Sync directory</button>
     </div>
     <div class="muted">${esc(p.node_id)} · ${esc(p.url)} · ` +
     `paired ${esc(fmtTime(p.paired_at))} · last seen ${esc(fmtTime(p.last_seen))}</div></div>`).join("")
    : '<p class="muted">No paired machines yet — pair the first one above.</p>';
  document.querySelectorAll("[data-psync]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      try {
        await api(`/peers/${btn.dataset.psync}/sync`, { method: "POST", body: {} });
        await api("/peers/flush", { method: "POST", body: {} });
        await loadAll();
        toast("Directory sync queued and sent.");
      } catch (e) { toast(e.message, true); }
    }));
  const c = state.outbox || { pending: 0, delivered: 0, failed: 0 };
  $("outbox-counts").textContent =
    `Outbox: ${c.pending} waiting · ${c.delivered} delivered · ${c.failed} failed`;
}

$("pair-code-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const out = await api("/peers/pairing-code", { method: "POST", body: {} });
    $("pair-code-out").innerHTML =
      `Code <b>${esc(out.code)}</b> — enter it on the other machine ` +
      `before ${esc(fmtTime(out.expires_at))}. It works once.`;
  } catch (e) { toast(e.message, true); }
});

$("pair-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const out = await api("/peers/pair", { method: "POST", body: {
      peer_url: $("pair-url").value.trim(), code: $("pair-code").value.trim() } });
    ev.target.reset();
    await api("/peers/flush", { method: "POST", body: {} }).catch(() => {});
    await loadAll();
    toast("Paired with " + (out.peer.name || out.peer.node_id) + ".");
  } catch (e) { toast(e.message, true); }
});

$("outbox-flush").addEventListener("click", async () => {
  try {
    const out = await api("/peers/flush", { method: "POST", body: {} });
    await loadAll();
    toast(`Outbox sent: ${out.delivered} delivered, ${out.failed} still waiting.`);
  } catch (e) { toast(e.message, true); }
});

/* ---------- auto-refresh (Phase 5) ----------
   Every 10s, refresh ONLY the current screen's data (the same api()
   calls + render functions loadAll uses). Paused while the tab is
   hidden, and skipped while a form in the main content is being
   edited, so a refresh never yanks a half-typed form away. */
const AUTO_REFRESH_MS = 10000;
let autoRefreshTimer = null;

const SCREEN_REFRESH = {
  dashboard: { load: async () => {
      const [agents, tasks, approvals, activity] = await Promise.all([
        api("/agents"), api("/tasks"), api("/approvals"),
        api("/activity?limit=100")]);
      state.agents = agents.agents; state.tasks = tasks.tasks;
      state.approvals = approvals.approvals; state.entries = activity.entries;
    }, render: () => { renderDashboard(); updateApprovalBadge(); } },
  agents: { load: async () => {
      state.agents = (await api("/agents")).agents;
    }, render: renderAgents },
  tasks: { load: async () => {
      const [tasks, agents] = await Promise.all(
        [api("/tasks"), api("/agents")]);
      state.tasks = tasks.tasks; state.agents = agents.agents;
    }, render: renderTasks },
  messages: { load: async () => {
      const [messages, agents] = await Promise.all(
        [api("/messages"), api("/agents")]);
      state.messages = messages.messages; state.agents = agents.agents;
    }, render: renderMessages },
  vault: { load: async () => {
      const [services, agents] = await Promise.all(
        [api("/vault/services"), api("/agents")]);
      state.services = services.services; state.agents = agents.agents;
    }, render: renderVault },
  approvals: { load: async () => {
      const [approvals, agents] = await Promise.all(
        [api("/approvals"), api("/agents")]);
      state.approvals = approvals.approvals; state.agents = agents.agents;
    }, render: () => { renderApprovals(); updateApprovalBadge(); } },
  activity: { load: async () => {
      const [activity, agents, tasks, services] = await Promise.all([
        api("/activity?limit=100"), api("/agents"), api("/tasks"),
        api("/vault/services")]);
      state.entries = activity.entries; state.agents = agents.agents;
      state.tasks = tasks.tasks; state.services = services.services;
    }, render: renderActivity },
  settings: { load: async () => {
      const [peers, nodeInfo, agents, tasks, services] = await Promise.all([
        api("/peers"), api("/node/info"), api("/agents"), api("/tasks"),
        api("/vault/services")]);
      state.peers = peers.peers; state.outbox = peers.outbox;
      state.nodeInfo = nodeInfo; state.agents = agents.agents;
      state.tasks = tasks.tasks; state.services = services.services;
    }, render: () => { renderPeers(); renderActivity(); } },
};

function isEditing() {
  const el = document.activeElement;
  return !!el && ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)
    && !!el.closest("#app");
}

async function refreshCurrentScreen() {
  if (document.visibilityState !== "visible") return;  // hidden tab: paused
  if ($("app").classList.contains("hidden")) return;   // locked / logged out
  if (isEditing()) return;                            // form being edited
  const entry = SCREEN_REFRESH[state.screen];
  if (!entry) return;
  try { await entry.load(); entry.render(); }
  catch (e) { /* transient node hiccup — the next tick retries */ }
}

function startAutoRefresh() {
  const ind = $("auto-refresh-indicator");
  if (ind) ind.textContent = "Auto-refresh: on";
  if (autoRefreshTimer) return;
  autoRefreshTimer = setInterval(refreshCurrentScreen, AUTO_REFRESH_MS);
}

document.addEventListener("visibilitychange", () => {
  const ind = $("auto-refresh-indicator");
  if (document.visibilityState === "visible") {
    if (ind) ind.textContent = "Auto-refresh: on";
    refreshCurrentScreen();
  } else if (ind) {
    ind.textContent = "Auto-refresh: paused";
  }
});

/* ---------- navigation + login ---------- */
const TITLES = { dashboard: "Dashboard", agents: "Agents", tasks: "Tasks",
                 messages: "Messages", vault: "Vault", approvals: "Approvals",
                 activity: "Activity", settings: "Settings" };
document.querySelectorAll(".nav-btn[data-screen]").forEach((btn) =>
  btn.addEventListener("click", () => {
    state.screen = btn.dataset.screen;
    document.querySelectorAll(".nav-btn").forEach((b) =>
      b.classList.toggle("active", b === btn));
    document.querySelectorAll(".screen").forEach((s) =>
      s.classList.toggle("active", s.id === "screen-" + state.screen));
    $("page-title").textContent = TITLES[state.screen];
  }));

$("refresh-btn").addEventListener("click", () =>
  loadAll().then(() => toast("Refreshed.")).catch((e) => toast(e.message, true)));

function lock() {
  sessionStorage.removeItem(TOKEN_KEY);
  $("app").classList.add("hidden");
  $("login-screen").classList.remove("hidden");
}
$("logout-btn").addEventListener("click", lock);
$("settings-lock").addEventListener("click", lock);

$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  sessionStorage.setItem(TOKEN_KEY, $("login-token").value.trim());
  try {
    await loadAll();
    $("login-screen").classList.add("hidden");
    $("app").classList.remove("hidden");
    $("login-error").textContent = "";
    startAutoRefresh();
  } catch (e) {
    lock();
    $("login-error").textContent = "That token was not accepted by the node.";
  }
});

(async () => {
  if (sessionStorage.getItem(TOKEN_KEY)) {
    try {
      await loadAll();
      $("login-screen").classList.add("hidden");
      $("app").classList.remove("hidden");
      startAutoRefresh();
    } catch (e) { lock(); }
  }
})();
