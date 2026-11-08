"""Turn archived HTML pages into clean, ordered chapters.

Main-content extraction is done by `trafilatura`; sanitising by `nh3`.
This module only adds the novel-specific heuristics: chapter numbering,
ordering, title clean-up and filtering of non-chapter pages.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import nh3
import trafilatura
from selectolax.lexbor import LexborHTMLParser as HTMLParser

ALLOWED_TAGS = {
    "p", "br", "hr", "em", "strong", "i", "b", "u", "s", "del", "sub", "sup", "small",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "li", "pre", "code",
    "table", "thead", "tbody", "tr", "th", "td", "img", "figure", "figcaption", "span",
}
ALLOWED_ATTRS = {"img": {"src", "alt", "title"}, "td": {"colspan", "rowspan"},
                 "th": {"colspan", "rowspan"}}

ERROR_TITLE_RE = re.compile(
    r"\b(page not found|404|not found|access denied|just a moment|attention required|"
    r"error \d{3})\b",
    re.I,
)
TITLE_SEPARATORS = re.compile(r"\s+[|–—\-:»·]\s+")

# "Chapter 12", "Ch. 12.5", "Ep 3", "第12章", "c12", "/chapter-12/"
_NUM = r"(\d+(?:\.\d+)?)"
CHAPTER_PATTERNS = [
    re.compile(rf"\b(?:chapter|chap|ch|episode|ep|part)\s*[.#:\-]?\s*{_NUM}", re.I),
    re.compile(rf"第\s*{_NUM}\s*[章话話回]"),
    re.compile(rf"(?:^|[/\-_])(?:c|ch|chapter|episode|ep)[\-_]?{_NUM}(?:[/\-_.]|$)", re.I),
]

MIN_CHAPTER_CHARS = 300


@dataclass
class ExtractedChapter:
    url: str
    title: str
    html: str
    text_length: int
    number: float | None = None
    ts: str = ""


def detect_charset(body: bytes, content_type: str = "") -> str:
    m = re.search(r"charset=([\w\-]+)", content_type, re.I)
    if m:
        return m.group(1)
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w\-]+)", body[:4096], re.I)
    return m.group(1).decode() if m else "utf-8"


def decode_html(body: bytes, content_type: str = "") -> str:
    try:
        return body.decode(detect_charset(body, content_type), errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def sanitize(html: str) -> str:
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        url_schemes={"http", "https", "data"},
        link_rel=None,
    )


def _strip_wrapper(html: str) -> str:
    tree = HTMLParser(html)
    body = tree.body
    return body.inner_html if body is not None else html


def extract_content(html: str, url: str | None = None) -> str | None:
    """Return sanitised HTML of the page's main content, or None."""
    best: str | None = None
    # Recall mode keeps paragraph structure best on typical WordPress/translator sites;
    # fall back to the default (balanced) mode if it finds nothing.
    for opts in ({"favor_recall": True}, {}):
        best = trafilatura.extract(
            html,
            url=url,
            output_format="html",
            include_formatting=True,
            include_images=True,
            include_links=False,
            include_comments=False,
            include_tables=True,
            deduplicate=False,
            **opts,
        )
        if best:
            break
    if not best:
        return None
    cleaned = sanitize(_strip_wrapper(best))
    # Drop empty paragraphs left behind by sanitising.
    cleaned = re.sub(r"<p>\s*(?:<br>\s*)*</p>", "", cleaned)
    return cleaned.strip() or None


def text_length(html: str) -> int:
    tree = HTMLParser(html)
    node = tree.body or tree.root
    return len((node.text(separator=" ") if node else "").strip())


def page_title(html: str) -> str:
    tree = HTMLParser(html)
    for sel in ('meta[property="og:title"]', 'meta[name="twitter:title"]'):
        node = tree.css_first(sel)
        if node and node.attributes.get("content"):
            return node.attributes["content"].strip()
    node = tree.css_first("title")
    return node.text(strip=True) if node else ""


def chapter_number(*candidates: str) -> float | None:
    for cand in candidates:
        if not cand:
            continue
        for pat in CHAPTER_PATTERNS:
            m = pat.search(cand)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    continue
    return None


def url_path_text(url: str) -> str:
    return unquote(urlparse(url).path)


def is_error_page(title: str, status: int) -> bool:
    return not (200 <= status < 300) or bool(ERROR_TITLE_RE.search(title or ""))


def strip_common_affixes(titles: list[str]) -> list[str]:
    """Remove a site-name segment shared by most titles ("Ch 1 – Site" -> "Ch 1")."""
    if len(titles) < 2:
        return titles
    last = Counter()
    first = Counter()
    for t in titles:
        parts = TITLE_SEPARATORS.split(t)
        if len(parts) > 1:
            last[parts[-1]] += 1
            first[parts[0]] += 1
    threshold = max(2, int(len(titles) * 0.6))
    suffix = next((s for s, n in last.most_common(1) if n >= threshold), None)
    prefix = next((s for s, n in first.most_common(1) if n >= threshold), None)
    out = []
    for t in titles:
        parts = TITLE_SEPARATORS.split(t)
        if suffix and len(parts) > 1 and parts[-1] == suffix:
            t = t[: t.rfind(suffix)].rstrip(" |–—-:»·")
        if prefix and len(parts) > 1 and parts[0] == prefix and not chapter_number(prefix):
            t = t[len(prefix):].lstrip(" |–—-:»·")
        out.append(t.strip() or "Untitled")
    return out


def order_chapters(chapters: list[ExtractedChapter]) -> list[ExtractedChapter]:
    """Order by detected chapter number; unnumbered chapters keep capture order."""
    numbered = sum(c.number is not None for c in chapters)
    if numbered >= max(1, len(chapters) // 2):
        # Unnumbered pages (prologues, side stories) sort by capture time among themselves,
        # before numbered ones if they were captured first.
        return sorted(
            chapters,
            key=lambda c: (c.number if c.number is not None else float("-inf"), c.ts),
        )
    return sorted(chapters, key=lambda c: c.ts)
