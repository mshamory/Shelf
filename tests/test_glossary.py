"""A novel's glossary: terms marked in chapters, with their descriptions."""

import io
import json
import re
import zipfile

from shelf.glossary import CompiledTerm, compile_term, mark_terms

from .test_app import client  # noqa: F401  (fixture)
from .test_capture import page


def marks(html, *terms):
    compiled = [CompiledTerm(i, compile_term(name, aliases, ignore_case))
                for i, (name, aliases, ignore_case) in enumerate(terms, 1)]
    return mark_terms(html, compiled)


def test_matching():
    html, found = marks(
        '<p>Lin Dong met Linley and Lin  Dong\'s friend, Little Marten.</p>'
        '<p><img alt="Lin Dong &gt; all" src="x.png">Spirit Stones &amp; a spirit stone.</p>',
        ("Lin Dong", ["Little Marten"], False),
        ("Spirit Stone", ["/[Ss]pirit [Ss]tones?/"], False),
        ("Linley", [], False))
    # Whole words, any spacing, aliases and regular expressions; never inside tags.
    assert html.count('data-gloss="1"') == 3 and html.count('data-gloss="2"') == 2
    assert '<span class="gloss" data-gloss="3">Linley</span>' in html
    assert 'alt="Lin Dong &gt; all"' in html
    assert '<span class="gloss" data-gloss="2">Spirit Stones</span> &amp; a ' in html
    assert found == [1, 3, 2]
    # Whole words on both sides, ignoring case too.
    assert marks("<p>Berlin, Linley, Lin's</p>", ("Lin", [], True))[0].count('class="gloss"') == 1
    assert marks("<p>LIN-DONG</p>", ("lin", [], True))[1] == [1]
    # Case, and terms without word edges (CJK).
    assert not marks("<p>lin dong</p>", ("Lin Dong", [], False))[1]
    assert marks("<p>lin dong</p>", ("Lin Dong", [], True))[1] == [1]
    assert marks("<p>他是林动的朋友</p>", ("林动", [], False))[1] == [1]
    # The longest of overlapping terms wins.
    html, _ = marks("<p>the Qingyang Town gate</p>", ("Qingyang", [], False),
                    ("Qingyang Town", [], False))
    assert '<span class="gloss" data-gloss="2">Qingyang Town</span>' in html


def test_glossary_in_the_reader(client):  # noqa: F811
    nid = client.post("/api/novels", json={"title": "Night Train"}).json()["novel"]["id"]
    r = client.post(f"/api/novels/{nid}/chapters", json={
        "url": "https://example.com/night-train/chapter-1",
        "html": page(1, extra="<p>The conductor, Old Wen, checked the Night Pass.</p>")})
    cid = r.json()["chapter"]["id"]

    r = client.post(f"/novel/{nid}/glossary", data={
        "term": "Old Wen", "category": "Character", "aliases": "Conductor Wen\n/[Cc]onductor/",
        "description": "Runs the train.\nNever sleeps."})
    assert "appears in 1 chapter" in r.text
    r = client.post(f"/novel/{nid}/glossary", data={"term": "Night Pass", "aliases": "/(/"})
    assert "Invalid regular expression" in r.text
    r = client.post(f"/novel/{nid}/glossary", data={"term": "old wen"})
    assert "already in the glossary" in r.text

    reader = client.get(f"/chapter/{cid}").text
    assert '<span class="gloss" data-gloss="' in reader and ">conductor</span>" in reader
    data = json.loads(re.search(r'id="glossary-data" type="application/json">(.*?)</script>',
                                reader).group(1))
    (entry,) = data.values()
    assert entry == {"term": "Old Wen", "category": "Character",
                     "description": "Runs the train.\nNever sleeps."}

    page_html = client.get(f"/novel/{nid}/glossary").text
    assert "Old Wen" in page_html and "<code>/[Cc]onductor/</code>" in page_html
    tid = re.search(r'action="/glossary/(\d+)/edit"', page_html).group(1)
    client.post(f"/glossary/{tid}/edit", data={"term": "Old Wen", "aliases": "",
                                               "description": "Runs the night train."})
    reader = client.get(f"/chapter/{cid}").text
    assert ">conductor</span>" not in reader and "Runs the night train." in reader

    # Exported books carry the glossary, the first mention linked to it.
    book = zipfile.ZipFile(io.BytesIO(client.get(f"/novel/{nid}/export.epub").content))
    chapter = book.read("OEBPS/c00001.xhtml").decode()
    assert f'<a epub:type="noteref" href="glossary.xhtml#g{tid}">Old Wen</a>' in chapter
    assert f'<dt id="g{tid}">Old Wen</dt><dd><p>Runs the night train.</p></dd>' in \
        book.read("OEBPS/glossary.xhtml").decode()

    client.post(f"/glossary/{tid}/delete")
    assert 'class="gloss"' not in client.get(f"/chapter/{cid}").text
