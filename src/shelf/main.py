from __future__ import annotations

import html as html_lib
import logging
import re
import shutil
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlmodel import Session, col, select
from starlette.concurrency import run_in_threadpool

from . import __version__, auth, opds
from .capture import (
    CaptureError,
    add_chapter,
    create_novel,
    identify_novel,
    match_score,
    novel_by_nu_url,
)
from .config import settings
from .db import (
    Chapter,
    ChapterVersion,
    GlossaryTerm,
    Novel,
    TrimRule,
    get_session,
    has_archive,
    init_db,
    restore_version,
    save_version,
    utcnow,
)
from .extract import SIDE_SEASON, sanitize, text_length
from .gaps import find_season_gaps, gaps_before, missing_count, reason_counts
from .glossary import InvalidTerm, compile_term, compile_terms, mark_terms, mentions, split_aliases
from .images import (
    STORED_NAME_RE,
    editable_images,
    fetch_status,
    images_dir,
    read_archived_image,
    resolve_images,
    restore_image_sources,
    save_image,
    start_image_fetch,
    wanted_images,
)
from .library import (
    ARCHIVE_SUFFIXES,
    apply_metadata,
    delete_captures,
    delete_versions,
    import_wacz,
    resequence,
    scan_status,
    start_scan,
)
from .novelupdates import MetadataError, fetch_series, is_series_url, parse_series_page
from .reading import (
    STATUSES,
    chapters_left,
    keep_new,
    new_chapters,
    new_count,
    note_read,
)
from .saved import MAX_PAGE_BYTES, add_saved_pages, is_saved_page
from .trim import MODES, InvalidRule, apply_rules, compile_rule, compile_rules
from .volumes import UPLOADS_DIR, volume_number

log = logging.getLogger("shelf")
HERE = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    settings.covers_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    if settings.scan_on_startup:
        start_scan()
    yield


app = FastAPI(title="Shelf", version=__version__, lifespan=lifespan)
# Sign-in, when a password or login proxy is set up. Inside CORS, so a 401 still tells the
# extension what went wrong.
app.add_middleware(auth.AuthMiddleware)
# The browser extension calls the API from its own origin.
app.add_middleware(CORSMiddleware, allow_origin_regex=r"^(moz|chrome|safari-web)-extension://.+$",
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Authorization"])
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(version=__version__, settings=settings, has_archive=has_archive)


def asset(path: str) -> str:
    """A static file's address, changing whenever the file does, so browsers never pair a
    cached old script or style with a newer page."""
    try:
        stamp = format((HERE / "static" / path).stat().st_mtime_ns, "x")
    except OSError:
        stamp = __version__
    return f"/static/{path}?v={stamp}"


templates.env.globals["asset"] = asset


def epub_file(novel: Novel, url: str) -> str:
    """The e-book a chapter is from: the novel's file, or the volume in its folder."""
    if novel.wacz_path.endswith("/"):
        name = url.removeprefix("epub:").split("/", 1)[0]
        return novel.wacz_path + name
    return novel.wacz_path


templates.env.globals["epub_file"] = epub_file

SessionDep = Annotated[Session, Depends(get_session)]


def flash_redirect(url: str, msg: str | None = None, error: bool = False) -> RedirectResponse:
    from urllib.parse import quote

    if msg:
        url, hash_, fragment = url.partition("#")
        url += ("&" if "?" in url else "?") + ("error=" if error else "msg=") + quote(msg)
        url += hash_ + fragment  # the query must come before any #fragment
    return RedirectResponse(url, status_code=303)


def get_novel(session: Session, novel_id: int) -> Novel:
    novel = session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(404, "Novel not found")
    return novel


def visible_chapters(session: Session, novel_id: int, include_hidden: bool = False):
    q = select(Chapter.id, Chapter.position, Chapter.number, Chapter.number_end,
               Chapter.auto_number_end, Chapter.season,
               Chapter.season_label, Chapter.title, Chapter.char_count, Chapter.hidden,
               Chapter.url, Chapter.auto_title, Chapter.auto_number, Chapter.custom_title,
               Chapter.custom_number, Chapter.custom_season,
               Chapter.origin, Chapter.duplicate).where(Chapter.novel_id == novel_id)
    if not include_hidden:
        q = q.where(Chapter.hidden == False)  # noqa: E712
    return session.exec(q.order_by(Chapter.position)).all()


def novel_gaps(session: Session, novel_id: int | None = None):
    """Missing-chapter gaps per novel (hidden chapters count as present)."""
    q = select(Chapter.novel_id, Chapter.season, Chapter.season_label, Chapter.number,
               Chapter.number_end)
    nq = select(Novel.id, Novel.unreadable_chapters)
    if novel_id is not None:
        q = q.where(Chapter.novel_id == novel_id)
        nq = nq.where(Novel.id == novel_id)
    rows: dict[int, list] = {}
    for nid, season, label, number, end in session.exec(q).all():
        rows.setdefault(nid, []).append((season, label, number, end))
    unreadable = {nid: {int(k): v for k, v in (u or {}).items()}
                  for nid, u in session.exec(nq).all()}
    return {nid: find_season_gaps(r, unreadable.get(nid)) for nid, r in rows.items()}


# -- library ---------------------------------------------------------------


def matches(novel: Novel, q: str) -> bool:
    """The library's text filter: title, other names, authors, genres or tags."""
    return q.lower() in " ".join([novel.title, novel.archive_title, *novel.alt_names,
                                  *novel.authors, *novel.genres, *novel.tags]).lower()


def shown_counts(session: Session) -> dict[int, int]:
    """Shown chapters per novel."""
    return dict(
        session.exec(
            select(Chapter.novel_id, func.count(Chapter.id))
            .where(Chapter.hidden == False)  # noqa: E712
            .group_by(Chapter.novel_id)
        ).all()
    )


@app.get("/", response_class=HTMLResponse)
def library(request: Request, session: SessionDep, q: str = "", sort: str = "recent",
            status: str = ""):
    counts = shown_counts(session)
    missing = {nid: missing_count(g) for nid, g in novel_gaps(session).items()}
    novels = list(session.exec(select(Novel)).all())
    if q:
        novels = [n for n in novels if matches(n, q)]
    left = chapters_left(session)
    new = {n.id: k for n in novels if (k := new_count(n, left.get(n.id, 0)))}
    # How many novels each filter shows (within the text filter), for the filter links.
    status_counts = {key: sum(1 for n in novels if n.reading_status == key) for key in STATUSES}
    status_counts["new"] = len(new)
    if status == "new":
        novels = [n for n in novels if n.id in new]
    elif status in STATUSES:
        novels = [n for n in novels if n.reading_status == status]
    else:
        status = ""
    if sort == "title":
        novels.sort(key=lambda n: n.title.lower())
    elif sort == "added":
        novels.sort(key=lambda n: n.added_at, reverse=True)
    elif sort == "updated":
        novels.sort(key=lambda n: n.updated_at or n.added_at, reverse=True)
    else:
        novels.sort(key=lambda n: (n.last_read_at or n.added_at), reverse=True)

    progress = {}
    for n in novels:
        if n.last_chapter_id:
            ch = session.get(Chapter, n.last_chapter_id)
            if ch:
                progress[n.id] = ch
    return templates.TemplateResponse(
        request,
        "library.html",
        {"novels": novels, "counts": counts, "progress": progress, "q": q, "sort": sort,
         "scan": scan_status, "missing": missing, "status": status,
         "statuses": STATUSES, "status_counts": status_counts, "new": new, "left": left,
         "all_novels": sorted(session.exec(select(Novel)).all(), key=lambda n: n.title.lower())},
    )


@app.post("/scan")
def scan(force: Annotated[bool, Form()] = False):
    started = start_scan(force=force)
    return flash_redirect("/", "Scanning library…" if started else "A scan is already running")


@app.get("/api/scan")
def scan_state():
    return asdict(scan_status)


SAFE_NAME_RE = re.compile(r"[^\w.\- ()\[\]]+")


@app.post("/upload")
async def upload(session: SessionDep, files: list[UploadFile],
                 novel: Annotated[str, Form()] = "new", title: Annotated[str, Form()] = "",
                 volumes: Annotated[bool, Form()] = False):
    """WACZ archives and e-books (each one a novel) and saved pages (chapters for `novel`,
    or for a new novel), in one upload. E-books become volumes of `novel` when it is a
    folder of volumes, or, with `volumes`, of one new novel."""
    archives = [f for f in files if (f.filename or "").lower().endswith(ARCHIVE_SUFFIXES)]
    pages = [f for f in files if is_saved_page(f.filename or "")]
    others = [f.filename for f in files if f not in archives and f not in pages]
    if not archives and not pages:
        return flash_redirect(
            "/", "Upload .wacz archives, .epub e-books or saved web pages (.html, .mhtml)",
            error=True)
    if archives and not settings.allow_uploads:
        return flash_redirect("/", "Uploading archives is disabled", error=True)
    target = None
    if novel != "new":
        try:
            target = get_novel(session, int(novel))
        except (ValueError, HTTPException):
            return flash_redirect("/", "Unknown novel", error=True)

    messages, errors, touched = [], [], []
    books = [f for f in archives if (f.filename or "").lower().endswith(".epub")]
    folder = None
    if books and target is not None and has_archive(target) and target.wacz_path.endswith("/"):
        folder = settings.library_dir / target.wacz_path
    elif books and target is None and volumes:
        folder = settings.library_dir / UPLOADS_DIR / _volumes_folder_name(books, title)
    if folder is not None:
        archives = [f for f in archives if f not in books]
        for f in books:
            await _save_upload(folder, f)
        imported = await run_in_threadpool(import_wacz, session, folder)
        touched.append(imported.id)
        if imported.import_error:
            errors.append(imported.import_error)
        else:
            n = len(books)
            messages.append(f"Added {n} volume{'s' if n != 1 else ''} to “{imported.title}”; "
                            f"{len(imported.chapters)} chapters in all")
            if imported.import_warning:
                errors.append(imported.import_warning)
    for f in archives:
        imported = await _import_uploaded_archive(session, f)
        touched.append(imported.id)
        if imported.import_error:
            errors.append(f"{f.filename}: {imported.import_error}")
        else:
            messages.append(f"Imported {len(imported.chapters)} chapters from {f.filename}")
    if pages:
        report = await _add_uploaded_pages(session, target, pages, title)
        if report.novel is not None:
            touched.append(report.novel.id)
        (messages if report.novel is not None else errors).append(report.summary())
    if others:
        errors.append("Not an archive, e-book or saved page: " + ", ".join(others))
    dest = f"/novel/{touched[0]}" if len(set(touched)) == 1 else "/"
    msg = ". ".join(messages + errors)
    return flash_redirect(dest, msg, error=not messages)


def _volumes_folder_name(books: list[UploadFile], title: str) -> str:
    """The folder new volumes go in: the title given, else the series their file names
    share ("Ishura - Volume 10.epub" -> "Ishura")."""
    names = [n for f in books if (n := volume_number("", f.filename or "")[1])]
    name = title.strip() or min(names, key=len, default="") or Path(
        books[0].filename or "Volumes").stem
    return SAFE_NAME_RE.sub("_", name).strip(" .") or "Volumes"


async def _save_upload(dest_dir: Path, file: UploadFile) -> Path:
    name = SAFE_NAME_RE.sub("_", Path(file.filename or "upload.wacz").name).strip(" .")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    i = 1
    while dest.exists():
        dest = dest_dir / f"{Path(name).stem} ({i}){Path(name).suffix.lower()}"
        i += 1
    with dest.open("wb") as out:
        await run_in_threadpool(shutil.copyfileobj, file.file, out, 1024 * 1024)
    return dest


async def _import_uploaded_archive(session: Session, file: UploadFile) -> Novel:
    dest = await _save_upload(settings.library_dir / UPLOADS_DIR, file)
    return await run_in_threadpool(import_wacz, session, dest)


async def _add_uploaded_pages(session: Session, novel: Novel | None, files: list[UploadFile],
                              title: str = ""):
    data = [(Path(f.filename or "page.html").name, await f.read(MAX_PAGE_BYTES + 1))
            for f in files]
    return await run_in_threadpool(add_saved_pages, session, novel, data, title)


@app.post("/novel/{novel_id}/pages")
async def upload_pages(session: SessionDep, novel_id: int, files: list[UploadFile]):
    """Saved web pages as chapters of this novel."""
    novel = get_novel(session, novel_id)
    pages = [f for f in files if is_saved_page(f.filename or "")]
    if not pages:
        return flash_redirect(f"/novel/{novel_id}",
                              "Choose saved web pages (.html, .htm or .mhtml)", error=True)
    report = await _add_uploaded_pages(session, novel, pages)
    return flash_redirect(f"/novel/{novel_id}", report.summary(),
                          error=not report.actions and not report.metadata)


# -- novel -------------------------------------------------------------------


@app.get("/novel/{novel_id}", response_class=HTMLResponse)
def novel_page(request: Request, session: SessionDep, novel_id: int, hidden: bool = False):
    novel = get_novel(session, novel_id)
    chapters = visible_chapters(session, novel_id, include_hidden=hidden)
    hidden_count = session.exec(
        select(func.count(Chapter.id)).where(Chapter.novel_id == novel_id, Chapter.hidden == True)  # noqa: E712
    ).one()
    last = session.get(Chapter, novel.last_chapter_id) if novel.last_chapter_id else None
    left = chapters_left(session, [novel_id]).get(novel_id, 0)
    gaps = novel_gaps(session, novel_id).get(novel_id, [])
    main = [c for c in chapters if c.number is not None and c.season != SIDE_SEASON]
    first_season = min((c.season or 0 for c in main), default=0)
    numbered = [c.number for c in main if (c.season or 0) == first_season]
    return templates.TemplateResponse(
        request,
        "novel.html",
        {"novel": novel, "chapters": chapters, "last": last, "show_hidden": hidden,
         "left": left, "new": new_count(novel, left), "statuses": STATUSES,
         "hidden_count": hidden_count, "gaps": gaps,
         "total_chapters": (len(chapters) if hidden else len(chapters) + hidden_count),
         "archive_exists": has_archive(novel)
         and (settings.library_dir / novel.wacz_path).is_file(),
         "volume_folder": has_archive(novel) and novel.wacz_path.endswith("/"), "missing_total": missing_count(gaps),
         "missing_reasons": reason_counts(gaps),
         "gaps_before": gaps_before(chapters, gaps),
         "first_number": int(min(numbered)) if numbered else None,
         "has_images": session.exec(select(Chapter.id).where(
             Chapter.novel_id == novel_id, ~col(Chapter.url).startswith("epub:"),
             func.instr(Chapter.content_html, "<img") > 0).limit(1)).first() is not None,
         "image_fetch": fetch_status(novel_id),
         "seasons": novel_seasons(session, novel_id) if hidden else [],
         "versions": version_counts(session, novel_id) if hidden else {}},
    )


def version_counts(session: Session, novel_id: int) -> dict[int, int]:
    """Earlier versions per chapter id."""
    return dict(session.exec(
        select(ChapterVersion.chapter_id, func.count(ChapterVersion.id))
        .where(ChapterVersion.novel_id == novel_id,
               ChapterVersion.chapter_id != None)  # noqa: E711
        .group_by(ChapterVersion.chapter_id)).all())


@app.post("/novel/{novel_id}/metadata/fetch")
def metadata_fetch(session: SessionDep, novel_id: int, url: Annotated[str, Form()]):
    novel = get_novel(session, novel_id)
    try:
        meta = fetch_series(url)
    except MetadataError as e:
        return flash_redirect(f"/novel/{novel_id}", str(e), error=True)
    apply_metadata(novel, meta, "fetch")
    session.commit()
    return flash_redirect(f"/novel/{novel_id}", "Metadata updated from NovelUpdates")


@app.post("/novel/{novel_id}/metadata/upload")
async def metadata_upload(session: SessionDep, novel_id: int, file: UploadFile,
                          url: Annotated[str, Form()] = ""):
    novel = get_novel(session, novel_id)
    raw = await file.read(10 * 1024 * 1024)
    try:
        meta = parse_series_page(raw.decode("utf-8", errors="replace"), url.strip())
    except MetadataError as e:
        return flash_redirect(f"/novel/{novel_id}", str(e), error=True)
    await run_in_threadpool(apply_metadata, novel, meta, "upload")
    session.commit()
    return flash_redirect(f"/novel/{novel_id}", "Metadata imported from saved page")


@app.post("/novel/{novel_id}/metadata/edit")
def metadata_edit(
    session: SessionDep,
    novel_id: int,
    title: Annotated[str, Form()],
    authors: Annotated[str, Form()] = "",
    genres: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "",
):
    novel = get_novel(session, novel_id)

    def split(s: str) -> list[str]:
        return [x.strip() for x in s.split(",") if x.strip()]

    novel.title = title.strip() or novel.title
    novel.authors, novel.genres, novel.tags = split(authors), split(genres), split(tags)
    novel.status = status.strip() or None
    novel.metadata_source = novel.metadata_source or "manual"
    session.commit()
    return flash_redirect(f"/novel/{novel_id}", "Saved")


@app.post("/novel/{novel_id}/reimport")
async def reimport(session: SessionDep, novel_id: int):
    novel = get_novel(session, novel_id)
    if not has_archive(novel):
        return flash_redirect(f"/novel/{novel_id}", "This novel has no archive to re-import",
                              error=True)
    path = settings.library_dir / novel.wacz_path
    if not path.exists():
        return flash_redirect(f"/novel/{novel_id}", "Archive file is missing", error=True)
    novel = await run_in_threadpool(import_wacz, session, path, True)
    if novel.import_error:
        return flash_redirect(f"/novel/{novel_id}", novel.import_error, error=True)
    return flash_redirect(f"/novel/{novel_id}", f"Re-imported {len(novel.chapters)} chapters")


@app.post("/novel/{novel_id}/delete")
def delete_novel(session: SessionDep, novel_id: int,
                 delete_file: Annotated[bool, Form()] = False):
    """Delete a novel that has no chapters (e.g. created from NovelUpdates but never filled,
    or an archive nothing could be imported from)."""
    novel = get_novel(session, novel_id)
    count = session.exec(select(func.count(Chapter.id)).where(Chapter.novel_id == novel_id)).one()
    if count:
        return flash_redirect(f"/novel/{novel_id}",
                              "Only novels without chapters can be deleted", error=True)
    title = novel.title
    if has_archive(novel):
        path = settings.library_dir / novel.wacz_path  # a symlink: only the link goes
        if delete_file and path.is_file() and ".." not in Path(novel.wacz_path).parts:
            try:
                path.unlink()
            except OSError as e:
                return flash_redirect(f"/novel/{novel_id}",
                                      f"Couldn't delete the archive file: {e.strerror}", error=True)
    if novel.cover_file:
        (settings.covers_dir / novel.cover_file).unlink(missing_ok=True)
    delete_captures(novel.id)
    delete_versions(session, novel.id)
    for rule in session.exec(select(TrimRule).where(TrimRule.novel_id == novel_id)).all():
        session.delete(rule)
    for term in glossary_terms(session, novel_id):
        session.delete(term)
    session.delete(novel)
    session.commit()
    return flash_redirect("/", f"Deleted “{title}”")


@app.post("/novel/{novel_id}/progress/reset")
def reset_progress(session: SessionDep, novel_id: int):
    novel = get_novel(session, novel_id)
    novel.last_chapter_id, novel.last_position, novel.last_read_at = None, 0.0, None
    novel.left_when_read = None
    session.commit()
    return flash_redirect(f"/novel/{novel_id}", "Progress reset")


@app.post("/novel/{novel_id}/status")
def set_reading_status(session: SessionDep, novel_id: int,
                       status: Annotated[str, Form()] = ""):
    novel = get_novel(session, novel_id)
    if status and status not in STATUSES:
        raise HTTPException(400, "Unknown reading status")
    novel.reading_status = status or None
    session.commit()
    return flash_redirect(f"/novel/{novel_id}",
                          f"Marked as {STATUSES[status]}" if status else "Removed from your lists")


@app.post("/chapter/{chapter_id}/toggle-hidden")
def toggle_hidden(session: SessionDep, chapter_id: int):
    ch = session.get(Chapter, chapter_id)
    if ch is None:
        raise HTTPException(404)
    novel = get_novel(session, ch.novel_id)
    before = new_chapters(session, novel)
    ch.hidden = not ch.hidden
    keep_new(session, novel, before)
    session.commit()
    return flash_redirect(f"/novel/{ch.novel_id}?hidden=1#ch-{ch.id}")


CHAPTER_NUMBER_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)(?:\s*-\s*(\d{1,2})|\s*(?:–|—|~|\.\.|to)\s*(\d+))?\s*$", re.I)


def parse_chapter_number(text: str) -> tuple[float, float | None]:
    """(number, last number covered) from '12', '12.5', '207-2' (part 2 of chapter 207, as
    the importer numbers it) or '96 to 97' / '96–97' (a post covering chapters 96 and 97)."""
    m = CHAPTER_NUMBER_RE.match(text)
    if not m:
        raise ValueError(text)
    number = float(m.group(1))
    if m.group(2):
        return number + int(m.group(2)) / 1000, None
    if m.group(3):
        end = float(m.group(3))
        if number != int(number) or end <= number:
            raise ValueError(text)
        return number, end
    return number, None


def format_chapter_number(number: float | None, end: float | None = None) -> str:
    """Inverse of parse_chapter_number: 207.002 -> '207-2', 12.5 -> '12.5', (96, 97) ->
    '96–97'."""
    if number is None:
        return ""
    if end is not None and end > number:
        return f"{number:g}–{end:g}"
    part = round((number - int(number)) * 1000)
    if 0 < part < 100 and abs(number - int(number) - part / 1000) < 1e-9:
        return f"{int(number)}-{part}"
    return f"{number:g}"


templates.env.filters["chapter_number"] = format_chapter_number


def format_captured(ts: str) -> str:
    """'20261111044932' or '2026-11-11T04:49:32Z' -> '2026-11-11'."""
    if len(ts) >= 8 and ts[:8].isdigit():
        return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
    return ts[:10]


templates.env.filters["captured"] = format_captured


def novel_seasons(session: Session, novel_id: int) -> list[tuple[int, str]]:
    rows = session.exec(
        select(Chapter.season, Chapter.season_label)
        .where(Chapter.novel_id == novel_id, Chapter.season != None)  # noqa: E711
        .distinct()
    ).all()
    return sorted({(s, label) for s, label in rows}, key=lambda r: r[0])


@app.post("/chapter/{chapter_id}/edit")
def edit_chapter(
    session: SessionDep,
    chapter_id: int,
    title: Annotated[str, Form()] = "",
    number: Annotated[str, Form()] = "",
    season: Annotated[str, Form()] = "auto",
    reset: Annotated[str, Form()] = "",
):
    ch = session.get(Chapter, chapter_id)
    if ch is None:
        raise HTTPException(404)
    back = f"/novel/{ch.novel_id}?hidden=1#ch-{ch.id}"
    novel = get_novel(session, ch.novel_id)
    new_before = new_chapters(session, novel)
    auto_title = ch.auto_title or ch.title
    if reset:
        title, number, season = "", "", "auto"

    title = title.strip()
    if title and title != auto_title:
        ch.title, ch.custom_title = title, True
    else:
        ch.title, ch.custom_title = auto_title, False

    number = number.strip()
    if number:
        try:
            value, end = parse_chapter_number(number)
        except ValueError:
            return flash_redirect(
                back, f"“{number}” isn't a chapter number (e.g. 12, 12.5, 207-2 or 96 to 97)",
                error=True)
        ch.number, ch.number_end = value, end
        ch.custom_number = (value, end) != (ch.auto_number, ch.auto_number_end)
    else:
        ch.number, ch.number_end, ch.custom_number = ch.auto_number, ch.auto_number_end, False

    if season == "auto":
        ch.season, ch.season_label, ch.custom_season = (
            ch.auto_season, ch.auto_season_label, False)
    elif season == "none":  # out of every section: the main story's order
        ch.season, ch.season_label = None, ""
        ch.custom_season = ch.auto_season is not None
    else:
        labels = dict(novel_seasons(session, ch.novel_id))
        try:
            wanted = int(season)
        except ValueError:
            wanted = -1
        if wanted not in labels:
            return flash_redirect(back, "Unknown season", error=True)
        ch.season, ch.season_label = wanted, labels[wanted]
        ch.custom_season = wanted != ch.auto_season

    session.add(ch)
    session.flush()
    chapters = list(session.exec(select(Chapter).where(Chapter.novel_id == ch.novel_id)).all())
    resequence(chapters)
    keep_new(session, novel, new_before)
    session.commit()
    return flash_redirect(back, "Chapter reset" if reset else "Chapter saved")


# -- reader ------------------------------------------------------------------


@app.get("/novel/{novel_id}/read", response_class=HTMLResponse)
def continue_reading(session: SessionDep, novel_id: int):
    novel = get_novel(session, novel_id)
    chapters = visible_chapters(session, novel_id)
    if not chapters:
        return flash_redirect(f"/novel/{novel_id}", "No chapters to read", error=True)
    target = next((c for c in chapters if c.id == novel.last_chapter_id), chapters[0])
    return RedirectResponse(f"/chapter/{target.id}", status_code=303)


@app.get("/chapter/{chapter_id}", response_class=HTMLResponse)
def reader(request: Request, session: SessionDep, chapter_id: int, original: bool = False):
    ch = session.get(Chapter, chapter_id)
    if ch is None:
        raise HTTPException(404, "Chapter not found")
    novel = get_novel(session, ch.novel_id)
    toc = visible_chapters(session, novel.id)
    ids = [c.id for c in toc]
    idx = ids.index(ch.id) if ch.id in ids else None
    prev_id = ids[idx - 1] if idx else None
    next_id = ids[idx + 1] if idx is not None and idx + 1 < len(ids) else None
    resume = novel.last_position if novel.last_chapter_id == ch.id else 0.0
    rules = compile_rules(trim_rules(session, novel.id))
    content, trimmed = ch.content_html, 0
    if rules and not original:
        content, trimmed = apply_rules(ch.content_html, rules)
    content = resolve_images(session, novel, content)
    terms = glossary_terms(session, novel.id)
    content, found = mark_terms(content, compile_terms(terms))
    by_id = {t.id: t for t in terms}
    glossary = {t: {"term": by_id[t].term, "category": by_id[t].category,
                    "description": by_id[t].description} for t in found}
    # Warn when the next chapter skips numbers (e.g. 128 -> 130), and list chapters known
    # to exist after the last one.
    gaps_ahead = []
    if ch.number is not None and idx is not None:
        nxt = toc[idx + 1] if next_id is not None else None
        if nxt is None or (nxt.number is not None and nxt.season == ch.season):
            limit = nxt.number if nxt is not None else float("inf")
            gaps_ahead = [g for g in novel_gaps(session, novel.id).get(novel.id, [])
                          if g.season == ch.season and ch.number < g.start and g.end < limit]
    return templates.TemplateResponse(
        request,
        "reader.html",
        {"novel": novel, "chapter": ch, "toc": toc, "index": idx, "prev_id": prev_id,
         "next_id": next_id, "resume": resume, "gaps_ahead": gaps_ahead,
         "content": content, "trimmed": trimmed, "original": original,
         "glossary": glossary, "has_glossary": bool(terms),
         "has_rules": bool(rules),
         "versions": version_counts(session, novel.id).get(ch.id, 0)},
    )


def _chapter_versions(session: Session, ch: Chapter) -> list[ChapterVersion]:
    return list(session.exec(
        select(ChapterVersion).where(ChapterVersion.chapter_id == ch.id,
                                     ChapterVersion.novel_id == ch.novel_id)
        .order_by(col(ChapterVersion.saved_at).desc(), col(ChapterVersion.id).desc())).all())


@app.get("/chapter/{chapter_id}/history", response_class=HTMLResponse)
def chapter_history(request: Request, session: SessionDep, chapter_id: int,
                    show: int | None = None):
    """The chapter's earlier versions (before captures or restores replaced it)."""
    ch = session.get(Chapter, chapter_id)
    if ch is None:
        raise HTTPException(404, "Chapter not found")
    versions = _chapter_versions(session, ch)
    shown = next((v for v in versions if v.id == show), None)
    novel = get_novel(session, ch.novel_id)
    return templates.TemplateResponse(
        request, "history.html",
        {"novel": novel, "chapter": ch, "versions": versions, "shown": shown,
         "shown_html": resolve_images(session, novel, shown.content_html) if shown else ""})


@app.get("/chapter/{chapter_id}/text", response_class=HTMLResponse)
def chapter_text(request: Request, session: SessionDep, chapter_id: int):
    """The chapter editor: its text as imported or last edited, before trim rules."""
    ch = session.get(Chapter, chapter_id)
    if ch is None:
        raise HTTPException(404, "Chapter not found")
    novel = get_novel(session, ch.novel_id)
    rules = compile_rules(trim_rules(session, novel.id))
    trimmed = apply_rules(ch.content_html, rules)[1] if rules else 0
    return templates.TemplateResponse(
        request, "chapter_text.html",
        {"novel": novel, "chapter": ch, "trimmed": trimmed,
         "content": editable_images(session, novel, ch.content_html),
         "versions": version_counts(session, novel.id).get(ch.id, 0)})


@app.post("/chapter/{chapter_id}/text")
def save_chapter_text(session: SessionDep, chapter_id: int,
                      html: Annotated[str, Form()] = ""):
    """Save edited text. The text before goes into the chapter's history."""
    ch = session.get(Chapter, chapter_id)
    if ch is None:
        raise HTTPException(404, "Chapter not found")
    new = sanitize(restore_image_sources(html)).strip()
    if not text_length(new) and "<img" not in new:
        return flash_redirect(f"/chapter/{ch.id}/text",
                              "The chapter would be empty. To drop it, hide it in the "
                              "chapter list instead.", error=True)
    if new == sanitize(ch.content_html).strip():
        return flash_redirect(f"/chapter/{ch.id}", "No changes")
    save_version(session, ch, "Replaced by your edit")
    ch.content_html, ch.char_count, ch.custom_content = new, text_length(new), True
    session.commit()
    return flash_redirect(f"/chapter/{ch.id}", "Chapter saved")


@app.post("/chapter/{chapter_id}/history/{version_id}/restore")
def restore_chapter_version(session: SessionDep, chapter_id: int, version_id: int):
    """Put an earlier version back. The current one becomes a version, so this can be
    undone the same way."""
    ch = session.get(Chapter, chapter_id)
    v = session.get(ChapterVersion, version_id)
    if ch is None or v is None or v.chapter_id != ch.id:
        raise HTTPException(404, "Version not found")
    when = v.saved_at.strftime("%Y-%m-%d %H:%M")
    novel = get_novel(session, ch.novel_id)
    new_before = new_chapters(session, novel)
    save_version(session, ch, f"Replaced by restoring the version from {when}")
    restore_version(ch, v)
    session.delete(v)
    session.flush()
    resequence(list(session.exec(select(Chapter).where(Chapter.novel_id == ch.novel_id)).all()))
    keep_new(session, novel, new_before)
    session.commit()
    return flash_redirect(f"/chapter/{ch.id}", f"Restored the version from {when}")


class Progress(BaseModel):
    chapter_id: int
    position: float = 0.0


@app.post("/api/progress")
def save_progress(session: SessionDep, p: Progress):
    ch = session.get(Chapter, p.chapter_id)
    if ch is None:
        raise HTTPException(404)
    novel = get_novel(session, ch.novel_id)
    novel.last_chapter_id = ch.id
    novel.last_position = min(max(p.position, 0.0), 1.0)
    novel.last_read_at = utcnow()
    note_read(session, novel)
    session.commit()
    return {"ok": True}


# -- trimming ----------------------------------------------------------------


def trim_rules(session: Session, novel_id: int) -> list[TrimRule]:
    """Rules for one novel plus rules that apply to every novel."""
    return list(session.exec(
        select(TrimRule)
        .where(or_(TrimRule.novel_id == novel_id, TrimRule.novel_id == None))  # noqa: E711
        .order_by(TrimRule.created_at)
    ).all())


# -- glossary ----------------------------------------------------------------


def glossary_terms(session: Session, novel_id: int) -> list[GlossaryTerm]:
    return list(session.exec(select(GlossaryTerm).where(GlossaryTerm.novel_id == novel_id)
                             .order_by(func.lower(GlossaryTerm.term))).all())


@app.get("/novel/{novel_id}/glossary", response_class=HTMLResponse)
def glossary_page(request: Request, session: SessionDep, novel_id: int, term: str = ""):
    novel = get_novel(session, novel_id)
    terms = glossary_terms(session, novel_id)
    categories = sorted({t.category for t in terms if t.category}, key=str.casefold)
    groups = [(c, [t for t in terms if t.category == c]) for c in categories]
    if any(not t.category for t in terms):
        groups.append(("", [t for t in terms if not t.category]))
    return templates.TemplateResponse(
        request, "glossary.html",
        {"novel": novel, "groups": groups, "count": len(terms), "categories": categories,
         "new_term": term.strip()[:200]})


def _save_term(session: Session, t: GlossaryTerm, term: str, category: str, aliases: str,
               description: str, ignore_case: bool) -> str:
    """Check and store a term; returns how often it's found, for the message."""
    alias_list = split_aliases(aliases)
    rx = compile_term(term, alias_list, ignore_case)  # raises InvalidTerm
    t.term, t.category = re.sub(r"\s+", " ", term).strip(), category.strip()
    t.aliases, t.description, t.ignore_case = alias_list, description.strip(), ignore_case
    session.add(t)
    session.commit()
    found = mentions(rx, session.exec(select(Chapter.content_html).where(
        Chapter.novel_id == t.novel_id, Chapter.hidden == False)))  # noqa: E712
    return (f"“{t.term}” saved; it appears in {found} chapter{'s' if found != 1 else ''}"
            if found else f"“{t.term}” saved; no chapter mentions it yet")


@app.post("/novel/{novel_id}/glossary")
def glossary_add(session: SessionDep, novel_id: int, term: Annotated[str, Form()],
                 category: Annotated[str, Form()] = "", aliases: Annotated[str, Form()] = "",
                 description: Annotated[str, Form()] = "",
                 ignore_case: Annotated[bool, Form()] = False,
                 next: Annotated[str, Form()] = ""):
    get_novel(session, novel_id)
    back = safe_next(next, f"/novel/{novel_id}/glossary")
    existing = session.exec(select(GlossaryTerm).where(
        GlossaryTerm.novel_id == novel_id,
        func.lower(GlossaryTerm.term) == term.strip().lower())).first()
    if existing is not None:
        return flash_redirect(f"/novel/{novel_id}/glossary#term-{existing.id}",
                              f"“{existing.term}” is already in the glossary", error=True)
    try:
        msg = _save_term(session, GlossaryTerm(novel_id=novel_id, term=term), term, category,
                         aliases, description, ignore_case)
    except InvalidTerm as e:
        return flash_redirect(back, str(e), error=True)
    return flash_redirect(back, msg)


@app.post("/glossary/{term_id}/edit")
def glossary_edit(session: SessionDep, term_id: int, term: Annotated[str, Form()],
                  category: Annotated[str, Form()] = "", aliases: Annotated[str, Form()] = "",
                  description: Annotated[str, Form()] = "",
                  ignore_case: Annotated[bool, Form()] = False):
    t = session.get(GlossaryTerm, term_id)
    if t is None:
        raise HTTPException(404)
    back = f"/novel/{t.novel_id}/glossary#term-{t.id}"
    try:
        msg = _save_term(session, t, term, category, aliases, description, ignore_case)
    except InvalidTerm as e:
        session.rollback()
        return flash_redirect(back, str(e), error=True)
    return flash_redirect(back, msg)


@app.post("/glossary/{term_id}/delete")
def glossary_delete(session: SessionDep, term_id: int):
    t = session.get(GlossaryTerm, term_id)
    if t is None:
        raise HTTPException(404)
    novel_id, name = t.novel_id, t.term
    session.delete(t)
    session.commit()
    return flash_redirect(f"/novel/{novel_id}/glossary", f"“{name}” removed from the glossary")


def safe_next(url: str, fallback: str) -> str:
    # Only allow local redirects.
    return url if url.startswith("/") and not url.startswith("//") else fallback


@app.get("/novel/{novel_id}/trim", response_class=HTMLResponse)
def trim_page(request: Request, session: SessionDep, novel_id: int, pattern: str = ""):
    novel = get_novel(session, novel_id)
    rules = trim_rules(session, novel_id)
    texts = [
        re.sub(r"\s+", " ", html_lib.unescape(TAG_RE.sub(" ", html)))
        for html in session.exec(
            select(Chapter.content_html).where(Chapter.novel_id == novel_id)
        ).all()
    ]
    affected = {}
    for rule in rules:
        try:
            rx = compile_rule(rule.pattern, rule.is_regex, rule.ignore_case)
        except InvalidRule:
            affected[rule.id] = None
            continue
        affected[rule.id] = sum(1 for t in texts if rx.search(t))
    return templates.TemplateResponse(
        request,
        "trim.html",
        {"novel": novel, "rules": rules, "affected": affected, "modes": MODES,
         "chapter_total": len(texts), "pattern": pattern},
    )


@app.post("/trim")
def trim_add(
    session: SessionDep,
    pattern: Annotated[str, Form()],
    novel_id: Annotated[int, Form()],
    scope: Annotated[str, Form()] = "novel",
    mode: Annotated[str, Form()] = "text",
    is_regex: Annotated[bool, Form()] = False,
    ignore_case: Annotated[bool, Form()] = False,
    next: Annotated[str, Form()] = "",
):
    get_novel(session, novel_id)
    back = safe_next(next, f"/novel/{novel_id}/trim")
    if mode not in MODES:
        return flash_redirect(back, "Unknown trim mode", error=True)
    try:
        compile_rule(pattern, is_regex, ignore_case)
    except InvalidRule as e:
        return flash_redirect(back, str(e), error=True)
    session.add(TrimRule(
        novel_id=None if scope == "all" else novel_id,
        pattern=pattern if is_regex else pattern.strip(),
        is_regex=is_regex, ignore_case=ignore_case, mode=mode,
    ))
    session.commit()
    return flash_redirect(back, "Trim rule added")


def _get_rule(session: Session, rule_id: int) -> TrimRule:
    rule = session.get(TrimRule, rule_id)
    if rule is None:
        raise HTTPException(404, "Rule not found")
    return rule


@app.post("/trim/{rule_id}/toggle")
def trim_toggle(session: SessionDep, rule_id: int, next: Annotated[str, Form()] = "/"):
    rule = _get_rule(session, rule_id)
    rule.enabled = not rule.enabled
    session.commit()
    return flash_redirect(safe_next(next, "/"))


@app.post("/trim/{rule_id}/delete")
def trim_delete(session: SessionDep, rule_id: int, next: Annotated[str, Form()] = "/"):
    session.delete(_get_rule(session, rule_id))
    session.commit()
    return flash_redirect(safe_next(next, "/"), "Trim rule deleted")


# -- search ------------------------------------------------------------------


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, session: SessionDep, q: str = "", novel: int | None = None):
    results = []
    q = q.strip()
    if len(q) >= 2:
        stmt = (
            select(Chapter, Novel.title)
            .join(Novel, Novel.id == Chapter.novel_id)
            .where(Chapter.hidden == False)  # noqa: E712
            .where(or_(col(Chapter.content_html).contains(q), col(Chapter.title).contains(q)))
            .order_by(Novel.title, Chapter.position)
            .limit(100)
        )
        if novel:
            stmt = stmt.where(Chapter.novel_id == novel)
        for ch, ntitle in session.exec(stmt).all():
            results.append({"chapter": ch, "novel_title": ntitle, "snippet": snippet(ch, q)})
    return templates.TemplateResponse(
        request, "search.html", {"q": q, "results": results, "novel_id": novel}
    )


TAG_RE = re.compile(r"<[^>]+>")


def snippet(ch: Chapter, q: str, width: int = 90) -> str:
    text = re.sub(r"\s+", " ", TAG_RE.sub(" ", ch.content_html))
    i = text.lower().find(q.lower())
    if i < 0:
        return text[: width * 2]
    start = max(0, i - width)
    return ("…" if start else "") + text[start : i + len(q) + width] + "…"


# -- signing in ------------------------------------------------------------


def signed_in_as(request: Request) -> tuple[str, str | None] | None:
    """How the request signed in, ("cookie" | "basic" | "header", name), or None."""
    return request.scope.get("state", {}).get("auth")


templates.env.globals["signed_in_as"] = signed_in_as


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if not auth.password():
        return RedirectResponse(safe_next(next, "/"), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": safe_next(next, "/")})


@app.post("/login")
def login(request: Request, password: Annotated[str, Form()] = "",
          next: Annotated[str, Form()] = "/"):
    target = safe_next(next, "/")
    if not auth.password():
        return RedirectResponse(target, status_code=303)
    addr = request.client.host if request.client else "?"
    if auth.too_many_failures(addr):
        return templates.TemplateResponse(
            request, "login.html", {"next": target, "error":
                                    "Too many wrong passwords. Try again in a few minutes."},
            status_code=429)
    if not auth.check_password(password):
        auth.note_failure(addr)
        return templates.TemplateResponse(
            request, "login.html", {"next": target, "error": "That's not the password."},
            status_code=401)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(auth.COOKIE, auth.login_cookie(), max_age=auth.COOKIE_AGE,
                        path=request.scope.get("root_path") or "/", httponly=True,
                        samesite="lax", secure=request.url.scheme == "https")
    return response


@app.post("/logout")
def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE, path=request.scope.get("root_path") or "/")
    return response


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "version": __version__})


@app.get("/epub-image")
def epub_image(session: SessionDep, book: str, path: str):
    """An image inside an imported e-book (chapters link here instead of copying it)."""
    import mimetypes
    import zipfile

    from fastapi.responses import Response

    # The novel's own file, or a volume in its folder.
    folder = book.rsplit("/", 1)[0] + "/" if "/" in book else None
    novel = session.exec(select(Novel).where(Novel.wacz_path.in_([book, folder]))).first()
    if novel is None or not book.lower().endswith(".epub"):
        raise HTTPException(404)
    file = settings.library_dir / book  # `book` is a novel's file, as found by the scan
    if ".." in Path(book).parts or not file.is_file():
        raise HTTPException(404)
    mime = mimetypes.guess_type(path)[0] or ""
    if not mime.startswith("image/"):
        raise HTTPException(404)
    try:
        with zipfile.ZipFile(file) as z:
            data = z.read(path)
    except (KeyError, zipfile.BadZipFile, OSError) as e:
        raise HTTPException(404) from e
    # An SVG opened directly must not run scripts on Shelf's origin.
    return Response(data, media_type=mime, headers={
        "Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})


@app.get("/covers/{name}")
def cover(name: str):
    from fastapi.responses import FileResponse

    path = (settings.covers_dir / name).resolve()
    if path.parent != settings.covers_dir.resolve() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=604800, immutable"})


# -- browser extension API -------------------------------------------------
#
# The Shelf Capture extension sends pages exactly as the browser displayed them. With a
# password set, it sends the password with HTTP Basic auth (see auth.py).


class PagePayload(BaseModel):
    url: str
    html: str


class CoverPayload(BaseModel):
    data: str  # base64
    mime: str = "image/jpeg"


class NewNovel(BaseModel):
    title: str = ""
    novelupdates: PagePayload | None = None
    cover: CoverPayload | None = None


class MetadataPayload(PagePayload):
    cover: CoverPayload | None = None


class ChapterPayload(PagePayload):
    title: str = ""
    number: float | None = None
    dry_run: bool = False


def _decode_cover(cover: CoverPayload | None) -> tuple[bytes, str] | None:
    import base64

    if cover is None or not cover.mime.startswith("image/"):
        return None
    try:
        data = base64.b64decode(cover.data, validate=False)
    except ValueError:
        return None
    return (data, cover.mime) if data else None


def _novel_json(novel: Novel, chapters: int, last: float | None, score: int = 0) -> dict:
    return {"id": novel.id, "title": novel.title, "chapters": chapters,
            "last_number": last, "nu_url": novel.nu_url, "has_archive": has_archive(novel),
            "cover": f"/covers/{novel.cover_file}" if novel.cover_file else None,
            "url": f"/novel/{novel.id}", "match": score}


@app.get("/api/novels")
def api_novels(session: SessionDep, url: str = "", title: str = ""):
    """Novels for the extension's picker; with `url`, best match for that page first.
    `identified` is the novel the page surely belongs to (see identify_novel), if any."""
    stats = {nid: (n, last) for nid, n, last in session.exec(
        select(Chapter.novel_id, func.count(Chapter.id),
               func.max(func.coalesce(Chapter.number_end, Chapter.number)))
        .group_by(Chapter.novel_id)).all()}
    urls: dict[int, list[str]] = {}
    if url:
        host = urlsplit(url).hostname or ""
        for nid, u in session.exec(select(Chapter.novel_id, Chapter.url)
                                   .where(col(Chapter.url).contains(host))).all():
            urls.setdefault(nid, []).append(u)
    nu_match = novel_by_nu_url(session, url) if is_series_url(url) else None
    out, scored = [], []
    for novel in session.exec(select(Novel)).all():
        n, last = stats.get(novel.id, (0, None))
        score = match_score(url, urls.get(novel.id, [])) if url else 0
        if nu_match is novel:
            score = 1000
        out.append(_novel_json(novel, n, last, score))
        scored.append((novel, score))
    out.sort(key=lambda d: (-d["match"], d["title"].casefold()))
    found = identify_novel(url, title, scored) if url and not is_series_url(url) else None
    return {"novels": out, "identified": found.id if found else None}


@app.post("/api/novels")
async def api_create_novel(session: SessionDep, body: NewNovel):
    meta = None
    if body.novelupdates is not None:
        try:
            meta = parse_series_page(body.novelupdates.html, body.novelupdates.url)
        except MetadataError as e:
            raise HTTPException(422, str(e)) from e
        existing = novel_by_nu_url(session, meta.url or body.novelupdates.url)
        if existing is not None:
            return JSONResponse({"novel": _novel_json(existing, len(existing.chapters), None),
                                 "created": False}, status_code=200)
    novel = create_novel(session, body.title or (meta.title if meta else ""))
    if meta is not None:
        await run_in_threadpool(apply_metadata, novel, meta, "upload",
                                _decode_cover(body.cover))
        session.commit()
    return JSONResponse({"novel": _novel_json(novel, 0, None), "created": True},
                        status_code=201)


@app.post("/api/novels/{novel_id}/metadata")
async def api_novel_metadata(session: SessionDep, novel_id: int, body: MetadataPayload):
    novel = get_novel(session, novel_id)
    try:
        meta = parse_series_page(body.html, body.url)
    except MetadataError as e:
        raise HTTPException(422, str(e)) from e
    await run_in_threadpool(apply_metadata, novel, meta, "upload", _decode_cover(body.cover))
    session.commit()
    return {"novel": _novel_json(novel, len(novel.chapters), None)}


@app.post("/api/novels/{novel_id}/chapters")
async def api_add_chapter(session: SessionDep, novel_id: int, body: ChapterPayload):
    novel = get_novel(session, novel_id)
    try:
        result = await run_in_threadpool(add_chapter, session, novel, body.url, body.html,
                                         body.title, body.number, body.dry_run)
    except CaptureError as e:
        raise HTTPException(422, str(e)) from e
    ch = result.chapter
    wanted = [] if body.dry_run else await run_in_threadpool(
        wanted_images, session, novel, ch.content_html)
    return {
        "action": result.action,
        "images_wanted": wanted,
        "replaced_title": result.replaced_title,
        "other_page": result.other_page,
        "dry_run": body.dry_run,
        "chapter": {"id": ch.id, "title": ch.title, "number": ch.number,
                    "number_end": ch.number_end,
                    "season_label": ch.season_label, "chars": ch.char_count,
                    "url": f"/chapter/{ch.id}" if ch.id else None},
        "novel": {"id": novel.id, "title": novel.title, "url": f"/novel/{novel.id}"},
    }


class ImagePayload(BaseModel):
    url: str
    data: str  # base64


class ImagesPayload(BaseModel):
    images: list[ImagePayload]


@app.post("/api/novels/{novel_id}/images")
def api_add_images(session: SessionDep, novel_id: int, body: ImagesPayload):
    """Images of chapters, sent by the browser extension after the chapter
    (see `images_wanted`)."""
    import base64
    import binascii

    novel = get_novel(session, novel_id)
    saved = 0
    for img in body.images[:200]:
        try:
            data = base64.b64decode(img.data, validate=True)
        except (binascii.Error, ValueError):
            continue
        saved += save_image(novel.id, img.url, data)
    return {"saved": saved}


IMAGE_HEADERS = {"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff",
                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"}


@app.get("/novel/{novel_id}/export.epub")
def export_novel(session: SessionDep, novel_id: int):
    """The novel as an EPUB e-book, as Shelf shows it."""
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    from .export import export_filename, export_to_temp

    novel = get_novel(session, novel_id)
    path = export_to_temp(session, novel, trim_rules(session, novel_id))
    return FileResponse(path, media_type="application/epub+zip",
                        filename=export_filename(novel),
                        background=BackgroundTask(path.unlink, missing_ok=True))


# -- OPDS catalog ----------------------------------------------------------
#
# For e-reader apps: the library as OPDS feeds, each novel offered as its EPUB export.


def _opds_response(body: bytes, kind: str) -> Response:
    return Response(body, media_type=kind + ";charset=utf-8")


def _opds_url(request: Request):
    base = str(request.base_url).rstrip("/")
    return lambda path: base + path


def _catalog_novels(session: Session) -> tuple[list[Novel], dict[int, int]]:
    """Novels with chapters to read (an empty EPUB is no use), most recently updated first."""
    counts = shown_counts(session)
    novels = [n for n in session.exec(select(Novel)).all() if counts.get(n.id)]
    novels.sort(key=opds.novel_updated, reverse=True)
    return novels, counts


def _new_novels(session: Session, novels: list[Novel]) -> list[Novel]:
    left = chapters_left(session)
    return [n for n in novels if new_count(n, left.get(n.id, 0))]


@app.get("/opds")
def opds_root(request: Request, session: SessionDep):
    novels, _ = _catalog_novels(session)
    sections = [
        ("/opds/all", "All novels", "Every novel, by title", len(novels)),
        ("/opds/updated", "Recently updated", "Novels with chapters added lately",
         len(novels)),
        ("/opds/new", "New chapters", "Novels with chapters since you last read",
         len(_new_novels(session, novels))),
    ]
    for key, label in STATUSES.items():
        count = sum(1 for n in novels if n.reading_status == key)
        if count:
            sections.append((f"/opds/list/{key}", label, f"Your {label} list", count))
    updated = opds.novel_updated(novels[0]) if novels else None
    return _opds_response(opds.navigation(_opds_url(request), sections, updated),
                          opds.NAVIGATION)


@app.get("/opds/all")
def opds_all(request: Request, session: SessionDep, page: int = 1):
    novels, counts = _catalog_novels(session)
    novels.sort(key=lambda n: (n.title or n.archive_title).lower())
    return _opds_response(opds.acquisition(_opds_url(request), "/opds/all", "All novels",
                                           novels, counts, page), opds.ACQUISITION)


@app.get("/opds/updated")
def opds_updated(request: Request, session: SessionDep, page: int = 1):
    novels, counts = _catalog_novels(session)
    return _opds_response(opds.acquisition(_opds_url(request), "/opds/updated",
                                           "Recently updated", novels, counts, page),
                          opds.ACQUISITION)


@app.get("/opds/new")
def opds_new(request: Request, session: SessionDep, page: int = 1):
    novels, counts = _catalog_novels(session)
    return _opds_response(opds.acquisition(_opds_url(request), "/opds/new", "New chapters",
                                           _new_novels(session, novels), counts, page),
                          opds.ACQUISITION)


@app.get("/opds/list/{status}")
def opds_list(request: Request, session: SessionDep, status: str, page: int = 1):
    if status not in STATUSES:
        raise HTTPException(404)
    novels, counts = _catalog_novels(session)
    novels = [n for n in novels if n.reading_status == status]
    return _opds_response(opds.acquisition(_opds_url(request), f"/opds/list/{status}",
                                           STATUSES[status], novels, counts, page),
                          opds.ACQUISITION)


@app.get("/opds/search.xml")
def opds_search_description(request: Request):
    return _opds_response(opds.opensearch(_opds_url(request)), opds.OPENSEARCH)


@app.get("/opds/search")
def opds_search(request: Request, session: SessionDep, q: str = "", page: int = 1):
    novels, counts = _catalog_novels(session)
    novels = [n for n in novels if matches(n, q)] if q.strip() else []
    return _opds_response(opds.acquisition(_opds_url(request), "/opds/search",
                                           f"Search: {q}", novels, counts, page, query=q),
                          opds.ACQUISITION)


@app.post("/novel/{novel_id}/images/fetch")
def images_fetch(session: SessionDep, novel_id: int):
    """Download the images of the novel's chapters that Shelf has no copy of."""
    get_novel(session, novel_id)
    start_image_fetch(novel_id)
    return RedirectResponse(f"/novel/{novel_id}#images", status_code=303)


@app.get("/api/novels/{novel_id}/images/fetch")
def images_fetch_status(novel_id: int):
    s = fetch_status(novel_id)
    return asdict(s) if s else {"running": False}


@app.get("/novel/{novel_id}/image/{name}")
def chapter_image(novel_id: int, name: str):
    """An image stored with a chapter sent from the browser."""
    import mimetypes

    from fastapi.responses import FileResponse

    path = images_dir(novel_id) / name
    if not STORED_NAME_RE.match(name) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type=mimetypes.guess_type(name)[0] or "image/jpeg",
                        headers=IMAGE_HEADERS)


@app.get("/novel/{novel_id}/archive-image")
def archive_image(session: SessionDep, novel_id: int, url: str):
    """An image in the novel's WACZ archive."""
    from fastapi.responses import Response

    found = read_archived_image(get_novel(session, novel_id), url)
    if found is None:
        raise HTTPException(404)
    return Response(found[0], media_type=found[1], headers=IMAGE_HEADERS)


@app.get("/extension.zip")
def extension_zip():
    """The browser extension, zipped (load it unpacked, or install the zip in Firefox)."""
    import io
    import zipfile

    from fastapi.responses import Response

    root = HERE / "extension"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(root))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="shelf-capture.zip"'})
