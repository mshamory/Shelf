"""Exporting a novel as an EPUB."""

import base64
import io
import re
import zipfile

from lxml import etree

from .test_app import client  # noqa: F401  (fixture)
from .test_capture import page
from .test_images import PARA, PNG


def book_files(data: bytes) -> dict[str, bytes]:
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.namelist()[0] == "mimetype" and z.read("mimetype") == b"application/epub+zip"
    assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
    return {n: z.read(n) for n in z.namelist()}


def test_export_epub(client):  # noqa: F811
    nid = client.post("/api/novels", json={"title": "Night Train: Volume/One"}).json()["novel"]["id"]
    art = "https://example.com/uploads/map.png"
    for n in (1, 2, 3):
        extra = f'<p><img src="{art}" alt="Map"><img src="https://example.com/gone.png"></p>' \
            if n == 2 else ""
        r = client.post(f"/api/novels/{nid}/chapters", json={
            "url": f"https://example.com/night-train/chapter-{n}",
            "html": page(n, extra=f"{extra}<p>Tom &amp; Jerry\x0b said WATERMARK.</p>{PARA}")})
    client.post(f"/api/novels/{nid}/images", json={"images": [
        {"url": art, "data": base64.b64encode(PNG).decode()}]})
    client.post("/trim", data={"novel_id": nid, "pattern": "WATERMARK", "mode": "text"})
    html = client.get(f"/novel/{nid}?hidden=1").text
    hide = re.findall(r'action="/chapter/(\d+)/toggle-hidden"', html)[-1]
    client.post(f"/chapter/{hide}/toggle-hidden")

    r = client.get(f"/novel/{nid}/export.epub")
    assert r.status_code == 200 and r.headers["content-type"] == "application/epub+zip"
    assert "Night%20Train%20Volume%20One.epub" in r.headers["content-disposition"]
    files = book_files(r.content)
    for name, data in files.items():  # every page and list is well-formed XML
        if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
            etree.fromstring(data)
    opf = files["OEBPS/content.opf"].decode()
    for href in re.findall(r'href="([^"]+)"', opf):
        assert f"OEBPS/{href}" in files
    chapters = sorted(n for n in files if re.match(r"OEBPS/c\d+\.xhtml", n))
    assert len(chapters) == 2  # the hidden one is left out
    text = b"".join(files[n] for n in chapters).decode()
    assert "Tom &amp; Jerry" in text and "WATERMARK" not in text
    # The saved image is in the book; the one Shelf doesn't have is left out.
    assert re.search(r'<img src="images/i0001\.png" alt="Map"/>', text)
    assert "gone.png" not in text and files["OEBPS/images/i0001.png"] == PNG
    nav = files["OEBPS/nav.xhtml"].decode()
    assert nav.count("<li>") == 2 and "Chapter 1" in nav
    assert "Download EPUB" in client.get(f"/novel/{nid}").text
