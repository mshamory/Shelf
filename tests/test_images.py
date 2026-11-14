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
