"""Find chapters in JSON captured from XHR/fetch requests.

Single-page sites often load the next chapter with JavaScript, so the archive holds a JSON
response instead of an HTML page. This module decodes the common framework payloads

  - SvelteKit ``…/__data.json`` and Nuxt ``_payload.json`` (devalue-encoded)
  - Next.js ``/_next/data/<build>/<path>.json``
  - plain JSON APIs (e.g. WordPress REST ``content.rendered``)

then looks for "chapter-like" objects: a substantial content field plus a title or number.
Content may be HTML, plain text, a ProseMirror/TipTap document or Editor.js blocks.
"""

import html
import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .extract import (
    MIN_CHAPTER_CHARS,
    chapter_number,
    is_placeholder,
    remove_hidden,
    sanitize,
    text_length,
)
from .wacz import WaczFile

log = logging.getLogger("shelf.xhr")

# Patreon posts carry the body as a ProseMirror document in a JSON string, "content_json_string".
CONTENT_KEYS = ("content", "content_html", "contentHtml", "html", "body", "body_html",
                "bodyHtml", "chapter_content", "chapterContent", "content_json_string",
                "content_json", "contentJson", "text")
TITLE_KEYS = ("title", "chapter_title", "chapterTitle", "name", "chapter_name", "chapterName")
NUMBER_KEYS = ("number", "chapter_number", "chapterNumber", "chapter_no", "chapterNo", "num")
URL_KEYS = ("url", "link", "permalink", "canonical_url")
MIN_RECORD_BYTES = 0  # CDX lengths are compressed sizes; filtering on them is unreliable
MAX_RECORD_BYTES = 20 * 1024 * 1024
# Text of a chapter marked locked/encrypted this short is a teaser, not the chapter.
LOCKED_PREVIEW_MAX_CHARS = 1500


# Why a chapter seen in a payload has no readable text, most informative first.
UNAVAILABLE_REASONS = {
    "encrypted": "only encrypted text was captured",
    "locked": "locked; only a preview was captured",
    "preview": "only a preview was captured",
}


@dataclass
class XhrScan:
    chapters: list["XhrChapter"]
    unavailable: dict[int, str]  # chapter number -> key of UNAVAILABLE_REASONS


@dataclass
class XhrChapter:
    url: str  # the page URL the chapter belongs to
    title: str
    html: str
    text_length: int
    number: float | None
    ts: str


# -- payload decoding -------------------------------------------------------


def devalue_unflatten(values: list):
    """Rebuild the object graph from devalue's flat array (used by SvelteKit and Nuxt)."""
    if not isinstance(values, list) or not values:
        return values
    cache: dict[int, object] = {}

    def hydrate(i):
        if not isinstance(i, int) or i < 0 or i >= len(values):
            return None  # -1 undefined, -2 hole, -3 NaN, ... -> None
        if i in cache:
            return cache[i]
        v = values[i]
        if isinstance(v, list):
            if v and isinstance(v[0], str):  # typed value: ["Date", "…"], ["Map", …], …
                kind = v[0]
                if kind in ("Date", "BigInt", "RegExp", "URL"):
                    out = v[1] if len(v) > 1 else None
                elif kind in ("Set", "Map", "Object"):
                    out = [hydrate(x) for x in v[1:]]
                elif kind == "null":  # null-prototype object: ["null", key, value, …]
                    out = {v[j]: hydrate(v[j + 1]) for j in range(1, len(v) - 1, 2)}
                else:
                    out = None
                cache[i] = out
                return out
            out = []
            cache[i] = out
            out.extend(hydrate(x) for x in v)
            return out
        if isinstance(v, dict):
            out = {}
            cache[i] = out
            for k, x in v.items():
                out[k] = hydrate(x)
            return out
        cache[i] = v
        return v

    return hydrate(0)


def _load_json(body: bytes):
    text = body.decode("utf-8", errors="replace").strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except ValueError:
        # Streamed SvelteKit responses are newline-delimited JSON chunks.
        out = []
        for line in text.splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out or None


def decode_payload(url: str, body: bytes):
    """Return (decoded JSON, page URL the payload belongs to) or (None, None)."""
    data = _load_json(body)
    if data is None:
        return None, None
    parts = urlsplit(url)
    page = url
    is_svelte = parts.path.endswith("__data.json") or (
        isinstance(data, dict) and data.get("type") == "data" and "nodes" in data)
    if is_svelte:  # SvelteKit
        path = parts.path.removesuffix("__data.json").rstrip("/") or "/"
        page = urlunsplit(parts._replace(path=path, query=""))
        chunks = data if isinstance(data, list) else [data]
        nodes = []
        for chunk in chunks:
            if isinstance(chunk, dict):
                nodes += [n for n in chunk.get("nodes") or [] if isinstance(n, dict)]
                if chunk.get("type") == "chunk" and "data" in chunk:
                    nodes.append(chunk)
        data = [devalue_unflatten(n["data"]) for n in nodes if isinstance(n.get("data"), list)]
    elif parts.path.endswith("/_payload.json"):  # Nuxt
        path = parts.path[: -len("_payload.json")] or "/"
        page = urlunsplit(parts._replace(path=path, query=""))
        data = devalue_unflatten(data) if isinstance(data, list) else data
    else:
        m = re.match(r"^/_next/data/[^/]+/(.+)\.json$", parts.path)  # Next.js
        if m:
            path = "/" + m.group(1)
            path = path[: -len("/index")] if path.endswith("/index") else path
            page = urlunsplit(parts._replace(path=path or "/", query=""))
    return data, page


# -- content conversion -----------------------------------------------------

_MARKS = {"bold": "strong", "strong": "strong", "italic": "em", "em": "em", "underline": "u",
          "strike": "s", "code": "code", "superscript": "sup", "subscript": "sub"}
_NODES = {"paragraph": "p", "blockquote": "blockquote", "bulletList": "ul", "bullet_list": "ul",
          "orderedList": "ol", "ordered_list": "ol", "listItem": "li", "list_item": "li",
          "codeBlock": "pre", "code_block": "pre", "table": "table", "tableRow": "tr",
          "table_row": "tr", "tableCell": "td", "table_cell": "td", "tableHeader": "th"}


def prosemirror_to_html(node) -> str:
    """Render a ProseMirror/TipTap JSON document to HTML."""
    if isinstance(node, list):
        return "".join(prosemirror_to_html(n) for n in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        out = html.escape(node.get("text", ""))
        for mark in node.get("marks") or []:
            tag = _MARKS.get((mark or {}).get("type"))
            if tag:
                out = f"<{tag}>{out}</{tag}>"
        return out
    inner = prosemirror_to_html(node.get("content") or [])
    if kind in ("hardBreak", "hard_break"):
        return "<br>"
    if kind in ("horizontalRule", "horizontal_rule"):
        return "<hr>"
    if kind == "image":
        src = (node.get("attrs") or {}).get("src")
        return f'<img src="{html.escape(src)}">' if src else ""
    if kind == "heading":
        level = min(max(int((node.get("attrs") or {}).get("level") or 2), 1), 6)
        return f"<h{level}>{inner}</h{level}>"
    tag = _NODES.get(kind)
    if tag == "p" and not inner.strip():
        return ""
    return f"<{tag}>{inner}</{tag}>" if tag else inner


def editorjs_to_html(doc: dict) -> str:
    out = []
    for block in doc.get("blocks") or []:
        kind, data = block.get("type"), block.get("data") or {}
        if kind == "paragraph":
            out.append(f"<p>{data.get('text', '')}</p>")
        elif kind == "header":
            level = min(max(int(data.get("level") or 2), 1), 6)
            out.append(f"<h{level}>{data.get('text', '')}</h{level}>")
        elif kind == "quote":
            out.append(f"<blockquote>{data.get('text', '')}</blockquote>")
        elif kind == "list":
            tag = "ol" if data.get("style") == "ordered" else "ul"
            items = "".join(
                f"<li>{i if isinstance(i, str) else (i or {}).get('content', '')}</li>"
                for i in data.get("items") or []
            )
            out.append(f"<{tag}>{items}</{tag}>")
        elif kind == "delimiter":
            out.append("<hr>")
    return "".join(out)


def text_to_html(text: str) -> str:
    paras = re.split(r"\n\s*\n", text) if "\n\n" in text else text.split("\n")
    return "".join(f"<p>{html.escape(p.strip())}</p>" for p in paras if p.strip())


def content_to_html(value) -> str | None:
    """Convert a content field (HTML, text, ProseMirror, Editor.js, WP 'rendered')."""
    if isinstance(value, dict) and isinstance(value.get("rendered"), str):
        value = value["rendered"]  # WordPress REST
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("{") and '"type"' in s[:200] or s.startswith('{"blocks"'):
            try:
                value = json.loads(s)
            except ValueError:
                pass
    if isinstance(value, dict):
        if value.get("type") == "doc":
            return prosemirror_to_html(value)
        if isinstance(value.get("blocks"), list):
            return editorjs_to_html(value)
        return None
    if not isinstance(value, str):
        return None
    if re.search(r"<(p|br|div|span|em|strong|h\d)\b", value, re.I):
        return value
    return text_to_html(value)


# -- finding chapters -------------------------------------------------------


def _first(obj: dict, keys) -> object:
    for k in keys:
        v = obj.get(k)
        if isinstance(v, dict) and isinstance(v.get("rendered"), str):
            v = v["rendered"]
        if v not in (None, ""):
            return v
    return None


def _as_number(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and re.fullmatch(r"\s*\d+(\.\d+)?\s*", v):
        return float(v)
    return None


def _walk(obj, depth: int = 0) -> Iterator[tuple[dict, str | None]]:
    """Yield (object, cleaned content or None) for every object with a content field and a
    title or number. Doesn't descend into them."""
    if depth > 12:
        return
    if isinstance(obj, dict):
        title = _first(obj, TITLE_KEYS)
        number = _as_number(_first(obj, NUMBER_KEYS))
        keys = [k for k in CONTENT_KEYS if k in obj]
        if keys and (isinstance(title, str) or number is not None):
            for key in keys:
                converted = content_to_html(obj[key])
                if converted:
                    cleaned = sanitize(remove_hidden(converted))
                    n = text_length(cleaned)
                    if n >= MIN_CHAPTER_CHARS and not is_placeholder(cleaned) and not (
                            n < LOCKED_PREVIEW_MAX_CHARS and unavailable_reason(obj) != "preview"):
                        yield obj, cleaned
                        return
            yield obj, None
            return
        for v in obj.values():
            yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, depth + 1)


def find_chapter_objects(obj) -> Iterator[tuple[dict, str]]:
    """Yield (object, content_html) for chapter-like objects with readable content."""
    for o, content in _walk(obj):
        if content is not None:
            yield o, content


def object_number(obj: dict) -> float | None:
    number = _as_number(_first(obj, NUMBER_KEYS))
    if number is None:
        title = _first(obj, TITLE_KEYS)
        number = chapter_number(title) if isinstance(title, str) else None
    return number


def unavailable_reason(obj: dict) -> str:
    """Why a chapter object has no readable text: encrypted, locked or preview."""
    fmt = str(obj.get("content_format") or obj.get("format") or "").lower()
    content = next((obj[k] for k in CONTENT_KEYS if k in obj), None)
    if "encrypt" in fmt or obj.get("encrypted") is True or (
            isinstance(content, list) and content and all(isinstance(x, str) for x in content)):
        return "encrypted"
    locked = obj.get("locked", obj.get("is_locked"))
    if fmt == "locked" or locked is True or obj.get("current_user_can_view") is False or (
            isinstance(locked, dict) and (locked.get("price") or 0) > 0
            and not locked.get("unlocked_at")):
        return "locked"
    return "preview"


def extract_xhr_chapters(w: WaczFile) -> list[XhrChapter]:
    return scan_xhr(w).chapters


def scan_xhr(w: WaczFile) -> XhrScan:
    """Chapters in captured JSON, plus chapters seen there without readable text."""
    found: dict[str, XhrChapter] = {}
    unavailable: dict[int, str] = {}
    rank = list(UNAVAILABLE_REASONS)
    scanned = 0
    for url in w.urls():
        # Try captures best-first: a newer capture may be an error payload
        # ("Chapter not found") while an older one holds the chapter.
        # Captures of one URL can differ: an error payload, a locked preview, the full
        # chapter. Keep the capture with the most chapter text.
        objects, entry, page_url, best_len = [], None, url, 0
        for cap in w.captures(url):
            if ("json" not in cap.mime or not 200 <= cap.status < 300
                    or not MIN_RECORD_BYTES <= cap.length <= MAX_RECORD_BYTES):
                continue
            resp = w.read(cap)
            if resp is None:
                continue
            scanned += 1
            data, page = decode_payload(url, resp.body)
            found_here = []
            for obj, content in _walk(data) if data is not None else ():
                if content is not None:
                    found_here.append((obj, content))
                    continue
                number = object_number(obj)
                if number is not None and number >= 0:
                    reason = unavailable_reason(obj)
                    prev = unavailable.get(int(number))
                    if prev is None or rank.index(reason) < rank.index(prev):
                        unavailable[int(number)] = reason
            total = sum(text_length(c) for _o, c in found_here)
            if total > best_len:
                objects, entry, page_url, best_len = found_here, cap, page, total
        for i, (obj, content) in enumerate(objects):
            own_url = _first(obj, URL_KEYS)
            if isinstance(own_url, str) and own_url.startswith(("http://", "https://")):
                chapter_url = own_url
            elif len(objects) == 1:
                chapter_url = page_url
            else:
                ident = obj.get("slug") or obj.get("id") or i
                chapter_url = f"{page_url}#{ident}"
            title = _first(obj, TITLE_KEYS)
            title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip() if isinstance(
                title, str) else ""
            chapter = XhrChapter(
                url=chapter_url,
                title=title,
                html=content,
                text_length=text_length(content),
                number=_as_number(_first(obj, NUMBER_KEYS)),
                ts=entry.timestamp,
            )
            prev = found.get(chapter_url)
            if prev is None or chapter.ts > prev.ts:
                found[chapter_url] = chapter
    log.info("Scanned %d JSON responses, found %d chapters (%d unreadable)",
             scanned, len(found), len(unavailable))
    return XhrScan(list(found.values()), unavailable)
