"use strict";
/* Agent Mesh service worker (v2.1) — deliberately minimal.
   It caches ONLY the static app shell so the GUI installs as a PWA
   and opens fast; every API call goes to the network, always — mesh
   data (agents, tasks, vault decisions) must never be served stale
   from a cache. Bump SHELL_VERSION when the shell files change. */
const SHELL_VERSION = "agent-mesh-shell-v2.2.0";
const SHELL = [
  "/",
  "/ui/app.js",
  "/ui/app.css",
  "/manifest.webmanifest",
  "/icons/icon-256.png",
  "/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_VERSION)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== SHELL_VERSION)
            .map((k) => caches.delete(k))))
      .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin)
    return;  // network only
  if (SHELL.includes(url.pathname)) {
    // App shell: cache-first, refreshed in the background.
    event.respondWith(
      caches.match(event.request).then((hit) => {
        const refresh = fetch(event.request).then((resp) => {
          if (resp.ok) {
            const copy = resp.clone();
            caches.open(SHELL_VERSION).then((c) => c.put(event.request, copy));
          }
          return resp;
        }).catch(() => hit);
        return hit || refresh;
      }));
  }
  // Everything else (all API GETs): network only, no caching.
});
