import pytest

from shelf.novelupdates import (
    MetadataError,
    is_series_url,
    normalize_series_url,
    parse_series_page,
)

from .conftest import FIXTURES


def test_parse_series_page():
    m = parse_series_page((FIXTURES / "nu_series.html").read_text())
    assert m.title == "The Sample Novel"
    assert m.url == "https://www.novelupdates.com/series/the-sample-novel/"
    assert m.cover_url.endswith("sample.jpg")
    assert m.genres == ["Fantasy", "Mystery"]
    assert m.tags == ["Apocalypse", "Female Protagonist"]
    assert m.authors == ["Some Author"] and m.artists == []
    assert m.rating == 4.3 and m.votes == 1234
    assert m.language == "Chinese" and m.year == "2023"
    assert m.status == "320 Chapters (Completed)"
    assert m.alt_names == ["Sample Alt One", "示例小说", "Sample Alt Two"]
    assert m.original_publisher == ["jjwxc"] and m.english_publisher == []
    assert "what it seems" in m.description_html and "<script" not in m.description_html


def test_rejects_other_pages():
    with pytest.raises(MetadataError):
        parse_series_page("<html><body><p>hello</p></body></html>")


def test_urls():
    assert is_series_url("https://www.novelupdates.com/series/foo/")
    assert not is_series_url("https://www.novelupdates.com/extnu/123/")
    assert normalize_series_url("foo-bar") == "https://www.novelupdates.com/series/foo-bar/"
    assert normalize_series_url("http://novelupdates.com/series/x") == \
        "https://www.novelupdates.com/series/x/"
    with pytest.raises(MetadataError):
        normalize_series_url("https://evil.example/series/x/")
