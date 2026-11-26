(() => {
  const KEY = "shelf.reader";
  const DEFAULTS = { theme: "auto", font: "serif", size: 19, leading: 1.7, width: 40, align: "left", gloss: "all", tap: "page" };
  const root = document.documentElement;
  const form = document.getElementById("settings");
  const article = document.getElementById("chapter");
  const chapterId = Number(article.dataset.chapterId);

  // ---- settings -----------------------------------------------------------
  const load = () => { try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) || "{}") }; } catch { return { ...DEFAULTS }; } };
  const save = (s) => { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch {} };

  function apply(s) {
    if (s.theme === "auto") delete root.dataset.theme; else root.dataset.theme = s.theme;
    root.style.setProperty("--reader-size", s.size + "px");
    root.style.setProperty("--reader-leading", s.leading);
    root.style.setProperty("--reader-width", s.width + "rem");
    root.style.setProperty("--reader-align", s.align);
    root.dataset.readerFont = s.font;
    root.dataset.glossMode = s.gloss;
  }

  let settings = load();
  apply(settings);
  for (const [k, v] of Object.entries(settings)) if (form.elements[k]) form.elements[k].value = v;
  form.addEventListener("input", (e) => {
    const el = e.target;
    settings[el.name] = el.type === "range" ? Number(el.value) : el.value;
    apply(settings); save(settings);
  });

  const toggle = document.getElementById("settings-toggle");
  toggle.addEventListener("click", () => {
    form.hidden = !form.hidden;
    toggle.setAttribute("aria-expanded", String(!form.hidden));
  });

  document.getElementById("toc").addEventListener("change", (e) => { location.href = "/chapter/" + e.target.value; });

  // ---- keyboard navigation -------------------------------------------------
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, select, textarea") || e.altKey || e.ctrlKey || e.metaKey) return;
    const link = (rel) => document.querySelector(`a[rel="${rel}"]`);
    if (e.key === "ArrowLeft" && link("prev")) location.href = link("prev").href;
    if (e.key === "ArrowRight" && link("next")) location.href = link("next").href;
  });

  // ---- touch: tap the edges to move on, the bar hides while you read ----------------
  // Only taps (not mouse clicks): the left or right 30% of the screen turns the page (or
  // changes chapter, per the settings); the middle shows or hides the bar.
  const readerBar = document.querySelector(".reader-bar");
  const touchScreen = matchMedia("(pointer: coarse)");
  const smooth = () => (matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");
  const maxScroll = () => document.documentElement.scrollHeight - innerHeight;
  const hideBar = (hide) => readerBar.classList.toggle("bar-hidden", hide && form.hidden);
  const INTERACTIVE = "a, button, input, select, textarea, label, summary, dialog, [tabindex], " +
    ".gloss, .gloss-pop, .reader-bar, .trim-fab, .chapter-nav, .gap-notice";

  function goChapter(dir) {
    const link = document.querySelector(`a[rel="${dir > 0 ? "next" : "prev"}"]`);
    // Going back from the top of a chapter lands at the end of the one before.
    if (link) location.href = link.href + (dir < 0 && settings.tap === "page" ? "#end" : "");
  }
  function turnPage(dir) {
    const overlap = 2 * (parseFloat(getComputedStyle(chapterBody).lineHeight) || 30);
    if (dir > 0) {
      if (scrollY >= maxScroll() - 2) return goChapter(1);
      hideBar(true);
      const progressLine = 3; // what stays of the bar when it's hidden
      scrollBy({ top: innerHeight - progressLine - overlap, behavior: smooth() });
    } else {
      if (scrollY <= 2) return goChapter(-1);
      scrollBy({ top: -(innerHeight - readerBar.offsetHeight - overlap), behavior: smooth() });
    }
  }

  let pointer = "mouse";
  addEventListener("pointerdown", (e) => { pointer = e.pointerType; }, { capture: true, passive: true });
  // In the capture phase, before the glossary's own click handlers: a tap that closes the
  // glossary popup or ends a selection does just that.
  document.addEventListener("click", (e) => {
    if (pointer !== "touch" || e.target.closest(INTERACTIVE)) return;
    if (!pop.hidden || getSelection().toString() || !form.hidden) return;
    const x = e.clientX / innerWidth;
    if (x > 0.3 && x < 0.7) return hideBar(!readerBar.classList.contains("bar-hidden"));
    if (settings.tap === "off") return;
    const dir = x < 0.3 ? -1 : 1;
    if (settings.tap === "chapter") goChapter(dir); else turnPage(dir);
  }, { capture: true });

  let lastY = scrollY;
  addEventListener("scroll", () => {
    const dy = scrollY - lastY;
    if (Math.abs(dy) < 6) return;
    lastY = scrollY;
    if (touchScreen.matches) hideBar(dy > 0 && scrollY > readerBar.offsetHeight);
  }, { passive: true });
  toggle.addEventListener("click", () => hideBar(false));

  // ---- trim selected text, or add it to the glossary --------------------------
  const fab = document.getElementById("selection-actions");
  const dialog = document.getElementById("trim-dialog");
  const glossDialog = document.getElementById("gloss-dialog");
  const chapterBody = article.querySelector(".chapter-body");
  const selectedText = () => {
    const sel = getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return "";
    const range = sel.getRangeAt(0);
    if (!chapterBody.contains(range.commonAncestorContainer)) return "";
    return sel.toString().replace(/\s+/g, " ").trim();
  };
  document.addEventListener("selectionchange", () => {
    const text = selectedText();
    if (!text) { fab.hidden = true; return; }
    const rect = getSelection().getRangeAt(0).getBoundingClientRect();
    fab.style.top = (scrollY + rect.bottom + 8) + "px";
    fab.style.left = Math.max(scrollX + 8, Math.min(scrollX + rect.left, scrollX + innerWidth - 220)) + "px";
    fab.hidden = false;
  });
  fab.addEventListener("mousedown", (e) => e.preventDefault()); // keep the selection
  document.getElementById("trim-selection").addEventListener("click", () => {
    dialog.querySelector("textarea").value = selectedText();
    fab.hidden = true;
    dialog.showModal();
  });
  document.getElementById("gloss-selection").addEventListener("click", () => {
    glossDialog.querySelector('[name="term"]').value = selectedText().slice(0, 200);
    fab.hidden = true;
    glossDialog.showModal();
    glossDialog.querySelector('[name="description"]').focus();
  });
  document.getElementById("trim-cancel").addEventListener("click", () => dialog.close());
  document.getElementById("gloss-cancel").addEventListener("click", () => glossDialog.close());

  // ---- glossary ------------------------------------------------------------------
  // Terms are marked by the server; hovering, focusing or tapping one shows what it is.
  const glossary = JSON.parse(document.getElementById("glossary-data").textContent || "{}");
  const pop = document.getElementById("gloss-pop");
  const seen = new Set();
  const marks = [...chapterBody.querySelectorAll(".gloss")];
  for (const el of marks) {
    if (!seen.has(el.dataset.gloss)) { seen.add(el.dataset.gloss); el.classList.add("gloss-first"); }
  }
  const active = (el) => {
    const mode = settings.gloss;
    return el && mode !== "off" && (mode !== "first" || el.classList.contains("gloss-first"));
  };
  // Only marked terms can be reached with Tab.
  const updateMarks = () => marks.forEach((el) => {
    // (No role="button": the page's styles would draw each one as a button.)
    if (active(el)) { el.tabIndex = 0; el.setAttribute("aria-describedby", "gloss-pop"); }
    else { el.removeAttribute("tabindex"); el.removeAttribute("aria-describedby"); }
  });
  updateMarks();
  let shownFor = null, pinned = false, hideTimer;
  function showGloss(el) {
    const entry = glossary[el.dataset.gloss];
    if (!entry) return;
    clearTimeout(hideTimer);
    pop.replaceChildren();
    const head = document.createElement("strong");
    head.textContent = entry.term;
    pop.append(head);
    if (entry.category) {
      const cat = document.createElement("small");
      cat.className = "muted";
      cat.textContent = " · " + entry.category;
      pop.append(cat);
    }
    const desc = document.createElement("p");
    desc.textContent = entry.description || "No description yet.";
    if (!entry.description) desc.className = "muted";
    const edit = document.createElement("a");
    edit.href = `/novel/${article.dataset.novelId}/glossary#term-${el.dataset.gloss}`;
    edit.textContent = "Edit";
    edit.className = "gloss-edit";
    pop.append(desc, edit);
    pop.hidden = false;
    const r = el.getBoundingClientRect();
    const width = Math.min(360, innerWidth - 16);
    pop.style.width = width + "px";
    pop.style.left = Math.max(8, Math.min(r.left, innerWidth - width - 8)) + scrollX + "px";
    const below = r.bottom + pop.offsetHeight + 8 < innerHeight;
    pop.style.top = (below ? r.bottom + 6 : r.top - pop.offsetHeight - 6) + scrollY + "px";
    shownFor = el;
  }
  function hideGloss() { pop.hidden = true; shownFor = null; pinned = false; }
  const later = () => { if (!pinned) hideTimer = setTimeout(hideGloss, 200); };

  chapterBody.addEventListener("mouseover", (e) => {
    const el = e.target.closest(".gloss");
    if (active(el) && !pinned) showGloss(el);
  });
  chapterBody.addEventListener("mouseout", (e) => { if (e.target.closest(".gloss")) later(); });
  pop.addEventListener("mouseover", () => clearTimeout(hideTimer));
  pop.addEventListener("mouseout", later);
  chapterBody.addEventListener("focusin", (e) => {
    const el = e.target.closest(".gloss");
    if (active(el)) showGloss(el);
  });
  chapterBody.addEventListener("focusout", (e) => {
    if (e.target.closest(".gloss") && !pop.contains(e.relatedTarget)) later();
  });
  chapterBody.addEventListener("click", (e) => {
    const el = e.target.closest(".gloss");
    if (!active(el) || getSelection().toString()) return;
    if (pinned && shownFor === el) return hideGloss();
    showGloss(el);
    pinned = true;
  });
  chapterBody.addEventListener("keydown", (e) => {
    const el = e.target.closest(".gloss");
    if (active(el) && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); showGloss(el); pinned = true; }
  });
  document.addEventListener("click", (e) => {
    if (!pop.hidden && !pop.contains(e.target) && !e.target.closest(".gloss")) hideGloss();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !pop.hidden) hideGloss(); });
  form.addEventListener("input", () => { updateMarks(); if (shownFor && !active(shownFor)) hideGloss(); });

  // ---- progress ------------------------------------------------------------
  const bar = document.getElementById("progress-bar");
  const fraction = () => {
    const max = document.documentElement.scrollHeight - innerHeight;
    return max > 0 ? Math.min(1, Math.max(0, scrollY / max)) : 1;
  };
  const body = () => JSON.stringify({ chapter_id: chapterId, position: fraction() });
  const send = () => fetch("/api/progress", { method: "POST", headers: { "Content-Type": "application/json" }, body: body(), keepalive: true }).catch(() => {});

  let timer;
  addEventListener("scroll", () => {
    bar.style.width = (fraction() * 100).toFixed(1) + "%";
    clearTimeout(timer); timer = setTimeout(send, 800);
  }, { passive: true });
  addEventListener("pagehide", () => navigator.sendBeacon?.("/api/progress", new Blob([body()], { type: "application/json" })));

  // Restore position when resuming (or go to the end, coming back from the next chapter),
  // otherwise mark this chapter as current.
  const resume = Number(article.dataset.resume || 0);
  const toEnd = location.hash === "#end";
  if (toEnd) history.replaceState(null, "", location.pathname + location.search);
  // A chapter loaded ahead in the background (see reader.html) isn't being read yet: it
  // saves your place when it's shown.
  const shown = () => new Promise((resolve) => {
    if (document.prerendering) document.addEventListener("prerenderingchange", resolve, { once: true });
    else resolve();
  });
  addEventListener("load", async () => {
    if (toEnd) scrollTo(0, maxScroll());
    else if (resume > 0 && resume < 1) scrollTo(0, resume * maxScroll());
    bar.style.width = (fraction() * 100).toFixed(1) + "%";
    await shown();
    await send();
    keepNext();
  });

  // Browsers that don't load the next chapter ahead themselves ask Shelf's service worker
  // to (see sw.js), once this chapter's place is saved.
  function keepNext() {
    const next = article.dataset.next;
    if (!next || HTMLScriptElement.supports?.("speculationrules")) return;
    navigator.serviceWorker?.controller?.postMessage({ keep: next });
  }
})();
