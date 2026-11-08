# Shelf

A self-hosted reader for local archives of web novels. Drop
[WACZ](https://specs.webrecorder.net/wacz/latest/) files — e.g. captured with
[ArchiveWeb.page](https://archiveweb.page) or Browsertrix — into a folder, and Shelf extracts
the chapters, orders them, and serves them in a clean, distraction-free reader with
metadata from [NovelUpdates](https://www.novelupdates.com).

## Features

- **WACZ parsing**: random-access reads through the archive's CDXJ index; falls back to
  scanning the WARCs when there's no index, and to all captured HTML when there's no page list.
- **Chapter extraction**: main-content extraction removes navigation, share buttons, comments
  and so on. Chapters are numbered (`Chapter 12`, `Ch. 7.5`, `第12章`, `/ch-12/` …) and ordered
  by that number. Error pages, near-empty pages and duplicate captures are skipped, and the
  site name and series name repeated in every chapter title are removed.
- **NovelUpdates metadata**: title, cover, description, authors, genres, tags, rating, status,
  associated names, and so on.
- **Reader**: themes (light, dark, sepia), font, size, line-height, width and alignment
  settings, ←/→ keys for chapter navigation, and progress saved per novel (chapter and
  scroll position).
- **Library**: covers, filter by title/author/genre/tag, "continue reading" links, full-text
  chapter search, upload of WACZ files from the browser, and hiding of non-chapter pages.

### Getting NovelUpdates metadata

NovelUpdates is behind bot protection, and Shelf does not try to get around it. Use one of
these instead, in order of reliability:

1. **Archive the series page with the chapters.** If the WACZ contains a
   `novelupdates.com/series/…` page, Shelf picks up its metadata and cover automatically.
2. **Upload a saved page.** Open the series page in your browser, save it
   (Ctrl+S → "Web page, HTML only"), and upload it on the novel's page.
3. **Fetch by URL.** A plain HTTP request; it works only if NovelUpdates allows it.

You can also edit the title, authors, genres, tags and status by hand.

## Running with Docker

```sh
mkdir -p library data
cp ~/Downloads/*.wacz library/
docker compose up -d --build
```

Then open <http://localhost:8000>. The library is scanned at startup and whenever you press
**Rescan library**. Uploaded files are saved to `library/uploads/`.

| Variable               | Default    | Description                                       |
| ---------------------- | ---------- | ------------------------------------------------- |
| `SHELF_LIBRARY_DIR`    | `/library` | Folder scanned (recursively) for `.wacz` files    |
| `SHELF_DATA_DIR`       | `/data`    | SQLite database and cover images                  |
| `SHELF_SCAN_ON_STARTUP`| `true`     | Scan the library when the server starts           |
| `SHELF_ALLOW_UPLOADS`  | `true`     | Allow uploading WACZ files through the web UI     |
| `SHELF_FETCH_COVERS`   | `true`     | Download cover images that aren't in the archive  |
| `SHELF_PORT`           | `8000`     | Port to listen on                                 |

The container runs as UID 1000. Make sure the mounted folders are writable by that user, or
mount `library` read-only and set `SHELF_ALLOW_UPLOADS=false`.

Shelf has no authentication. Run it on a trusted network or put it behind a reverse proxy
that handles authentication.

## How a WACZ becomes a novel

Each WACZ file is one novel. The title comes from the NovelUpdates metadata if there is any,
otherwise from the archive's `datapackage.json` title, otherwise from the file name. Every
page in `pages/pages.jsonl` is a candidate chapter:

1. The newest successful capture is read from the WARC.
2. The main content is extracted and sanitised.
3. Pages that are errors, too short, or duplicates are dropped.
4. The remaining chapters are sorted by detected chapter number, then by capture time.

Re-importing an archive after you add more captures keeps your reading progress and your
hidden-page choices, matched by chapter URL.

## Development

```sh
uv sync
uv run shelf            # http://localhost:8000, uses ./library and ./data
uv run pytest
uv run ruff check src tests
```

### Built on

- [FastAPI](https://fastapi.tiangolo.com) + [Jinja2](https://jinja.palletsprojects.com):
  web app and templates
- [SQLModel](https://sqlmodel.tiangolo.com) / SQLite: storage
- [warcio](https://github.com/webrecorder/warcio): WARC record parsing
- [trafilatura](https://trafilatura.readthedocs.io): main-content extraction
- [nh3](https://github.com/messense/nh3): HTML sanitising
- [selectolax](https://github.com/rushter/selectolax): HTML parsing for NovelUpdates pages
- [httpx](https://www.python-httpx.org): HTTP client
- [Pico CSS](https://picocss.com): styling
- [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/):
  configuration
