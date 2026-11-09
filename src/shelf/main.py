from __future__ import annotations

import logging
import re
import shutil
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlmodel import Session, col, select
from starlette.concurrency import run_in_threadpool

from . import __version__
from .config import settings
from .db import Chapter, Novel, get_session, init_db, utcnow
from .gaps import find_gaps, gaps_before, missing_count
from .library import apply_metadata, import_wacz, scan_status, start_scan
from .novelupdates import MetadataError, fetch_series, parse_series_page

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
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(version=__version__, settings=settings)

SessionDep = Annotated[Session, Depends(get_session)]


def flash_redirect(url: str, msg: str | None = None, error: bool = False) -> RedirectResponse:
    from urllib.parse import quote

    if msg:
        url += ("&" if "?" in url else "?") + ("error=" if error else "msg=") + quote(msg)
    return RedirectResponse(url, status_code=303)


def get_novel(session: Session, novel_id: int) -> Novel:
    novel = session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(404, "Novel not found")
    return novel


def visible_chapters(session: Session, novel_id: int, include_hidden: bool = False):
    q = select(Chapter.id, Chapter.position, Chapter.number, Chapter.title, Chapter.char_count,
               Chapter.hidden, Chapter.url).where(Chapter.novel_id == novel_id)
    if not include_hidden:
        q = q.where(Chapter.hidden == False)  # noqa: E712
    return session.exec(q.order_by(Chapter.position)).all()


def chapter_numbers(session: Session, novel_id: int | None = None):
    q = select(Chapter.novel_id, Chapter.number)
    if novel_id is not None:
        q = q.where(Chapter.novel_id == novel_id)
    by_novel: dict[int, list[float | None]] = {}
    for nid, number in session.exec(q).all():
        by_novel.setdefault(nid, []).append(number)
    return by_novel


# -- library ---------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def library(request: Request, session: SessionDep, q: str = "", sort: str = "recent"):
    counts = dict(
        session.exec(
            select(Chapter.novel_id, func.count(Chapter.id))
            .where(Chapter.hidden == False)  # noqa: E712
            .group_by(Chapter.novel_id)
        ).all()
    )
    missing = {nid: missing_count(find_gaps(nums))
               for nid, nums in chapter_numbers(session).items()}
    novels = list(session.exec(select(Novel)).all())
    if q:
        needle = q.lower()
        novels = [
            n for n in novels
            if needle in " ".join(
                [n.title, n.archive_title, *n.alt_names, *n.authors, *n.genres, *n.tags]
            ).lower()
        ]
    if sort == "title":
        novels.sort(key=lambda n: n.title.lower())
    elif sort == "added":
        novels.sort(key=lambda n: n.added_at, reverse=True)
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
         "scan": scan_status, "missing": missing},
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
async def upload(session: SessionDep, file: UploadFile):
    if not settings.allow_uploads:
        raise HTTPException(403, "Uploads are disabled")
    name = SAFE_NAME_RE.sub("_", Path(file.filename or "upload.wacz").name).strip(" .")
    if not name.lower().endswith(".wacz"):
        return flash_redirect("/", "Only .wacz files can be uploaded", error=True)
    dest_dir = settings.library_dir / "uploads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    i = 1
    while dest.exists():
        dest = dest_dir / f"{Path(name).stem} ({i}).wacz"
        i += 1
    with dest.open("wb") as out:
        await run_in_threadpool(shutil.copyfileobj, file.file, out, 1024 * 1024)
    novel = await run_in_threadpool(import_wacz, session, dest)
    if novel.import_error:
        return flash_redirect(f"/novel/{novel.id}", novel.import_error, error=True)
    return flash_redirect(f"/novel/{novel.id}", f"Imported {len(novel.chapters)} chapters")


# -- novel -------------------------------------------------------------------


@app.get("/novel/{novel_id}", response_class=HTMLResponse)
def novel_page(request: Request, session: SessionDep, novel_id: int, hidden: bool = False):
    novel = get_novel(session, novel_id)
    chapters = visible_chapters(session, novel_id, include_hidden=hidden)
    hidden_count = session.exec(
        select(func.count(Chapter.id)).where(Chapter.novel_id == novel_id, Chapter.hidden == True)  # noqa: E712
    ).one()
    last = session.get(Chapter, novel.last_chapter_id) if novel.last_chapter_id else None
    gaps = find_gaps(chapter_numbers(session, novel_id).get(novel_id, []))
    numbered = [c.number for c in chapters if c.number is not None]
    return templates.TemplateResponse(
        request,
        "novel.html",
        {"novel": novel, "chapters": chapters, "last": last, "show_hidden": hidden,
         "hidden_count": hidden_count, "gaps": gaps, "missing_total": missing_count(gaps),
         "gaps_before": gaps_before(chapters, gaps),
         "first_number": int(min(numbered)) if numbered else None},
    )


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
    path = settings.library_dir / novel.wacz_path
    if not path.exists():
        return flash_redirect(f"/novel/{novel_id}", "Archive file is missing", error=True)
    novel = await run_in_threadpool(import_wacz, session, path, True)
    if novel.import_error:
        return flash_redirect(f"/novel/{novel_id}", novel.import_error, error=True)
    return flash_redirect(f"/novel/{novel_id}", f"Re-imported {len(novel.chapters)} chapters")


@app.post("/novel/{novel_id}/progress/reset")
def reset_progress(session: SessionDep, novel_id: int):
    novel = get_novel(session, novel_id)
    novel.last_chapter_id, novel.last_position, novel.last_read_at = None, 0.0, None
    session.commit()
    return flash_redirect(f"/novel/{novel_id}", "Progress reset")


@app.post("/chapter/{chapter_id}/toggle-hidden")
def toggle_hidden(session: SessionDep, chapter_id: int):
    ch = session.get(Chapter, chapter_id)
    if ch is None:
        raise HTTPException(404)
    ch.hidden = not ch.hidden
    session.commit()
    return flash_redirect(f"/novel/{ch.novel_id}?hidden=1#ch-{ch.id}")


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
def reader(request: Request, session: SessionDep, chapter_id: int):
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
    # Warn when the next chapter skips numbers (e.g. 128 -> 130).
    gap_ahead = None
    if next_id is not None and ch.number is not None:
        nxt = toc[idx + 1]
        if nxt.number is not None:
            gap_ahead = next(
                (g for g in find_gaps([ch.number, nxt.number])), None
            )
            if gap_ahead:
                present = {int(n) for n in chapter_numbers(session, novel.id)[novel.id]
                           if n is not None}
                if all(n in present for n in range(gap_ahead.start, gap_ahead.end + 1)):
                    gap_ahead = None  # skipped only because those chapters are hidden
    return templates.TemplateResponse(
        request,
        "reader.html",
        {"novel": novel, "chapter": ch, "toc": toc, "index": idx, "prev_id": prev_id,
         "next_id": next_id, "resume": resume, "gap_ahead": gap_ahead},
    )


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
    session.commit()
    return {"ok": True}


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


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "version": __version__})


@app.get("/covers/{name}")
def cover(name: str):
    from fastapi.responses import FileResponse

    path = (settings.covers_dir / name).resolve()
    if path.parent != settings.covers_dir.resolve() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=604800, immutable"})
