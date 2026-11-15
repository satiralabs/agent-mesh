"use strict";
/* Agent Mesh popup: a status glance for ONE configured node.
   The extension talks to no server except the node URL saved in the
   options page (chrome.storage.local). The admin token is sent only
   to that node, as the X-Admin-Token header — the same read paths
   the GUI uses. Presence mirrors the node's own rule (v2.1):
   online < 120 s, idle < 15 min, offline after; revoked wins. */

const $ = (id) => document.getElementById(id);

function presenceOf(agent) {
  if (agent.status === "revoked") return "revoked";
  if (typeof agent.presence === "string") return agent.presence;
  const age = Date.now() / 1000 - (agent.last_seen || 0);
  if (age < 120) return "online";
  if (age < 900) return "idle";
  return "offline";
}

async function getSettings() {
  const s = await chrome.storage.local.get(["nodeUrl", "adminToken"]);
  return { nodeUrl: (s.nodeUrl || "").replace(/\/+$/, ""),
           adminToken: s.adminToken || "" };
}

async function api(settings, path) {
  const resp = await fetch(settings.nodeUrl + path, {
    headers: { "X-Admin-Token": settings.adminToken },
  });
  if (!resp.ok) throw new Error("HTTP " + resp.status);
  return resp.json();
}

async function refresh() {
  const settings = await getSettings();
  const dot = $("reach-dot");
  if (!settings.nodeUrl || !settings.adminToken) {
    $("node-line").textContent = "not configured";
    $("message").textContent =
      "Set your node URL and admin token in Settings first.";
    dot.className = "dot unknown";
    $("open-gui").disabled = true;
    $("open-approvals").disabled = true;
    return;
  }
  $("open-gui").disabled = false;
  $("open-approvals").disabled = false;
  try {
    const [info, agents, approvals] = await Promise.all([
      api(settings, "/node/info"),
      api(settings, "/agents"),
      api(settings, "/approvals?status=pending"),
    ]);
    dot.className = "dot ok";
    $("node-line").textContent =
      (info.name || "node") + " · " + settings.nodeUrl;
    const counts = { online: 0, idle: 0, offline: 0 };
    (agents.agents || []).forEach((a) => {
      const p = presenceOf(a);
      if (p in counts) counts[p] += 1;
    });
    $("count-online").textContent = counts.online;
    $("count-idle").textContent = counts.idle;
    $("count-offline").textContent = counts.offline;
    $("count-approvals").textContent = (approvals.approvals || []).length;
    $("stats").classList.remove("hidden");
    $("message").textContent = "";
  } catch (e) {
    dot.className = "dot bad";
    $("node-line").textContent = settings.nodeUrl;
    $("stats").classList.add("hidden");
    $("message").textContent =
      "Node unreachable (" + e.message + "). Check the URL/token in " +
      "Settings, and grant the extension access to the node address " +
      "when asked.";
  }
}

$("open-gui").addEventListener("click", async () => {
  const { nodeUrl } = await getSettings();
  if (nodeUrl) chrome.tabs.create({ url: nodeUrl + "/" });
});
$("open-approvals").addEventListener("click", async () => {
  const { nodeUrl } = await getSettings();
  if (nodeUrl) chrome.tabs.create({ url: nodeUrl + "/#approvals" });
});
$("open-options").addEventListener("click", () =>
  chrome.runtime.openOptionsPage());

refresh();
