"""Import WACZ files into the database and keep the library in sync with disk."""

from __future__ import annotations

import hashlib
import html as html_lib
import logging
import mimetypes
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from sqlmodel import Session, select

from .config import settings
from .db import Chapter, Novel, get_engine, utcnow
from .extract import (
    MIN_CHAPTER_CHARS,
    ExtractedChapter,
    chapter_number,
    classify,
    decode_html,
    extract_content,
    is_error_page,
    order_chapters,
    page_title,
    strip_common_affixes,
    text_length,
    url_path_text,
)
from .novelupdates import (
    MetadataError,
    NovelMetadata,
    fetch_image,
    is_series_url,
    looks_like_series_page,
    parse_series_page,
)
from .wacz import Page, WaczFile, normalize_url
from .xhr import extract_xhr_chapters

log = logging.getLogger("shelf.library")

HTML_MIMES = {"text/html", "application/xhtml+xml", ""}

# Bump when extraction changes so existing archives are re-imported on the next scan.
EXTRACTOR_VERSION = "4"


@dataclass
class ParsedArchive:
    archive_title: str
    chapters: list[ExtractedChapter]
    metadata: NovelMetadata | None = None
    cover: tuple[bytes, str] | None = None
    skipped: list[str] = field(default_factory=list)


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
        own_title = page_title(html)
        # Judge the response by its own title: pages.jsonl titles can be stale
        # (e.g. "Just a moment..." recorded before a challenge resolved).
        if is_error_page(own_title, resp.status):
            continue
        content = extract_content(html, url)
        n = text_length(content) if content else 0
        if not content or n < MIN_CHAPTER_CHARS:
            continue
        title = listed_title if listed_title and not is_error_page(listed_title, 200) else ""
        return (title or own_title or url), content, n, entry.timestamp
    return None


def _url_key(url: str) -> str:
    return normalize_url(url).rstrip("/")


def _url_tail(url: str) -> tuple[str, ...]:
    """Host plus the last two path segments, to pair API URLs with page URLs."""
    parts = urlsplit(normalize_url(url))
    segments = [s for s in parts.path.split("/") if s]
    return (parts.netloc.lower(), *segments[-2:]) if len(segments) >= 2 else (url,)


BARE_TITLE_RE = re.compile(r"(?:chapter|chap\.?|ch\.?|episode|ep\.?)\s*[\d.\-]+", re.I)
_LEADING_BLOCK_RE = re.compile(r"\s*(<(p|h[1-6])>(.*?)</\2>|<img[^>]*>|<br>)", re.S)


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

        # Chapters loaded by JavaScript live in captured JSON responses. Prefer them (they
        # carry the real title/number) unless the HTML capture is much longer, e.g. when the
        # JSON only holds a preview of a locked chapter.
        by_url = {_url_key(c.url): c for c in chapters}
        by_tail = {_url_tail(c.url): k for k, c in by_url.items()}
        for x in extract_xhr_chapters(w):
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

        for c in chapters:
            classify(c, url_path_text(c.url))
        _numbers_from_url_pattern(chapters)
        # Clean titles per source: page titles share a site format ("Series - Ch 3 - Site"),
        # titles from JSON payloads don't, and mixing them would dilute the vote.
        for source in {c.source for c in chapters}:
            group = [c for c in chapters if c.source == source]
            for c, t in zip(group, strip_common_affixes([c.title for c in group]), strict=True):
                c.title = t
        series = (metadata.title if metadata else "") or w.title
        for c in chapters:
            c.title = _strip_series_prefix(c.title, series)
            if c.source == "html" and BARE_TITLE_RE.fullmatch(c.title):
                c.title, c.html = _promote_heading(c.title, c.html)

        cover = None
        if metadata and metadata.cover_url:
            resp = w.get(metadata.cover_url)
            if resp and resp.status == 200 and resp.mime.startswith("image/"):
                cover = (resp.body, resp.mime)

        return ParsedArchive(
            archive_title=w.title,
            chapters=order_chapters(chapters),
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
    rel = str(path.resolve().relative_to(settings.library_dir.resolve()))
    stat = path.stat()
    novel = session.exec(select(Novel).where(Novel.wacz_path == rel)).first()
    if (
        novel
        and not force
        and novel.wacz_size == stat.st_size
        and novel.wacz_mtime == stat.st_mtime
        and not novel.import_error
    ):
        return novel

    if novel is None:
        novel = Novel(wacz_path=rel, title=path.stem)
        session.add(novel)
        session.flush()

    novel.wacz_size, novel.wacz_mtime = stat.st_size, stat.st_mtime
    try:
        parsed = parse_wacz(path)
    except Exception as e:  # corrupt zip, unreadable WARC, ...
        log.exception("Failed to import %s", path)
        novel.import_error = f"{type(e).__name__}: {e}"
        session.commit()
        return novel

    novel.import_error = None if parsed.chapters else "No chapters found in this archive"
    novel.archive_title = parsed.archive_title
    if not novel.metadata_source:
        novel.title = parsed.archive_title or path.stem.replace("_", " ").replace("-", " ")

    # Keep reading progress and hidden flags across re-imports by matching chapter URLs.
    old = {c.url: c for c in novel.chapters}
    last_url = next((c.url for c in novel.chapters if c.id == novel.last_chapter_id), None)
    for c in list(novel.chapters):
        session.delete(c)
    session.flush()

    new_chapters = []
    for i, ec in enumerate(parsed.chapters):
        ch = Chapter(
            novel_id=novel.id,
            position=i,
            number=ec.number,
            season=ec.season,
            season_label=ec.season_label,
            title=ec.title,
            url=ec.url,
            captured_at=ec.ts,
            content_html=ec.html,
            char_count=ec.text_length,
            auto_title=ec.title,
            auto_number=ec.number,
            auto_season=ec.season,
            auto_season_label=ec.season_label,
        )
        prev = old.get(ec.url)
        if prev is not None:
            ch.hidden = prev.hidden
            if prev.custom_title:
                ch.title, ch.custom_title = prev.title, True
            if prev.custom_number:
                ch.number, ch.custom_number = prev.number, True
            if prev.custom_season:
                ch.season, ch.season_label, ch.custom_season = (
                    prev.season, prev.season_label, True)
        session.add(ch)
        new_chapters.append(ch)
    if any(c.custom_number or c.custom_season for c in new_chapters):
        resequence(new_chapters)
    session.flush()
    novel.last_chapter_id = next((c.id for c in new_chapters if c.url == last_url), None)
    if novel.last_chapter_id is None:
        novel.last_position = 0.0

    # Metadata from the archive wins over nothing, but never overrides user-supplied metadata.
    if parsed.metadata and novel.metadata_source in (None, "wacz"):
        apply_metadata(novel, parsed.metadata, "wacz", parsed.cover)

    session.commit()
    session.refresh(novel)
    log.info("Imported %s: %d chapters (%d pages skipped)", rel, len(new_chapters),
             len(parsed.skipped))
    return novel


def chapter_sort_key(ch: Chapter):
    """Reading order: season, then number; unnumbered chapters keep their relative place."""
    return (
        ch.season or 0,
        ch.number if ch.number is not None else float("-inf"),
        ch.position,
    )


def resequence(chapters: list[Chapter]) -> None:
    """Renumber positions after numbers/seasons were edited."""
    numbered = sum(c.number is not None for c in chapters)
    if numbered < max(1, len(chapters) // 2):
        return  # mostly unnumbered: capture order is all we have, keep it
    for i, ch in enumerate(sorted(chapters, key=chapter_sort_key)):
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
        files = sorted(p for p in settings.library_dir.rglob("*.wacz") if p.is_file())
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
            # Forget novels whose files have disappeared.
            present = {str(p.resolve().relative_to(settings.library_dir.resolve())) for p in files}
            for novel in session.exec(select(Novel)).all():
                if novel.wacz_path not in present:
                    log.info("Removing %s (file no longer in library)", novel.wacz_path)
                    if novel.cover_file:
                        (settings.covers_dir / novel.cover_file).unlink(missing_ok=True)
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
