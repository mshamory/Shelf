"""Novels from EPUB e-books."""

import zipfile

import pytest

from shelf.epub import EpubError, parse_epub

from .conftest import PARA
from .test_app import client  # noqa: F401  (fixture)

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00"
       b"\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
       b"\x00\x00\x00\x00IEND\xaeB`\x82")


def xhtml(body: str, title: str = "Page") -> str:
    return ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
            f'<head><title>{title}</title><script type="text/javascript" src="../js/kobo.js"/>'
            f"</head><body>{body}</body></html>")


def make_epub(path, docs: dict[str, str], toc: list | None, *, ncx: bool = False,
              title: str = "The Long Road", extra: dict | None = None):
    """docs: name -> body, in reading order. toc: (title, href) or (title, [children]) as
    an EPUB 3 nav document, or as an NCX with ncx=True; None for no contents."""
    manifest = "".join(f'<item id="d{i}" href="{n}" media-type="application/xhtml+xml"/>'
                       for i, n in enumerate(docs))
    spine = "".join(f'<itemref idref="d{i}"/>' for i in range(len(docs)))

    def nav_items(items):
        out = ""
        for label, href in items:
            if isinstance(href, list):
                out += f"<li><span>{label}</span><ol>{nav_items(href)}</ol></li>"
            else:
                out += f'<li><a href="{href}">{label}</a></li>'
        return out

    def ncx_points(items, n=[0]):  # noqa: B006
        out = ""
        for label, href in items:
            n[0] += 1
            src = href[0][1] if isinstance(href, list) else href
            kids = ncx_points(href) if isinstance(href, list) else ""
            out += (f'<navPoint id="p{n[0]}"><navLabel><text>{label}</text></navLabel>'
                    f'<content src="{src}"/>{kids}</navPoint>')
        return out

    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        nav_item = ncx_item = spine_attr = ""
        if toc is not None and not ncx:
            nav_item = '<item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>'
            z.writestr("OEBPS/nav.xhtml", xhtml(
                f'<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><ol>'
                f"{nav_items(toc)}</ol></nav>", "Contents"))
        if toc is not None and ncx:
            ncx_item = '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
            spine_attr = ' toc="ncx"'
            z.writestr("OEBPS/toc.ncx", '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">'
                       f"<navMap>{ncx_points(toc)}</navMap></ncx>")
        z.writestr("OEBPS/content.opf",
                   '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   f"<dc:title>{title}</dc:title><dc:creator>A. Writer</dc:creator>"
                   "<dc:language>en</dc:language><dc:subject>Fantasy</dc:subject>"
                   "<dc:description>A long journey.\n\nAnd its end.</dc:description>"
                   "<dc:date>2021-05-01</dc:date></metadata>"
                   f'<manifest>{manifest}{nav_item}{ncx_item}'
                   '<item id="cov" href="images/cover.png" media-type="image/png" '
                   'properties="cover-image"/></manifest>'
                   f"<spine{spine_attr}>{spine}</spine></package>")
        z.writestr("OEBPS/images/cover.png", PNG)
        for name, body in docs.items():
            z.writestr(f"OEBPS/{name}", xhtml(body))
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return path


def text(n: int) -> str:
    return f"<p>{PARA[3:-4]} ({n})</p>" * 2


def test_chapters_follow_the_table_of_contents(tmp_path):
    docs = {
        "cover.xhtml": '<img src="images/cover.png"/>',
        "ch1.xhtml": f"<h1>Chapter 1: Dawn</h1>{text(1)}",
        "ch1b.xhtml": f'<div class="x"><span>Still chapter one.</span></div>{text(11)}',  # split file
        "ch2.xhtml": (f'<h2 id="c2">Chapter 2: Noon</h2>{text(2)}'
                      f'<h2 id="c3">Chapter 3: Dusk</h2><p><img src="images/cover.png"/></p>{text(3)}'),
        "after.xhtml": f"<h1>Afterword</h1>{text(99)}",
    }
    toc = [("Cover", "cover.xhtml"),
           ("Volume 1", [("Chapter 1: Dawn", "ch1.xhtml"), ("Chapter 2: Noon", "ch2.xhtml#c2"),
                         ("Chapter 3: Dusk", "ch2.xhtml#c3")]),
           ("Afterword", "after.xhtml")]
    book = parse_epub(make_epub(tmp_path / "b.epub", docs, toc), "books/b.epub")
    got = [(c.number, c.title) for c in book.chapters]
    assert got == [(1, "Chapter 1: Dawn"), (2, "Chapter 2: Noon"), (3, "Chapter 3: Dusk"),
                   (None, "Afterword")]
    one, two, three = book.chapters[:3]
    assert "Still chapter one." in one.html and "(11)" in one.html  # the split file joins
    assert "<h1>Chapter 1" not in one.html  # the title isn't repeated in the text
    assert "(3)" not in two.html and "(2)" in two.html and "(3)" in three.html
    assert "/epub-image?book=books%2Fb.epub&amp;path=OEBPS%2Fimages%2Fcover.png" in three.html
    assert "<p><span>Still chapter one.</span></p>" in one.html  # a text <div>: a paragraph
    assert book.skipped == ["Cover"]  # too short to be a chapter
    assert book.metadata.title == "The Long Road" and book.metadata.authors == ["A. Writer"]
    assert book.metadata.tags == ["Fantasy"] and book.metadata.year == "2021"
    assert book.metadata.description_html == "<p>A long journey.</p><p>And its end.</p>"
    assert book.cover == (PNG, "image/png")


def test_volumes_restarting_their_numbers_become_sections(tmp_path):
    docs = {f"v{v}c{n}.xhtml": f"<h1>Chapter {n}</h1>{text(n)}" for v in (1, 2) for n in (1, 2)}
    toc = [(f"Book {v}", [(f"Chapter {n}", f"v{v}c{n}.xhtml") for n in (1, 2)]) for v in (1, 2)]
    book = parse_epub(make_epub(tmp_path / "v.epub", docs, toc, ncx=True))
    assert [(c.season, c.season_label, c.number) for c in book.chapters] == [
        (1, "Book 1", 1), (1, "Book 1", 2), (2, "Book 2", 1), (2, "Book 2", 2)]


def test_books_without_structure(tmp_path):
    # No contents; one file holding several chapters marked by label lines only.
    whole = "".join(f"<div><span>[Chapter {n}]</span></div>{text(n)}" for n in (31, 32, 33))
    book = parse_epub(make_epub(tmp_path / "u.epub", {"all.xhtml": whole}, None))
    assert [(c.number, c.title) for c in book.chapters] == [
        (31, "Chapter 31"), (32, "Chapter 32"), (33, "Chapter 33")]
    # A volume listed as one entry, its chapters in files starting with a heading.
    docs = {f"c{n}.xhtml": f"<section><h1>.{n}</h1>{text(n)}</section>" for n in (255, 256, 257)}
    book = parse_epub(make_epub(tmp_path / "w.epub", docs, [("Volume 8", "c255.xhtml")]))
    assert [c.title for c in book.chapters] == ["Chapter 255", "Chapter 256", "Chapter 257"]
    # Numbered scenes inside real chapters are not chapters.
    scenes = "".join(f"<h2>{n}</h2>{text(n)}" for n in (1, 2, 3))
    docs = {"t1.xhtml": "<h1>Chapter 1: Upside Down</h1>", "s1.xhtml": scenes,
            "t2.xhtml": "<h1>Last Chapter: Good-bye</h1>", "s2.xhtml": scenes}
    toc = [("Chapter 1: Upside Down", "t1.xhtml"), ("Last Chapter: Good-bye", "t2.xhtml")]
    book = parse_epub(make_epub(tmp_path / "s.epub", docs, toc))
    assert [c.title for c in book.chapters] == ["Chapter 1: Upside Down", "Last Chapter: Good-bye"]


def test_drm_protected_books_are_refused(tmp_path):
    enc = ('<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
           'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
           '<enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>'
           "</enc:EncryptedData></encryption>")
    path = make_epub(tmp_path / "drm.epub", {"c.xhtml": text(1)}, None,
                     extra={"META-INF/encryption.xml": enc})
    with pytest.raises(EpubError, match="DRM"):
        parse_epub(path)
    # Font obfuscation alone is not DRM.
    fonts = enc.replace("http://www.w3.org/2001/04/xmlenc#aes128-cbc",
                        "http://www.idpf.org/2008/embedding")
    path = make_epub(tmp_path / "fonts.epub", {"c.xhtml": f"<h1>Chapter 1</h1>{text(1)}"}, None,
                     extra={"META-INF/encryption.xml": fonts})
    assert len(parse_epub(path).chapters) == 1


def test_upload_and_read_an_epub(client, tmp_path):  # noqa: F811
    docs = {f"c{n}.xhtml": f'<h1>Chapter {n}</h1><img src="images/cover.png"/>{text(n)}'
            for n in (1, 2, 3)}
    docs["c4.xhtml"] = f"<h1>Afterword</h1>{text(4)}"
    toc = [(f"Chapter {n}", f"c{n}.xhtml") for n in (1, 2, 3)] + [("Afterword", "c4.xhtml")]
    path = make_epub(tmp_path / "Long Road.epub", docs, toc)
    with path.open("rb") as fh:
        r = client.post("/upload", files={"files": ("Long Road.epub", fh)})
    assert "Imported 4 chapters" in r.text and "The Long Road" in r.text
    assert "From the e-book" in r.text and "A. Writer" in r.text
    nid = int(r.url.path.rsplit("/", 1)[1])
    reader = client.get(f"/novel/{nid}/read").text
    assert "Chapter 1" in reader and "From the e-book" in reader
    img = client.get("/epub-image", params={"book": "uploads/Long Road.epub",
                                           "path": "OEBPS/images/cover.png"})
    assert img.status_code == 200 and img.content == PNG and "sandbox" in img.headers[
        "content-security-policy"]
    for bad in ({"book": "uploads/Long Road.epub", "path": "OEBPS/c1.xhtml"},
                {"book": "../secret.epub", "path": "OEBPS/images/cover.png"}):
        assert client.get("/epub-image", params=bad).status_code == 404

    # Editing a chapter keeps the afterword after the last chapter.
    html = client.get(f"/novel/{nid}?hidden=1").text
    import re
    first = re.search(r'id="ch-(\d+)"', html).group(1)
    client.post(f"/chapter/{first}/edit", data={"title": "Chapter 1: Renamed"})
    html = client.get(f"/novel/{nid}").text
    assert html.index("Chapter 3") < html.index("Afterword")
    assert client.post(f"/novel/{nid}/reimport").status_code == 200
