import pytest

from shelf.extract import chapter_number, extract_content, sanitize, strip_common_affixes
from shelf.library import parse_wacz

from .conftest import build_wacz, chapter_html


@pytest.mark.parametrize("text,expected", [
    ("Chapter 12: Something", 12),
    ("Ch. 7.5 - Side", 7.5),
    ("Ch 284: Apocalypse Star House Hoarding", 284),
    ("第123章 标题", 123),
    ("/2025/02/27/ch-284-apocalypse/", 284),
    ("/novel/c45/", 45),
    ("Episode 3", 3),
    ("Prologue", None),
    ("Volume 2 Afterword", None),
])
def test_chapter_number(text, expected):
    assert chapter_number(text) == expected


def test_strip_common_affixes():
    assert strip_common_affixes(
        ["Ch 1: Star – Site", "Ch 2: Star – Site", "Ch 3: Star – Site"]
    ) == ["Ch 1", "Ch 2", "Ch 3"]
    assert strip_common_affixes(["Chapter 1 - Dawn", "Chapter 2 - Dusk"]) == [
        "Chapter 1 - Dawn", "Chapter 2 - Dusk"]


def test_extract_content_drops_boilerplate():
    html = extract_content(chapter_html(5))
    assert "End of chapter 5." in html
    assert "Share this post" not in html and "Novels" not in html


def test_sanitize_strips_scripts_and_handlers():
    out = sanitize('<p onclick="x()">Hi<script>alert(1)</script><a href="javascript:x">l</a></p>')
    assert "script" not in out and "onclick" not in out and "javascript" not in out


@pytest.mark.parametrize("with_index", [True, False])
@pytest.mark.parametrize("with_pages", [True, False])
def test_parse_wacz(tmp_path, with_index, with_pages):
    parsed = parse_wacz(build_wacz(tmp_path / "a.wacz", with_index=with_index,
                                   with_pages=with_pages))
    assert [c.number for c in parsed.chapters] == [1, 2, 3, 10]
    assert [c.title for c in parsed.chapters][0] == "Chapter 1"
    assert parsed.metadata is not None and parsed.metadata.title == "The Sample Novel"
    assert parsed.cover is not None and parsed.cover[1] == "image/jpeg"


CHALLENGE = (b"<html><head><title>Just a moment...</title></head>"
             b"<body>Checking your browser</body></html>")


def test_post_replays_and_challenges(tmp_path):
    """ArchiveWeb.page POST captures and Cloudflare challenges (regression)."""
    from .conftest import write_wacz

    html = "text/html; charset=utf-8"
    base = "https://example.com/novel/chapter-"
    records = [
        # Captured via POST: index has the plain URL, pages.jsonl the __wb_method form.
        {"url": base + "1/", "status": 200, "body": chapter_html(1).encode(), "ctype": html,
         "method": "POST"},
        # Challenge token in the original URL.
        {"url": base + "2/?__cf_chl_f_tk=XYZ", "status": 200, "body": chapter_html(2).encode(),
         "ctype": html, "method": "POST"},
        # Challenge (403 POST) first, real page later via GET.
        {"url": base + "3/", "status": 403, "body": CHALLENGE, "ctype": html, "method": "POST",
         "ts": "20260101000010"},
        {"url": base + "3/", "status": 200, "body": chapter_html(3).encode(), "ctype": html,
         "ts": "20260101000020"},
        # Part two of a chapter.
        {"url": base + "3-2/", "status": 200,
         "body": chapter_html(3).replace("Chapter 3:", "Chapter 3-2:").replace(
             "End of chapter 3.", "End of chapter 3, part 2.").encode(), "ctype": html},
    ]
    pages = [
        {"url": base + "1/?__wb_method=POST&tok=abc", "title": "Chapter 1: Night Train"},
        {"url": base + "2/?__cf_chl_f_tk=XYZ&__wb_method=POST&a=b&c=d", "title": "Chapter 2"},
        {"url": base + "3/?__wb_method=POST&a=b", "title": "Just a moment..."},
        {"url": base + "3/", "title": "Just a moment..."},
        {"url": base + "3-2/", "title": "Chapter 3-2: Night Train"},
    ]
    parsed = parse_wacz(write_wacz(tmp_path / "post.wacz", records, pages))
    assert [c.number for c in parsed.chapters] == [1, 2, 3, 3.002]
    assert "End of chapter 3." in parsed.chapters[2].html
    assert parsed.chapters[2].url == base + "3/"
    assert parsed.chapters[2].title.startswith("Chapter 3")
    assert parsed.skipped == []


def test_stacked_site_suffixes_and_glued_prefix():
    titles = [f"Series Name - Chapter {i}.1 - Novel Live - Reading Novel Free" for i in range(1, 6)]
    titles.append("SERIES NAME - Chapter 6.1 - Novel Live - Reading Novel Free")
    assert strip_common_affixes(titles) == [f"Chapter {i}.1" for i in range(1, 7)]
    titles = [f"Series Name – Chapter {i} – Title {i} – EAP" for i in range(1, 5)]
    titles.append("Series Name Part 2 Chapter 1: Again – EAP")
    assert strip_common_affixes(titles)[-1] == "Part 2 Chapter 1: Again"


@pytest.mark.parametrize("title,error", [
    ("Trash – Chapter 404 – Only Me (1) – EAP", False),
    ("Chapter 503: Service", False),
    ("Error", True),
    ("404 Not Found", True),
    ("Page not found – Site", True),
    ("Just a moment...", True),
    ("Errors of Youth", False),
])
def test_error_titles(title, error):
    from shelf.extract import is_error_page
    assert is_error_page(title, 200) is error


def _chapters(*titles):
    from shelf.extract import ExtractedChapter, classify

    out = []
    for i, t in enumerate(titles):
        c = ExtractedChapter(url=f"https://x/{i}", title=t, html="", text_length=0,
                             ts=f"{i:04d}")
        classify(c, "")
        out.append(c)
    return out


def test_seasons_split_when_numbering_restarts():
    from shelf.extract import order_chapters

    chs = _chapters("Part 2 Chapter 2: b", "Chapter 2", "Side Story 1-2", "Part 2 Chapter 1: a",
                    "Chapter 1", "Side Story 1-1", "Chapter 404 – Only Me")
    ordered = order_chapters(chs)
    assert [(c.season_label, c.number) for c in ordered] == [
        ("Part 1", 1), ("Part 1", 2), ("Part 1", 404), ("Part 2", 1), ("Part 2", 2),
        ("Side stories", 1.001), ("Side stories", 1.002)]


def test_volumes_with_continuous_numbering_stay_one_list():
    from shelf.extract import order_chapters

    chs = _chapters("Volume 2, Chapter 3", "Volume 1, Chapter 1", "Chapter 2",
                    "Volume 1, Chapter 2")
    ordered = order_chapters(chs)
    assert [c.number for c in ordered] == [1, 2, 2, 3]
    assert {c.season_label for c in ordered} == {""} and {c.season for c in ordered} == {None}


def test_chapter_ranges_and_parts():
    from shelf.extract import ChapterSpan, chapter_number, chapter_span

    assert chapter_span("Chapter 96-97: The Small Stronghold") == ChapterSpan(96, 97, "dash")
    assert chapter_span("Ch. 7 & 8") == ChapterSpan(7, 8, "word")
    assert chapter_span("Chapters 7 to 9: Away") == ChapterSpan(7, 9, "word")
    assert chapter_span("Volume 1, Chapter 207-2 – Title") == ChapterSpan(207.002)
    assert chapter_span("Chapter 250 – 3 Days (1)") == ChapterSpan(250)  # a title, not a range
    assert chapter_span("Chapter 1-100") == ChapterSpan(1)  # too wide for one post
    assert chapter_number("Chapter 96-97") == 96
    assert ChapterSpan(3, 4, "dash").as_part() == ChapterSpan(3.004)
    assert ChapterSpan(7, 8, "word").as_part() == ChapterSpan(7, 8, "word")


def test_dash_ranges_are_parts_in_novels_that_number_parts():
    from shelf.extract import ExtractedChapter, classify
    from shelf.library import resolve_ranges

    def chapters(*titles):
        out = [ExtractedChapter(url=f"https://ex.com/{i}", title=t, html="", text_length=0)
               for i, t in enumerate(titles)]
        for c in out:
            classify(c, "")
        resolve_ranges(out)
        return [(c.number, c.number_end) for c in out]

    assert chapters("Chapter 2", "Chapter 3-4", "Chapter 5") == [(2, None), (3, 4), (5, None)]
    assert chapters("Chapter 3-1", "Chapter 3-2", "Chapter 3-3") == [
        (3.001, None), (3.002, None), (3.003, None)]
    assert chapters("Chapter 3-1", "Chapter 4 & 5") == [(3.001, None), (4, 5)]


def test_renamed_post_captured_twice_is_kept_once():
    from shelf.extract import ExtractedChapter, classify
    from shelf.library import drop_renamed_duplicates

    base = "https://www.patreon.com/Author/posts/"
    rows = [("chapter-692-dual-165442119", "Chapter 692: Dual", "20260101"),
            ("chapter-692-form-165442119", "Chapter 692: Form", "20260201"),
            ("chapter-693-165442200", "Chapter 693", "20260101")]
    chapters = [ExtractedChapter(url=base + slug, title=t, html="", text_length=0, ts=ts)
                for slug, t, ts in rows]
    for c in chapters:
        classify(c, "")
    assert [c.title for c in drop_renamed_duplicates(chapters)] == [
        "Chapter 692: Form", "Chapter 693"]
