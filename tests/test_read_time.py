"""Reading times: chapters' word counts, kept up to date with their text."""

import re

from sqlmodel import Session, select

from .conftest import build_wacz
from .test_app import client  # noqa: F401  (fixture)
from .test_capture import page
from .test_reading import novel_state


def words(cid: int) -> int:
    from shelf.db import Chapter, get_engine

    with Session(get_engine()) as s:
        return s.get(Chapter, cid).word_count


def test_word_count():
    from shelf.extract import word_count

    assert word_count("<p>The road bent east — past fields; he didn't look back.</p>") == 10
    assert word_count("<p>他走了很远的路。</p>") == 4  # 7 characters, two to a word
    assert word_count("<p>ひらがなとカタカナ</p>") == 5
    assert word_count("<p>나는 학교에 갔다.</p>") == 3  # Korean: words
    assert word_count("<p>Chapter 12: 第十二章</p>") == 2 + 2
    assert word_count("") == 0 and word_count("<p><img src=x></p>") == 0


def test_read_time():
    from shelf.reading import read_time

    assert read_time(0) == "1 min" and read_time(3000) == "12 min"
    assert read_time(250 * 60) == "1 h" and read_time(250 * 125) == "2 h 5 min"
    assert read_time(250 * 60 * 52, about=True) == "about 52 h"


def test_counts_follow_the_text(client):  # noqa: F811
    nid = client.post("/api/novels", json={"title": "Night Train"}).json()["novel"]["id"]
    body = "<p>" + " ".join(["word"] * 2900) + "</p>"
    r = client.post(f"/api/novels/{nid}/chapters", json={
        "url": "https://example.com/night-train/chapter-1", "html": page(1, extra=body)})
    cid = r.json()["chapter"]["id"]
    captured = words(cid)
    assert 2900 < captured < 3100

    reader = client.get(f"/chapter/{cid}").text
    assert re.search(r'title="[\d,]+ words">12 min</span>', reader)
    novel = client.get(f"/novel/{nid}").text
    assert ">12 min</small>" in novel and "· about 12 min)" in novel

    # Edited: recounted. Restored: back to what it was.
    r = client.post(f"/chapter/{cid}/text", data={"html": "<p>Just a few words now.</p>"})
    assert "Chapter saved" in r.text and words(cid) == 5
    history = client.get(f"/chapter/{cid}/history").text
    client.post(re.search(r'action="(/chapter/\d+/history/\d+/restore)"', history).group(1))
    assert words(cid) == captured


def test_time_left_and_upgrade(client):  # noqa: F811
    from shelf.config import settings
    from shelf.db import Chapter, get_engine, init_db
    from shelf.library import scan_library

    build_wacz(settings.library_dir / "night-train.wacz")
    scan_library()
    _, ids = novel_state(1)
    with Session(get_engine()) as s:
        counts = [c.word_count for c in s.exec(select(Chapter).where(Chapter.novel_id == 1)
                                                .order_by(Chapter.position))]
    assert all(counts)
    client.post("/api/progress", json={"chapter_id": ids[1], "position": 0.0})
    page_ = client.get("/novel/1").text
    from shelf.reading import read_time
    assert f"2 chapters left, {read_time(sum(counts[2:]), about=True)}" in page_

    # A database from before word counts: they're counted when Shelf starts.
    with get_engine().begin() as conn:
        conn.exec_driver_sql("ALTER TABLE chapter DROP COLUMN word_count")
    init_db()
    with Session(get_engine()) as s:
        assert [c.word_count for c in s.exec(select(Chapter).where(Chapter.novel_id == 1)
                                             .order_by(Chapter.position))] == counts
