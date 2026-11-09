import json

from shelf.library import parse_wacz
from shelf.xhr import (
    content_to_html,
    decode_payload,
    devalue_unflatten,
    find_chapter_objects,
    prosemirror_to_html,
)

from .conftest import PARA, chapter_html, write_wacz

LONG = "The train rattled on through the endless night. " * 12


def flatten(obj) -> list:
    """Minimal devalue encoder for tests (no cycles/dedup)."""
    values: list = []

    def add(v):
        i = len(values)
        values.append(None)
        if isinstance(v, dict):
            values[i] = {k: add(x) for k, x in v.items()}
        elif isinstance(v, list):
            values[i] = [add(x) for x in v]
        else:
            values[i] = v
        return i

    add(obj)
    return values


def prosemirror(*paras: str) -> dict:
    return {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paras]}


def test_devalue_roundtrip_and_typed_values():
    obj = {"a": [1, "x", {"b": None}], "n": 3}
    assert devalue_unflatten(flatten(obj)) == obj
    assert devalue_unflatten([{"d": 1}, ["Date", "2025-01-01T00:00:00Z"]]) == {
        "d": "2025-01-01T00:00:00Z"}
    # Shared references resolve to the same object.
    shared = devalue_unflatten([[1, 1], {"k": 2}, "v"])
    assert shared[0] is shared[1]


def test_prosemirror_rendering():
    doc = {"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "T"}]},
        {"type": "paragraph", "content": [
            {"type": "text", "text": "a<b", "marks": [{"type": "bold"}, {"type": "italic"}]},
            {"type": "hardBreak"}, {"type": "text", "text": "c"}]},
        {"type": "paragraph"},
        {"type": "horizontalRule"},
    ]}
    assert prosemirror_to_html(doc) == "<h3>T</h3><p><em><strong>a&lt;b</strong></em><br>c</p><hr>"


def test_content_formats():
    assert content_to_html(json.dumps(prosemirror("x"))) == "<p>x</p>"
    assert content_to_html({"rendered": "<p>wp</p>"}) == "<p>wp</p>"
    assert content_to_html("one\n\ntwo") == "<p>one</p><p>two</p>"
    assert content_to_html({"blocks": [{"type": "paragraph", "data": {"text": "e"}}]}) == "<p>e</p>"
    assert content_to_html({"something": "else"}) is None


def test_sveltekit_page_url_and_chapter_objects():
    payload = {"type": "data", "nodes": [None, {"type": "data", "data": flatten({
        "series": {"title": "S", "description": "short"},
        "chapter": {"title": "Chapter 5", "number": 5, "content": json.dumps(prosemirror(LONG))},
    })}]}
    data, page = decode_payload(
        "https://ex.com/series/s/5/__data.json?x-sveltekit-invalidated=0001",
        json.dumps(payload).encode())
    assert page == "https://ex.com/series/s/5"
    found = list(find_chapter_objects(data))
    assert len(found) == 1 and found[0][0]["number"] == 5


def test_next_data_url():
    _, page = decode_payload("https://ex.com/_next/data/abc123/novel/ch-3.json", b"{}")
    assert page == "https://ex.com/novel/ch-3"


def test_parse_wacz_merges_xhr_chapters(tmp_path):
    html = "text/html; charset=utf-8"

    def svelte(n: int, body: str) -> bytes:
        return json.dumps({"type": "data", "nodes": [{"type": "data", "data": flatten({
            "chapterData": {"title": f"Chapter {n}", "number": n,
                            "content": json.dumps(prosemirror(f"Chapter {n}: Heading {n}", body))},
        })}]}).encode()

    base = "https://ex.com/series/s/"
    records = [
        # First chapter server-rendered; the rest loaded through SvelteKit's __data.json.
        {"url": base + "1", "status": 200, "body": chapter_html(1).encode(), "ctype": html},
        {"url": base + "2/__data.json?x-sveltekit-invalidated=0001", "status": 200,
         "body": svelte(2, "Two. " + LONG), "ctype": "application/json"},
        {"url": base + "3/__data.json?x-sveltekit-invalidated=01", "status": 200,
         "body": svelte(3, "Three. " + LONG), "ctype": "application/json"},
        # A locked chapter: JSON only has a teaser, the HTML capture is complete.
        {"url": base + "4", "status": 200, "body": chapter_html(4).encode(), "ctype": html},
        {"url": base + "4/__data.json", "status": 200, "body": svelte(4, "Teaser " * 50),
         "ctype": "application/json"},
        {"url": "https://ex.com/api/track", "status": 200, "body": b'{"ok":true}',
         "ctype": "application/json"},
        {"url": base + "9", "status": 200, "ctype": html, "body":
         b"<html><head><title>Making sure you're not a bot!</title></head><body>"
         + PARA.encode() * 3 + b"</body></html>"},
    ]
    pages = [{"url": base + "1", "title": "Chapter 1"}, {"url": base + "4", "title": "Chapter 4"},
             {"url": base + "9", "title": "Making sure you're not a bot!"}]
    parsed = parse_wacz(write_wacz(tmp_path / "spa.wacz", records, pages))
    assert [c.number for c in parsed.chapters] == [1, 2, 3, 4]
    two = parsed.chapters[1]
    assert two.url == base + "2" and two.title == "Chapter 2: Heading 2"
    assert "Heading 2" not in two.html and "endless night" in two.html
    assert "End of chapter 4." in parsed.chapters[3].html  # full HTML beat the teaser
    assert parsed.skipped == [base + "9"]


def test_hidden_antiscraping_junk_is_removed():
    from shelf.extract import remove_hidden

    html = ('<p>Keep</p><div aria-hidden="true" style="position:absolute;top:-1000px">J1</div>'
            '<p>a<span style="display: none">J2</span>b</p><p hidden>J3</p>'
            '<p style="font-size:0">J4</p><p style="font-size:0.9em">ok</p>')
    out = remove_hidden(html)
    assert not any(j in out for j in ("J1", "J2", "J3", "J4"))
    assert "Keep" in out and "ab" in out and "ok" in out


def test_older_capture_used_when_newest_is_error(tmp_path):
    error = json.dumps({"type": "data", "nodes": [{"type": "skip"}, {
        "type": "error", "error": {"message": "Chapter not found"}, "status": 404}]}).encode()
    good = json.dumps({"type": "data", "nodes": [{"type": "data", "data": flatten({
        "chapter": {"title": "Chapter 7", "number": 7,
                    "content": "<p>" + LONG + '<span aria-hidden="true">JUNK</span></p>'}})}]})
    url = "https://ex.com/s/7/__data.json"
    records = [
        {"url": url, "status": 200, "body": good.encode(), "ctype": "application/json",
         "ts": "20260101000001"},
        {"url": url, "status": 200, "body": error, "ctype": "application/json",
         "ts": "20260101000002"},
    ]
    parsed = parse_wacz(write_wacz(tmp_path / "e.wacz", records, []))
    assert [c.number for c in parsed.chapters] == [7]
    assert "JUNK" not in parsed.chapters[0].html


def test_css_class_hidden_junk_and_zero_width_watermarks():
    from shelf.extract import remove_hidden, sanitize

    html = ('<style>.c5{position:absolute;width:1px;height:1px;overflow:hidden;'
            'clip:rect(0,0,0,0)}</style><p>Real</p><div class="c5">JUNK</div>'
            '<p class="x c5">MORE</p>')
    out = remove_hidden(html)
    assert "JUNK" not in out and "MORE" not in out and "Real" in out and "<style" not in out
    marked = "Af‍​‌‌​ter"
    assert sanitize(f"<p>{marked} a‍b</p>") == "<p>After a‍b</p>"


def test_promote_heading_skips_logo_and_repeated_label():
    from shelf.library import _promote_heading

    content = ('<img src="logo.png"> <h2>Chapter 678 : Chapter 678</h2>'
               '<p>Chapter 678: Body Time</p><p>Body text.</p>')
    assert _promote_heading("Chapter 678", content) == ("Chapter 678: Body Time",
                                                         "<p>Body text.</p>")
    # Nothing to promote: leave as is.
    assert _promote_heading("Chapter 3", "<p>Story starts.</p>") == (
        "Chapter 3", "<p>Story starts.</p>")


def test_api_url_pairs_with_page_and_url_pattern_numbers(tmp_path):
    html = "text/html; charset=utf-8"
    base = "https://ex.com/series/s/"

    def api(n: int, body: str) -> bytes:
        return json.dumps({"slug": str(n), "title": f"Chapter {n}", "number": n,
                           "content": f"<p>Chapter {n}: Full {n}</p><p>{body}</p>"}).encode()

    def page(n, title) -> bytes:
        return (f"<html><head><title>{title}</title></head><body><article>"
                f"<p>Teaser {n}.</p>{PARA}</article></body></html>").encode()

    records = [{"url": base + str(n), "status": 200, "ctype": html,
                "body": page(n, f"S - Chapter {n} - Site")} for n in (1, 2, 3)]
    # Page with a generic title: numbered from its URL.
    records.append({"url": base + "4", "status": 200, "ctype": html, "body": page(4, "Site")})
    # Chapter 2 also fetched from an API endpoint, with full text.
    records.append({"url": "https://ex.com/api/v2/series/s/2", "status": 200,
                    "ctype": "application/json", "body": api(2, "Two " + LONG * 3)})
    pages = [{"url": base + str(n), "title": ""} for n in (1, 2, 3, 4)]
    parsed = parse_wacz(write_wacz(tmp_path / "api.wacz", records, pages))
    assert [(c.number, c.title) for c in parsed.chapters] == [
        (1, "Chapter 1"), (2, "Chapter 2: Full 2"), (3, "Chapter 3"), (4, "Chapter 4")]
    assert parsed.chapters[1].url == base + "2" and "Two" in parsed.chapters[1].html
