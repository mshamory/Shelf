"""Where you are with each novel: its reading status, and chapters added since you last read.

"New" chapters are counted, not tracked one by one: when you read, Shelf notes how many shown
chapters come after your place, and any more than that later are new. Chapter ids change when
an archive is re-imported, so counting is what survives re-imports."""

from __future__ import annotations

from sqlalchemy.orm import aliased
from sqlmodel import Session, func, select

from .db import Chapter, Novel, utcnow

# Stored value -> label, in the order the library lists them.
STATUSES = {
    "reading": "Reading",
    "planned": "Plan to read",
    "on_hold": "On hold",
    "finished": "Finished",
    "dropped": "Dropped",
}
# Reading a chapter moves a novel to "Reading" only from these.
BECOMES_READING = (None, "planned")


def chapters_left(session: Session, novel_ids: list[int] | None = None) -> dict[int, int]:
    """Shown chapters after your place, per novel with a place."""
    last = aliased(Chapter)
    after = (
        select(func.count(Chapter.id))
        .where(Chapter.novel_id == Novel.id, Chapter.hidden == False,  # noqa: E712
               Chapter.position > last.position)
        .correlate(Novel, last)
        .scalar_subquery()
    )  # per novel, so only the chapters of novels you've started are read
    q = select(Novel.id, after).join(last, last.id == Novel.last_chapter_id)
    if novel_ids is not None:
        q = q.where(Novel.id.in_(novel_ids))
    return {nid: n for nid, n in session.exec(q).all() if n}


def new_count(novel: Novel, left: int) -> int:
    if novel.last_chapter_id is None or novel.left_when_read is None:
        return 0
    return max(0, left - novel.left_when_read)


def note_read(session: Session, novel: Novel) -> None:
    """You read a chapter: what's after your place now counts as known."""
    session.flush()
    novel.left_when_read = chapters_left(session, [novel.id]).get(novel.id, 0)
    if novel.reading_status in BECOMES_READING:
        novel.reading_status = "reading"


def new_chapters(session: Session, novel: Novel) -> int:
    session.flush()
    return new_count(novel, chapters_left(session, [novel.id]).get(novel.id, 0))


def keep_new(session: Session, novel: Novel, before: int) -> None:
    """After hiding, showing or moving chapters by hand: keep the count of new chapters as
    it was (`before`), so your own edits never look like new chapters or hide them."""
    if novel.last_chapter_id is None or novel.left_when_read is None:
        return
    session.flush()
    left = chapters_left(session, [novel.id]).get(novel.id, 0)
    novel.left_when_read = max(0, left - before)


def shown_urls(chapters) -> set[str]:
    return {c.url for c in chapters if not c.hidden}


def note_added(novel: Novel, before: set[str], after: set[str]) -> None:
    """An import or capture changed the chapters: if any is new, the novel was updated."""
    if after - before:
        novel.updated_at = utcnow()


WORDS_PER_MINUTE = 250  # a typical pace for fiction


def read_time(words: int, about: bool = False) -> str:
    """How long `words` take to read: "12 min", "2 h 5 min", "about 40 h"."""
    minutes = max(1, round(words / WORDS_PER_MINUTE))
    if minutes < 60:
        text = f"{minutes} min"
    elif minutes < 600:
        hours, rest = divmod(minutes, 60)
        text = f"{hours} h {rest} min" if rest else f"{hours} h"
    else:
        text = f"{round(minutes / 60)} h"
    return f"about {text}" if about else text
