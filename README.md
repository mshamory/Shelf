# Shelf

A self-hosted reader for local archives of web novels. Drop
[WACZ](https://specs.webrecorder.net/wacz/latest/) files — e.g. captured with
[ArchiveWeb.page](https://archiveweb.page) or Browsertrix — into a folder, and Shelf extracts
the chapters, orders them, and serves them in a clean, distraction-free reader with
metadata from [NovelUpdates](https://www.novelupdates.com).

## Features

- **WACZ parsing**: random-access reads through the archive's CDXJ index; falls back to
  scanning the WARCs when there's no index, and to all captured HTML when there's no page list.
- **Chapters loaded by JavaScript**: chapters fetched by XHR/fetch are found in the
  captured JSON. This covers SvelteKit `__data.json`, Nuxt `_payload.json`, Next.js
  `_next/data`, Patreon post data and plain JSON APIs, with chapter bodies in HTML, plain text,
  ProseMirror/TipTap or Editor.js format. Hidden anti-scraping text and invisible zero-width watermarks are removed.
- **Chapters opened from another chapter (Next.js App Router)**: clicking "next chapter" on
  these sites fetches `…/chapter-2?_rsc=…` (a React Server Components payload) instead of
  a page. Shelf rebuilds the page from that payload and extracts it like an archived page,
  or, when the site hands the chapter to its reader script as data (title, number, text),
  reads that chapter object. Link prefetches carry no chapter text and are ignored.
- **Chapters from the rendered page**: when a chapter's network response isn't readable (e.g.
  the site sends it encrypted and its script fills in the page), Shelf uses the text the
  browser displayed, which ArchiveWeb.page and Browsertrix save with each page. Site menus,
  comments, filler lines and invisible watermarks are removed, and pages that scrolled on
  into the next chapters are split into chapters.
- **Missing chapters**: numbering gaps are listed in the contents and the reader, with the
  reason when the archive shows one: not captured, locked with only a preview, or captured only
  in encrypted form and never displayed while recording (Shelf doesn't decrypt
  site-protected content). Pages captured before their
  text loaded ("Verifying access…") are skipped.
- **Chapter extraction**: main-content extraction removes navigation, share buttons, comments
  and so on. Chapters are numbered (`Chapter 12`, `Ch. 7.5`, `第12章`, `/ch-12/` …) and ordered
  by that number. A post covering several chapters (`Chapter 96-97`, `Ch. 7 & 8`) counts for
  all of them; in novels that number parts as `Chapter 3-1`, `3-2`, a dash means a part.
  Error pages, near-empty pages and duplicate captures (including a post captured again
  under a new URL after it was renamed) are skipped. When one chapter number was captured
  more than once (e.g. from two sites), one copy is shown and the others are hidden and
  marked *duplicate*, so you can switch. A copy much shorter than the others (a stub) is
  never preferred; otherwise the site with the most chapters wins. Site names, series names
  and aggregator "Read … Online" text in chapter titles are removed; when every page is titled
  with just the site name, the chapter's own heading is used. Chapters titled with a
  repeated word and a number ("Patron 12", "Volume 3"), with a short name, a number and the
  chapter's name ("OVRMMO 106: The Last Stratagem"), or with just a number once the series
  and site names are removed ("Series — 17 | Site", when the URL ends in 17 too), are
  numbered by it when most unnumbered chapters follow the same pattern. Extras numbered that
  way ("OVRMMO 94.5: Extra Edition") stay in the story's order instead of the side stories.
  Blog post IDs in addresses (`/2016/09/26/1274/`) aren't taken for chapter numbers.
- **NovelUpdates metadata**: title, cover, description, authors, genres, tags, rating, status,
  associated names, and so on.
- **Reader**: themes (light, dark, sepia), font, size, line-height, width and alignment
  settings, ←/→ keys for chapter navigation, and progress saved per novel (chapter and
  scroll position).
- **Library**: covers, filter by title/author/genre/tag, "continue reading" links, full-text
  chapter search, upload of WACZ files and saved web pages from the browser, and hiding of
  non-chapter pages.

### Browser extension

**Shelf Capture** sends the page you're reading to Shelf exactly as your browser displays it,
after the site's scripts have loaded the chapter. Elements the page hides with CSS are left
out, and text drawn by CSS is kept. Use it for chapters an archive didn't capture well, or to
build a novel without any archive.

- On a **NovelUpdates series page**, it creates a novel with that page's metadata and cover,
  or applies the metadata to a novel you already have.
- On a **chapter page**, pick the novel; the one whose chapters come from the same site and
  path is preselected. You can also start a new novel. Before you send, it shows the detected
  title and number and whether the page adds a chapter or replaces one. Correct the title or
  number if needed. With text selected, you can send just the selection.
- **Alt+Shift+D** sends a chapter page without asking, when Shelf is sure of both the novel
  and the chapter number. It shows the result and closes. The novel counts as sure when
  the page sits in the same folder as chapters already in Shelf, or when the novel's title or
  one of its alternative names is in the page title or URL, and no other novel fits as well.
  On blogs that file posts by date (`/2026/09/07/series-ch-12/`), the post name has to match
  too, since the date says nothing about the series. When the page would replace a chapter
  that was captured from a different page, the shortcut doesn't send; the menu opens with a
  warning, in case it's the wrong novel. Otherwise the menu opens with a note on what was
  unclear. To change either shortcut, use
  `about:addons` → ⚙ → *Manage Extension Shortcuts* in Firefox, or `chrome://extensions/shortcuts`.

Chapters sent this way are kept when the novel's archive is re-imported, and they take the
place of archived chapters with the same URL or number. The chapter they replace isn't lost:
it's kept in the chapter's **history** (linked from the reader's footer and from *Edit list*),
where you can view earlier versions and restore one. A restore keeps the version it replaces,
so it can be undone the same way. History stays with a chapter across re-imports. If the archive is removed, the novel
stays with just the sent chapters. The raw pages are kept in `data/captures/`.

To install, download the extension from the link at the bottom of any Shelf page
(`/extension.zip`) and unzip it.

- **Chrome, Edge, Brave**: open `chrome://extensions`, turn on *Developer mode*, choose *Load
  unpacked* and select the unzipped folder.
- **Firefox**: open `about:debugging#/runtime/this-firefox` and choose *Load Temporary
  Add-on…* with the `manifest.json` file. This lasts until Firefox restarts. For a permanent
  install, sign the extension as an unlisted add-on on addons.mozilla.org, or use Firefox
  Developer Edition with `xpinstall.signatures.required` set to `false`.

Open the extension (or press Alt+Shift+S) and set Shelf's address under *Settings*. The default
is `http://localhost:8000`. If your browser refuses a plain-HTTP address on your network,
serve Shelf over HTTPS through a reverse proxy.

### Saved web pages

Pages saved from the browser (Ctrl+S) can be uploaded as chapters, one file per page, as
"Web page, HTML only", "Web page, complete" (`.html`) or "Web page, single file" (`.mhtml`).
Use **Upload** on the library page, where you choose the novel or start a new one (its title
is guessed from the pages' titles if you leave it empty), or **Add saved pages** on a novel's
page. Several files can be uploaded at once, also mixed with `.wacz` archives.

- Each page is read like one sent by the extension. The page's address, which numbering and
  matching rely on, comes from the file: Chrome's "saved from" note, the MHTML header, or the
  page's canonical link. A page without any is named after its file.
- A saved NovelUpdates series page among the files sets the novel's metadata (and its title).
- Pages replace chapters with the same address or number; the replaced text stays in the
  chapter's history. Images saved into a local `…_files` folder are left out, since that
  folder isn't uploaded.
- "HTML only" saves the page as the site sent it, so chapters that the site's scripts load
  afterwards may be missing; save "complete" or "single file" for those. Text hidden with
  CSS can't be detected in a saved file, so the extension is better for sites that hide
  anti-scraping text that way.

Like chapters sent from the extension, uploaded pages are kept when an archive is re-imported,
and saving them doesn't need `SHELF_ALLOW_UPLOADS` (which covers archives only).

### Getting NovelUpdates metadata

NovelUpdates is behind bot protection, and Shelf does not try to get around it. Use one of
these instead, in order of reliability:

1. **Archive the series page with the chapters.** If the WACZ contains a
   `novelupdates.com/series/…` page, Shelf picks up its metadata and cover automatically.
2. **Send the page with the browser extension** (see above).
3. **Upload a saved page.** Open the series page in your browser, save it
   (Ctrl+S → "Web page, HTML only"), and upload it on the novel's page, or together with
   the chapters' saved pages.
4. **Fetch by URL.** A plain HTTP request; it works only if NovelUpdates allows it.

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
