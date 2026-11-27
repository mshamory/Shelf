from datetime import UTC, datetime

from sqlalchemy import JSON, Column, Text, event
from sqlmodel import Field, Relationship, Session, SQLModel, create_engine

from .config import settings


def utcnow() -> datetime:
    return datetime.now(UTC)


class Novel(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    # Relative to library_dir; "browser:<id>" for novels created from the browser extension.
    wacz_path: str = Field(index=True, unique=True)
    wacz_size: int = 0
    wacz_mtime: float = 0.0
    archive_title: str = ""
    title: str = ""
    added_at: datetime = Field(default_factory=utcnow)
    import_error: str | None = None
    import_warning: str | None = None  # imported, but parts left out (a volume with DRM)

    # NovelUpdates metadata
    nu_url: str | None = None
    metadata_source: str | None = None  # "wacz" | "upload" | "fetch" | "manual"
    cover_url: str | None = None
    cover_file: str | None = None  # filename in covers_dir
    description_html: str | None = Field(default=None, sa_column=Column(Text))
    novel_type: str | None = None
    language: str | None = None
    year: str | None = None
    status: str | None = None
    licensed: str | None = None
    rating: float | None = None
    votes: int | None = None
    authors: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    artists: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    genres: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    alt_names: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    original_publisher: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    english_publisher: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    # Chapters the archive mentions but holds no readable text for: {"1009": "encrypted"}
    unreadable_chapters: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON))

    # Reading progress
    last_chapter_id: int | None = None
    last_position: float = 0.0  # scroll fraction within the chapter
    last_read_at: datetime | None = None
    reading_status: str | None = None  # a key of reading.STATUSES; None: not on a list
    left_when_read: int | None = None  # shown chapters after your place when you last read
    updated_at: datetime | None = None  # when an import or capture last added a chapter

    chapters: list["Chapter"] = Relationship(
        back_populates="novel",
        sa_relationship_kwargs={"cascade": "all, delete-orphan", "order_by": "Chapter.position"},
    )


class Chapter(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    novel_id: int = Field(foreign_key="novel.id", index=True)
    position: int = Field(index=True)  # 0-based reading order
    number: float | None = None
    number_end: float | None = None  # last chapter covered, for "Chapter 96-97"
    season: int | None = None  # set only when numbering restarts per season/part/book
    season_label: str = ""  # "Part 2", "Side stories", ...
    title: str = ""
    url: str = ""
    captured_at: str = ""
    content_html: str = Field(default="", sa_column=Column(Text))
    char_count: int = 0
    word_count: int = 0  # for the reading time; kept up to date with content_html (below)
    hidden: bool = False
    # Another capture of the same chapter number was preferred; imported hidden.
    duplicate: bool = False
    # "archive": imported from the WACZ (replaced on re-import); "browser": sent by the
    # browser extension (kept across re-imports, wins over archive chapters with its number).
    origin: str = "archive"
    capture_file: str | None = None  # raw page as sent, relative to data_dir

    # title/number/season above are the effective values. What the importer detected is
    # kept here so user edits can be reset; custom_* flags survive re-imports (by URL).
    auto_title: str = ""
    auto_number: float | None = None
    auto_number_end: float | None = None
    auto_season: int | None = None
    auto_season_label: str = ""
    custom_title: bool = False
    custom_number: bool = False
    custom_season: bool = False
    # The text was edited in Shelf: re-imports and re-extraction keep it.
    custom_content: bool = False

    novel: Novel | None = Relationship(back_populates="chapters")


@event.listens_for(Chapter, "before_insert")
@event.listens_for(Chapter, "before_update")
def _count_words(_mapper, _conn, chapter):
    """Whenever a chapter's text is saved (import, capture, edit, restore), recount its
    words. (On saving: SQLModel's constructor sets fields without attribute events.)"""
    from sqlalchemy import inspect

    from .extract import word_count

    state = inspect(chapter)
    if state.pending or state.attrs.content_html.history.has_changes():
        chapter.word_count = word_count(chapter.content_html or "")


VERSION_FIELDS = (
    "title", "custom_title", "number", "number_end", "custom_number", "season",
    "season_label", "custom_season", "url", "captured_at", "char_count", "origin",
    "capture_file", "auto_title", "auto_number", "auto_number_end", "auto_season",
    "auto_season_label", "custom_content",
)


class ChapterVersion(SQLModel, table=True):
    """A chapter as it was before a capture (or a restore) replaced it.

    `chapter_id` is not a foreign key: archive chapters are recreated on every re-import,
    and their versions are moved to the new row by URL (see library.import_wacz). Versions
    whose chapter is gone keep `chapter_id` None.
    """

    id: int | None = Field(default=None, primary_key=True)
    novel_id: int = Field(foreign_key="novel.id", index=True, ondelete="CASCADE")
    chapter_id: int | None = Field(default=None, index=True)
    saved_at: datetime = Field(default_factory=utcnow)  # when it was replaced
    reason: str = ""  # what replaced it, for the history page
    fields: dict = Field(default_factory=dict, sa_column=Column(JSON))  # VERSION_FIELDS
    content_html: str = Field(default="", sa_column=Column(Text))

    def get(self, name: str):
        return self.fields.get(name)


def save_version(session: Session, ch: "Chapter", reason: str) -> ChapterVersion:
    """Keep the chapter's current state before it's overwritten."""
    v = ChapterVersion(novel_id=ch.novel_id, chapter_id=ch.id, reason=reason,
                       fields={f: getattr(ch, f) for f in VERSION_FIELDS},
                       content_html=ch.content_html)
    session.add(v)
    return v


def restore_version(ch: "Chapter", v: ChapterVersion) -> None:
    for f in VERSION_FIELDS:
        if f in v.fields:
            setattr(ch, f, v.fields[f])
    ch.content_html = v.content_html
    ch.custom_content = bool(v.fields.get("custom_content"))  # versions from before edits


BROWSER_PREFIX = "browser:"


def has_archive(novel: "Novel") -> bool:
    return not novel.wacz_path.startswith(BROWSER_PREFIX)


class GlossaryTerm(SQLModel, table=True):
    """A term of a novel's world (a name, place, technique) with what it means, shown
    where it appears in chapters (see glossary.py)."""

    id: int | None = Field(default=None, primary_key=True)
    novel_id: int = Field(foreign_key="novel.id", index=True, ondelete="CASCADE")
    term: str
    category: str = ""  # "Character", "Place", ... (free text)
    # Other ways it's written, one per entry; "/.../" is a regular expression.
    aliases: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    description: str = Field(default="", sa_column=Column(Text))
    ignore_case: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class TrimRule(SQLModel, table=True):
    """Text to strip from chapters when displayed (watermarks, translator notes, ...).

    Applied at render time, so rules survive re-imports and can be switched off.
    """

    id: int | None = Field(default=None, primary_key=True)
    novel_id: int | None = Field(
        default=None, foreign_key="novel.id", index=True, ondelete="CASCADE"
    )  # None = applies to every novel
    pattern: str
    is_regex: bool = False
    ignore_case: bool = True
    mode: str = "text"  # "text": remove the match; "paragraph": remove the whole block
    enabled: bool = True
    created_at: datetime = Field(default_factory=utcnow)


engine = None


def get_engine():
    global engine
    if engine is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        engine = create_engine(settings.db_url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return engine


def _add_missing_columns(conn) -> set[str]:
    """Minimal forward-only migration: add columns introduced after a table was created."""
    from sqlalchemy import inspect

    insp = inspect(conn)
    added: set[str] = set()
    for table in SQLModel.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            ddl_type = col.type.compile(dialect=conn.dialect)
            default = ""
            if col.default is not None and getattr(col.default, "is_scalar", False):
                value = col.default.arg
                default = f" DEFAULT {int(value) if isinstance(value, bool) else repr(value)}"
            conn.exec_driver_sql(
                f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}{default}'
            )
            added.add(f"{table.name}.{col.name}")
    return added


def init_db() -> None:
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        added = _add_missing_columns(conn)
        if "chapter.auto_title" in added:
            # Existing chapters: what we have now is what was detected.
            conn.exec_driver_sql(
                "UPDATE chapter SET auto_title = title, auto_number = number, "
                "auto_season = season, auto_season_label = season_label"
            )
        if "novel.reading_status" in added:
            # Novels you had started are the ones you're reading.
            conn.exec_driver_sql(
                "UPDATE novel SET reading_status = 'reading' WHERE last_chapter_id IS NOT NULL")
        if "chapter.word_count" in added:
            _count_all_words(conn)
        if "novel.left_when_read" in added:
            # What's after your place now is known; anything added from here on is new.
            conn.exec_driver_sql(
                "UPDATE novel SET left_when_read = (SELECT count(*) FROM chapter c, chapter l "
                "WHERE l.id = novel.last_chapter_id AND c.novel_id = novel.id "
                "AND c.hidden = 0 AND c.position > l.position) "
                "WHERE last_chapter_id IS NOT NULL")


def _count_all_words(conn) -> None:
    """Existing chapters' word counts, when the column is new."""
    import logging

    from .extract import word_count

    total = conn.exec_driver_sql("SELECT count(*) FROM chapter").scalar()
    logging.getLogger("shelf").info(
        "Counting the words in %d chapters for their reading times (once; this can take a "
        "minute)", total)
    last = 0
    while True:
        rows = conn.exec_driver_sql(
            "SELECT id, content_html FROM chapter WHERE id > ? ORDER BY id LIMIT 500",
            (last,)).fetchall()
        if not rows:
            return
        conn.exec_driver_sql("UPDATE chapter SET word_count = ? WHERE id = ?",
                             [(word_count(html or ""), cid) for cid, html in rows])
        last = rows[-1][0]


def get_session():
    with Session(get_engine()) as session:
        yield session
