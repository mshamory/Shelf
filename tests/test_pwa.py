"""Shelf as an installable app, and the reader loading the next chapter ahead."""

import json
import re

from pydantic import SecretStr

from .conftest import build_wacz
from .test_app import client  # noqa: F401  (fixture)
from .test_reading import novel_state


def test_manifest_and_service_worker(client, monkeypatch):  # noqa: F811
    from shelf.config import settings

    # Both load before you sign in: browsers fetch the manifest without cookies.
    monkeypatch.setattr(settings, "password", SecretStr("pw"))
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    manifest = r.json()
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).headers["content-type"] == "image/png"
    assert any(i.get("purpose") == "maskable" for i in manifest["icons"])

    r = client.get("/sw.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert "__VERSION__" not in r.text
    assert re.search(r'const VERSION = "[0-9a-f]+-[0-9a-f]+";', r.text)
    assert client.get("/static/offline.html").status_code == 200

    page = client.post("/login", data={"password": "pw"}).text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page
    assert 'serviceWorker.register("/sw.js")' in page


def test_reader_loads_next_chapter_ahead(client):  # noqa: F811
    from shelf.config import settings
    from shelf.library import scan_library

    build_wacz(settings.library_dir / "night-train.wacz")
    scan_library()
    _, ids = novel_state(1)

    page = client.get(f"/chapter/{ids[0]}").text
    rules = re.search(r'<script type="speculationrules">(.*?)</script>', page, re.S)
    assert json.loads(rules.group(1)) == {
        "prerender": [{"source": "list", "urls": [f"/chapter/{ids[1]}"]}]}
    assert f'data-next="/chapter/{ids[1]}"' in page
    assert '<select name="tap">' in page

    last = client.get(f"/chapter/{ids[-1]}").text
    assert "speculationrules" not in last and "data-next" not in last

    # Loading a chapter ahead doesn't move your place: only the reader's script does that.
    client.get(f"/chapter/{ids[2]}")
    assert novel_state(1)[0].last_chapter_id is None
