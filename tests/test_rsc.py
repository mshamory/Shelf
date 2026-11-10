"""Chapters in Next.js React Server Components payloads (``?_rsc=`` navigations)."""

import json

from shelf.library import parse_wacz
from shelf.rsc import page_url, parse_rows, render_page

from .conftest import PARA, chapter_html, write_wacz

SENTENCE = "The lantern swung as the carriage climbed the pass. "


def flight(n: int, mapped: bool = False) -> bytes:
    """A chapter payload as Next.js sends it: client components, a fragment, the chapter's
    paragraphs in their own rows, a long text row, and the page title in the metadata."""
    paras = [json.dumps(["$", "p", f"p-{i}", {"children": SENTENCE * 6 + f"({i})"}])
             for i in range(6)]
    long_text = ("$" + SENTENCE * 8).encode()
    rows = [
        '1:I[339756,["/_next/static/chunks/a.js"],"LoadingBoundaryProvider"]',
        '2:"$Sreact.fragment"',
        "3:" + json.dumps([["$", "$L1", None, {"loading": [["$", "div", None, {
            "className": "animate-pulse"}]]}], ["$", "$2", "c", {"children": [
                ["$", "nav", None, {"children": "Home | Novels"}],
                ["$", "article", None, {"children": [
                    ["$", "h1", None, {"children": f"Chapter {n}: Pass {n}"}],
                    *[f"$L{10 + i:x}" for i in range(6)],
                    ["$", "p", None, {"children": "$L20"}],
                    ["$", "div", None, {"hidden": True, "children": "secret junk"}],
                    ["$", "script", None, {"children": "alert(1)"}],
                ]}]]}]]),
        "4:" + json.dumps([["$", "title", "0", {
            "children": f"Chapter {n} · Mountain Pass · Example Reads"}]]),
        *[f"{10 + i:x}:{p}" for i, p in enumerate(paras)],
    ]
    if mapped:  # newer Next.js: the tree hangs off Maps in their own rows
        rows += ['5:[["children",{"c":"$3"}]]', '0:{"b":"build","t":{"c":"$Q5"},"h":"$4"}']
    else:
        rows += ['0:{"f":[["children","novel",["$3","$4"]]]}']
    body = "\n".join(rows).encode() + b"\n"
    return body + b"20:T" + f"{len(long_text):x},".encode() + long_text + b"\n"


def test_render_flight_payload():
    for mapped in (False, True):
        body = flight(4, mapped)
        assert parse_rows(body)["20"].startswith("$The lantern")
        doc = render_page(body)
        assert "<title>Chapter 4 · Mountain Pass · Example Reads</title>" in doc
        assert "<h1>Chapter 4: Pass 4</h1>" in doc and doc.count("(5)") == 1
        assert "$The lantern" in doc  # a text row is literal text, not a reference
        assert "secret junk" not in doc and "alert" not in doc and "LoadingBoundary" not in doc
        assert "novel" not in doc.replace("Novels", "")  # router state isn't page text
    assert render_page(b'1:I[1,[],"X"]\n') is None


def test_page_url():
    assert page_url("https://ex.com/s/ch-2?_rsc=abc") == "https://ex.com/s/ch-2"
    assert page_url("https://ex.com/s/ch-2?a=1&_rsc=abc") == "https://ex.com/s/ch-2?a=1"
    assert page_url("https://ex.com/s/ch-2") is None


def test_parse_wacz_reads_rsc_navigations(tmp_path):
    base = "https://reads.example/novel/mountain-pass/chapter-"
    records = [
        {"url": base + "1", "status": 200, "body": chapter_html(1).encode(),
         "ctype": "text/html; charset=utf-8"},
        {"url": base + "2?_rsc=x1", "status": 200, "body": flight(2), "ctype": "text/x-component"},
        {"url": base + "3?_rsc=y2", "status": 200, "body": flight(3, mapped=True),
         "ctype": "text/x-component"},
        # A link prefetch: layout only, no chapter text.
        {"url": base + "4?_rsc=z3", "status": 200, "ctype": "text/x-component",
         "body": b'0:{"f":[["children",["$","div",null,{"children":"Loading"}]]]}\n'},
        # The chapter-1 page also has a prefetch; the HTML page is kept.
        {"url": base + "1?_rsc=w4", "status": 200, "body": flight(1), "ctype": "text/x-component"},
    ]
    pages = [{"url": base + "1", "ts": "2026-01-01T00:00:00Z", "title": "Chapter 1"}]
    parsed = parse_wacz(write_wacz(tmp_path / "mp.wacz", records, pages))
    got = {c.number: c for c in parsed.chapters}
    assert sorted(got) == [1, 2, 3]
    assert got[2].url == base + "2" and got[2].title == "Chapter 2: Pass 2"
    assert "carriage" in got[3].html and "Home | Novels" not in got[3].html
    assert PARA[3:40] in got[1].html  # chapter 1 from its HTML page, not the payload
