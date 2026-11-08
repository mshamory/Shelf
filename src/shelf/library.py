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
    chapter_number,
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
from .wacz import WaczFile

log = logging.getLogger("shelf.library")

HTML_MIMES = {"text/html", "application/xhtml+xml", ""}


@dataclass
class ParsedArchive:
    archive_title: str
    chapters: list[ExtractedChapter]
    metadata: NovelMetadata | None = None
    cover: tuple[bytes, str] | None = None
    skipped: list[str] = field(default_factory=list)


def parse_wacz(path: Path) -> ParsedArchive:
    with WaczFile(path) as w:
        pages = w.pages()
        page_urls = [p.url for p in pages]
        if not page_urls:  # no page list: consider every captured HTML document
            page_urls = [
                u for u in w.urls()
                if (c := w.best_capture(u)) and c.mime in ("text/html", "application/xhtml+xml")
            ]
        titles = {p.url: p.title for p in pages}
        timestamps = {p.url: p.ts for p in pages}

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
        for url in page_urls:
            if is_series_url(url) or url in nu_urls:
                continue
            entry = w.best_capture(url)
            resp = w.read(entry) if entry else None
            if resp is None or resp.mime not in HTML_MIMES:
                skipped.append(url)
                continue
            html = decode_html(resp.body, resp.headers.get("content-type", ""))
            if metadata is None and looks_like_series_page(html):
                try:
                    metadata = parse_series_page(html, url)
                    continue
                except MetadataError:
                    pass
            title = titles.get(url) or page_title(html) or url
            if is_error_page(title, resp.status):
                skipped.append(url)
                continue
            content = extract_content(html, url)
            n = text_length(content) if content else 0
            if not content or n < MIN_CHAPTER_CHARS:
                skipped.append(url)
                continue
            chapters.append(
                ExtractedChapter(
                    url=url,
                    title=title,
                    html=content,
                    text_length=n,
                    number=chapter_number(title, url_path_text(url)),
                    ts=timestamps.get(url) or (entry.timestamp if entry else ""),
                )
            )

        # Drop duplicate captures of the same chapter (e.g. "/ch-12/" and "/ch-12-2/").
        seen: dict[str, ExtractedChapter] = {}
        for c in chapters:
            key = hashlib.sha1(c.html.encode()).hexdigest()
            if key not in seen or c.ts > seen[key].ts:
                seen[key] = c
        chapters = list(seen.values())

        for c, t in zip(chapters, strip_common_affixes([c.title for c in chapters])):
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
            title=ec.title,
            url=ec.url,
            captured_at=ec.ts,
            content_html=ec.html,
            char_count=ec.text_length,
            hidden=old[ec.url].hidden if ec.url in old else False,
        )
        session.add(ch)
        new_chapters.append(ch)
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
