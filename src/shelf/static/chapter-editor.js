// The chapter editor: the text is edited in place; the form sends its HTML.
(() => {
  const form = document.getElementById("editor-form");
  const editor = document.getElementById("editor");
  const source = document.getElementById("editor-source");
  const field = document.getElementById("editor-html");
  const htmlButton = form.querySelector('[data-command="html"]');
  const BLOCKS = "p, li, h1, h2, h3, h4, h5, h6, blockquote, pre, figure, table, hr";
  let dirty = false, saving = false;

  // Enter starts a paragraph, not a <div> (Shelf keeps no <div>s).
  document.execCommand("defaultParagraphSeparator", false, "p");

  const showingSource = () => !source.hidden;
  const html = () => (showingSource() ? source.value : editor.innerHTML);

  function blocksInSelection() {
    const sel = getSelection();
    if (!sel.rangeCount || !editor.contains(sel.anchorNode)) return [];
    const range = sel.getRangeAt(0);
    if (range.collapsed) {  // the paragraph the cursor is in
      const node = range.startContainer;
      const el = node.nodeType === 1 ? node : node.parentElement;
      const b = el && el.closest(BLOCKS);
      return b && editor.contains(b) && b !== editor ? [b] : [];
    }
    // The innermost paragraphs the selection touches.
    const touched = [...editor.querySelectorAll(BLOCKS)].filter((b) => range.intersectsNode(b));
    return touched.filter((b) => !touched.some((o) => o !== b && b.contains(o)));
  }

  function removeBlocks() {
    const blocks = blocksInSelection();
    if (!blocks.length) return;
    // Select them whole and delete, so Undo brings them back.
    const range = document.createRange();
    range.setStartBefore(blocks[0]);
    range.setEndAfter(blocks[blocks.length - 1]);
    const sel = getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand("delete");
  }

  function toggleSource() {
    if (showingSource()) {
      editor.innerHTML = source.value;
      source.hidden = true;
      editor.hidden = false;
      editor.focus();
    } else {
      source.value = editor.innerHTML;
      editor.hidden = true;
      source.hidden = false;
      source.focus();
    }
    htmlButton.setAttribute("aria-pressed", String(showingSource()));
    form.querySelectorAll("[data-command]").forEach((b) => {
      if (b !== htmlButton) b.disabled = showingSource();
    });
  }

  form.querySelectorAll("[data-command]").forEach((button) => {
    // Keep the selection in the text when a button is clicked.
    button.addEventListener("mousedown", (e) => e.preventDefault());
    button.addEventListener("click", () => {
      const cmd = button.dataset.command;
      if (cmd === "html") return toggleSource();
      editor.focus();
      if (cmd === "removeBlock") removeBlocks();
      else document.execCommand(cmd);
    });
  });

  editor.addEventListener("input", () => (dirty = true));
  source.addEventListener("input", () => (dirty = true));
  // Pasted text comes in plain, not with another site's markup.
  editor.addEventListener("paste", (e) => {
    const text = e.clipboardData && e.clipboardData.getData("text/plain");
    if (text == null) return;
    e.preventDefault();
    document.execCommand("insertText", false, text);
  });
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener("submit", () => {
    field.value = html();
    saving = true;
    form.querySelector('button[type="submit"]').setAttribute("aria-busy", "true");
  });
  addEventListener("beforeunload", (e) => {
    if (dirty && !saving) e.preventDefault();
  });
})();
