"""Chapters from pages saved in the browser (Ctrl+S), uploaded one file per page.

A saved page is handled like a page sent by the extension (see capture.py), with two
differences: the page's address has to be recovered from the file, and the browser may have
rewritten image links to a local "<name>_files/" folder that was not uploaded.

Supported: "Web page, HTML only" and "Web page, complete" (.html/.htm), and single-file
MHTML (.mhtml/.mht, Chrome's "Web page, single file").
"""

import email
import email.policy
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from sqlmodel import Session

from .capture import (
    TITLE_SPLIT_RE,
    CaptureError,
    _site_words,
    add_chapter,
    create_novel,
    novel_by_nu_url,
)
from .db import Novel
from .extract import SEASON_RE, _chapter_match, chapter_number, page_title
from .library import BARE_TITLE_RE, apply_metadata, delete_captures
from .novelupdates import MetadataError, is_series_url, parse_series_page

PAGE_EXTENSIONS = (".html", ".htm", ".xhtml", ".mhtml", ".mht")
MAX_PAGE_BYTES = 50 * 1024 * 1024
# Chrome and IE mark saved pages with their address: <!-- saved from url=(0042)https://… -->
SAVED_FROM_RE = re.compile(rb"<!--\s*saved from url=\(\d+\)\s*(\S+?)\s*-->", re.I)
# <img src="./Chapter 3_files/art.jpg">: a relative link (no scheme) into a "_files" folder.
LOCAL_IMG_RE = re.compile(
    r"""<img\b[^>]*?\bsrc\s*=\s*(?:"[^":]*?_files/[^"]*"|'[^':]*?_files/[^']*'"""
    r"""|[^\s>"':]*?_files/[^\s>]*)[^>]*>""", re.I)
META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w.:-]+)""", re.I)


@dataclass
class SavedPage:
    name: str  # the uploaded file's name
    url: str
    html: str


def is_saved_page(name: str) -> bool:
    return name.lower().endswith(PAGE_EXTENSIONS)


def _decode(data: bytes, charset: str | None = None) -> str:
    for bom, codec in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"),
                       (b"\xfe\xff", "utf-16-be")):
        if data.startswith(bom):
            return data[len(bom):].decode(codec, errors="replace")
    if not charset:
        m = META_CHARSET_RE.search(data[:4096])
        charset = m.group(1).decode("ascii", errors="ignore") if m else None
    for codec in (charset, "utf-8"):
        if not codec:
            continue
        try:
            return data.decode(codec)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _web_url(url: str | None) -> str | None:
    url = (url or "").strip()
    return url if urlsplit(url).scheme in ("http", "https") and urlsplit(url).netloc else None


def _url_in_page(html: str) -> str | None:
    """The page's own address, as the site states it."""
    tree = HTMLParser(html[:200_000])
    for sel, attr in (('link[rel="canonical"]', "href"), ('meta[property="og:url"]', "content"),
                      ("base[href]", "href")):
        node = tree.css_first(sel)
        if node is not None and (url := _web_url(node.attributes.get(attr))):
            return url
    return None


def _drop_local_images(html: str) -> str:
    """'Web page, complete' points images at a local "<name>_files/" folder that wasn't
    uploaded; such images would only show as broken."""
    return LOCAL_IMG_RE.sub("", html)


def _from_mhtml(data: bytes) -> tuple[str | None, str]:
    msg = email.message_from_bytes(data, policy=email.policy.default)
    url = _web_url(msg.get("Snapshot-Content-Location"))
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        body = part.get_payload(decode=True) or b""
        return url or _web_url(part.get("Content-Location")), _decode(
            body, part.get_content_charset())
    raise CaptureError("No web page found in this MHTML file")


def read_saved_page(name: str, data: bytes) -> SavedPage:
    """The page in an uploaded file, and the address it was saved from (or, when the file
    doesn't say, a file: address made from its name)."""
    if len(data) > MAX_PAGE_BYTES:
        raise CaptureError("The file is too large for a saved page")
    head = data[:2048].lstrip()
    if name.lower().endswith((".mhtml", ".mht")) or head.startswith(
            (b"From:", b"MIME-Version:", b"Snapshot-Content-Location:")):
        url, html = _from_mhtml(data)
    else:
        m = SAVED_FROM_RE.search(data[:4096])
        url = _web_url(m.group(1).decode("ascii", errors="ignore")) if m else None
        html = _decode(data)
    url = url or _url_in_page(html)
    if url is None:
        url = "file:///" + quote(name)
    else:
        html = _drop_local_images(html)
    return SavedPage(name=name, url=urlsplit(url)._replace(fragment="").geturl(), html=html)


def guess_series(pages: list[SavedPage]) -> str:
    """The novel's name from its chapters' page titles: the part that isn't the chapter or
    the site ("Night Train - Chapter 3: Stop 3 | Example Reads" -> "Night Train")."""
    found: Counter[str] = Counter()
    place: dict[str, int] = {}  # earliest position in a title
    in_url: Counter[str] = Counter()  # pages whose address has the name ("/hiding-tunnels-…")
    for p in pages:
        path = "-" + "-".join(re.findall(r"[^\W_]+", urlsplit(p.url).path.casefold())) + "-"
        site = {re.sub(r"\W", "", w) for w in _site_words(p.url, p.html)}
        parts = [part.strip() for part in TITLE_SPLIT_RE.split(page_title(p.html))]
        names: dict[str, int] = {}
        for i, part in enumerate(parts):
            if chapter_number(part) is not None:
                # "Series Part 2 Chapter 428: Name": the series comes before the label.
                names.setdefault(_before_label(part), i)
            else:
                names.setdefault(part, i)
        for name, i in names.items():
            # On a single page, "Chapter 5 – Name" names the chapter, unless the address
            # names it too ("Chapter 91 · Hiding Tunnels in Xianxia" at /hiding-tunnels-…).
            after_label = i and BARE_TITLE_RE.fullmatch(parts[i - 1])
            slug = "-".join(re.findall(r"[^\W_]+", name.casefold()))
            if len(pages) == 1 and after_label and f"-{slug}-" not in path:
                continue
            if len(name) >= 2 and re.sub(r"\W", "", name.casefold()) not in site:
                found[name] += 1
                place[name] = min(place.get(name, i), i)
                in_url[name] += f"-{slug}-" in path
    # Shared by most pages: with several pages, a part only one has is a chapter's name.
    # The address often names the series; titles put it before the site's name.
    need = max(2, len(pages) // 2) if len(pages) > 1 else 1
    common = [(n, in_url[part], -place[part], part) for part, n in found.items() if n >= need]
    return max(common)[-1] if common else ""


def _before_label(part: str) -> str:
    """The text before a chapter label and any season label in front of it."""
    m = _chapter_match(part)
    head = part[:m.start()] if m else ""
    season = SEASON_RE.search(head)
    return (head[:season.start()] if season else head).strip(" :-–—|,")


@dataclass
class UploadReport:
    novel: Novel | None = None
    created: bool = False
    metadata: bool = False
    actions: Counter = field(default_factory=Counter)  # added / updated / replaced
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (file, reason)

    def summary(self) -> str:
        parts = []
        if self.metadata:
            parts.append("NovelUpdates metadata imported")
        for action, label in (("added", "added"), ("updated", "updated"),
                              ("replaced", "replaced")):
            if n := self.actions[action]:
                parts.append(f"{n} chapter{'s' if n != 1 else ''} {label}")
        if self.actions["replaced"] or self.actions["updated"]:
            parts.append("the chapters they replaced are kept in each chapter's history")
        text = "; ".join(parts) if parts else "Nothing was added"
        text = text[:1].upper() + text[1:]
        if self.skipped:
            shown = "; ".join(f"{name}: {why}" for name, why in self.skipped[:3])
            more = len(self.skipped) - 3
            text += (f". {len(self.skipped)} file{'s' if len(self.skipped) != 1 else ''} "
                     f"skipped ({shown}{f'; and {more} more' if more > 0 else ''})")
        return text


def add_saved_pages(session: Session, novel: Novel | None, files: list[tuple[str, bytes]],
                    title: str = "") -> UploadReport:
    """Add uploaded pages to a novel, or to a new one when `novel` is None. A saved
    NovelUpdates series page among them supplies the novel's metadata."""
    report = UploadReport(novel=novel)
    pages, series_pages = [], []
    for name, data in files:
        try:
            page = read_saved_page(name, data)
        except CaptureError as e:
            report.skipped.append((name, str(e)))
            continue
        if is_series_url(page.url):
            try:
                series_pages.append((page, parse_series_page(page.html, page.url)))
            except MetadataError as e:
                report.skipped.append((name, str(e)))
        else:
            pages.append(page)
    # Pages that state the same address (e.g. a canonical link to the novel's index) would
    # replace one another: tell them apart by file name instead.
    stated = Counter(p.url for p in pages)
    for p in pages:
        if stated[p.url] > 1:
            p.url = "file:///" + quote(p.name)

    meta = series_pages[0][1] if series_pages else None
    if report.novel is None:
        if meta is not None:
            report.novel = novel_by_nu_url(session, meta.url or series_pages[0][0].url)
        if report.novel is None:
            if not pages and meta is None:
                return report
            name = title.strip() or (meta.title if meta else "") or guess_series(pages)
            report.novel, report.created = create_novel(session, name), True
    if meta is not None:
        apply_metadata(report.novel, meta, "upload")
        session.commit()
        report.metadata = True

    for page in pages:
        try:
            result = add_chapter(session, report.novel, page.url, page.html)
        except CaptureError as e:
            session.rollback()
            report.skipped.append((page.name, str(e)))
            continue
        report.actions[result.action] += 1

    if report.created and not report.metadata and not report.actions:
        # Nothing could be read: don't leave an empty novel behind.
        delete_captures(report.novel.id)
        session.delete(report.novel)
        session.commit()
        report.novel, report.created = None, False
    return report
