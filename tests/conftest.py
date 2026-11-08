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


def build_wacz(path: Path, with_index: bool = True, with_pages: bool = True,
               title: str = "Night Train Archive") -> Path:
    warc = io.BytesIO()
    writer = WARCWriter(warc, gzip=True)
    cdx = []
    resources = [(u, s, h.encode(), "text/html; charset=utf-8") for u, (s, h) in PAGES.items()]
    resources.append((COVER_URL, 200, COVER_BYTES, "image/jpeg"))
    for i, (url, status, body, ctype) in enumerate(resources):
        http = StatusAndHeaders(f"{status} {'OK' if status == 200 else 'Not Found'}",
                                [("Content-Type", ctype)], protocol="HTTP/1.1")
        rec = writer.create_warc_record(url, "response", payload=io.BytesIO(body),
                                        http_headers=http)
        offset = warc.tell()
        writer.write_record(rec)
        ts = f"2026010100000{i}"
        cdx.append(f"{url} {ts} " + json.dumps({
            "url": url, "mime": ctype.split(";")[0], "status": str(status), "offset": offset,
            "length": warc.tell() - offset, "filename": "data.warc.gz"}))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        z.writestr("archive/data.warc.gz", warc.getvalue())
        if with_index:
            z.writestr("indexes/index.cdx", "\n".join(sorted(cdx)) + "\n")
        if with_pages:
            lines = [json.dumps({"format": "json-pages-1.0", "id": "pages"})]
            for i, (url, (_s, html)) in enumerate(PAGES.items()):
                lines.append(json.dumps({"url": url, "ts": f"2026-01-01T00:00:0{i}Z",
                                         "title": html.split("<title>")[1].split("</title>")[0]}))
            z.writestr("pages/pages.jsonl", "\n".join(lines) + "\n")
        z.writestr("datapackage.json", json.dumps({"title": title, "wacz_version": "1.1.1"}))
    return path


@pytest.fixture
def wacz_path(tmp_path) -> Path:
    return build_wacz(tmp_path / "night-train.wacz")
