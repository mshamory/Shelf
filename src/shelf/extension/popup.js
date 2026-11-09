"use strict";
const api = globalThis.browser ?? globalThis.chrome;
const $ = (id) => document.getElementById(id);
const DEFAULT_SERVER = "http://localhost:8000";
const NEW = "new";

let server = DEFAULT_SERVER;
let page = null; // what capture.js returned
let tabId = null;
let previewSeq = 0;

function setStatus(text, { error = false, link = null } = {}) {
  const el = $("status");
  el.className = "status" + (error ? " error" : "");
  el.textContent = text;
  if (link) {
    el.append(" ");
    const a = document.createElement("a");
    a.href = link.href;
    a.textContent = link.text;
    a.addEventListener("click", (e) => {
      e.preventDefault();
      api.tabs.create({ url: link.href });
    });
    el.append(a);
  }
}

async function call(path, body) {
  let resp;
  try {
    resp = await fetch(server + path, body === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    throw new Error(`Can't reach Shelf at ${server}. Check the address in Settings.`);
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `Shelf answered ${resp.status}`);
  return data;
}

async function capture(selectionOnly = false) {
  await api.scripting.executeScript({ target: { tabId }, files: ["capture.js"] });
  const [res] = await api.scripting.executeScript({
    target: { tabId },
    func: (sel) => shelfCapture(sel), // defined by capture.js
    args: [selectionOnly],
  });
  return res.result;
}

async function coverPayload(url) {
  if (!url) return null;
  try {
    const resp = await fetch(url);
    if (!resp.ok) return null;
    const blob = await resp.blob();
    const data = await new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(",", 2)[1]);
      r.onerror = reject;
      r.readAsDataURL(blob);
    });
    return { data, mime: blob.type || "image/jpeg" };
  } catch {
    return null; // Shelf will try to download it itself
  }
}

function fillNovels(select, novels, { allowNew }) {
  select.textContent = "";
  for (const n of novels) {
    const o = document.createElement("option");
    o.value = n.id;
    o.textContent = n.title + (n.chapters ? ` (${n.chapters})` : "");
    select.append(o);
  }
  if (allowNew) {
    const o = document.createElement("option");
    o.value = NEW;
    o.textContent = "+ New novel…";
    select.append(o);
  }
}

function formatNumber(n) {
  return n == null ? "" : String(Number.isInteger(n) ? n : +n.toFixed(3));
}

// -- NovelUpdates series page ---------------------------------------------

async function showSeries(novels) {
  $("series").hidden = false;
  const match = novels.find((n) => n.match >= 1000);
  if (match) {
    $("create-novel").textContent = `Update “${match.title}” from this page`;
  }
  fillNovels($("metadata-novel"), novels, { allowNew: false });
  $("existing-novel").hidden = !novels.length;

  $("create-novel").addEventListener("click", async () => {
    $("create-novel").disabled = true;
    try {
      setStatus("Sending…");
      const cover = await coverPayload(page.cover);
      if (match) {
        await call(`/api/novels/${match.id}/metadata`, { url: page.url, html: page.html, cover });
        setStatus("Metadata updated.", { link: { href: server + match.url, text: "Open in Shelf" } });
        return;
      }
      const res = await call("/api/novels", {
        novelupdates: { url: page.url, html: page.html }, cover,
      });
      setStatus(res.created ? `Created “${res.novel.title}”.` : `“${res.novel.title}” already exists.`,
        { link: { href: server + res.novel.url, text: "Open in Shelf" } });
    } catch (e) {
      setStatus(e.message, { error: true });
    } finally {
      $("create-novel").disabled = false;
    }
  });

  $("apply-metadata").addEventListener("click", async () => {
    const id = $("metadata-novel").value;
    $("apply-metadata").disabled = true;
    try {
      setStatus("Sending…");
      const cover = await coverPayload(page.cover);
      const res = await call(`/api/novels/${id}/metadata`, { url: page.url, html: page.html, cover });
      setStatus(`Metadata applied to “${res.novel.title}”.`,
        { link: { href: server + res.novel.url, text: "Open in Shelf" } });
    } catch (e) {
      setStatus(e.message, { error: true });
    } finally {
      $("apply-metadata").disabled = false;
    }
  });
}

// -- chapter page -----------------------------------------------------------

function chapterBody(dryRun) {
  const number = $("number").value.trim();
  return {
    url: page.url,
    html: page.html,
    title: $("title").value.trim(),
    number: number ? Number(number) : null,
    dry_run: dryRun,
  };
}

async function preview() {
  const seq = ++previewSeq;
  const id = $("novel").value;
  const box = $("preview");
  if (id === NEW) {
    box.textContent = "The chapter will be the first in a new novel.";
    return;
  }
  box.textContent = "Checking…";
  try {
    const res = await call(`/api/novels/${id}/chapters`, chapterBody(true));
    if (seq !== previewSeq) return;
    const ch = res.chapter;
    box.textContent = "";
    const head = document.createElement("div");
    head.textContent = ch.title;
    const info = document.createElement("div");
    info.className = "muted";
    const parts = [];
    if (ch.number != null) parts.push(`#${formatNumber(ch.number)}`);
    if (ch.season_label) parts.push(ch.season_label);
    parts.push(`${ch.chars.toLocaleString()} characters`);
    parts.push({
      added: "new chapter",
      replaced: `replaces archived “${res.replaced_title}”`,
      updated: "replaces your earlier capture",
    }[res.action]);
    info.textContent = parts.join(" · ");
    box.append(head, info);
    $("title").placeholder = ch.title;
    $("number").placeholder = formatNumber(ch.number) || "—";
    $("send").disabled = false;
  } catch (e) {
    if (seq !== previewSeq) return;
    box.innerHTML = "";
    const warn = document.createElement("span");
    warn.className = "warn";
    warn.textContent = e.message;
    box.append(warn);
    $("send").disabled = true;
  }
}

async function showChapter(novels) {
  $("chapter").hidden = false;
  fillNovels($("novel"), novels, { allowNew: true });
  const remembered = (await api.storage.local.get({ lastNovel: null })).lastNovel;
  const best = novels[0]?.match > 0 ? novels[0] : novels.find((n) => String(n.id) === String(remembered));
  $("novel").value = best ? best.id : (novels.length ? novels[0].id : NEW);
  $("new-title-label").hidden = $("novel").value !== NEW;
  $("selection-label").hidden = !page.hasSelection;

  $("novel").addEventListener("change", () => {
    $("new-title-label").hidden = $("novel").value !== NEW;
    $("send").disabled = false;
    preview();
  });
  let timer;
  $("number").addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(preview, 400);
  });
  $("selection-only").addEventListener("change", async () => {
    page = await capture($("selection-only").checked);
    preview();
  });

  $("chapter").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("send").disabled = true;
    try {
      let id = $("novel").value;
      if (id === NEW) {
        const title = $("new-title").value.trim();
        if (!title) throw new Error("Give the new novel a title.");
        id = (await call("/api/novels", { title })).novel.id;
      }
      setStatus("Sending…");
      const res = await call(`/api/novels/${id}/chapters`, chapterBody(false));
      await api.storage.local.set({ lastNovel: id });
      const verb = { added: "Added", replaced: "Replaced", updated: "Updated" }[res.action];
      setStatus(`${verb} “${res.chapter.title}” in ${res.novel.title}.`,
        { link: { href: server + res.chapter.url, text: "Read" } });
    } catch (err) {
      setStatus(err.message, { error: true });
      $("send").disabled = false;
    }
  });
  await preview();
}

// -- settings -----------------------------------------------------------------

function showSettings(show) {
  $("settings").hidden = !show;
  $("server").value = server;
}

$("toggle-settings").addEventListener("click", () => showSettings($("settings").hidden));
$("settings").addEventListener("submit", async (e) => {
  e.preventDefault();
  server = $("server").value.trim().replace(/\/+$/, "") || DEFAULT_SERVER;
  await api.storage.local.set({ server });
  try {
    const res = await call("/healthz");
    $("settings-status").textContent = `Connected (Shelf ${res.version})`;
    location.reload();
  } catch (err) {
    $("settings-status").textContent = err.message;
  }
});

async function main() {
  server = (await api.storage.local.get({ server: DEFAULT_SERVER })).server;
  const [tab] = await api.tabs.query({ active: true, currentWindow: true });
  tabId = tab?.id;
  $("page-title").textContent = tab?.title || "";
  try {
    page = await capture(false);
  } catch (e) {
    setStatus("This page can't be captured (browser pages and extension stores are off limits).",
      { error: true });
    return;
  }
  let novels;
  try {
    novels = (await call(`/api/novels?url=${encodeURIComponent(page.url)}`)).novels;
  } catch (e) {
    setStatus(e.message, { error: true });
    showSettings(true);
    return;
  }
  if (page.isSeries) await showSeries(novels);
  else await showChapter(novels);
}

main();
