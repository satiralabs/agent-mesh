"use strict";
/* Agent Mesh options: node URL + admin token in chrome.storage.local.
   Saving also requests host permission for exactly that node's
   origin (optional_host_permissions in the manifest) — no broad
   access is taken up front. */

const $ = (id) => document.getElementById(id);

(async () => {
  const s = await chrome.storage.local.get(["nodeUrl", "adminToken"]);
  if (s.nodeUrl) $("node-url").value = s.nodeUrl;
  if (s.adminToken) $("admin-token").value = s.adminToken;
})();

$("opts-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const nodeUrl = $("node-url").value.trim().replace(/\/+$/, "");
  const adminToken = $("admin-token").value.trim();
  await chrome.storage.local.set({ nodeUrl, adminToken });
  let note = "Saved.";
  try {
    const origin = new URL(nodeUrl).origin + "/*";
    const granted = await chrome.permissions.request({ origins: [origin] });
    note += granted
      ? " Node access granted."
      : " Node access not granted — the popup cannot reach the node " +
        "until you allow it.";
  } catch (e) {
    note += " (Permission request failed: " + e.message + ")";
  }
  $("saved").textContent = note;
});
