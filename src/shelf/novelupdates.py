"""Parse NovelUpdates series pages into metadata.

Shelf never tries to get around NovelUpdates' bot protection. Metadata comes from:
  1. a NovelUpdates series page captured inside the WACZ itself (preferred: archive it
     alongside the chapters with ArchiveWeb.page / Browsertrix),
  2. a series page saved from the browser ("Save Page As…") and uploaded, or
  3. a plain HTTP fetch, which only works when the site allows it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from . import __version__
from .extract import sanitize

NU_HOSTS = {"novelupdates.com", "www.novelupdates.com"}
SERIES_PATH_RE = re.compile(r"^/series/[^/]+/?$")


class MetadataError(Exception):
    pass


@dataclass
class NovelMetadata:
    url: str = ""
    title: str = ""
    cover_url: str = ""
    description_html: str = ""
    novel_type: str = ""
    language: str = ""
    year: str = ""
    status: str = ""
    licensed: str = ""
    rating: float | None = None
    votes: int | None = None
    authors: list[str] = field(default_factory=list)
    artists: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    alt_names: list[str] = field(default_factory=list)
    original_publisher: list[str] = field(default_factory=list)
    english_publisher: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def is_series_url(url: str) -> bool:
    p = urlparse(url)
    return p.netloc.lower() in NU_HOSTS and bool(SERIES_PATH_RE.match(p.path))


def normalize_series_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://www.novelupdates.com/series/" + url.strip("/") + "/"
    p = urlparse(url)
    if p.netloc.lower() not in NU_HOSTS or not SERIES_PATH_RE.match(p.path):
        raise MetadataError("Not a NovelUpdates series URL (expected …/series/<name>/)")
    return f"https://www.novelupdates.com{p.path.rstrip('/')}/"


def _texts(tree: HTMLParser, selector: str) -> list[str]:
    out = []
    for n in tree.css(selector):
        t = n.text(strip=True)
        if t and t.lower() != "n/a" and t not in out:
            out.append(t)
    return out


def _text(tree: HTMLParser, selector: str) -> str:
    n = tree.css_first(selector)
    if n is None:
        return ""
    t = re.sub(r"\s+", " ", n.text(separator=" ")).strip()
    return "" if t.lower() == "n/a" else t


def _lines(node: Node | None) -> list[str]:
    if node is None:
        return []
    html = re.sub(r"<br\s*/?>", "\n", node.inner_html or "", flags=re.I)
    text = HTMLParser(f"<div>{html}</div>").text(separator="")
    return [ln.strip() for ln in text.splitlines() if ln.strip() and ln.strip().lower() != "n/a"]


def looks_like_series_page(html: str) -> bool:
    tree = HTMLParser(html)
    return tree.css_first(".seriestitlenu") is not None or tree.css_first("#editdescription") is not None


def parse_series_page(html: str, url: str = "") -> NovelMetadata:
    tree = HTMLParser(html)
    title = _text(tree, ".seriestitlenu")
    if not title:
        raise MetadataError("This doesn't look like a NovelUpdates series page")

    if not url:
        canon = tree.css_first('link[rel="canonical"]') or tree.css_first('meta[property="og:url"]')
        if canon is not None:
            url = canon.attributes.get("href") or canon.attributes.get("content") or ""

    cover = ""
    img = tree.css_first(".seriesimg img") or tree.css_first(".serieseditimg img")
    if img is not None:
        cover = img.attributes.get("src") or img.attributes.get("data-src") or ""
        if "noimagemid" in cover:
            cover = ""
        elif cover:
            cover = urljoin(url or "https://www.novelupdates.com/", cover)

    desc = tree.css_first("#editdescription")
    description_html = sanitize(desc.inner_html or "") if desc is not None else ""

    rating = votes = None
    m = re.search(
        r"([\d.]+)\s*/\s*5(?:\.0)?\s*,\s*([\d,]+)\s*votes?", _text(tree, ".uvotes")
    )
    if m:
        rating = float(m.group(1))
        votes = int(m.group(2).replace(",", ""))

    return NovelMetadata(
        url=url,
        title=title,
        cover_url=cover,
        description_html=description_html.strip(),
        novel_type=_text(tree, "#showtype"),
        language=_text(tree, "#showlang"),
        year=_text(tree, "#edityear"),
        status=_text(tree, "#editstatus"),
        licensed=_text(tree, "#showlicensed"),
        rating=rating,
        votes=votes,
        authors=_texts(tree, "#showauthors a"),
        artists=_texts(tree, "#showartists a"),
        genres=_texts(tree, "#seriesgenre a"),
        tags=_texts(tree, "#showtags a"),
        alt_names=_lines(tree.css_first("#editassociated")),
        original_publisher=_texts(tree, "#showopublisher a"),
        english_publisher=_texts(tree, "#showepublisher a"),
    )


USER_AGENT = f"Shelf/{__version__} (self-hosted web novel reader)"


def fetch_series(url: str, timeout: float = 20.0) -> NovelMetadata:
    """Fetch a series page with a plain HTTP client. Fails cleanly if blocked."""
    url = normalize_series_url(url)
    try:
        r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout,
                      follow_redirects=True)
    except httpx.HTTPError as e:
        raise MetadataError(f"Could not reach NovelUpdates: {e}") from e
    if r.status_code in (403, 429, 503):
        raise MetadataError(
            f"NovelUpdates refused the request (HTTP {r.status_code}). Open the series page in "
            "your browser, save it (Ctrl+S, 'HTML only'), and upload the file instead — or "
            "archive the page into the WACZ."
        )
    if r.status_code != 200:
        raise MetadataError(f"NovelUpdates returned HTTP {r.status_code}")
    return parse_series_page(r.text, url)


def fetch_image(url: str, timeout: float = 20.0) -> tuple[bytes, str] | None:
    try:
        r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout,
                      follow_redirects=True)
    except httpx.HTTPError:
        return None
    ctype = r.headers.get("content-type", "").split(";")[0]
    if r.status_code != 200 or not ctype.startswith("image/"):
        return None
    return r.content, ctype
