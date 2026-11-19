"""Images in chapters. A chapter shows only images Shelf has: ones stored when the
browser extension sent the chapter, ones in the novel's WACZ archive, or ones in its
e-book. Site decoration (logos, donation buttons, the same picture in every chapter) is
left out, and so are images Shelf doesn't have: their addresses usually stop working.
"Save missing images" on a novel's page downloads the ones still online.

Chapters keep their images' original addresses; they are resolved when shown, so an
image stored later shows up in chapters captured before."""

from __future__ import annotations

import hashlib
import html as html_lib
import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

from sqlmodel import Session, func, select

from .config import settings
from .db import Chapter, Novel, has_archive
from .library import captures_dir

IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
SRC_RE = re.compile(r"""\ssrc\s*=\s*(["'])(.*?)\1""", re.I | re.S)
ALT_RE = re.compile(r"""\salt\s*=\s*(["'])(.*?)\1""", re.I | re.S)
# File names and alt texts of site decoration rather than story images.
DECOR_RE = re.compile(
    r"logo|button|banner|ko-?fi|discord|donat|paypal|patreon|merch|avatar|gravatar|"
    r"no-?cover|badge|icon|emoji|smiley|spinner|loading|placeholder|cup-border|sidebar",
    re.I)
REPEATED_IN = 3  # an image in this many chapters is the site's, not the story's
MAX_IMAGE_BYTES = 20 * 1024 * 1024
STORED_NAME_RE = re.compile(r"^[0-9a-f]{40}\.[a-z0-9]{1,5}$")
MIME_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp",
            "image/avif": "avif", "image/svg+xml": "svg", "image/bmp": "bmp"}


def _key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()


def images_dir(novel_id: int) -> Path:
    return captures_dir(novel_id) / "images"


def stored_image(novel_id: int, url: str) -> Path | None:
    folder = images_dir(novel_id)
    if not folder.is_dir():
        return None
    return next(folder.glob(f"{_key(url)}.*"), None)


def _sniff(data: bytes) -> str | None:
    """The image type the bytes are, whatever the sender says."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    if data.startswith(b"BM"):
        return "image/bmp"
    head = data[:512].lstrip().lower()
    if head.startswith((b"<svg", b"<?xml")) and b"<svg" in data[:2048].lower():
        return "image/svg+xml"
    return None


def save_image(novel_id: int, url: str, data: bytes) -> bool:
    """Store an image of a chapter (sent by the browser extension). Only real images."""
    mime = _sniff(data)
    if mime is None or len(data) > MAX_IMAGE_BYTES or not url.startswith(("http://", "https://")):
        return False
    folder = images_dir(novel_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{_key(url)}.{MIME_EXT[mime]}").write_bytes(data)
    return True


# -- images in the novel's archive -------------------------------------------

_open: OrderedDict[tuple[str, float], object] = OrderedDict()
_lock = threading.Lock()


def _archive(novel: Novel):
    """The novel's WACZ, kept open (its index is costly to read) for the few most recently
    read novels."""
    from .wacz import WaczFile

    if not has_archive(novel) or not novel.wacz_path.lower().endswith(".wacz"):
        return None
    path = settings.library_dir / novel.wacz_path
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return None
    with _lock:
        w = _open.get(key)
        if w is None:
            try:
                w = WaczFile(path)
            except Exception:
                return None
            _open[key] = w
            while len(_open) > 4:
                _open.popitem(last=False)[1].close()
        _open.move_to_end(key)
    return w


def archived_image(novel: Novel, url: str):
    """The archive's capture of an image, if it has a good one."""
    w = _archive(novel)
    if w is None:
        return None
    try:
        with _lock:
            entry = w.best_capture(url)
    except Exception:
        return None
    if entry is None or entry.status not in (0, 200) or not (
            entry.mime or "image/").startswith("image/"):
        return None
    return entry


def read_archived_image(novel: Novel, url: str) -> tuple[bytes, str] | None:
    entry = archived_image(novel, url)
    if entry is None:
        return None
    w = _archive(novel)
    with _lock:
        resp = w.read(entry)
    if resp is None or resp.status != 200 or not resp.body:
        return None
    mime = _sniff(resp.body)
    return (resp.body, mime) if mime else None


# -- resolving a chapter's images --------------------------------------------


_counts: dict[int, tuple[tuple, dict[str, int]]] = {}


def _image_counts(session: Session, novel_id: int) -> dict[str, int]:
    """How many of the novel's chapters show each image, counted once until its chapters
    change."""
    stamp = tuple(session.exec(select(func.count(Chapter.id), func.max(Chapter.id),
                                      func.sum(Chapter.char_count))
                               .where(Chapter.novel_id == novel_id)).one())
    cached = _counts.get(novel_id)
    if cached and cached[0] == stamp:
        return cached[1]
    counts: dict[str, int] = {}
    for html in session.exec(select(Chapter.content_html).where(
            Chapter.novel_id == novel_id, func.instr(Chapter.content_html, "<img") > 0)):
        for src in {src for _m, src, _a in _images(html)}:
            counts[src] = counts.get(src, 0) + 1
    _counts[novel_id] = (stamp, counts)
    return counts


def _decorative(session: Session, novel_id: int, src: str, alt: str) -> bool:
    name = urlsplit(src).path.rsplit("/", 1)[-1]
    if DECOR_RE.search(name) or DECOR_RE.search(alt):
        return True
    return _image_counts(session, novel_id).get(src, 0) >= REPEATED_IN


def _images(html: str):
    for m in IMG_RE.finditer(html):
        src = SRC_RE.search(m.group(0))
        alt = ALT_RE.search(m.group(0))
        yield (m, html_lib.unescape(src.group(2)).strip() if src else "",
               html_lib.unescape(alt.group(2)) if alt else "")


def resolve_images(session: Session, novel: Novel, html: str) -> str:
    """The chapter's HTML as shown: images from Shelf's own copies, decoration and images
    it doesn't have left out."""
    if "<img" not in html:
        return html
    out, last = [], 0
    for m, src, alt in _images(html):
        out.append(html[last:m.start()])
        last = m.end()
        tag = m.group(0)
        if src.startswith("/epub-image?"):
            out.append(tag)
            continue
        if not src.startswith(("http://", "https://")) or _decorative(
                session, novel.id, src, alt):
            continue
        if (p := stored_image(novel.id, src)) is not None:
            local = f"/novel/{novel.id}/image/{p.name}"
        elif archived_image(novel, src) is not None:
            local = f"/novel/{novel.id}/archive-image?url={quote(src, safe='')}"
        else:
            continue
        new_src = f' src="{html_lib.escape(local)}"'
        out.append(SRC_RE.sub(lambda _m, v=new_src: v, tag, count=1))
    out.append(html[last:])
    return "".join(out)


def wanted_images(session: Session, novel: Novel, html: str) -> list[str]:
    """Images of a chapter Shelf has no copy of, for the browser extension to send."""
    wanted = []
    for _m, src, alt in _images(html):
        if (src.startswith(("http://", "https://")) and src not in wanted
                and stored_image(novel.id, src) is None
                and not _decorative(session, novel.id, src, alt)
                and archived_image(novel, src) is None):
            wanted.append(src)
    return wanted


# -- downloading images Shelf has no copy of ---------------------------------
#
# A plain HTTP client that says who it is. A host that refuses (403, 429, a challenge page
# instead of an image) is left alone for the rest of the run, never worked around.

FETCH_TIMEOUT = 20.0
MAX_REDIRECTS = 5
REFUSED = (401, 403, 429, 503)


class Refused(Exception):
    """The host turned the request away."""


def _public(host: str) -> bool:
    """Whether every address the host has is on the public internet (chapters are
    untrusted: an image address must not reach this computer or its network)."""
    import ipaddress
    import socket

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0].split("%")[0]).is_global
                               for i in infos)


def _client():
    import httpx

    from .novelupdates import USER_AGENT

    return httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=False,
                        headers={"User-Agent": USER_AGENT, "Accept": "image/*"})


def download_image(client, url: str, referer: str = "") -> bytes | None:
    """The image at `url`, or None. Raises Refused when the host turns Shelf away."""
    import httpx

    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or not _public(
                parts.hostname):
            return None
        headers = {"Referer": referer} if referer.startswith(("http://", "https://")) else {}
        try:
            with client.stream("GET", url, headers=headers) as r:
                if r.is_redirect and (loc := r.headers.get("location")):
                    url = str(httpx.URL(url).join(loc))
                    continue
                if r.status_code in REFUSED:
                    raise Refused(f"HTTP {r.status_code}")
                if r.status_code != 200:
                    return None
                data = bytearray()
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > MAX_IMAGE_BYTES:
                        return None
        except httpx.HTTPError:
            return None
        if _sniff(bytes(data)) is None:
            if b"<html" in data[:2048].lower():  # a challenge or error page, not the image
                raise Refused("a web page instead of the image")
            return None
        return bytes(data)
    return None


def missing_images(session: Session, novel: Novel) -> list[tuple[str, str]]:
    """(image address, chapter address) for every story image in the novel's shown
    chapters that Shelf has no copy of."""
    found: dict[str, str] = {}
    for html, page in session.exec(select(Chapter.content_html, Chapter.url).where(
            Chapter.novel_id == novel.id, Chapter.hidden == False,  # noqa: E712
            func.instr(Chapter.content_html, "<img") > 0).order_by(Chapter.position)):
        for src in wanted_images(session, novel, html):
            found.setdefault(src, page)
    return list(found.items())


@dataclass
class ImageFetch:
    running: bool = False
    total: int = 0
    done: int = 0
    saved: int = 0
    failed: int = 0
    refused: dict[str, str] = field(default_factory=dict)  # host -> why
    finished: str = ""


_fetches: dict[int, ImageFetch] = {}
_fetch_lock = threading.Lock()


def fetch_status(novel_id: int) -> ImageFetch | None:
    return _fetches.get(novel_id)


def fetch_missing_images(novel_id: int) -> None:
    """Download the novel's missing images, one at a time, recording progress."""
    import time

    from .db import get_engine, utcnow

    status = _fetches[novel_id]
    try:
        with Session(get_engine()) as session:
            novel = session.get(Novel, novel_id)
            todo = missing_images(session, novel) if novel else []
        status.total = len(todo)
        with _client() as client:
            last: dict[str, float] = {}
            for url, page in todo:
                host = urlsplit(url).hostname or ""
                if host in status.refused:
                    status.failed += 1
                else:
                    # A pause between requests to the same host.
                    if (wait := last.get(host, 0) + 0.5 - time.monotonic()) > 0:
                        time.sleep(wait)
                    try:
                        data = download_image(client, url, page)
                    except Refused as e:
                        status.refused[host], data = str(e), None
                    last[host] = time.monotonic()
                    if data is not None and save_image(novel_id, url, data):
                        status.saved += 1
                    else:
                        status.failed += 1
                status.done += 1
    except Exception:
        logging.getLogger("shelf.images").exception("Saving images of novel %s", novel_id)
    finally:
        status.running = False
        status.finished = utcnow().isoformat(timespec="seconds")


def start_image_fetch(novel_id: int) -> bool:
    with _fetch_lock:
        if (s := _fetches.get(novel_id)) and s.running:
            return False
        _fetches[novel_id] = ImageFetch(running=True)
    threading.Thread(target=fetch_missing_images, args=(novel_id,), daemon=True).start()
    return True


# -- the chapter editor ------------------------------------------------------

PLACEHOLDER = "/static/missing-image.svg"
ORIGINAL_ATTR = "data-shelf-src"
NOTE_ATTR = "data-shelf-note"  # the tooltip is the editor's, not the chapter's
TITLE_RE = re.compile(r"""\stitle\s*=\s*(["'])(.*?)\1""", re.I | re.S)
NOTE_RE = re.compile(r"\s" + NOTE_ATTR + r"""(?:\s*=\s*(["'])\1)?""", re.I)
ORIGINAL_RE = re.compile(r"""\s""" + ORIGINAL_ATTR + r"""\s*=\s*(["'])(.*?)\1""", re.I | re.S)


def editable_images(session: Session, novel: Novel, html: str) -> str:
    """The chapter's HTML for the editor: every image kept (so saving loses none), shown
    from Shelf's copy or as a placeholder, its original address alongside."""
    if "<img" not in html:
        return html
    out, last = [], 0
    for m, src, alt in _images(html):
        out.append(html[last:m.start()])
        last = m.end()
        tag = m.group(0)
        if not src.startswith(("http://", "https://")):
            out.append(tag)
            continue
        shown = resolve_images(session, novel, tag)
        local = SRC_RE.search(shown) if shown else None
        new_src = local.group(2) if local else PLACEHOLDER
        title = "" if local or TITLE_RE.search(tag) else (
            f' {NOTE_ATTR}="" title="' + html_lib.escape(
                ("Site decoration, not shown when reading: " if _decorative(
                    session, novel.id, src, alt) else "Shelf has no copy of this image: ")
                + src) + '"')
        attrs = (f' src="{html_lib.escape(html_lib.unescape(new_src))}"'
                 f' {ORIGINAL_ATTR}="{html_lib.escape(src)}"{title}')
        out.append(SRC_RE.sub(lambda _m, v=attrs: v, tag, count=1))
    out.append(html[last:])
    return "".join(out)


def restore_image_sources(html: str) -> str:
    """Edited HTML back to the images' original addresses."""
    def original(m: re.Match) -> str:
        tag = m.group(0)
        if o := ORIGINAL_RE.search(tag):
            tag = ORIGINAL_RE.sub("", tag)
            tag = SRC_RE.sub(lambda _m: f' src="{o.group(2)}"', tag, count=1)
        if NOTE_RE.search(tag):
            tag = TITLE_RE.sub("", NOTE_RE.sub("", tag), count=1)
        return tag

    return IMG_RE.sub(original, html)
