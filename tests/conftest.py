import io
import json
import zipfile
from pathlib import Path

import pytest
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

FIXTURES = Path(__file__).parent / "fixtures"
PARA = "<p>" + "The train rattled on through the endless night. " * 12 + "</p>"


def chapter_html(n: int, site: str = "Example Translations") -> str:
    return f"""<!doctype html><html><head><title>Chapter {n}: Night Train – {site}</title></head>
<body><header><nav><a href="/">Home</a> <a href="/novels">Novels</a></nav></header>
<main><article><h1 class="entry-title">Chapter {n}: Night Train</h1>
<div class="entry-content">{PARA * 3}<p>End of chapter {n}.</p></div></article></main>
<footer>Share this post · Like · Published by translator</footer></body></html>"""


PAGES = {
    # url -> (status, html); listed out of order on purpose
    "https://example.com/novel/chapter-3/": (200, chapter_html(3)),
    "https://example.com/novel/chapter-1/": (200, chapter_html(1)),
    "https://example.com/novel/chapter-2/": (200, chapter_html(2)),
    "https://example.com/novel/chapter-10/": (200, chapter_html(10)),
    "https://example.com/novel/chapter-4-2/": (
        404, "<html><head><title>Page not found – Example Translations</title></head><body>Oops</body></html>"
    ),
    "https://www.novelupdates.com/series/the-sample-novel/": (
        200, (FIXTURES / "nu_series.html").read_text()
    ),
}
COVER_URL = "https://cdn.novelupdates.com/images/2024/01/sample.jpg"
COVER_BYTES = b"\xff\xd8\xff\xe0fakejpeg"


def write_wacz(path: Path, records: list[dict], pages: list[dict] | None,
               with_index: bool = True, title: str = "Night Train Archive") -> Path:
    """records: {url, status, body, ctype, [method], [ts]}; pages: pages.jsonl rows."""
    warc = io.BytesIO()
    writer = WARCWriter(warc, gzip=True)
    cdx = []
    for i, r in enumerate(records):
        status = r["status"]
        http = StatusAndHeaders(f"{status} {'OK' if status == 200 else 'Error'}",
                                [("Content-Type", r["ctype"])], protocol="HTTP/1.1")
        rec = writer.create_warc_record(r["url"], "response", payload=io.BytesIO(r["body"]),
                                        http_headers=http)
        offset = warc.tell()
        writer.write_record(rec)
        data = {"url": r["url"], "mime": r["ctype"].split(";")[0], "status": str(status),
                "offset": offset, "length": warc.tell() - offset, "filename": "data.warc.gz"}
        if r.get("method"):
            data |= {"method": r["method"], "requestBody": "tok=abc"}
        cdx.append(f"{r['url']} {r.get('ts', f'202601010000{i:02d}')} {json.dumps(data)}")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        z.writestr("archive/data.warc.gz", warc.getvalue())
        if with_index:
            z.writestr("indexes/index.cdx", "\n".join(sorted(cdx)) + "\n")
        if pages is not None:
            lines = [json.dumps({"format": "json-pages-1.0", "id": "pages"})]
            lines += [json.dumps(row) for row in pages]
            z.writestr("pages/pages.jsonl", "\n".join(lines) + "\n")
        z.writestr("datapackage.json", json.dumps({"title": title, "wacz_version": "1.1.1"}))
    return path


def build_wacz(path: Path, with_index: bool = True, with_pages: bool = True,
               title: str = "Night Train Archive") -> Path:
    records = [
        {"url": u, "status": s, "body": h.encode(), "ctype": "text/html; charset=utf-8"}
        for u, (s, h) in PAGES.items()
    ]
    records.append({"url": COVER_URL, "status": 200, "body": COVER_BYTES, "ctype": "image/jpeg"})
    pages = [
        {"url": u, "ts": f"2026-01-01T00:00:0{i}Z",
         "title": h.split("<title>")[1].split("</title>")[0]}
        for i, (u, (_s, h)) in enumerate(PAGES.items())
    ]
    return write_wacz(path, records, pages if with_pages else None, with_index, title)


@pytest.fixture
def wacz_path(tmp_path) -> Path:
    return build_wacz(tmp_path / "night-train.wacz")
