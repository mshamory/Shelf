"""Import WACZ files into the database and keep the library in sync with disk."""

from __future__ import annotations

import hashlib
import html as html_lib
import logging
import mimetypes
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from sqlmodel import Session, select

from .config import settings
from .db import (
    BROWSER_PREFIX,
    Chapter,
    ChapterVersion,
    Novel,
    get_engine,
    has_archive,
    utcnow,
)
from .extract import (
    MIN_CHAPTER_CHARS,
    SIDE_SEASON,
    ChapterSpan,
    ExtractedChapter,
    chapter_number,
    classify,
    decode_html,
    extract_content,
    is_error_page,
    is_placeholder,
    order_chapters,
    page_title,
    strip_common_affixes,
    text_length,
    url_path_text,
)
from .gaps import covered, same_chapter
from .novelupdates import (
    MetadataError,
    NovelMetadata,
    fetch_image,
    is_series_url,
    looks_like_series_page,
    parse_series_page,
)
from .rendered import chapters_from_page_texts, looks_like_prose
from .rsc import RSC_MIME
from .rsc import page_url as rsc_page_url
from .rsc import render_page as render_rsc
from .wacz import Page, WaczFile, normalize_url
from .xhr import scan_xhr

log = logging.getLogger("shelf.library")

HTML_MIMES = {"text/html", "application/xhtml+xml", ""}

# Bump when extraction changes so existing archives are re-imported on the next scan.
EXTRACTOR_VERSION = "14"


@dataclass
class ParsedArchive:
    archive_title: str
    chapters: list[ExtractedChapter]
    metadata: NovelMetadata | None = None
    cover: tuple[bytes, str] | None = None
    skipped: list[str] = field(default_factory=list)
    # Chapter numbers seen in the archive without readable text -> reason (see xhr.py).
    unreadable: dict[int, str] = field(default_factory=dict)
    metadata_source: str = "wacz"  # "wacz": a NovelUpdates page in it; "epub": the book's own
    warnings: list[str] = field(default_factory=list)  # parts left out (a volume with DRM)


ARCHIVE_SUFFIXES = (".wacz", ".epub")


def library_rel(path: Path) -> str:
    """A file's path in the library folder, as listed there: a file linked in from
    elsewhere (a symlink) keeps the name it has in the library. A folder of volumes ends
    in "/"."""
    try:
        rel = path.relative_to(settings.library_dir)
    except ValueError:
        rel = path.resolve().relative_to(settings.library_dir.resolve())
    return f"{rel}/" if path.is_dir() else str(rel)


def library_items(files: list[Path]) -> list[Path]:
    """What the scan imports: each WACZ and loose EPUB, and each folder of EPUB volumes."""
    from .volumes import is_volume_folder

    items: dict[Path, None] = {}
    for p in files:
        if p.suffix.lower() == ".epub" and is_volume_folder(Path(library_rel(p)).parent):
            items[p.parent] = None
        else:
            items[p] = None
    return list(items)


def _signature(path: Path) -> tuple[int, float]:
    """Size and modification time; for a folder of volumes, all its books' together, and
    the folder's own time (a volume renamed, added or removed)."""
    if not path.is_dir():
        stat = path.stat()
        return stat.st_size, stat.st_mtime
    stats = [p.stat() for p in path.iterdir() if p.suffix.lower() == ".epub" and p.is_file()]
    return (sum(s.st_size for s in stats),
            max([path.stat().st_mtime] + [s.st_mtime for s in stats]))


def parse_book(path: Path) -> ParsedArchive:
    """An EPUB as an archive: chapters in the book's order, its metadata and cover."""
    from .epub import parse_epub

    book = parse_epub(path, library_rel(path))
    meta = book.metadata if book.metadata.title or book.metadata.authors else None
    return ParsedArchive(archive_title=book.title, chapters=book.chapters, metadata=meta,
                         cover=book.cover, skipped=book.skipped, metadata_source="epub")


def _page_from_capture(
    w: WaczFile, url: str, listed_title: str
) -> tuple[str, str, int, str] | None | str:
    """Find a capture of `url` that holds real chapter content.

    Returns (title, content_html, text_length, timestamp), the raw HTML of a NovelUpdates
    series page (as a str), or None. Captures are tried best-first, so a Cloudflare
    challenge recorded for one request doesn't hide a good capture from another.
    """
    for entry in w.captures(url):
        if entry.mime not in HTML_MIMES:
            continue
        resp = w.read(entry)
        if resp is None or resp.mime not in HTML_MIMES:
            continue
        html = decode_html(resp.body, resp.headers.get("content-type", ""))
        if looks_like_series_page(html):
            return html
        found = _chapter_from_html(html, url, resp.status)
        if found is None:
            continue
        own_title, content, n = found
        title = listed_title if listed_title and not is_error_page(listed_title, 200) else ""
        return (title or own_title or url), content, n, entry.timestamp
    return None


def _chapter_from_html(html: str, url: str, status: int) -> tuple[str, str, int] | None:
    """(own title, content_html, text_length) if the page holds real chapter content."""
    own_title = page_title(html)
    # Judge the response by its own title: pages.jsonl titles can be stale
    # (e.g. "Just a moment..." recorded before a challenge resolved).
    if is_error_page(own_title, status):
        return None
    content = extract_content(html, url)
    n = text_length(content) if content else 0
    if not content or n < MIN_CHAPTER_CHARS or is_placeholder(content):
        return None
    if n < SHORT_PAGE_CHARS and not looks_like_prose(_text_lines(content)):
        return None  # a short page of links/promos, e.g. a chapter page before it loaded
    return own_title, content, n


def rsc_chapters(w: WaczFile) -> list[ExtractedChapter]:
    """Chapters reached by navigating inside a Next.js site: the archive holds the page's
    React Server Components payload (``…/chapter-2?_rsc=…``) rather than an HTML page."""
    best: dict[str, ExtractedChapter] = {}
    for url in w.urls():
        page = rsc_page_url(url)
        if page is None:
            continue
        for entry in w.captures(url):
            if entry.mime not in (RSC_MIME, "") or not 200 <= entry.status < 300:
                continue
            resp = w.read(entry)
            if resp is None or resp.mime not in (RSC_MIME, ""):
                continue
            doc = render_rsc(resp.body)
            found = _chapter_from_html(doc, page, resp.status) if doc else None
            if found is None:
                continue
            title, content, n = found
            key = normalize_url(page)
            prev = best.get(key)
            if prev is None or n > prev.text_length:
                best[key] = ExtractedChapter(url=key, title=title or key, html=content,
                                             text_length=n, ts=entry.timestamp)
    return list(best.values())


def _url_key(url: str) -> str:
    return normalize_url(url).rstrip("/")


def _url_tail(url: str) -> tuple[str, ...]:
    """Host plus the last two path segments, to pair API URLs with page URLs."""
    parts = urlsplit(normalize_url(url))
    segments = [s for s in parts.path.split("/") if s]
    return (parts.netloc.lower(), *segments[-2:]) if len(segments) >= 2 else (url,)


BARE_TITLE_RE = re.compile(r"(?:chapter|chap\.?|ch\.?|episode|ep\.?)\s*[\d.\-]+", re.I)
# Byline/metadata lines between a chapter label and its full heading.
META_LINE_RE = re.compile(
    r"(?:\d+\s*(?:min(?:ute)?s?|m)\s*read\b|\d{1,2}\s+[a-z]{3,9}\.?\s+\d{4}\b"
    r"|[a-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}\b|\d{4}-\d{2}-\d{2}\b|(?:posted|updated|published)\b"
    r"|(?:by|translator|translated by|editor|edited by)\s*:?\s+\S|[\d,.]+k?\s+(?:words|views)\b)",
    re.I,
)
_LEADING_BLOCK_RE = re.compile(r"\s*(<(p|h[1-6])>(.*?)</\2>|<img[^>]*>|<br>)", re.S)


# Pages shorter than this must read as prose to count as a chapter.
SHORT_PAGE_CHARS = 1500
# Aggregator page titles: "#Chapter 1: X - Read Series Chapter 1: X Online - All Page - Novel Bin"
SEO_TAIL_RE = re.compile(r"\s+[-|–—]\s+(?:read|download)\b(?=.*\b(?:online|free|novel|chapter)\b)"
                         r".*$", re.I)
HASH_PREFIX_RE = re.compile(r"^(?:.*?\s)?#(?=[^\d\s#])")


def _text_lines(content: str) -> list[str]:
    tree = HTMLParser(content)
    node = tree.body or tree.root
    text = node.text(separator="\n") if node else ""
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def clean_seo_title(title: str) -> str:
    """"Series #Chapter 1: X - Read Series Chapter 1: X Online - Site" -> "Chapter 1: X"."""
    title = SEO_TAIL_RE.sub("", title.strip())
    return HASH_PREFIX_RE.sub("", title).strip() or title


def _drop_leading_repeats(content: str, title: str, series: str) -> str:
    """After promoting a heading: drop the series name and a repeat of the heading that
    follow it ("<h1>Ch. 3 - X</h1>Series<p>Chapter 3: X</p>…")."""
    number = chapter_number(title)
    for _ in range(3):
        stripped = content.lstrip()
        m = re.match(r"([^<]+)(?=<)", stripped)
        if m and series and m.group(1).strip().casefold() == series.strip().casefold():
            content = stripped[m.end():]
            continue
        m = _LEADING_BLOCK_RE.match(stripped)
        if m and m.group(2):
            text = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", "", m.group(3))))
            if len(text) <= 200 and number is not None and chapter_number(text) == number:
                content = stripped[m.end():]
                continue
        break
    return content.lstrip()


def promote_generic_titles(chapters: list[ExtractedChapter], series: str) -> None:
    """Pages all titled with the site name ("iNovelTranslation"): use the numbered heading
    at the start of the text instead."""
    counts = Counter(c.title.strip().casefold() for c in chapters if c.source == "html")
    for c in chapters:
        if (c.source != "html" or counts[c.title.strip().casefold()] < 3
                or chapter_number(c.title) is not None):
            continue
        title, content = _promote_heading("", c.html)
        if title:
            c.title, c.html = title, _drop_leading_repeats(content, title, series)


TITLE_NUMBER_RE = re.compile(r"^(.*?\D)?\s*#?(\d+(?:\.\d+)?)\s*$")
# "OVRMMO 106: The Last Stratagem", "VRMMO 003 - Time to Leave", "OVRMMO17: Arrows": a
# short name, the number, then the chapter's own name.
LEAD_NUMBER_RE = re.compile(
    r"^([^\d:]*?[^\W\d_])\s*#?(\d+(?:\.\d+)?)(?=\s*[:\-–—]\s*\S|\s+[^\W\d])")


def title_pattern_numbers(items: list[tuple[str, str, bool]]) -> list[tuple[float, str] | None]:
    """Numbers for unnumbered chapters titled alike: "<same words> <n>" ("Patron 12"),
    "<same words> <n>: <name>" ("OVRMMO 106: …"), or just a number once the site and
    series are removed ("Series — 17 | Site"; only if the URL ends in it too).

    `items` are (title, url, is_side_story). The pattern counts when most of the unnumbered
    main-story chapters follow it; side stories that follow it are numbered too ("OVRMMO
    94.5: Extra Edition" sits between 94 and 95). Returns (number, prefix) or None per item.
    """
    # Each title's readings, by prefix: "OVRMMO 099: Invasion: Part 2" reads both as
    # "OVRMMO" 99 and as "OVRMMO 099: Invasion: Part" 2; the novel's pattern decides.
    found: list[dict[str, float]] = []
    for title, url, _side in items:
        t = title.strip()
        readings = {}
        m = TITLE_NUMBER_RE.match(t)
        if m and (m.group(1) or _number_in_url(url, m.group(2))):
            readings[(m.group(1) or "").strip().casefold()] = float(m.group(2))
        if m := LEAD_NUMBER_RE.match(t):
            readings[m.group(1).strip().casefold()] = float(m.group(2))
        found.append(readings)
    main = [f for f, (_t, _u, side) in zip(found, items, strict=True) if not side]
    prefixes = Counter(p for f in main for p in f)
    if not prefixes:
        return [None] * len(items)
    prefix, count = prefixes.most_common(1)[0]
    if count < max(3, len(main) * 0.8):
        return [None] * len(items)
    return [(f[prefix], prefix) if prefix in f else None for f in found]


def _numbers_from_title_pattern(chapters: list[ExtractedChapter]) -> None:
    """Number chapters by their titles' shared pattern (see title_pattern_numbers)."""
    # Compare titles without their site suffix ("Patron 3 – Site"), cleaned per site.
    clean: dict[int, str] = {}
    for group in _groups_by_site(chapters):
        for c, t in zip(group, strip_common_affixes([c.title for c in group]), strict=True):
            clean[id(c)] = t
    loose = [c for c in chapters if c.number is None and c.season in (None, SIDE_SEASON)]
    found = title_pattern_numbers([(clean[id(c)], c.url, c.season == SIDE_SEASON)
                                   for c in loose])
    for c, f in zip(loose, found, strict=True):
        if f is None:
            continue
        c.number, c.season, c.season_word = f[0], None, ""
        if not re.search(r"\w", f[1]):  # just the number ("17", ".256")
            c.title = f"Chapter {f[0]:g}"


def _number_in_url(url: str, number: str) -> bool:
    """The URL's last path segment ends in this number ("…/tensei-slowlife-17/")."""
    last = urlsplit(url).path.rstrip("/").rpartition("/")[2]
    return re.search(rf"(?:^|\D){re.escape(number)}$", last) is not None


def _groups_by_site(chapters: list[ExtractedChapter]) -> list[list[ExtractedChapter]]:
    """Chapters by (source, host): each site formats its page titles its own way, and titles
    from JSON payloads have no site format at all."""
    groups: dict[tuple, list[ExtractedChapter]] = {}
    for c in chapters:
        groups.setdefault((c.source, urlsplit(c.url).netloc.lower()), []).append(c)
    return list(groups.values())


def _flat_text(c: ExtractedChapter) -> str:
    return " ".join(" ".join(_text_lines(c.html)).split())


def _same_text(a: ExtractedChapter, b: ExtractedChapter) -> bool:
    """A passage from the middle of one is in the other."""
    ta, tb = _flat_text(a), _flat_text(b)
    if len(ta) > len(tb):
        ta, tb = tb, ta
    mid = len(ta) // 2
    return bool(ta) and ta[mid:mid + 150] in tb


def mark_duplicates(chapters: list[ExtractedChapter]) -> None:
    """Several captures of one chapter number (two sites, two URLs for one post): keep one
    and mark the rest as duplicates (imported hidden). A capture much shorter than the
    longest is a stub; otherwise prefer the site with the most chapters, then the newest.

    On one site, a repeated number is only a duplicate if the text matches: arcs that
    restart their numbering, or "Chapter 2 – Part 1/2/3", are different chapters.
    """
    def host(c: ExtractedChapter) -> str:
        return urlsplit(c.url).netloc.lower()

    per_host = Counter(host(c) for c in chapters)
    groups: dict[tuple, list[ExtractedChapter]] = {}
    for c in chapters:
        if c.number is not None:
            groups.setdefault((c.season, c.number, c.number_end), []).append(c)
    for group in groups.values():
        if len(group) < 2:
            continue
        longest = max(c.text_length for c in group)
        best = max(group, key=lambda c: (c.text_length >= longest * 0.5, per_host[host(c)],
                                         c.ts))
        for c in group:
            if c is not best and (host(c) != host(best) or _same_text(c, best)):
                c.duplicate = True


def _promote_heading(title: str, content: str) -> tuple[str, str]:
    """Use a fuller heading from the start of the text as the title.

    "Chapter 10" + "<p>Chapter 10: Wow…</p>" -> title "Chapter 10: Wow…", and that line (plus
    any logo/heading clutter before it) is removed from the text. Looks at the first few
    blocks only.
    """
    base = re.sub(r"\s+", " ", title).strip()
    pos = 0
    for _ in range(4):
        m = _LEADING_BLOCK_RE.match(content, pos)
        if not m:
            break
        pos = m.end()
        if m.group(2) is None:
            continue  # image or line break
        text = html_lib.unescape(re.sub(r"<[^>]+>", "", m.group(3)))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        if base and text.casefold().startswith(base.casefold()):
            rest = text[len(base):].strip(" :-–—|.")
            if rest and len(text) <= 200 and not BARE_TITLE_RE.fullmatch(rest):
                return text, content[pos:].lstrip()
            if not rest or BARE_TITLE_RE.fullmatch(rest):
                continue  # "Chapter 678 : Chapter 678" - keep looking
        elif not base and len(text) <= 200 and chapter_number(text) is not None:
            return text, content[pos:].lstrip()
        if m.group(2) == "p" and len(text) <= 80 and META_LINE_RE.match(text):
            continue  # "12 min read · 11 Oct 2026", "Posted by X", "2,345 words"
        if m.group(2) == "p":
            break  # reached the body text
    return title or "", content


def _numbers_from_url_pattern(chapters: list[ExtractedChapter]) -> None:
    """Number chapters by URL when the site uses /…/<number> and their title doesn't say.

    Applies only if most numbered chapters follow that pattern under one parent path.
    """
    def split(url: str) -> tuple[str, str]:
        path = urlsplit(url).path.rstrip("/")
        parent, _, last = path.rpartition("/")
        return parent, last

    numbered = [c for c in chapters if c.number is not None]
    matches: dict[str, int] = {}
    for c in numbered:
        parent, last = split(c.url)
        if last.isdigit() and int(last) == int(c.number):
            matches[parent] = matches.get(parent, 0) + 1
    if not matches:
        return
    parent, count = max(matches.items(), key=lambda kv: kv[1])
    if count < max(3, len(numbered) * 0.8):
        return
    for c in chapters:
        if c.number is None and c.season is None:
            p, last = split(c.url)
            if p == parent and last.isdigit():
                c.number = float(last)
                if chapter_number(c.title) is None:
                    c.title = f"Chapter {last}"  # the page title was generic


def _is_part(number: float) -> bool:
    """207.002 ("Chapter 207-2"), not 12.5."""
    part = round((number - int(number)) * 1000)
    return 0 < part < 100 and abs(number - int(number) - part / 1000) < 1e-9


def resolve_ranges(chapters: list[ExtractedChapter]) -> None:
    """"Chapter 3-4" is a range of chapters, unless the novel numbers parts that way
    ("Chapter 3-1", "Chapter 3-2"): then it's part 4 of chapter 3."""
    if not any(c.number is not None and _is_part(c.number) for c in chapters):
        return
    for c in chapters:
        if c.range_kind == "dash" and c.number is not None:
            span = ChapterSpan(c.number, c.number_end, c.range_kind).as_part()
            c.number, c.number_end, c.range_kind = span.number, span.end, span.range_kind


POST_ID_RE = re.compile(r"(?:^|[-_])(\d{6,})$")


def _post_key(c: ExtractedChapter) -> tuple | None:
    parts = urlsplit(c.url)
    parent, _, last = parts.path.rstrip("/").rpartition("/")
    m = POST_ID_RE.search(last)
    if m is None or c.number is None:
        return None
    return parts.netloc.lower(), parent, m.group(1), c.season, int(c.number)


def drop_renamed_duplicates(chapters: list[ExtractedChapter]) -> list[ExtractedChapter]:
    """One post captured under two slugs (the author renamed it):
    /posts/chapter-692-dual-165442119 and /posts/chapter-692-form-165442119. Keeps the
    newest capture of each post id with the same chapter number."""
    newest: dict[tuple, ExtractedChapter] = {}
    for c in chapters:
        key = _post_key(c)
        if key is not None and (key not in newest or c.ts > newest[key].ts):
            newest[key] = c
    return [c for c in chapters if (key := _post_key(c)) is None or newest[key] is c]


def _strip_series_prefix(title: str, series: str) -> str:
    """"Novel Name - Chapter 3: X" -> "Chapter 3: X" when the remainder has a number."""
    series = series.strip()
    if not series or not title.casefold().startswith(series.casefold()):
        return title
    rest = title[len(series):].lstrip(" |–—-:»·,")
    return rest if rest and chapter_number(rest) is not None else title


def parse_wacz(path: Path) -> ParsedArchive:
    with WaczFile(path) as w:
        # One candidate per page, keyed by normalised URL (POST replays, challenge tokens…).
        candidates: dict[str, Page] = {}
        for page in w.pages():
            key = normalize_url(page.url)
            prev = candidates.get(key)
            if prev is None:
                candidates[key] = Page(url=key, title=page.title, ts=page.ts)
            elif is_error_page(prev.title, 200) and not is_error_page(page.title, 200):
                prev.title = page.title
        if not candidates:  # no page list: consider every captured HTML document
            for u in w.urls():
                c = w.best_capture(u)
                if c and c.mime in ("text/html", "application/xhtml+xml"):
                    candidates[u] = Page(url=u)

        metadata: NovelMetadata | None = None
        nu_urls = [u for u in w.urls() if is_series_url(u)]
        for url in nu_urls:
            resp = w.get(url)
            if resp and resp.status == 200:
                try:
                    metadata = parse_series_page(decode_html(resp.body, resp.mime), url)
                    break
                except MetadataError:
                    continue

        chapters: list[ExtractedChapter] = []
        skipped: list[str] = []
        for url, page in candidates.items():
            if is_series_url(url):
                continue
            found = _page_from_capture(w, url, page.title)
            if isinstance(found, str):
                if metadata is None:
                    try:
                        metadata = parse_series_page(found, url)
                    except MetadataError:
                        pass
                continue
            if found is None:
                skipped.append(url)
                continue
            title, content, n, captured = found
            chapters.append(
                ExtractedChapter(
                    url=url,
                    title=title,
                    html=content,
                    text_length=n,
                    ts=page.ts or captured,
                )
            )

        # Next.js chapters opened from another chapter: only the RSC payload was captured.
        have = {_url_key(c.url) for c in chapters}
        for c in rsc_chapters(w):
            if _url_key(c.url) not in have:
                chapters.append(c)

        # Chapters loaded by JavaScript live in captured JSON responses. Prefer them (they
        # carry the real title/number) unless the HTML capture is much longer, e.g. when the
        # JSON only holds a preview of a locked chapter.
        by_url = {_url_key(c.url): c for c in chapters}
        by_tail = {_url_tail(c.url): k for k, c in by_url.items()}
        xhr = scan_xhr(w)
        for x in xhr.chapters:
            key = _url_key(x.url)
            if key not in by_url and _url_tail(x.url) in by_tail:
                # An API URL for a page we have: /api/v2/series/s/12 <-> /series/s/12
                key = by_tail[_url_tail(x.url)]
            current = by_url.get(key)
            if current is not None and current.text_length > x.text_length * 1.5:
                continue
            title, content = _promote_heading(x.title, x.html)
            by_url[key] = ExtractedChapter(
                url=current.url if current else x.url, title=title, html=content,
                text_length=x.text_length, ts=x.ts, number_hint=x.number, source="xhr",
            )
        chapters = list(by_url.values())
        found = {_url_key(c.url) for c in chapters}
        skipped = [u for u in skipped if _url_key(u) not in found]

        # Drop duplicate captures of the same chapter (e.g. "/ch-12/" and "/ch-12-2/").
        seen: dict[str, ExtractedChapter] = {}
        for c in chapters:
            key = hashlib.sha1(c.html.encode()).hexdigest()
            if key not in seen or c.ts > seen[key].ts:
                seen[key] = c
        chapters = list(seen.values())

        series = (metadata.title if metadata else "") or w.title
        for c in chapters:
            if c.source == "html":
                c.title = clean_seo_title(c.title)
        promote_generic_titles(chapters, series)
        for c in chapters:
            classify(c, url_path_text(c.url))
        _numbers_from_url_pattern(chapters)
        _numbers_from_title_pattern(chapters)
        resolve_ranges(chapters)
        chapters = drop_renamed_duplicates(chapters)

        # Last resort: the text the browser displayed, saved with each page. It holds
        # chapters whose network responses aren't readable (e.g. encrypted and filled in by
        # the site's script), so it only fills numbers no other source has.
        present = {n for c in chapters for n in covered(c.number, c.number_end)}
        by_key = {_url_key(c.url): c for c in chapters}
        page_text: dict[str, str] = {}

        def already_imported(r) -> bool:
            """The page holding this text was imported whole (e.g. a volume per page)."""
            page = by_key.get(_url_key(r.page_url))
            if page is None:
                return False
            if r.page_url not in page_text:
                page_text[r.page_url] = " ".join(" ".join(_text_lines(page.html)).split())
            words = " ".join(" ".join(_text_lines(r.html)).split())
            mid = len(words) // 2
            return words[mid:mid + 150] in page_text[r.page_url]

        for r in chapters_from_page_texts(list(w.page_texts())):
            if int(r.number) in present or _url_key(r.url) in by_key or already_imported(r):
                continue
            c = ExtractedChapter(url=r.url, title=r.title, html=r.html,
                                 text_length=r.text_length, ts=r.ts, number_hint=r.number,
                                 source="text")
            classify(c, url_path_text(c.url))
            chapters.append(c)
            present.add(int(r.number))
        # Clean titles per site: page titles share a site format ("Series - Ch 3 - Site").
        for group in _groups_by_site(chapters):
            for c, t in zip(group, strip_common_affixes([c.title for c in group]), strict=True):
                c.title = t.rstrip(" :-–—|") or t  # "Chapter 1:" (no name after the colon)
        for c in chapters:
            c.title = _strip_series_prefix(c.title, series)
            if c.source == "html" and BARE_TITLE_RE.fullmatch(c.title):
                c.title, c.html = _promote_heading(c.title, c.html)

        cover = None
        if metadata and metadata.cover_url:
            resp = w.get(metadata.cover_url)
            if resp and resp.status == 200 and resp.mime.startswith("image/"):
                cover = (resp.body, resp.mime)

        chapters = order_chapters(chapters)
        mark_duplicates(chapters)
        # Payload numbers carry no season, so only report them for single-season novels.
        unreadable: dict[int, str] = {}
        if all(c.season in (None, SIDE_SEASON) for c in chapters):
            present = {n for c in chapters if c.season is None
                       for n in covered(c.number, c.number_end)}
            unreadable = {n: r for n, r in sorted(xhr.unavailable.items()) if n not in present}

        return ParsedArchive(
            archive_title=w.title,
            unreadable=unreadable,
            chapters=chapters,
            metadata=metadata,
            cover=cover,
            skipped=skipped,
        )


def save_cover(novel: Novel, data: bytes, mime: str) -> None:
    settings.covers_dir.mkdir(parents=True, exist_ok=True)
    ext = mimetypes.guess_extension(mime) or ".img"
    name = f"{novel.id}-{hashlib.sha1(data).hexdigest()[:10]}{ext}"
    (settings.covers_dir / name).write_bytes(data)
    if novel.cover_file and novel.cover_file != name:
        (settings.covers_dir / novel.cover_file).unlink(missing_ok=True)
    novel.cover_file = name


def apply_metadata(
    novel: Novel, meta: NovelMetadata, source: str, cover: tuple[bytes, str] | None = None
) -> None:
    novel.nu_url = meta.url or novel.nu_url
    novel.metadata_source = source
    novel.title = meta.title or novel.title
    for f in (
        "description_html", "novel_type", "language", "year", "status", "licensed", "rating",
        "votes", "authors", "artists", "genres", "tags", "alt_names", "original_publisher",
        "english_publisher",
    ):
        setattr(novel, f, getattr(meta, f))
    if meta.cover_url != novel.cover_url or not novel.cover_file:
        novel.cover_url = meta.cover_url or None
        if cover is None and meta.cover_url and settings.fetch_covers:
            cover = fetch_image(meta.cover_url)
        if cover:
            save_cover(novel, *cover)


def import_wacz(session: Session, path: Path, force: bool = False) -> Novel:
    """Create or refresh the Novel for a WACZ file. Returns the novel."""
    rel = library_rel(path)
    size, mtime = _signature(path)
    novel = session.exec(select(Novel).where(Novel.wacz_path == rel)).first()
    if (
        novel
        and not force
        and novel.wacz_size == size
        and novel.wacz_mtime == mtime
        and not novel.import_error
    ):
        return novel

    if novel is None:
        novel = Novel(wacz_path=rel, title=path.name if path.is_dir() else path.stem)
        session.add(novel)
        session.flush()

    novel.wacz_size, novel.wacz_mtime = size, mtime
    try:
        if path.is_dir():
            from .volumes import parse_volumes

            parsed = parse_volumes(path, rel)
        elif path.suffix.lower() == ".epub":
            parsed = parse_book(path)
        else:
            parsed = parse_wacz(path)
    except Exception as e:  # corrupt zip, unreadable WARC, ...
        log.exception("Failed to import %s", path)
        novel.import_error = f"{type(e).__name__}: {e}"
        session.commit()
        return novel

    novel.import_error = None if parsed.chapters else "No chapters found in this archive"
    novel.import_warning = "; ".join(parsed.warnings) or None
    novel.archive_title = parsed.archive_title
    if not novel.metadata_source:
        novel.title = parsed.archive_title or path.stem.replace("_", " ").replace("-", " ")

    # Keep reading progress and hidden flags across re-imports by matching chapter URLs.
    old = {c.url: c for c in novel.chapters}
    last_url = next((c.url for c in novel.chapters if c.id == novel.last_chapter_id), None)
    # Chapters sent from the browser stay, and win over archive chapters with their URL or
    # number.
    from .capture import refresh_captures

    refresh_captures(session, novel)
    kept = [c for c in novel.chapters if c.origin == "browser"]
    kept_urls = {_url_key(c.url) for c in kept}
    kept_numbers = {(c.season or 0, n) for c in kept for n in covered(c.number, c.number_end)
                    if c.number == int(c.number)}  # whole chapters, for unreadable numbers
    dropped = {c.id: _url_key(c.url) for c in novel.chapters if c.origin != "browser"}
    for c in list(novel.chapters):
        if c.origin != "browser":
            session.delete(c)
    session.flush()
    novel.unreadable_chapters = {str(n): r for n, r in parsed.unreadable.items()
                                 if (0, n) not in kept_numbers}

    new_chapters = []
    for i, ec in enumerate(parsed.chapters):
        if _url_key(ec.url) in kept_urls or (
                any((ec.season or 0) == (c.season or 0)
                    and same_chapter(ec.number, ec.number_end, c.number, c.number_end)
                    for c in kept)):
            continue
        ch = Chapter(
            novel_id=novel.id,
            position=i,
            number=ec.number,
            number_end=ec.number_end,
            season=ec.season,
            season_label=ec.season_label,
            title=ec.title,
            url=ec.url,
            captured_at=ec.ts,
            content_html=ec.html,
            char_count=ec.text_length,
            auto_title=ec.title,
            auto_number=ec.number,
            auto_number_end=ec.number_end,
            auto_season=ec.season,
            auto_season_label=ec.season_label,
            duplicate=ec.duplicate,
            hidden=ec.duplicate,
        )
        prev = old.get(ec.url)
        if prev is not None:
            # Your show/hide choice stands unless the chapter's duplicate status changed.
            if prev.duplicate == ec.duplicate:
                ch.hidden = prev.hidden
            if prev.custom_title:
                ch.title, ch.custom_title = prev.title, True
            if prev.custom_number:
                ch.number, ch.number_end, ch.custom_number = prev.number, prev.number_end, True
            if prev.custom_season:
                ch.season, ch.season_label, ch.custom_season = (
                    prev.season, prev.season_label, True)
        session.add(ch)
        new_chapters.append(ch)
    new_chapters += kept
    if kept or any(c.custom_number or c.custom_season for c in new_chapters):
        resequence(new_chapters)
    session.flush()
    move_versions(session, dropped, {_url_key(c.url): c.id for c in new_chapters})
    novel.last_chapter_id = next((c.id for c in new_chapters if c.url == last_url), None)
    if novel.last_chapter_id is None:
        novel.last_position = 0.0

    # Metadata from the archive wins over nothing, but never overrides user-supplied metadata.
    if parsed.metadata and novel.metadata_source in (None, parsed.metadata_source):
        apply_metadata(novel, parsed.metadata, parsed.metadata_source, parsed.cover)

    session.commit()
    session.refresh(novel)
    log.info("Imported %s: %d chapters (%d pages skipped)", rel, len(new_chapters),
             len(parsed.skipped))
    return novel


def move_versions(session: Session, dropped: dict[int, str], by_url: dict[str, int]) -> None:
    """Earlier versions of deleted chapters go to the chapter now at their URL, or are
    kept without a chapter."""
    if not dropped:
        return
    for v in session.exec(select(ChapterVersion).where(
            ChapterVersion.chapter_id.in_(list(dropped)))).all():
        v.chapter_id = by_url.get(dropped[v.chapter_id])


def delete_versions(session: Session, novel_id: int) -> None:
    for v in session.exec(select(ChapterVersion).where(ChapterVersion.novel_id == novel_id)):
        session.delete(v)


def captures_dir(novel_id: int) -> Path:
    return settings.data_dir / "captures" / str(novel_id)


def delete_captures(novel_id: int) -> None:
    import shutil

    shutil.rmtree(captures_dir(novel_id), ignore_errors=True)


def chapter_sort_key(ch: Chapter):
    """Reading order: season, then number; unnumbered chapters keep their relative place."""
    return (
        ch.season or 0,
        ch.number if ch.number is not None else float("-inf"),
        ch.position,
    )


def resequence(chapters: list[Chapter]) -> None:
    """Renumber positions after numbers/seasons were edited."""
    numbered = [c for c in chapters if c.number is not None]
    if len(numbered) >= max(1, len(chapters) // 2):
        # An unnumbered chapter (an afterword, a side story between chapters) stays after
        # the chapter it follows now.
        keys, prev = {}, (0, float("-inf"))
        for ch in sorted(chapters, key=lambda c: c.position):
            if ch.number is not None:
                prev = (ch.season or 0, ch.number)
                keys[id(ch)] = (*prev, 0, ch.position)
            elif ch.season is None or (ch.season or 0) == prev[0]:
                keys[id(ch)] = (*prev, 1, ch.position)
            else:
                keys[id(ch)] = (ch.season or 0, float("-inf"), 1, ch.position)
        for i, ch in enumerate(sorted(chapters, key=lambda c: keys[id(c)])):
            ch.position = i
        return
    # Mostly unnumbered: capture order is all there is for those, so they stay where they
    # are, and the numbered chapters are put in order among the places they hold.
    wanted = sorted(numbered, key=chapter_sort_key)
    ordered = sorted(chapters, key=lambda c: c.position)
    places = [i for i, c in enumerate(ordered) if c.number is not None]
    for i, ch in enumerate(ordered):
        ch.position = i
    for i, ch in zip(places, wanted, strict=True):
        ch.position = i


# -- background scanning ---------------------------------------------------


@dataclass
class ScanStatus:
    running: bool = False
    current: str = ""
    done: int = 0
    total: int = 0
    last_finished: str = ""
    errors: list[str] = field(default_factory=list)


scan_status = ScanStatus()
_scan_lock = threading.Lock()


def scan_library(force: bool = False) -> None:
    if not _scan_lock.acquire(blocking=False):
        return  # a scan is already running
    try:
        settings.library_dir.mkdir(parents=True, exist_ok=True)
        version_file = settings.data_dir / "extractor-version"
        try:
            outdated = version_file.read_text().strip() != EXTRACTOR_VERSION
        except FileNotFoundError:
            outdated = True
        force = force or outdated
        files = library_items(sorted(p for p in settings.library_dir.rglob("*")
                                     if p.suffix.lower() in ARCHIVE_SUFFIXES and p.is_file()))
        scan_status.running, scan_status.done, scan_status.total = True, 0, len(files)
        scan_status.errors = []
        with Session(get_engine()) as session:
            for p in files:
                scan_status.current = p.name
                try:
                    n = import_wacz(session, p, force=force)
                    if n.import_error:
                        scan_status.errors.append(f"{p.name}: {n.import_error}")
                except Exception as e:
                    session.rollback()
                    log.exception("Import failed for %s", p)
                    scan_status.errors.append(f"{p.name}: {e}")
                scan_status.done += 1
            if force:  # extraction changed: also redo novels made only from the browser
                from .capture import refresh_captures

                for novel in session.exec(select(Novel)).all():
                    if not has_archive(novel) and refresh_captures(session, novel):
                        session.commit()
            # Forget novels whose files have disappeared.
            present = {library_rel(p) for p in files}
            for novel in session.exec(select(Novel)).all():
                if not has_archive(novel) or novel.wacz_path in present:
                    continue
                if any(c.origin == "browser" for c in novel.chapters):
                    # Keep what was sent from the browser; drop only the archive's chapters.
                    log.info("Archive %s is gone; keeping browser chapters", novel.wacz_path)
                    dropped = {c.id: "" for c in novel.chapters if c.origin != "browser"}
                    for c in list(novel.chapters):
                        if c.origin != "browser":
                            session.delete(c)
                    move_versions(session, dropped, {})
                    novel.wacz_path = f"{BROWSER_PREFIX}{novel.id}"
                    novel.unreadable_chapters = {}
                    continue
                log.info("Removing %s (file no longer in library)", novel.wacz_path)
                if novel.cover_file:
                    (settings.covers_dir / novel.cover_file).unlink(missing_ok=True)
                delete_captures(novel.id)
                delete_versions(session, novel.id)
                session.delete(novel)
            session.commit()
        version_file.write_text(EXTRACTOR_VERSION)
    finally:
        scan_status.running = False
        scan_status.current = ""
        scan_status.last_finished = utcnow().isoformat(timespec="seconds")
        _scan_lock.release()


def start_scan(force: bool = False) -> bool:
    if scan_status.running:
        return False
    threading.Thread(target=scan_library, kwargs={"force": force}, daemon=True).start()
    return True
