"""Images in chapters: Shelf's own copies shown, decoration and lost images left out."""

import base64
import re

from .conftest import PAGES, PARA, write_wacz
from .test_app import client  # noqa: F401  (fixture)
from .test_capture import page

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00"
       b"\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
       b"\x00\x00\x00\x00IEND\xaeB`\x82")
ART = "https://example.com/uploads/illustration-{n}.png"
SITE = '<img src="https://example.com/uploads/NoCover.webp" alt="SHERRY">'


def chapter(n):
    return page(n, extra=f'<p><img src="{ART.format(n=n)}" alt="Map"></p>'
                         f'<p><img src="https://example.com/x/kofi_button.png"></p>'
                         f"{SITE}{PARA}")


def test_images_sent_from_the_browser(client):  # noqa: F811
    r = client.post("/api/novels", json={"title": "Night Train"})
    nid = r.json()["novel"]["id"]
    sent = []
    for n in (1, 2, 3):
        r = client.post(f"/api/novels/{nid}/chapters", json={
            "url": f"https://example.com/night-train/chapter-{n}", "html": chapter(n)})
        sent.append(r.json())
    # Shelf asks for the story's image; the donation button and the picture on every
    # chapter are the site's.
    assert sent[2]["images_wanted"] == [ART.format(n=3)]
    r = client.post(f"/api/novels/{nid}/images", json={"images": [
        {"url": ART.format(n=3), "data": base64.b64encode(PNG).decode()},
        {"url": ART.format(n=2), "data": base64.b64encode(b"<html>not an image").decode()},
        {"url": ART.format(n=1), "data": "%%%"}]})
    assert r.json() == {"saved": 1}

    html = client.get(sent[2]["chapter"]["url"]).text
    src = re.search(r'<img src="(/novel/\d+/image/[0-9a-f]+\.png)" alt="Map">', html).group(1)
    img = client.get(src)
    assert img.content == PNG and "sandbox" in img.headers["content-security-policy"]
    assert "NoCover" not in html and "kofi" not in html
    # An image Shelf has no copy of is left out (its address would stop working).
    assert "<img" not in client.get(sent[1]["chapter"]["url"]).text.split("chapter-body")[1]
    assert client.get(f"/novel/{nid}/image/x.png").status_code == 404


def test_images_in_the_archive(client, tmp_path):  # noqa: F811
    from shelf.config import settings
    from shelf.library import scan_library

    art, lost = "https://example.com/uploads/scene.png", "https://example.com/uploads/gone.png"
    first = next(iter(PAGES))
    records = [{"url": u, "status": s, "ctype": "text/html; charset=utf-8",
                "body": (h.replace("</p>", f'</p><p><img src="{art}"><img src="{lost}"></p>', 1)
                         if u == first else h).encode()}
               for u, (s, h) in PAGES.items()]
    records.append({"url": art, "status": 200, "body": PNG, "ctype": "image/png"})
    pages = [{"url": u, "ts": f"2026-01-01T00:00:0{i}Z",
              "title": h.split("<title>")[1].split("</title>")[0]}
             for i, (u, (_s, h)) in enumerate(PAGES.items())]
    write_wacz(settings.library_dir / "night-train.wacz", records, pages, True, "Night Train")
    scan_library()
    nid = client.get("/api/novels").json()["novels"][0]["id"]
    html = client.get(f"/novel/{nid}?hidden=1").text
    readers = [client.get(f"/chapter/{ch}").text
               for ch in dict.fromkeys(re.findall(r'href="/chapter/(\d+)"', html))]
    reader = next(r for r in readers if "Chapter 3" in r.split("<h1", 1)[-1][:200])
    src = re.search(r'<img src="([^"]+)"', reader).group(1)
    assert src.startswith(f"/novel/{nid}/archive-image?url=") and "gone.png" not in reader
    img = client.get(src.replace("&amp;", "&"))
    assert img.content == PNG and img.headers["content-type"] == "image/png"
    assert client.get(f"/novel/{nid}/archive-image", params={"url": first}).status_code == 404


def test_saving_missing_images(client, monkeypatch):  # noqa: F811
    import time

    import httpx

    from shelf import images

    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if request.url.host == "blocked.example":
            return httpx.Response(403, text="<html>Just a moment…</html>")
        if path.endswith("illustration-1.png"):
            return httpx.Response(302, headers={"location": "/cdn/one.png"})
        if path in ("/cdn/one.png", "/uploads/illustration-2.png"):
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx.Response(404)

    monkeypatch.setattr(images, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(handler), headers={"User-Agent": "Shelf/test"}))
    monkeypatch.setattr(images, "_public", lambda host: True)
    monkeypatch.setattr(images, "REPEATED_IN", 99)

    nid = client.post("/api/novels", json={"title": "Night Train"}).json()["novel"]["id"]
    blocked = '<p><img src="https://blocked.example/a.png"><img src="https://blocked.example/b.png"></p>'
    for n in (1, 2, 3):
        client.post(f"/api/novels/{nid}/chapters", json={
            "url": f"https://example.com/night-train/chapter-{n}",
            "html": page(n, extra=f'<p><img src="{ART.format(n=n)}"></p>{blocked if n == 3 else ""}{PARA}')})
    assert "Save missing images" in client.get(f"/novel/{nid}").text

    r = client.post(f"/novel/{nid}/images/fetch", follow_redirects=False)
    assert r.status_code == 303
    for _ in range(100):
        if not client.get(f"/api/novels/{nid}/images/fetch").json()["running"]:
            break
        time.sleep(0.05)
    s = client.get(f"/api/novels/{nid}/images/fetch").json()
    assert (s["total"], s["saved"], s["failed"]) == (5, 2, 3)
    assert s["refused"] == {"blocked.example": "HTTP 403"}
    # After a refusal, the host's other images aren't asked for.
    assert [r.url.path for r in requests if r.url.host == "blocked.example"] == ["/a.png"]
    first = next(r for r in requests if r.url.path.endswith("illustration-1.png"))
    assert first.headers["referer"] == "https://example.com/night-train/chapter-1"

    page_html = client.get(f"/novel/{nid}").text
    assert "Saved 2 of 5 missing images" in page_html and "blocked.example turned Shelf away" in page_html
    chapters = re.findall(r'href="(/chapter/\d+)"', page_html)
    assert re.search(r'<img src="/novel/\d+/image/[0-9a-f]+\.png"', client.get(chapters[0]).text)


def test_image_addresses_on_this_computer_are_not_fetched():
    import httpx

    from shelf.images import download_image

    def handler(request):
        raise AssertionError(f"fetched {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        for url in ("http://127.0.0.1/a.png", "http://[::1]/a.png", "http://192.168.1.2/a.png",
                    "http://169.254.169.254/latest", "file:///etc/passwd"):
            assert download_image(c, url) is None
