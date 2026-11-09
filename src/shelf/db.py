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

    novel: Novel | None = Relationship(back_populates="chapters")


BROWSER_PREFIX = "browser:"


def has_archive(novel: "Novel") -> bool:
    return not novel.wacz_path.startswith(BROWSER_PREFIX)


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


def get_session():
    with Session(get_engine()) as session:
        yield session
