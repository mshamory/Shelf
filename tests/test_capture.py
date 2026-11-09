"""The browser extension's API: novels from NovelUpdates pages, chapters sent as displayed."""

from .conftest import FIXTURES, PARA, build_wacz
from .test_app import client  # noqa: F401  (fixture)

NU_URL = "https://www.novelupdates.com/series/night-train/"


def page(n: int, extra: str = "", title: str | None = None) -> str:
    title = title or f"Night Train - Chapter {n}: Stop {n} | Example Reads"
    return (f"<html><head><title>{title}</title>"
            '<meta property="og:site_name" content="Example Reads"></head><body>'
            f"<nav>Home | Novels</nav><article><h1>Chapter {n}: Stop {n}</h1>"
            f"{PARA}{PARA.replace('night', 'dawn')}{extra}</article>"
            "<footer>Comments</footer></body></html>")


def test_create_novel_from_novelupdates_and_add_chapters(client):  # noqa: F811
    nu = (FIXTURES / "nu_series.html").read_text()
    r = client.post("/api/novels", json={"novelupdates": {"url": NU_URL, "html": nu}})
    assert r.status_code == 201, r.text
    novel = r.json()["novel"]
    assert novel["title"] and not novel["has_archive"]
    # The same series page again finds the novel instead of creating another.
    again = client.post("/api/novels", json={"novelupdates": {"url": NU_URL, "html": nu}})
    assert again.json() == {"novel": again.json()["novel"], "created": False}
    assert again.json()["novel"]["id"] == novel["id"]

    nid = novel["id"]
    url = "https://example.com/night-train/chapter-2"
    preview = client.post(f"/api/novels/{nid}/chapters",
                          json={"url": url, "html": page(2), "dry_run": True}).json()
    assert preview["action"] == "added" and preview["chapter"]["id"] is None
    assert preview["chapter"]["title"] == "Chapter 2: Stop 2"
    assert preview["chapter"]["number"] == 2

    r = client.post(f"/api/novels/{nid}/chapters", json={"url": url, "html": page(2)})
    assert r.status_code == 200 and r.json()["action"] == "added"
    client.post(f"/api/novels/{nid}/chapters",
                json={"url": "https://example.com/night-train/chapter-1", "html": page(1)})
    # Sending a chapter again replaces the earlier capture.
    r = client.post(f"/api/novels/{nid}/chapters", json={"url": url, "html": page(2, PARA)})
    assert r.json()["action"] == "updated"

    html = client.get(f"/novel/{nid}").text
    assert html.index("Chapter 1: Stop 1") < html.index("Chapter 2: Stop 2")
    assert "Re-import" not in html
    reader = client.get(r.json()["chapter"]["url"]).text
    assert "dawn" in reader and "Comments" not in reader and "Home | Novels" not in reader

    # The picker suggests this novel for the next chapter on that site.
    picks = client.get("/api/novels", params={"url": "https://example.com/night-train/chapter-3"})
    assert picks.json()["novels"][0]["id"] == nid


def test_captured_chapter_replaces_archive_chapter_and_survives_reimport(client):  # noqa: F811
    from shelf.config import settings
    from shelf.library import scan_library

    build_wacz(settings.library_dir / "night-train.wacz")  # chapters 1, 2, 3, 10
    scan_library()
    picks = client.get("/api/novels", params={"url": "https://example.com/night-train/ch-4"})
    nid = picks.json()["novels"][0]["id"]

    r = client.post(f"/api/novels/{nid}/chapters", json={
        "url": "https://elsewhere.example/nt/2", "html": page(2, title="Chapter 2: Stop 2")})
    assert r.json()["action"] == "replaced", r.text
    client.post(f"/api/novels/{nid}/chapters", json={
        "url": "https://elsewhere.example/nt/5", "html": page(5, title="Chapter 5: Stop 5")})

    assert client.post(f"/novel/{nid}/reimport").status_code in (200, 303)
    html = client.get(f"/novel/{nid}").text
    assert html.count("Stop 2") == 1 and "Stop 5" in html  # the capture won, nothing duplicated
    assert html.index("Stop 2") < html.index("Stop 5")
    assert "Missing: chapters 6–9" in html

    # If the archive disappears, captured chapters stay (the rest goes).
    (settings.library_dir / "night-train.wacz").unlink()
    scan_library()
    html = client.get(f"/novel/{nid}").text
    assert "Stop 2" in html and "Stop 5" in html and "Pages sent from the browser" in html


def test_placeholder_and_error_pages_are_refused(client):  # noqa: F811
    nid = client.post("/api/novels", json={"title": "Night Train"}).json()["novel"]["id"]
    loading = ("<html><head><title>Chapter 9</title></head><body><article>"
               "<p>Verifying access…</p>" + "<p>Theme settings and colours for reading.</p>" * 10
               + "</article></body></html>")
    r = client.post(f"/api/novels/{nid}/chapters", json={"url": "https://ex.com/9", "html": loading})
    assert r.status_code == 422 and "finished loading" in r.json()["detail"]
    bot = "<html><head><title>Just a moment...</title></head><body>" + PARA * 3 + "</body></html>"
    r = client.post(f"/api/novels/{nid}/chapters", json={"url": "https://ex.com/9", "html": bot})
    assert r.status_code == 422


def test_cors_allows_extension_origins(client):  # noqa: F811
    r = client.options("/api/novels", headers={
        "Origin": "moz-extension://abc", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert r.headers.get("access-control-allow-origin") == "moz-extension://abc"
    r = client.options("/api/novels", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers


def test_extension_zip(client):  # noqa: F811
    import io
    import json
    import zipfile

    z = zipfile.ZipFile(io.BytesIO(client.get("/extension.zip").content))
    assert {"manifest.json", "popup.html", "popup.js", "capture.js"} <= set(z.namelist())
    assert json.loads(z.read("manifest.json"))["manifest_version"] == 3


def test_header_metadata_and_author_card_are_trimmed(client):  # noqa: F811
    nid = client.post("/api/novels", json={"title": "Night Train"}).json()["novel"]["id"]
    html = ("<html><head><title>Chapter 14 - Night Train | Example Reads</title></head><body>"
            "<main><article><h2><span>Chapter 14</span></h2>"
            "<p><span>12 min read</span> <span>·</span> <span>11 Oct 2026</span></p>"
            "<p>Chapter 14: Trample the Gates</p>" + PARA + PARA.replace("night", "dusk")
            + '<div><img src="https://ex.com/a.jpg" alt="akaza">'
            "<p>Meet the author</p><a href='/user/a'>akaza</a></div>"
            "<p>Water is Good, Akaza is Water.</p></article></main></body></html>")
    r = client.post(f"/api/novels/{nid}/chapters",
                    json={"url": "https://ex.com/s/14", "html": html}).json()
    assert r["chapter"]["title"] == "Chapter 14: Trample the Gates"
    reader = client.get(r["chapter"]["url"]).text
    assert "dusk" in reader
    assert "Meet the author" not in reader and "Akaza is Water" not in reader
    assert "min read" not in reader and "a.jpg" not in reader


def test_reimport_reextracts_captures(client, monkeypatch):  # noqa: F811
    from shelf import capture
    from shelf.config import settings
    from shelf.library import scan_library

    build_wacz(settings.library_dir / "night-train.wacz")
    scan_library()
    nid = client.get("/api/novels").json()["novels"][0]["id"]
    client.post(f"/api/novels/{nid}/chapters", json={
        "url": "https://elsewhere.example/nt/5", "html": page(5, title="Chapter 5: Stop 5")})
    real = capture.extract_page

    def improved(url, html, series):
        c = real(url, html, series)
        c.title += " (improved)"
        return c

    monkeypatch.setattr(capture, "extract_page", improved)
    client.post(f"/novel/{nid}/reimport")
    assert "Chapter 5: Stop 5 (improved)" in client.get(f"/novel/{nid}").text
