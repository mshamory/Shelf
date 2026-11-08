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
