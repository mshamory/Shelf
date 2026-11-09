"""Import WACZ files into the database and keep the library in sync with disk."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import threading
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session, select

from .config import settings
from .db import Chapter, Novel, get_engine, utcnow
from .extract import (
    MIN_CHAPTER_CHARS,
    ExtractedChapter,
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

log = logging.getLogger("shelf.library")

HTML_MIMES = {"text/html", "application/xhtml+xml", ""}

# Bump when extraction changes so existing archives are re-imported on the next scan.
EXTRACTOR_VERSION = "3"


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

        # Drop duplicate captures of the same chapter (e.g. "/ch-12/" and "/ch-12-2/").
        seen: dict[str, ExtractedChapter] = {}
        for c in chapters:
            key = hashlib.sha1(c.html.encode()).hexdigest()
            if key not in seen or c.ts > seen[key].ts:
                seen[key] = c
        chapters = list(seen.values())

        for c in chapters:
            classify(c, url_path_text(c.url))
        for c, t in zip(chapters, strip_common_affixes([c.title for c in chapters]), strict=True):
            c.title = t

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
