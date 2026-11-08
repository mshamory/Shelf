import shutil

import pytest
from fastapi.testclient import TestClient

from .conftest import FIXTURES, build_wacz


@pytest.fixture
def client(tmp_path, monkeypatch):
    from shelf import db
    from shelf.config import settings

    monkeypatch.setattr(settings, "library_dir", tmp_path / "library")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "scan_on_startup", False)
    monkeypatch.setattr(settings, "fetch_covers", False)
    monkeypatch.setattr(db, "engine", None)
    from shelf.main import app

    with TestClient(app) as c:
        yield c


def test_scan_read_and_progress(client, tmp_path):
    from shelf.config import settings
    from shelf.library import scan_library

    build_wacz(settings.library_dir / "night-train.wacz")
    scan_library()

    r = client.get("/")
    assert "The Sample Novel" in r.text  # title taken from the archived NovelUpdates page

    r = client.get("/novel/1")
    assert r.status_code == 200
    assert "Some Author" in r.text and "Fantasy" in r.text and "/covers/" in r.text
    assert r.text.count('<li id="ch-') == 4

    r = client.get("/novel/1/read", follow_redirects=False)
    assert r.headers["location"] == "/chapter/1"
    r = client.get("/chapter/1")
    assert "End of chapter 1." in r.text and 'href="/chapter/2" rel="next"' in r.text

    assert client.post("/api/progress", json={"chapter_id": 3, "position": 0.5}).json()["ok"]
    r = client.get("/novel/1/read", follow_redirects=False)
    assert r.headers["location"] == "/chapter/3"

    # Hidden chapters drop out of navigation.
    client.post("/chapter/2/toggle-hidden")
    assert 'href="/chapter/3" rel="next"' in client.get("/chapter/1").text

    r = client.get("/search", params={"q": "End of chapter 10"})
    assert r.text.count("search-result") == 1


def test_upload_and_metadata_upload(client, tmp_path):
    src = build_wacz(tmp_path / "up.wacz")
    with src.open("rb") as fh:
        r = client.post("/upload", files={"file": ("../evil name.wacz", fh)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/novel/1")
    from shelf.config import settings
    assert (settings.library_dir / "uploads" / "evil name.wacz").exists()

    client.post("/novel/1/metadata/edit", data={"title": "Renamed", "authors": "A, B"})
    assert "Renamed" in client.get("/novel/1").text

    with (FIXTURES / "nu_series.html").open("rb") as fh:
        r = client.post("/novel/1/metadata/upload", files={"file": ("nu.html", fh)})
    assert "The Sample Novel" in r.text and "Uploaded NovelUpdates page" in r.text

    with (FIXTURES / "nu_series.html").open("rb") as fh:
        r = client.post("/upload", files={"file": ("x.html", fh)})
    assert "Only .wacz" in r.text


def test_rescan_removes_deleted_archives(client):
    from shelf.config import settings
    from shelf.library import scan_library

    p = build_wacz(settings.library_dir / "a.wacz")
    scan_library()
    assert "/novel/1" in client.get("/").text
    p.unlink()
    scan_library()
    assert "/novel/1" not in client.get("/").text
    shutil.rmtree(settings.library_dir)
