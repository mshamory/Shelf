"""Reading lists and chapters added since you last read."""

from sqlmodel import Session, select

from . import conftest
from .conftest import build_wacz, chapter_html
from .test_app import client  # noqa: F401  (fixture)


def novel_state(nid: int):
    from shelf.db import Chapter, Novel, get_engine

    with Session(get_engine()) as s:
        novel = s.get(Novel, nid)
        chapters = s.exec(select(Chapter).where(Chapter.novel_id == nid)
                          .order_by(Chapter.position)).all()
        return novel, [c.id for c in chapters]


def badge(html: str) -> str | None:
    import re

    m = re.search(r'class="new-badge">(\d+) new<', html)
    return m.group(1) if m else None


def cards(html: str) -> list[str]:
    import re

    return re.findall(r'class="card-title">([^<]*)<', html)


def test_reading_status_and_new_chapters(client):  # noqa: F811
    from shelf.config import settings
    from shelf.library import scan_library

    build_wacz(settings.library_dir / "night-train.wacz")
    scan_library()
    other = client.post("/api/novels", json={"title": "Another Story"}).json()["novel"]["id"]
    novel, ids = novel_state(1)
    assert novel.reading_status is None and len(ids) == 4

    # Reading puts the novel on your Reading list and remembers what's after your place.
    client.post("/api/progress", json={"chapter_id": ids[0], "position": 0.2})
    novel, _ = novel_state(1)
    assert novel.reading_status == "reading" and novel.left_when_read == 3
    page = client.get("/novel/1").text
    assert "3 chapters left" in page and "since you last read" not in page
    assert badge(client.get("/").text) is None

    # A chapter sent from the browser is new.
    html = chapter_html(11)
    r = client.post("/api/novels/1/chapters",
                    json={"url": "https://example.com/novel/chapter-11/", "html": html})
    assert r.status_code == 200, r.text
    library = client.get("/").text
    assert badge(library) == "1"
    assert "New chapters <span>1</span>" in library
    assert "1 new chapter</strong> since you last read" in client.get("/novel/1").text
    assert cards(client.get("/?status=new").text) == ["The Sample Novel"]
    assert cards(client.get("/?sort=updated").text) == ["The Sample Novel", "Another Story"]

    # Hiding or showing chapters yourself doesn't change what's new.
    _, ids = novel_state(1)
    client.post(f"/chapter/{ids[2]}/toggle-hidden")
    assert badge(client.get("/").text) == "1"
    client.post(f"/chapter/{ids[2]}/toggle-hidden")
    assert badge(client.get("/").text) == "1"

    # Reading again: nothing is new.
    client.post("/api/progress", json={"chapter_id": ids[1], "position": 0.0})
    assert badge(client.get("/").text) is None

    # Lists set by hand; reading doesn't move a finished novel back to Reading.
    r = client.post("/novel/1/status", data={"status": "finished"}, follow_redirects=False)
    assert r.status_code == 303
    client.post("/api/progress", json={"chapter_id": ids[2], "position": 0.0})
    assert novel_state(1)[0].reading_status == "finished"
    assert cards(client.get("/?status=finished").text) == ["The Sample Novel"]
    assert "No novels are marked Dropped" in client.get("/?status=dropped").text
    client.post(f"/novel/{other}/status", data={"status": "planned"})
    assert novel_state(other)[0].reading_status == "planned"
    assert client.post("/novel/1/status", data={"status": "bogus"}).status_code == 400
    client.post("/novel/1/status", data={"status": ""})
    assert novel_state(1)[0].reading_status is None


def test_reimport_with_more_chapters(client, monkeypatch):  # noqa: F811
    from shelf.config import settings
    from shelf.library import scan_library

    path = build_wacz(settings.library_dir / "night-train.wacz")
    scan_library()
    _, ids = novel_state(1)
    client.post("/api/progress", json={"chapter_id": ids[1], "position": 0.5})
    first, _ = novel_state(1)

    # The same archive again: nothing new.
    scan_library(force=True)
    novel, _ = novel_state(1)
    assert novel.updated_at == first.updated_at and badge(client.get("/").text) is None

    # A newer archive with two more chapters.
    for n in (11, 12):
        monkeypatch.setitem(conftest.PAGES, f"https://example.com/novel/chapter-{n}/",
                            (200, chapter_html(n)))
    path.unlink()
    build_wacz(path)
    scan_library(force=True)
    novel, ids = novel_state(1)
    assert novel.updated_at > first.updated_at and len(ids) == 6
    assert badge(client.get("/").text) == "2"
    assert "4 chapters left" in client.get("/novel/1").text
