"""Chapters from web pages saved in the browser and uploaded."""

from email.message import EmailMessage

from shelf.saved import guess_series, read_saved_page

from .conftest import FIXTURES, PARA, build_wacz
from .test_app import client  # noqa: F401  (fixture)
from .test_capture import page


def saved(n: int, how: str = "chrome") -> bytes:
    html = page(n)
    url = f"https://example.com/night-train/chapter-{n}"
    if how == "chrome":  # "Web page, complete": address comment, images in a local folder
        html = (f"<!-- saved from url=({len(url):04d}){url} -->\n"
                + html.replace("</article>", f'<img src="./Chapter {n}_files/art.jpg"></article>'))
    elif how == "canonical":  # "HTML only" from a browser that doesn't mark the address
        html = html.replace("</head>", f'<link rel="canonical" href="{url}"></head>')
    return html.encode()


def mhtml(n: int) -> bytes:
    url = f"https://example.com/night-train/chapter-{n}"
    msg = EmailMessage()
    msg["Snapshot-Content-Location"] = url
    msg["Subject"] = f"Chapter {n}"
    msg.make_related()
    msg.add_related(page(n), subtype="html", charset="utf-8")
    msg.get_payload()[0]["Content-Location"] = url
    msg.add_related(b"\x89PNG....", maintype="image", subtype="png")
    return msg.as_bytes()


def test_read_saved_pages():
    p = read_saved_page("Chapter 3.html", saved(3))
    assert p.url == "https://example.com/night-train/chapter-3"
    assert "Stop 3" in p.html and "_files/" not in p.html  # the local image is dropped
    assert read_saved_page("c.htm", saved(4, "canonical")).url.endswith("/chapter-4")
    m = read_saved_page("Chapter 5.mhtml", mhtml(5))
    assert m.url.endswith("/chapter-5") and "Stop 5" in m.html
    # No address in the file: one made from its name.
    assert read_saved_page("My Page.html", page(6).encode()).url == "file:///My%20Page.html"
    # The declared character set is honoured.
    latin = '<html><head><meta charset="windows-1252"><title>Café – Chapter 1</title>'
    assert "Café" in read_saved_page("x.html", latin.encode("cp1252")).html

    pages = [read_saved_page(f"{n}.html", saved(n)) for n in (1, 2, 3)]
    assert guess_series(pages) == "Night Train"


def test_guess_series_from_page_titles():
    def pages(titles, url="https://reads.example/p/{n}/"):
        return [read_saved_page("x.html", (f'<!-- saved from url=(0030){url.format(n=n)} -->'
                                           f"<title>{t}</title>").encode())
                for n, t in enumerate(titles)]

    # The series inside the chapter label, the site after it.
    assert guess_series(pages([f"Trash of the Family Part 2 Chapter {n}: Roles ({n}) – EAP"
                               for n in (1, 2, 3)])) == "Trash of the Family"
    # Series between the chapter and the site: the earlier part wins.
    assert guess_series(pages([f"Chapter {n} · Hiding Tunnels · HangulPlanet"
                               for n in (1, 2)])) == "Hiding Tunnels"
    # One page: the part after a bare label is the chapter's name, unless the URL has it.
    assert guess_series(pages(["Chapter 5 – Foolish Mistakes"])) == ""
    assert guess_series(pages(["Chapter 91 · Hiding Tunnels · HangulPlanet"],
                              "https://hp.example/novel/hiding-tunnels/chapter-{n}")) == (
        "Hiding Tunnels")


def test_upload_saved_pages_as_a_new_novel(client):  # noqa: F811
    files = [("files", (f"Chapter {n}.html", saved(n))) for n in (2, 1)]
    files.append(("files", ("Chapter 3.mhtml", mhtml(3))))
    files.append(("files", ("broken.html", b"<html><body>Not found</body></html>")))
    r = client.post("/upload", files=files, data={"novel": "new"})
    assert "3 chapters added" in r.text and "1 file skipped (broken.html:" in r.text
    assert r.text.index("Chapter 1: Stop 1") < r.text.index("Chapter 2: Stop 2") < r.text.index(
        "Chapter 3: Stop 3")
    novel_id = int(r.url.path.rsplit("/", 1)[1])
    titles = {n["id"]: n["title"] for n in client.get("/api/novels").json()["novels"]}
    assert titles[novel_id] == "Night Train"  # guessed from the page titles
    reader = client.get(f"/novel/{novel_id}/read").text
    assert PARA[:40] in reader and "_files" not in reader

    # With a saved NovelUpdates page, the novel is named and described from it.
    nu = (FIXTURES / "nu_series.html").read_bytes()
    r = client.post("/upload", data={"novel": "new"},
                    files=[("files", ("nu.html", nu)), ("files", ("c.html", saved(4)))])
    assert "NovelUpdates metadata imported" in r.text and "The Sample Novel" in r.text

    # Nothing readable: no empty novel is left behind.
    before = client.get("/api/novels").json()["novels"]
    r = client.post("/upload", data={"novel": "new"},
                    files=[("files", ("x.html", b"<html><body>Not found</body></html>"))])
    assert "skipped" in r.text and len(client.get("/api/novels").json()["novels"]) == len(before)


def test_saved_pages_added_to_an_archived_novel(client):  # noqa: F811
    from shelf.config import settings
    from shelf.library import scan_library

    build_wacz(settings.library_dir / "night-train.wacz")  # chapters 1, 2, 3, 10
    scan_library()
    nid = client.get("/api/novels").json()["novels"][0]["id"]
    r = client.post(f"/novel/{nid}/pages", files=[
        ("files", ("ch5.html", saved(5))), ("files", ("ch2.html", saved(2, "canonical")))])
    assert "1 chapter added; 1 chapter replaced" in r.text and "history" in r.text
    assert "Stop 5" in r.text
    # Through the library upload too, into the chosen novel.
    r = client.post("/upload", data={"novel": str(nid)}, files=[("files", ("c6.html", saved(6)))])
    assert "1 chapter added" in r.text and "Stop 6" in r.text
    r = client.post(f"/novel/{nid}/pages", files=[("files", ("a.wacz", b"x"))])
    assert "Choose saved web pages" in r.text
