"""The OPDS catalog for e-reader apps."""

import zipfile
from io import BytesIO

from lxml import etree

from .conftest import build_wacz, chapter_html
from .test_app import client  # noqa: F401  (fixture)
from .test_reading import novel_state

NS = {"a": "http://www.w3.org/2005/Atom", "opds": "http://opds-spec.org/2010/catalog",
      "os": "http://a9.com/-/spec/opensearch/1.1/"}


def feed(client, path: str):  # noqa: F811
    r = client.get(path)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/atom+xml;profile=opds-catalog")
    return etree.fromstring(r.content)


def titles(root) -> list[str]:
    return root.xpath("a:entry/a:title/text()", namespaces=NS)


def link(el, rel: str) -> str | None:
    found = el.xpath(f"a:link[@rel='{rel}']/@href", namespaces=NS)
    return found[0] if found else None


def test_catalog(client):  # noqa: F811
    from shelf.config import settings
    from shelf.library import scan_library

    build_wacz(settings.library_dir / "night-train.wacz")
    scan_library()
    client.post("/api/novels", json={"title": "Empty Novel"})  # no chapters: not offered

    root = feed(client, "/opds")
    assert titles(root) == ["All novels", "Recently updated", "New chapters"]
    assert link(root, "search") == "http://testserver/opds/search.xml"
    all_entry = root.xpath("a:entry[1]", namespaces=NS)[0]
    assert link(all_entry, "subsection") == "http://testserver/opds/all"
    assert all_entry.xpath("a:link/@opds:count", namespaces=NS) == ["1"]

    root = feed(client, "/opds/all")
    assert titles(root) == ["The Sample Novel"]
    entry = root.xpath("a:entry", namespaces=NS)[0]
    epub = link(entry, "http://opds-spec.org/acquisition")
    assert epub == "http://testserver/novel/1/export.epub"
    assert entry.xpath("a:summary/text()", namespaces=NS)[0].startswith("4 chapters.")
    r = client.get(epub)
    assert r.headers["content-type"] == "application/epub+zip"
    assert zipfile.ZipFile(BytesIO(r.content)).read("mimetype") == b"application/epub+zip"

    # Reading lists and new chapters.
    _, ids = novel_state(1)
    client.post("/api/progress", json={"chapter_id": ids[0], "position": 0.0})
    assert titles(feed(client, "/opds/new")) == []
    client.post("/api/novels/1/chapters",
                json={"url": "https://example.com/novel/chapter-11/", "html": chapter_html(11)})
    assert titles(feed(client, "/opds/new")) == ["The Sample Novel"]
    root = feed(client, "/opds")
    assert "Reading" in titles(root)
    assert titles(feed(client, "/opds/list/reading")) == ["The Sample Novel"]
    assert client.get("/opds/list/bogus").status_code == 404

    # Search, through the OpenSearch description's template.
    desc = etree.fromstring(client.get("/opds/search.xml").content)
    template = desc.xpath("os:Url/@template", namespaces=NS)[0]
    assert template == "http://testserver/opds/search?q={searchTerms}"
    assert titles(feed(client, "/opds/search?q=sample")) == ["The Sample Novel"]
    assert titles(feed(client, "/opds/search?q=nothing")) == []
    assert titles(feed(client, "/opds/search")) == []


def test_catalog_pages(client, monkeypatch):  # noqa: F811
    from shelf import opds
    from shelf.config import settings
    from shelf.library import scan_library

    monkeypatch.setattr(opds, "PAGE_SIZE", 2)
    for name in ("a", "b", "c"):
        build_wacz(settings.library_dir / f"{name}.wacz")
    scan_library()
    root = feed(client, "/opds/all")
    assert len(titles(root)) == 2
    assert link(root, "next") == "http://testserver/opds/all?page=2"
    assert link(root, "previous") is None
    root = feed(client, "/opds/all?page=2")
    assert len(titles(root)) == 1 and link(root, "next") is None
    assert link(root, "previous") == "http://testserver/opds/all"
    assert root.xpath("os:totalResults/text()", namespaces=NS) == ["3"]
    assert link(feed(client, "/opds/search?q=sample"), "next") == (
        "http://testserver/opds/search?q=sample&page=2")
