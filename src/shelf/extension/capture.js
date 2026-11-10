// Runs in the page (injected by the popup). Returns the page as the reader sees it:
// elements hidden with CSS are left out, text drawn by CSS (::before/::after) is written
// into the document, and image URLs are made absolute. The page itself isn't changed.
/* exported shelfCapture */
function shelfCapture(selectionOnly) {
  const MARK = "data-shelf-hidden";
  const BEFORE = "data-shelf-before";
  const AFTER = "data-shelf-after";
  const marked = [];
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "svg", "HEAD"]);
  const docWidth = document.documentElement.scrollWidth;

  function cssText(el, pseudo) {
    const c = getComputedStyle(el, pseudo).content;
    // Only literal strings: "…" or '…' (not counters, attr(), images or "none").
    const m = c && /^(["'])(.*)\1$/s.exec(c);
    return m && m[2].trim() ? m[2].replace(/\\([0-9a-f]{1,6}) ?/gi,
      (_, h) => String.fromCodePoint(parseInt(h, 16))).replace(/\\(.)/g, "$1") : null;
  }

  function hidden(el, cs) {
    if (cs.display === "none" || cs.visibility === "hidden" || cs.visibility === "collapse") {
      return true;
    }
    if (parseFloat(cs.opacity) === 0 || parseFloat(cs.fontSize) === 0) return true;
    if (/inset\(\s*50%/.test(cs.clipPath || "")) return true;
    if (cs.position === "absolute" || cs.position === "fixed") {
      if (/rect\(\s*0(px)?[\s,]+0(px)?[\s,]+0(px)?[\s,]+0(px)?\s*\)/.test(cs.clip || "")) {
        return true;
      }
      const r = el.getBoundingClientRect();
      if (r.right + scrollX < 0 || r.bottom + scrollY < 0 || r.left + scrollX > docWidth + 50) {
        return true;
      }
    }
    if (cs.overflow === "hidden" || cs.overflowX === "hidden") {
      const r = el.getBoundingClientRect();
      if (r.width <= 1 && r.height <= 1 && el.textContent.trim()) return true;
    }
    // Text painted in a fully transparent colour.
    if (/^(transparent|rgba\(.*,\s*0\))$/.test(cs.color) && el.childNodes.length &&
        [...el.childNodes].every((n) => n.nodeType === Node.TEXT_NODE)) {
      return true;
    }
    return false;
  }

  const root = selectionOnly && !getSelection().isCollapsed
    ? getSelection().getRangeAt(0).commonAncestorContainer
    : document.body;
  const start = root.nodeType === Node.ELEMENT_NODE ? root : root.parentElement;
  try {
    const walker = document.createTreeWalker(start, NodeFilter.SHOW_ELEMENT, {
      acceptNode(el) {
        if (SKIP.has(el.tagName)) return NodeFilter.FILTER_REJECT;
        const cs = getComputedStyle(el);
        if (hidden(el, cs)) {
          el.setAttribute(MARK, "");
          marked.push(el);
          return NodeFilter.FILTER_REJECT; // don't look inside
        }
        for (const [pseudo, attr] of [["::before", BEFORE], ["::after", AFTER]]) {
          const t = cssText(el, pseudo);
          if (t) {
            el.setAttribute(attr, t);
            marked.push(el);
          }
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    if (start.nodeType === Node.ELEMENT_NODE) walker.currentNode = start;
    while (walker.nextNode()) { /* visit */ }

    let clone;
    if (selectionOnly && !getSelection().isCollapsed) {
      clone = document.createElement("html");
      const body = document.createElement("body");
      const article = document.createElement("article");
      const title = document.createElement("title");
      title.textContent = document.title;
      const head = document.createElement("head");
      head.append(title);
      article.append(getSelection().getRangeAt(0).cloneContents());
      body.append(article);
      clone.append(head, body);
    } else {
      clone = document.documentElement.cloneNode(true);
    }
    // Sites that set the tab title from script may have no <title> element to clone.
    if (!clone.querySelector("title") && document.title) {
      const title = document.createElement("title");
      title.textContent = document.title;
      (clone.querySelector("head") || clone).prepend(title);
    }
    clone.querySelectorAll(`[${MARK}]`).forEach((el) => el.remove());
    clone.querySelectorAll(`[${BEFORE}]`).forEach((el) => {
      el.prepend(el.getAttribute(BEFORE));
      el.removeAttribute(BEFORE);
    });
    clone.querySelectorAll(`[${AFTER}]`).forEach((el) => {
      el.append(el.getAttribute(AFTER));
      el.removeAttribute(AFTER);
    });
    clone.querySelectorAll("script, noscript, template").forEach((el) => el.remove());
    clone.querySelectorAll("img").forEach((img) => {
      const src = img.getAttribute("data-src") || img.getAttribute("src");
      if (src) img.setAttribute("src", new URL(src, location.href).href);
      img.removeAttribute("srcset");
    });

    const isSeries = /(^|\.)novelupdates\.com$/.test(location.hostname) &&
      location.pathname.startsWith("/series/");
    const cover = isSeries
      ? document.querySelector(".seriesimg img, .serieseditimg img")?.src || null
      : null;
    return {
      url: location.href,
      title: document.title,
      html: "<!DOCTYPE html>\n" + clone.outerHTML,
      isSeries,
      cover,
      hasSelection: !getSelection().isCollapsed,
      textLength: (start.innerText || "").length,
    };
  } finally {
    for (const el of marked) {
      el.removeAttribute(MARK);
      el.removeAttribute(BEFORE);
      el.removeAttribute(AFTER);
    }
  }
}
