"""An OPDS 1.2 catalog of the library, for e-reader apps (KOReader, Thorium, Moon+ Reader...).

Each novel is offered as the EPUB Shelf exports (see export.py), so a reader app gets the
chapters as Shelf shows them. The catalog has feeds for the whole library, recently updated
novels, novels with new chapters and each reading list, and an OpenSearch search.

Shelf has no authentication, so neither does the catalog (see README)."""

from __future__ import annotations

import mimetypes
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlencode

import lxml.html
from lxml import etree

from . import __version__
from .db import Novel

ATOM = "http://www.w3.org/2005/Atom"
DC = "http://purl.org/dc/terms/"
OPDS = "http://opds-spec.org/2010/catalog"
OS = "http://a9.com/-/spec/opensearch/1.1/"
NSMAP = {None: ATOM, "dc": DC, "opds": OPDS, "opensearch": OS}

FEED_TYPE = "application/atom+xml;profile=opds-catalog"
NAVIGATION = FEED_TYPE + ";kind=navigation"
ACQUISITION = FEED_TYPE + ";kind=acquisition"
OPENSEARCH = "application/opensearchdescription+xml"
EPUB = "application/epub+zip"
PAGE_SIZE = 50

# A path in the app ("/opds/all") -> an absolute URL. Reader apps don't all resolve
# relative links, so every link is absolute.
UrlFor = Callable[[str], str]


def _stamp(dt: datetime | None) -> str:
    dt = dt or datetime(1970, 1, 1, tzinfo=UTC)
    if dt.tzinfo is None:  # SQLite hands back naive datetimes; they're stored as UTC
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _el(parent, tag: str, text: str | None = None, ns: str = ATOM, **attrs):
    e = etree.SubElement(parent, f"{{{ns}}}{tag}", {k: v for k, v in attrs.items() if v})
    if text is not None:
        e.text = text
    return e


def _link(parent, url: UrlFor, rel: str, href: str, type: str, title: str = "", **extra):
    return _el(parent, "link", rel=rel, href=url(href), type=type, title=title, **extra)


def _feed(url: UrlFor, path: str, title: str, kind: str, updated: datetime | None):
    feed = etree.Element(f"{{{ATOM}}}feed", nsmap=NSMAP)
    _el(feed, "id", "urn:shelf:" + path.strip("/").replace("/", ":"))
    _el(feed, "title", title)
    _el(feed, "updated", _stamp(updated))
    author = _el(feed, "author")
    _el(author, "name", "Shelf")
    _el(feed, "generator", "Shelf", version=__version__)
    _link(feed, url, "self", path, kind)
    _link(feed, url, "start", "/opds", NAVIGATION, "Shelf")
    _link(feed, url, "search", "/opds/search.xml", OPENSEARCH, "Search Shelf")
    return feed


def serialize(feed) -> bytes:
    return etree.tostring(feed, xml_declaration=True, encoding="utf-8", pretty_print=True)


def _text(html: str | None) -> str:
    if not html or not html.strip():
        return ""
    try:
        return " ".join(lxml.html.fromstring(html).text_content().split())
    except (etree.ParserError, ValueError):
        return ""


def novel_updated(novel: Novel) -> datetime:
    return novel.updated_at or novel.added_at


def navigation(url: UrlFor, sections: list[tuple[str, str, str, int]],
               updated: datetime | None) -> bytes:
    """The catalog's start: one entry per feed, as (path, title, description, count)."""
    feed = _feed(url, "/opds", "Shelf", NAVIGATION, updated)
    for path, title, description, count in sections:
        entry = _el(feed, "entry")
        _el(entry, "title", title)
        _el(entry, "id", "urn:shelf:" + path.strip("/").replace("/", ":"))
        _el(entry, "updated", _stamp(updated))
        _el(entry, "content", f"{description} ({count})", type="text")
        rel = "http://opds-spec.org/sort/new" if path == "/opds/updated" else "subsection"
        _link(entry, url, rel, path, ACQUISITION, title, **{f"{{{OPDS}}}count": str(count)})
    return serialize(feed)


def acquisition(url: UrlFor, path: str, title: str, novels: list[Novel],
                chapters: dict[int, int], page: int = 1, query: str = "") -> bytes:
    """A page of novels, each with its EPUB, cover and description."""
    pages = max(1, -(-len(novels) // PAGE_SIZE))
    page = min(max(page, 1), pages)
    shown = novels[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    updated = max((novel_updated(n) for n in novels), default=None)

    def page_path(n: int) -> str:
        params = {"q": query} if query else {}
        if n > 1:
            params["page"] = n
        return path + ("?" + urlencode(params) if params else "")

    feed = _feed(url, page_path(page), title, ACQUISITION, updated)
    _link(feed, url, "up", "/opds", NAVIGATION, "Shelf")
    if page > 1:
        _link(feed, url, "first", page_path(1), ACQUISITION)
        _link(feed, url, "previous", page_path(page - 1), ACQUISITION)
    if page < pages:
        _link(feed, url, "next", page_path(page + 1), ACQUISITION)
        _link(feed, url, "last", page_path(pages), ACQUISITION)
    _el(feed, "totalResults", str(len(novels)), ns=OS)
    _el(feed, "itemsPerPage", str(PAGE_SIZE), ns=OS)
    _el(feed, "startIndex", str((page - 1) * PAGE_SIZE + 1), ns=OS)
    for novel in shown:
        _entry(feed, url, novel, chapters.get(novel.id, 0))
    return serialize(feed)


def _entry(feed, url: UrlFor, novel: Novel, chapters: int) -> None:
    entry = _el(feed, "entry")
    _el(entry, "title", novel.title or novel.archive_title or "Untitled")
    _el(entry, "id", f"urn:shelf:novel:{novel.id}")
    _el(entry, "updated", _stamp(novel_updated(novel)))
    for name in novel.authors:
        _el(_el(entry, "author"), "name", name)
    if novel.language:
        _el(entry, "language", novel.language, ns=DC)
    if novel.year:
        _el(entry, "issued", novel.year, ns=DC)
    for genre in novel.genres:
        _el(entry, "category", term=genre, label=genre)
    summary = _text(novel.description_html)
    count = f"{chapters} chapter{'s' if chapters != 1 else ''}"
    _el(entry, "summary", f"{count}. {summary}" if summary else count + ".", type="text")
    if novel.cover_file:
        cover = f"/covers/{novel.cover_file}"
        mime = mimetypes.guess_type(novel.cover_file)[0] or "image/jpeg"
        _link(entry, url, "http://opds-spec.org/image", cover, mime)
        _link(entry, url, "http://opds-spec.org/image/thumbnail", cover, mime)
    _link(entry, url, "http://opds-spec.org/acquisition", f"/novel/{novel.id}/export.epub",
          EPUB, "EPUB")
    _link(entry, url, "alternate", f"/novel/{novel.id}", "text/html", "Open in Shelf")


def opensearch(url: UrlFor) -> bytes:
    root = etree.Element(f"{{{OS}}}OpenSearchDescription", nsmap={None: OS})
    _el(root, "ShortName", "Shelf", ns=OS)
    _el(root, "Description", "Search the novels on your Shelf", ns=OS)
    _el(root, "InputEncoding", "UTF-8", ns=OS)
    _el(root, "OutputEncoding", "UTF-8", ns=OS)
    _el(root, "Url", ns=OS, type=ACQUISITION,
        template=url("/opds/search") + "?q={searchTerms}")
    return serialize(root)
