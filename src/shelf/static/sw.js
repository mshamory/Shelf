// Shelf's service worker (served at /sw.js, so it covers the whole app).
//
// - Chapters you open are kept, the last 30, so you can read them without a connection.
//   Online, pages always come from Shelf.
// - The reader asks for the next chapter ahead of time (in browsers that can't load it in
//   the background themselves): it's then shown at once when you move on, if that's within
//   30 minutes and no form was sent meanwhile (a trim rule or a hidden chapter could change it).
// - Signing out forgets the kept chapters.
// - Offline, other pages show /static/offline.html, which lists the kept chapters.
"use strict";

const VERSION = "__VERSION__";
const STATIC = "shelf-static-" + VERSION; // scripts, styles and icons (their URLs change with them)
const PAGES = "shelf-pages"; // chapters you've opened, for reading offline
const READY = "shelf-ready"; // the next chapter, fetched ahead, shown once
const OFFLINE = "/static/offline.html";
const OFFLINE_ICON = "/static/icons/icon.svg"; // shown on the offline page
const KEEP_PAGES = 30;
const READY_AGE = 30 * 60 * 1000;
const CHAPTER = /^\/chapter\/\d+$/;

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(STATIC).then((c) => c.addAll([OFFLINE, OFFLINE_ICON])).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name.startsWith("shelf-static-") && name !== STATIC) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (req.method !== "GET") {
    if (req.mode === "navigate") {
      // A form was sent: chapters fetched ahead may be out of date.
      e.waitUntil(url.pathname === "/logout" ? forgetAll() : caches.delete(READY));
    }
    return;
  }
  if (url.pathname.startsWith("/static/")) e.respondWith(fromStatic(req));
  else if (req.mode === "navigate") e.respondWith(navigate(e, req, url));
});

self.addEventListener("message", (e) => {
  if (!e.data || typeof e.data.keep !== "string") return;
  const url = new URL(e.data.keep, location.origin);
  if (url.origin === location.origin && CHAPTER.test(url.pathname) && !url.search) {
    e.waitUntil(fetchAhead(url.href));
  }
});

async function fromStatic(req) {
  const cache = await caches.open(STATIC);
  const hit = await cache.match(req);
  if (hit) return hit;
  const resp = await fetch(req);
  if (resp.ok && resp.type === "basic") await cache.put(req, resp.clone());
  return resp;
}

async function navigate(e, req, url) {
  const chapter = CHAPTER.test(url.pathname) && !url.search;
  if (chapter) {
    const ready = await caches.open(READY);
    const hit = await ready.match(req);
    if (hit) {
      await ready.delete(req);
      if (Date.now() - Number(hit.headers.get("x-shelf-fetched")) < READY_AGE) return hit;
    }
  }
  try {
    const resp = await fetch(req);
    if (chapter && resp.ok && resp.type === "basic") e.waitUntil(remember(req, resp.clone()));
    return resp;
  } catch (err) {
    const kept = chapter && (await caches.match(req, { cacheName: PAGES }));
    return kept || (await caches.match(OFFLINE)) || Response.error();
  }
}

async function fetchAhead(href) {
  let resp;
  try {
    resp = await fetch(href, { credentials: "same-origin", redirect: "manual" });
  } catch {
    return;
  }
  if (!resp.ok || resp.type !== "basic") return; // signed out, or gone
  const headers = new Headers(resp.headers);
  headers.set("x-shelf-fetched", String(Date.now()));
  const copy = new Response(await resp.blob(),
    { status: resp.status, statusText: resp.statusText, headers });
  // Only the chapter after the one you're reading is worth having ready.
  await caches.delete(READY);
  await (await caches.open(READY)).put(href, copy.clone());
  await remember(new Request(href), copy);
}

async function remember(req, resp) {
  const cache = await caches.open(PAGES);
  await cache.delete(req); // re-added last: the cache keeps the order things were added in
  await cache.put(req, resp);
  const keys = await cache.keys();
  for (const old of keys.slice(0, Math.max(0, keys.length - KEEP_PAGES))) await cache.delete(old);
}

async function forgetAll() {
  await caches.delete(PAGES);
  await caches.delete(READY);
}
