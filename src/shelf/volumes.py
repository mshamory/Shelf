"""Novels published in volumes: a folder in the library holding one EPUB per volume is
read as one novel, each volume its own section."""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .epub import URL_SCHEME, EpubError, ParsedBook, parse_epub

log = logging.getLogger("shelf.volumes")

# Uploaded files land here; books uploaded one by one there are separate novels.
UPLOADS_DIR = "uploads"

ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}
_NUM = r"(\d+(?:\.\d+)?|[ivxlc]+)\b"
VOLUME_RE = re.compile(r"[\s,:;.\-–—(\[]*\b(?:vol(?:ume)?|book)\.?\s*" + _NUM, re.I)
TRAILING_NUMBER_RE = re.compile(r"\s+(\d{1,3})\s*$")
SEPARATORS = " ,:;.-–—"


def _roman(s: str) -> int | None:
    total, prev = 0, 0
    for ch in reversed(s.casefold()):
        v = ROMAN[ch]
        total, prev = (total - v, prev) if v < prev else (total + v, v)
    return total or None


def _value(s: str) -> float | None:
    if s[0].isdigit():
        return float(s)
    return _roman(s) if len(s) <= 6 else None


def volume_number(title: str, file_name: str = "") -> tuple[float | None, str, str]:
    """(volume number, the title before it, the subtitle after it), from the book's title
    or else its file name: "Ishura, Vol. 10: The Martyr Walks Alone" ->
    (10, "Ishura", "The Martyr Walks Alone")."""
    for text in (title, Path(file_name).stem):
        if m := VOLUME_RE.search(text):
            if (n := _value(m.group(1))) is not None:
                return n, text[:m.start()].strip(SEPARATORS), text[m.end():].strip(SEPARATORS)
        if m := TRAILING_NUMBER_RE.search(text):  # "... for My Retirement 9"
            return float(m.group(1)), text[:m.start()].strip(SEPARATORS), ""
    return None, title, ""


def is_volume_folder(rel_parent: Path) -> bool:
    """Whether EPUBs in this folder (relative to the library) are volumes of one novel:
    any folder but the library itself and the uploads folder."""
    return rel_parent.parts not in ((), (UPLOADS_DIR,))


@dataclass
class Volume:
    path: Path
    book: ParsedBook
    number: float | None
    series: str
    subtitle: str


def _natural(name: str) -> list:
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", name)]


def _label(v: Volume, i: int, shared_subtitle: bool) -> str:
    if v.number is None:
        return v.book.title
    n = int(v.number) if v.number == int(v.number) else v.number
    return f"Volume {n}" + (f": {v.subtitle}" if v.subtitle and not shared_subtitle else "")


def parse_volumes(folder: Path, rel: str):
    """All EPUBs directly in `folder`, in volume order, as one archive. `rel` is the
    folder's path in the library. Volumes that can't be read are left out and named in
    the result's warnings; none readable raises EpubError."""
    from .library import ParsedArchive

    volumes, failed = [], []
    for p in sorted((p for p in folder.iterdir() if p.suffix.lower() == ".epub"
                     and p.is_file()), key=lambda p: _natural(p.name)):
        try:
            book = parse_epub(p, rel + p.name)
        except Exception as e:  # DRM, a broken file: the other volumes still count
            log.warning("Skipping volume %s: %s", p, e)
            failed.append(f"{p.name}: {e}")
            continue
        number, series, subtitle = volume_number(book.title, p.name)
        volumes.append(Volume(p, book, number, series, subtitle))
    if not volumes:
        raise EpubError("; ".join(failed) or "No e-books in this folder")
    # By volume number; volumes without one keep their file-name order among the rest.
    volumes.sort(key=lambda v: v.number if v.number is not None else float("inf"))

    subtitles = {v.subtitle for v in volumes}
    shared = len(volumes) > 1 and len(subtitles) == 1  # "Perle Vol. I: A Memoir of…"
    chapters, skipped, season = [], [], 0
    for i, v in enumerate(volumes):
        label = _label(v, i, shared)
        inner: dict[int | None, int] = {}
        for c in v.book.chapters:
            # The volume's own sections (Book 1, Book 2) stay sections, within the volume.
            if c.season not in inner:
                season += 1
                inner[c.season] = season
            c.season_label = f"{label} · {c.season_label}" if c.season_label else label
            c.season, c.season_word = inner[c.season], ""
            c.url = f"{URL_SCHEME}{v.path.name}/{c.url.removeprefix(URL_SCHEME)}"
            chapters.append(c)
        skipped += [f"{label}: {s}" for s in v.book.skipped]

    names = Counter(v.series for v in volumes if v.number is not None and v.series)
    # The name most volumes give; between equally common ones, the shortest
    # ("Case File Compendium: Bing An Ben" over "… Bing An Ben (Novel)").
    title = (min(names, key=lambda s: (-names[s], len(s))) if names else "") or folder.name
    first = volumes[0].book
    meta = first.metadata
    if meta.title or meta.authors:
        authors = list(dict.fromkeys(a for v in volumes for a in v.book.metadata.authors))
        meta.title, meta.authors = title, authors
    else:
        meta = None
    return ParsedArchive(archive_title=title, chapters=chapters, metadata=meta,
                         cover=first.cover, skipped=skipped, metadata_source="epub",
                         warnings=[f"Not imported: {f}" for f in failed])
