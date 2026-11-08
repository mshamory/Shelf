"""Read WACZ (Web Archive Collection Zipped) files.

A WACZ is a zip containing:
  - datapackage.json        package metadata (title, created, ...)
  - pages/pages.jsonl       the pages the user captured (url, title, ts, ...)
  - indexes/index.cdx(.gz)  CDXJ index: urlkey, timestamp, JSON (offset/length into a WARC)
  - archive/*.warc(.gz)     the raw WARC records

Record parsing is delegated to `warcio`; this module only knows how to locate
records inside the zip using the CDXJ index (falling back to a full scan of the
WARCs when no index is present).
"""

from __future__ import annotations

import gzip
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from warcio.archiveiterator import ArchiveIterator


@dataclass
class Page:
    url: str
    title: str = ""
    ts: str = ""


@dataclass
class CdxEntry:
    url: str
    timestamp: str
    mime: str = ""
    status: int = 0
    offset: int = 0
    length: int = 0
    filename: str = ""


@dataclass
class Response:
    url: str
    status: int
    mime: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


def _decode_cdx_line(line: str) -> CdxEntry | None:
    line = line.strip()
    if not line or line.startswith("!"):
        return None
    try:
        _urlkey, ts, rest = line.split(" ", 2)
        data = json.loads(rest)
    except ValueError:
        return None
    try:
        status = int(data.get("status") or 0)
    except ValueError:
        status = 0
    return CdxEntry(
        url=data.get("url", ""),
        timestamp=ts,
        mime=(data.get("mime") or "").split(";")[0].strip().lower(),
        status=status,
        offset=int(data.get("offset") or 0),
        length=int(data.get("length") or 0),
        filename=data.get("filename", ""),
    )


class WaczFile:
    """Random-access reader for a single WACZ file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._zip = zipfile.ZipFile(self.path)
        self._names = set(self._zip.namelist())
        self._index: dict[str, list[CdxEntry]] | None = None

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> "WaczFile":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- metadata ---------------------------------------------------------

    def datapackage(self) -> dict:
        if "datapackage.json" not in self._names:
            return {}
        try:
            return json.loads(self._zip.read("datapackage.json"))
        except ValueError:
            return {}

    @property
    def title(self) -> str:
        return (self.datapackage().get("title") or "").strip()

    def pages(self) -> list[Page]:
        """Pages listed in pages/*.jsonl (the user's own captures first)."""
        out: list[Page] = []
        seen: set[str] = set()
        for name in ("pages/pages.jsonl", "pages/extraPages.jsonl"):
            if name not in self._names:
                continue
            with self._zip.open(name) as fh:
                for raw in io.TextIOWrapper(fh, encoding="utf-8", errors="replace"):
                    try:
                        d = json.loads(raw)
                    except ValueError:
                        continue
                    url = d.get("url")
                    if not url or "format" in d or url in seen:
                        continue
                    seen.add(url)
                    out.append(Page(url=url, title=d.get("title") or "", ts=d.get("ts") or ""))
        return out

    # -- index ------------------------------------------------------------

    def _index_lines(self) -> Iterator[str]:
        for name in sorted(self._names):
            if not name.startswith("indexes/"):
                continue
            if name.endswith((".cdx.gz", ".cdxj.gz")):
                # ZipNum indexes are concatenated gzip members; gzip handles that.
                data = gzip.decompress(self._zip.read(name))
            elif name.endswith((".cdx", ".cdxj")):
                data = self._zip.read(name)
            else:
                continue
            yield from data.decode("utf-8", errors="replace").splitlines()

    def index(self) -> dict[str, list[CdxEntry]]:
        """Map of URL -> captures (sorted oldest first)."""
        if self._index is None:
            idx: dict[str, list[CdxEntry]] = {}
            for line in self._index_lines():
                entry = _decode_cdx_line(line)
                if entry and entry.url:
                    idx.setdefault(entry.url, []).append(entry)
            if not idx:
                idx = self._scan_warcs()
            for entries in idx.values():
                entries.sort(key=lambda e: e.timestamp)
            self._index = idx
        return self._index

    def _warc_names(self) -> list[str]:
        return sorted(
            n for n in self._names if n.startswith("archive/") and n.endswith((".warc", ".warc.gz"))
        )

    def _scan_warcs(self) -> dict[str, list[CdxEntry]]:
        """Build an index by iterating every record (for WACZs without an index)."""
        idx: dict[str, list[CdxEntry]] = {}
        for name in self._warc_names():
            with self._zip.open(name) as fh:
                it = ArchiveIterator(fh)
                for rec in it:
                    if rec.rec_type not in ("response", "resource"):
                        continue
                    url = rec.rec_headers.get_header("WARC-Target-URI") or ""
                    ts = (rec.rec_headers.get_header("WARC-Date") or "").replace("-", "")
                    status, mime = 200, ""
                    if rec.http_headers is not None:
                        try:
                            status = int(rec.http_headers.get_statuscode() or 0)
                        except ValueError:
                            status = 0
                        mime = rec.http_headers.get_header("Content-Type") or ""
                    else:
                        mime = rec.rec_headers.get_header("Content-Type") or ""
                    offset = it.get_record_offset()
                    rec.content_stream().read()  # advance so the length is known
                    idx.setdefault(url, []).append(
                        CdxEntry(
                            url=url,
                            timestamp=ts,
                            mime=mime.split(";")[0].strip().lower(),
                            status=status,
                            offset=offset,
                            length=it.get_record_length(),
                            filename=name.removeprefix("archive/"),
                        )
                    )
        return idx

    def urls(self) -> list[str]:
        return list(self.index().keys())

    def best_capture(self, url: str) -> CdxEntry | None:
        """The newest successful capture of a URL (or newest of any status)."""
        entries = [e for e in self.index().get(url, []) if e.mime != "warc/revisit"]
        if not entries:
            return None
        ok = [e for e in entries if 200 <= e.status < 300]
        return (ok or entries)[-1]

    # -- records ----------------------------------------------------------

    def read(self, entry: CdxEntry) -> Response | None:
        name = entry.filename if entry.filename.startswith("archive/") else f"archive/{entry.filename}"
        if name not in self._names:
            return None
        with self._zip.open(name) as fh:
            fh.seek(entry.offset)
            chunk = fh.read(entry.length) if entry.length else fh.read()
        for rec in ArchiveIterator(io.BytesIO(chunk)):
            if rec.rec_type not in ("response", "resource"):
                continue
            headers: dict[str, str] = {}
            status = 200
            if rec.http_headers is not None:
                headers = {k.lower(): v for k, v in rec.http_headers.headers}
                try:
                    status = int(rec.http_headers.get_statuscode() or 0)
                except ValueError:
                    status = 0
            mime = headers.get("content-type") or rec.rec_headers.get_header("Content-Type") or ""
            return Response(
                url=rec.rec_headers.get_header("WARC-Target-URI") or entry.url,
                status=status,
                mime=mime.split(";")[0].strip().lower(),
                body=rec.content_stream().read(),
                headers=headers,
            )
        return None

    def get(self, url: str) -> Response | None:
        entry = self.best_capture(url)
        return self.read(entry) if entry else None
