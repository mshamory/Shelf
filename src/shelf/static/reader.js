(() => {
  const KEY = "shelf.reader";
  const DEFAULTS = { theme: "auto", font: "serif", size: 19, leading: 1.7, width: 40, align: "left" };
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

  // Restore position when resuming, otherwise mark this chapter as current.
  const resume = Number(article.dataset.resume || 0);
  addEventListener("load", () => {
    if (resume > 0 && resume < 1) scrollTo(0, resume * (document.documentElement.scrollHeight - innerHeight));
    bar.style.width = (fraction() * 100).toFixed(1) + "%";
    send();
  });
})();
