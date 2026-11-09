import re
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


def test_missing_chapters_are_reported(client):
    from shelf.config import settings
    from shelf.library import scan_library

    build_wacz(settings.library_dir / "night-train.wacz")  # chapters 1, 2, 3, 10
    scan_library()
    assert "6 missing" in client.get("/").text
    page = client.get("/novel/1").text
    assert "6 chapters missing" in page
    assert "Missing: chapters 4–9 <small>(not captured)" in page
    assert "Chapters 4–9: not captured" in client.get("/chapter/3").text
    assert "skips ahead" not in client.get("/chapter/2").text


def test_trim_rules(client):
    from shelf.config import settings
    from shelf.library import scan_library

    build_wacz(settings.library_dir / "a.wacz")
    build_wacz(settings.library_dir / "b.wacz")
    scan_library()
    assert "End of chapter 1." in client.get("/chapter/1").text

    r = client.post("/trim", data={"novel_id": 1, "pattern": "end of chapter 1.",
                                   "mode": "paragraph", "ignore_case": "true",
                                   "next": "/chapter/1"}, follow_redirects=False)
    assert r.headers["location"].startswith("/chapter/1")
    page = client.get("/chapter/1").text
    assert "End of chapter 1." not in page and "1 passage trimmed" in page
    assert "End of chapter 1." in client.get("/chapter/1?original=1").text

    other = client.get("/novel/2/read", follow_redirects=False).headers["location"]
    assert "End of chapter 1." in client.get(other).text  # novel-scoped rule

    client.post("/trim", data={"novel_id": 1, "pattern": "rattled", "scope": "all"})
    assert "rattled" not in client.get(other).text  # global rule

    trim_page = client.get("/novel/1/trim").text
    assert "1 / 4" in trim_page and "4 / 4" in trim_page

    r = client.post("/trim", data={"novel_id": 1, "pattern": "(", "is_regex": "true"})
    assert "Invalid regular expression" in r.text
    r = client.post("/trim", data={"novel_id": 1, "pattern": "x", "next": "https://evil.example/"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/novel/1/trim")

    client.post("/trim/1/toggle", data={"next": "/"})
    assert "End of chapter 1." in client.get("/chapter/1").text
    client.post("/trim/1/delete", data={"next": "/"})
    assert "/trim/1/" not in client.get("/novel/1/trim").text


def test_edit_chapter_title_and_number(client):
    from sqlmodel import Session

    from shelf.config import settings
    from shelf.db import get_engine
    from shelf.library import import_wacz, scan_library

    path = build_wacz(settings.library_dir / "a.wacz")  # chapters 1, 2, 3, 10 -> ids 1..4
    scan_library()

    def order():
        page = client.get("/novel/1").text
        return [int(x) for x in re.findall(r'<li id="ch-(\d+)"', page)]

    assert order() == [1, 2, 3, 4]
    client.post("/chapter/2/edit", data={"title": "  My Title  ", "number": "11"})
    assert order() == [1, 3, 4, 2]
    page = client.get("/novel/1?hidden=1").text
    assert "My Title" in page and "edited" in page
    assert "Missing: chapters 4–9" in page and "Missing: chapter 2" in page

    r = client.post("/chapter/3/edit", data={"number": "x"})
    assert "a chapter number (e.g. 12" in r.text
    client.post("/chapter/3/edit", data={"number": "1-2"})
    assert order() == [1, 3, 4, 2]  # 1.002 sorts right after chapter 1

    # A post covering several chapters closes the gap it spans.
    # (Present now: 1, 1-2 and 11, so 2-10 are missing.)
    assert "Missing: chapters 2–9" in client.get("/novel/1").text
    client.post("/chapter/4/edit", data={"number": "3 to 10"})
    page = client.get("/novel/1?hidden=1").text
    assert "#3–10" in page and "Missing: chapter 2 " in page and "chapters 2–" not in page
    client.post("/chapter/4/edit", data={"number": ""})
    assert "Missing: chapters 2–9" in client.get("/novel/1").text
    r = client.post("/chapter/4/edit", data={"number": "9 to 3"})
    assert "a chapter number (e.g. 12" in r.text

    # Edits survive a forced re-import (matched by URL).
    with Session(get_engine()) as session:
        import_wacz(session, path, force=True)
    page = client.get("/novel/1").text
    assert "My Title" in page
    titles = re.findall(r'<li id="ch-\d+"[^>]*>\s*<a href="/chapter/\d+">([^<]*)</a>', page)
    assert titles[-1] == "My Title" and titles[1].startswith("Chapter 3")

    # Reset restores what was detected.
    new_id = int(re.findall(r'<li id="ch-(\d+)"', page)[-1])
    client.post(f"/chapter/{new_id}/edit", data={"reset": "1"})
    titles = re.findall(r'<li id="ch-\d+"[^>]*>\s*<a href="/chapter/\d+">([^<]*)</a>',
                        client.get("/novel/1").text)
    assert titles[1:3] == ["Chapter 3", "Chapter 2"] and "My Title" not in titles
