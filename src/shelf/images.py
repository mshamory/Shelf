"""Images in chapters. A chapter shows only images Shelf has: ones stored when the
browser extension sent the chapter, ones in the novel's WACZ archive, or ones in its
e-book. Site decoration (logos, donation buttons, the same picture in every chapter) is
left out, and so are images Shelf doesn't have: their addresses usually stop working.

Chapters keep their images' original addresses; they are resolved when shown, so an
image stored later shows up in chapters captured before."""

from __future__ import annotations

import hashlib
import html as html_lib
import re
import threading
from collections import OrderedDict
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
