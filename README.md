# Shelf

A self-hosted reader for local archives of web novels. Drop
[WACZ](https://specs.webrecorder.net/wacz/latest/) files — e.g. captured with
[ArchiveWeb.page](https://archiveweb.page) or Browsertrix — into a folder, and Shelf extracts
the chapters, orders them, and serves them in a clean, distraction-free reader with
metadata from [NovelUpdates](https://www.novelupdates.com). EPUB e-books and pages saved from
the browser can be added too.

**Website:** <https://mshamory.github.io/Shelf/> (overview, screenshots and install steps)

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
- **Reading on a phone**: tap the right or left edge of the screen to turn a page; at the end
  of a chapter, the next tap opens the next one (or set taps to change chapter, or do nothing,
  under **Aa**). Tapping the middle shows or hides the top bar, which also hides as you scroll
  down. The next chapter is loaded ahead, so moving on is instant. See
  [Installing Shelf as an app](#installing-shelf-as-an-app).
- **Chapter editor**: **Edit text** under a chapter opens its text for fixing typos or
  removing what trim rules can't (select and delete, *Delete paragraph*, bold/italic, or
  the HTML itself). The text before each save stays in the chapter's history, where
  *Restore* undoes an edit. Re-imports and re-extraction keep edited text; only sending
  the chapter again from the browser replaces it. Trim rules still apply on top of edits.
- **Glossary**: each novel's names, places and terms with a description, marked in the
  reader wherever they appear; hover, tap or Tab to one to see what it is. A term matches
  its name as a whole word and any aliases, one per line, where `/…/` is a regular
  expression (`/Spirit Stones?/`). Select a name in the reader and press **Glossary…** to
  add it. The reader's **Aa** settings mark every mention, only the first in each chapter,
  or none. Exported EPUBs end with the glossary, each term's first mention in a chapter
  linked to it.
- **Library**: covers, filter by title/author/genre/tag, "continue reading" links, full-text
  chapter search, upload of WACZ files, EPUB e-books and saved web pages from the browser,
  and hiding of non-chapter pages.
- **Reading lists**: put each novel on Reading, Plan to read, On hold, Finished or Dropped
  (*My list* on the novel's page) and filter the library by list. Opening a chapter moves a
  novel that isn't on a list, or is on Plan to read, to Reading.
- **New chapters**: when an import or a chapter sent from the browser adds chapters after your
  place, the novel's cover shows "3 new" until you next read it, and the *New chapters* filter
  lists those novels. Each card shows how many chapters are left after your place, and
  *Recently updated* sorts by when chapters were last added. Hiding, showing or renumbering
  chapters yourself doesn't count as new.
- **OPDS catalog**: browse the library from an e-reader app and download novels as EPUBs
  (see [OPDS catalog](#opds-catalog)).

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
is `http://localhost:8000`. If Shelf has a password, enter it there too. If your browser refuses a plain-HTTP address on your network,
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
docker compose up -d
```

This pulls the published image, `ghcr.io/mshamory/shelf` (for `linux/amd64` and
`linux/arm64`). `latest` is the newest release and `edge` follows `master`; set `SHELF_IMAGE`
to pick another tag, such as `SHELF_IMAGE=ghcr.io/mshamory/shelf:0.1`. To build from your
checkout instead, run `docker compose up -d --build`.

Then open <http://localhost:8000>. The library is scanned at startup and whenever you press
**Rescan library**. Uploaded files are saved to `library/uploads/`.

| Variable               | Default    | Description                                       |
| ---------------------- | ---------- | ------------------------------------------------- |
| `SHELF_LIBRARY_DIR`    | `/library` | Folder scanned (recursively) for `.wacz`/`.epub`  |
| `SHELF_DATA_DIR`       | `/data`    | SQLite database and cover images                  |
| `SHELF_SCAN_ON_STARTUP`| `true`     | Scan the library when the server starts           |
| `SHELF_ALLOW_UPLOADS`  | `true`     | Allow uploading WACZ/EPUB files in the web UI     |
| `SHELF_FETCH_COVERS`   | `true`     | Download cover images that aren't in the archive  |
| `SHELF_PORT`           | `8000`     | Port to listen on                                 |
| `SHELF_PASSWORD`       |            | Require this password (see [Signing in](#signing-in)) |
| `SHELF_AUTH_HEADER`    |            | Header a login proxy puts your name in, e.g. `Remote-User` |
| `SHELF_TRUSTED_PROXIES`| `127.0.0.1,::1` | Addresses or networks of your reverse proxy   |

The container runs as UID 1000. Make sure the mounted folders are writable by that user, or
mount `library` read-only and set `SHELF_ALLOW_UPLOADS=false`.

### Signing in

Out of the box, anyone who can reach Shelf can use it. Run it on a network you trust, or
turn on one of these, or both:

- **A password.** Set `SHELF_PASSWORD`, and Shelf asks for it on a sign-in page. You stay
  signed in on that browser for 400 days, or until you choose **Sign out** at the bottom of
  a page. Changing the password signs every browser out. E-reader apps and the browser
  extension send the password instead: give it to the app (any user name will do), and to
  the extension under *Settings*.
- **A login proxy.** If a reverse proxy such as Authelia, Authentik or oauth2-proxy signs you
  in, set `SHELF_AUTH_HEADER` to the header it puts your name in (often `Remote-User`). Set
  `SHELF_TRUSTED_PROXIES` to the address the proxy connects to Shelf from. The default is
  the same machine; for a proxy in another Docker container, use its address or the
  network's range, such as `172.16.0.0/12`. Shelf ignores the header from any other address,
  since anyone could send it.

  E-reader apps and the extension usually can't get past a login page. To use them, also
  set `SHELF_PASSWORD`, and have the proxy let through requests that carry an
  `Authorization` header (or the paths `/opds`, `/api/`, `/covers/` and
  `/novel/*/export.epub`). Shelf checks the password on those itself.

Shelf also uses `SHELF_TRUSTED_PROXIES` to decide whose `X-Forwarded-For` and
`X-Forwarded-Proto` headers to believe, so behind a proxy that serves https, set it even
without a login proxy. The OPDS catalog's links then start with `https://`.

Wrong passwords are limited to 10 every 15 minutes from one address. Shelf keeps the key
that signs its sign-in cookies in `secret_key` in the data folder; deleting it also signs
every browser out.

## Releases

GitHub Actions runs ruff and the tests on every push and pull request, and publishes the
`edge` image from `master`. To make a release, set the same version in `pyproject.toml` and
`src/shelf/__init__.py`, commit, then push a tag:

```sh
git tag v0.2.0 && git push origin v0.2.0
```

The tag publishes the image as `0.2.0`, `0.2` and `latest` (and `0` from 1.0 on), and creates
a GitHub release with generated notes, the wheel, the source archive and the browser
extension. A tag with a suffix, like `v0.2.0-rc1`, is marked as a pre-release.

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

## Images in chapters

A chapter shows only images Shelf has a copy of, so none break when a site moves or goes
away:

- Images in the novel's WACZ archive are shown from it; images in an e-book, from the book.
- When the browser extension sends a chapter, Shelf asks it for the chapter's images it has
  no copy of; the extension reads them as the page does (often from the browser's cache)
  and Shelf keeps them with the novel's captures.
- Site decoration is left out: the same picture in three or more chapters (an avatar, the
  covers of other novels), and logos, buttons and banners (Ko-fi, Patreon, Discord, …).
- Other images are left out: their addresses usually stop working.
- **Save missing images** on a novel's page downloads the left-out images that are still
  online, one at a time, as Shelf (it doesn't pretend to be a browser). A site that refuses
  (an error, a challenge page) isn't asked again in that run; send those chapters again
  from the browser extension instead. Addresses on this computer or your local network are
  never fetched.

Chapters keep their images' original addresses, so an image saved later also shows in
chapters captured before.

## Exporting as an EPUB

**Download EPUB** on a novel's page makes an e-book of the novel as Shelf shows it: the
chapters in the list (hidden ones left out), with their edited titles and the novel's trim
rules applied, a page for each part or volume with its chapters under it in the contents,
and the cover, title, authors, genres and description. Images Shelf has a copy of (from the
archive, the e-book or the browser extension) go inside the book; others are left out, as
in the reader.

### Installing Shelf as an app

Shelf can be installed as an app from the browser on a phone or computer: in Chrome or
Edge, *Install app* or *Add to Home screen* in the menu; in Safari on an iPhone or iPad,
*Share* → *Add to Home Screen*. It then opens full screen, without the browser's address bar.

Browsers allow this only over https, or on `localhost`, so on your network serve Shelf through
a reverse proxy with a certificate. The same goes for keeping chapters for offline reading:

- The last 30 chapters you opened stay readable without a connection. When Shelf can't be
  reached, its pages say so and list those chapters. Your place in them isn't saved
  while offline.
- In browsers that don't load the next chapter ahead by themselves (Firefox, Safari, Brave),
  Shelf's service worker fetches it instead. A copy fetched ahead is used only within 30
  minutes, and not after you send a form (a trim rule or a hidden chapter could change it).
- Signing out of Shelf forgets the kept chapters.

### OPDS catalog

E-reader apps that read OPDS catalogs (KOReader, Thorium Reader, Moon+ Reader, Librera,
Panels and others) can browse Shelf and download novels. Add
`http://<your-shelf-address>/opds` as a catalog in the app. It lists:

- **All novels**, by title
- **Recently updated**: novels with chapters added lately first
- **New chapters**: novels with chapters added since you last read them
- each of your reading lists that has novels on it

You can also search by title, other name, author, genre or tag. Each novel downloads as
the same EPUB that **Download EPUB** makes, so it has the chapters as they are now.
Novels with no chapters are left out.

If Shelf has a password (see [Signing in](#signing-in)), give it to the app with any user
name. Without one, anyone who can reach Shelf can download your novels from the catalog.

## How an EPUB becomes a novel

Each `.epub` file in the library folder (or uploaded with **Upload**) is one novel, like a
WACZ archive. Its title, authors, description, subjects, language, publisher, date and cover
become the novel's metadata, until you add NovelUpdates metadata or edit it.

- Chapters follow the book's table of contents, in the book's reading order. An entry can
  start a chapter at an anchor inside a file; files the contents don't list continue the
  chapter before them, unless they start with a heading of their own (books that list a
  whole volume as one entry). A book without usable contents is split at its chapter
  headings or at label lines such as "[Chapter 31]".
- Covers, title pages, copyright pages, the contents page and other short pages are left out.
- When chapter numbers start again (Book 2, Chapter 1), the contents' groups become sections.
- Images stay in the book and are shown from it.
- Books with DRM are refused: Shelf doesn't decrypt protected content.

### Series in volumes

EPUBs together in a folder of the library are volumes of one novel, each volume its own
section ("Volume 3", or "Volume 10: The Martyr Walks Alone" when volumes have their own
subtitles). Only EPUBs directly in the library folder, or in `uploads/`, are novels of their own.

```
library/
  Ishura/
    Ishura - Volume 01.epub
    Ishura - Volume 02.epub
  A Standalone Book.epub
```

- Volumes are ordered by the volume number in their title or file name ("Vol. 2",
  "Volume 08", "Vol. II", a trailing "9"), else by file name.
- The novel is named for the series the volume titles share, else for the folder. Its
  description and cover come from the first volume.
- Adding, removing or replacing a volume re-imports the novel on the next scan; reading
  progress, hidden chapters and your edits stay.
- A volume that can't be read (DRM, a broken file) is left out and named on the novel's
  page; the other volumes are imported.
- To upload, tick **The e-books are volumes of one novel** in **Upload** (they're saved in a
  folder under `uploads/`), or use **Add volumes** on a novel that is a folder of volumes.

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
