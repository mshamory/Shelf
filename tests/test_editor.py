"""Editing a chapter's text."""

import base64
import re

from .test_app import client  # noqa: F401  (fixture)
from .test_capture import build_wacz, page
from .test_images import PNG


def chapter_ids(client, nid):  # noqa: F811
    html = client.get(f"/novel/{nid}?hidden=1").text
    return list(dict.fromkeys(re.findall(r'href="/chapter/(\d+)"', html)))


def editor_html(client, cid):  # noqa: F811
    html = client.get(f"/chapter/{cid}/text").text
    return re.search(r'id="editor"[^>]*>(.*?)</div>\s*<textarea', html, re.S).group(1)


def test_edit_chapter_text(client):  # noqa: F811
    nid = client.post("/api/novels", json={"title": "Night Train"}).json()["novel"]["id"]
    art, gone = "https://example.com/uploads/map.png", "https://example.com/uploads/gone.png"
    r = client.post(f"/api/novels/{nid}/chapters", json={
        "url": "https://example.com/night-train/chapter-1",
        "html": page(1, extra=f'<p>The trian left.</p><p>Read on Example Reads!</p>'
                              f'<p><img src="{art}" alt="Map"><img src="{gone}"></p>')})
    cid = r.json()["chapter"]["id"]
    client.post(f"/api/novels/{nid}/images", json={"images": [
        {"url": art, "data": base64.b64encode(PNG).decode()}]})
    assert f"/chapter/{cid}/text" in client.get(f"/chapter/{cid}").text

    # The editor keeps every image: Shelf's copy shown, the other as a placeholder.
    text = editor_html(client, cid)
    assert re.search(r'<img src="/novel/\d+/image/[0-9a-f]+\.png" data-shelf-src="'
                     + re.escape(art) + '"', text)
    assert re.search(r'src="/static/missing-image.svg" data-shelf-src="' + re.escape(gone), text)

    # Unchanged text saves nothing.
    r = client.post(f"/chapter/{cid}/text", data={"html": text})
    assert "No changes" in r.text

    # Fix a typo and remove a line, as the browser would send it (with a script slipped in).
    edited = (text.replace("The trian left.", "The train left.")
                  .replace("<p>Read on Example Reads!</p>", "")
              + "<script>alert(1)</script>")
    r = client.post(f"/chapter/{cid}/text", data={"html": edited})
    assert "Chapter saved" in r.text
    reader = client.get(f"/chapter/{cid}").text
    assert "The train left." in reader and "Read on Example Reads" not in reader
    assert "alert(1)" not in reader and "1 earlier version" in reader
    # Images are stored with their original addresses again.
    text = editor_html(client, cid)
    assert f'data-shelf-src="{art}"' in text and f'data-shelf-src="{gone}"' in text
    assert "static/missing-image" not in client.get(f"/chapter/{cid}").text

    # An empty chapter isn't saved.
    r = client.post(f"/chapter/{cid}/text", data={"html": "<p> </p>"})
    assert "would be empty" in r.text

    # Sending the chapter again replaces the edit; the edit stays in the history.
    client.post(f"/api/novels/{nid}/chapters", json={
        "url": "https://example.com/night-train/chapter-1",
        "html": page(1, extra="<p>The trian left.</p>")})
    assert "The trian left." in client.get(f"/chapter/{cid}").text
    assert "2 earlier versions" in client.get(f"/chapter/{cid}").text


def test_edits_survive_reimport_and_can_be_undone(client):  # noqa: F811
    from shelf.config import settings
    from shelf.library import scan_library

    build_wacz(settings.library_dir / "night-train.wacz")
    scan_library()
    nid = client.get("/api/novels").json()["novels"][0]["id"]
    cid = chapter_ids(client, nid)[0]
    text = editor_html(client, cid)
    client.post(f"/chapter/{cid}/text", data={"html": "<p>My own words.</p>" + text})

    client.post(f"/novel/{nid}/reimport")
    cid = chapter_ids(client, nid)[0]
    assert "My own words." in client.get(f"/chapter/{cid}").text

    # Restoring the version from before the edit undoes it, and the next re-import
    # brings in the archive's text again.
    history = client.get(f"/chapter/{cid}/history").text
    restore = re.search(r'action="(/chapter/\d+/history/\d+/restore)"', history).group(1)
    client.post(restore)
    assert "My own words." not in client.get(f"/chapter/{cid}").text
    client.post(f"/novel/{nid}/reimport")
    cid = chapter_ids(client, nid)[0]
    assert "My own words." not in client.get(f"/chapter/{cid}").text
