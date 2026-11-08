from datetime import UTC, datetime

from sqlalchemy import JSON, Column, Text, event
from sqlmodel import Field, Relationship, Session, SQLModel, create_engine

from .config import settings


def utcnow() -> datetime:
    return datetime.now(UTC)


class Novel(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    wacz_path: str = Field(index=True, unique=True)  # relative to library_dir
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
    title: str = ""
    url: str = ""
    captured_at: str = ""
    content_html: str = Field(default="", sa_column=Column(Text))
    char_count: int = 0
    hidden: bool = False

    novel: Novel | None = Relationship(back_populates="chapters")


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


def init_db() -> None:
    SQLModel.metadata.create_all(get_engine())


def get_session():
    with Session(get_engine()) as session:
        yield session
