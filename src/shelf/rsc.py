"""Pages in captured React Server Components payloads (Next.js App Router).

When a reader moves to the next chapter inside a Next.js site, the browser fetches
``/chapter-2?_rsc=<hash>`` instead of a new HTML page. The response (``text/x-component``)
is the page's React tree in the "Flight" format, one row per line:

    1:I[339756,["/_next/static/chunks/….js"],"LoadingBoundaryProvider"]   a client component
    1b:["$","p","p-27",{"children":"Even after packing everything, …"}]   an element
    c:[["$","$L18",null,{…}],"$L1b",…]                                    references to rows
    2a:T5a3,<0x5a3 bytes of text>                                          a long string

Here the tree is rendered back to plain HTML, so it can be extracted like any archived page.
Link prefetches hold only the layout, without the chapter text; those simply come out too
short to count as a chapter.

Some sites instead hand the chapter to a client component as data
(``{"chapter_num": 2, "title": …, "content": "$13"}`` with the text in row 13). For those,
``payload_rows`` gives the rows as plain JSON for xhr.py's chapter-object search.
"""

import html
import json
import re
from urllib.parse import urlsplit, urlunsplit

RSC_MIME = "text/x-component"
ROW_ID_RE = re.compile(rb"([0-9a-fA-F]+):")
# References: "$1b" a row, "$L1b" lazy, "$@1b" promise, "$Q1b"/"$W1b" a Map/Set held in row 1b.
REF_RE = re.compile(r"^\$[L@QWK]?([0-9a-fA-F]+)((?::[^:]+)*)$")
SKIP_TAGS = {"script", "style", "meta", "link", "noscript", "template", "svg", "head",
             "iframe", "form", "input", "button", "select", "textarea"}
VOID_TAGS = {"br", "hr", "img", "wbr"}
KEEP_ATTRS = ("href", "src", "alt")
TAG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
MAX_DEPTH = 400


class _Client:
    """A client component (an ``I`` row): rendered as its children."""


class _Text(str):
    """A text row's value: literal text, even if it starts with "$"."""


def page_url(url: str) -> str | None:
    """The page an RSC request belongs to (``…/chapter-2?_rsc=x`` -> ``…/chapter-2``)."""
    parts = urlsplit(url)
    params = parts.query.split("&") if parts.query else []
    kept = [p for p in params if p.split("=", 1)[0] != "_rsc"]
    if len(kept) == len(params):
        return None
    return urlunsplit(parts._replace(query="&".join(kept)))


def parse_rows(body: bytes) -> dict[str, object]:
    """Flight rows by id. Text rows (``T<hex len>,``) are read by byte length."""
    rows: dict[str, object] = {}
    i, n = 0, len(body)
    while i < n:
        if body[i] in b"\r\n":
            i += 1
            continue
        m = ROW_ID_RE.match(body, i)
        if not m:  # not a row: skip the line
            j = body.find(b"\n", i)
            i = n if j < 0 else j + 1
            continue
        rid = m.group(1).decode().lower()
        i = m.end()
        if body[i:i + 1] == b"T":
            comma = body.find(b",", i)
            try:
                size = int(body[i + 1:comma], 16)
            except ValueError:
                size = -1
            if comma > 0 and size >= 0:
                rows[rid] = _Text(body[comma + 1:comma + 1 + size].decode("utf-8", errors="replace"))
                i = comma + 1 + size
                continue
        j = body.find(b"\n", i)
        line = body[i:n if j < 0 else j]
        i = n if j < 0 else j + 1
        if line[:1] == b"I":
            rows[rid] = _Client()
            continue
        if line[:1].isalpha():  # HL hints, E errors, …
            continue
        try:
            rows[rid] = json.loads(line)
        except ValueError:
            continue
    return rows


class _Renderer:
    def __init__(self, rows: dict[str, object]):
        self.rows = rows
        self.title = ""
        self.active: set[str] = set()

    def resolve(self, value: str):
        """The value a "$…" string stands for, or the string itself."""
        if isinstance(value, _Text) or not value.startswith("$"):
            return value
        if value.startswith("$$"):
            return value[1:]
        m = REF_RE.match(value)
        if not m:
            return None  # $undefined, $Sreact.fragment, $D…, $n…
        rid, path = m.group(1).lower(), m.group(2)
        if rid in self.active:
            return None
        target = self.rows.get(rid)
        for key in filter(None, path.split(":")):
            if isinstance(target, list) and key.isdigit() and int(key) < len(target):
                target = target[int(key)]
            elif isinstance(target, dict):
                target = target.get(key)
            else:
                return None
        return ("ref", rid, target)

    def node(self, value, depth: int = 0, text: bool = True) -> str:
        """Render a value. Bare strings are text only inside an element's children; other
        strings in the payload (router state, props) are not page content."""
        if depth > MAX_DEPTH or value is None or isinstance(value, bool | _Client):
            return ""
        if isinstance(value, str):
            r = self.resolve(value)
            if isinstance(r, tuple):
                _, rid, target = r
                self.active.add(rid)
                try:
                    return self.node(target, depth + 1, text)
                finally:
                    self.active.discard(rid)
            return html.escape(r) if text and isinstance(r, str) else ""
        if isinstance(value, int | float):
            return str(value) if text else ""
        if isinstance(value, list):
            if len(value) >= 4 and value[0] == "$" and isinstance(value[3], dict | type(None)):
                return self.element(value[1], value[3] or {}, depth)
            return "".join(self.node(v, depth + 1, text) for v in value)
        if isinstance(value, dict):
            return "".join(self.node(v, depth + 1, False) for v in value.values())
        return ""

    def element(self, kind, props: dict, depth: int) -> str:
        children = props.get("children")
        if not isinstance(kind, str) or kind.startswith("$") or not TAG_RE.match(kind):
            # A component or fragment: what it renders is in its children. Other props
            # (e.g. a layout's "loading" fallback) may hold elements, but no text.
            inner = self.node(children, depth + 1)
            rest = "".join(self.node(v, depth + 1, False) for k, v in props.items()
                           if k != "children")
            return inner + rest
        if kind == "title":
            if not self.title:
                self.title = html.unescape(re.sub(r"<[^>]+>", "", self.node(children, depth + 1)))
            return ""
        if kind in SKIP_TAGS or props.get("hidden") is True:
            return ""
        attrs = "".join(f' {a}="{html.escape(str(props[a]))}"' for a in KEEP_ATTRS
                        if isinstance(props.get(a), str))
        if kind in VOID_TAGS:
            return f"<{kind}{attrs}>"
        return f"<{kind}{attrs}>{self.node(children, depth + 1)}</{kind}>"


def render_page(body: bytes) -> str | None:
    """An RSC payload as an HTML document, or None if it has no rows to render."""
    rows = parse_rows(body)
    if "0" not in rows:
        return None
    r = _Renderer(rows)
    content = r.node(rows["0"], text=False)
    title = r.title
    if not title:  # the metadata row may not be reachable from the tree
        for row in rows.values():
            if isinstance(row, list):
                r.node(row, text=False)
                if r.title:
                    title = r.title
                    break
    return (f"<html><head><title>{html.escape(title)}</title></head>"
            f"<body>{content}</body></html>")


def payload_rows(body: bytes) -> list:
    """The payload's rows as JSON values, with references to string rows (e.g. a chapter's
    text in a ``T`` row) filled in. Other references are left as they are."""
    rows = parse_rows(body)

    def fill(v, depth: int = 0):
        if depth > MAX_DEPTH:
            return v
        if isinstance(v, str) and not isinstance(v, _Text):
            m = REF_RE.match(v)
            if m and not m.group(2):
                target = rows.get(m.group(1).lower())
                if isinstance(target, str):
                    return str(target)
            return v
        if isinstance(v, list):
            if len(v) == 4 and v[0] == "$" and v[1] in SKIP_TAGS:
                return None  # <meta name="description" content="…"> is no chapter
            return [fill(x, depth + 1) for x in v]
        if isinstance(v, dict):
            return {k: fill(x, depth + 1) for k, x in v.items()}
        return v

    return [fill(v) for v in rows.values() if isinstance(v, list | dict)]
