"""A novel as an EPUB e-book: the chapters as Shelf shows them (hidden ones left out, trim
rules applied, edited titles and numbers), a section page for each part or volume, the
images Shelf has a copy of, inside the book, and the novel's glossary, with each term's first
mention in a chapter linked to it."""

from __future__ import annotations

import html as html_lib
import os
import re
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import lxml.html
from lxml import etree
from sqlmodel import Session, select

from . import __version__
from .config import settings
from .db import Chapter, GlossaryTerm, Novel
from .glossary import compile_terms, mark_terms
from .images import _sniff, images_dir, read_archived_image, resolve_images
from .trim import apply_rules, compile_rules

# Characters XML doesn't allow (control characters some sites leave in their text).
BAD_XML_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
IMAGE_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp",
             "image/avif": "avif", "image/svg+xml": "svg", "image/bmp": "bmp"}
STORED_RE = re.compile(r"^/novel/(\d+)/image/([0-9a-f]{40}\.[a-z0-9]{1,5})$")

CSS = """body { margin: 0 4%; line-height: 1.5; }
h1 { font-size: 1.4em; margin: 1.5em 0 1em; }
h1.section { text-align: center; margin-top: 30%; }
.title-page { text-align: center; }
.title-page h1 { font-size: 2em; margin-top: 20%; }
.description { text-align: left; margin-top: 2em; }
img { max-width: 100%; height: auto; }
table { border-collapse: collapse; }
td, th { border: 1px solid #888; padding: .2em .4em; }
.cover { text-align: center; margin: 0; padding: 0; }
.cover img { max-height: 100%; }
"""


def _x(s: str) -> str:
    return html_lib.escape(BAD_XML_RE.sub("", s or ""), quote=True)


def _page(title: str, body: str, lang: str, body_class: str = "") -> str:
    cls = f' class="{body_class}"' if body_class else ""
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            f'xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">\n'
            f'<head><meta charset="utf-8"/><title>{_x(title)}</title>'
            '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
            f"<body{cls}>\n{body}\n</body>\n</html>\n")


@dataclass
class _Entry:
    file: str
    title: str
    section: str  # the part or volume it's in, "" for none


class _Images:
    """The images of the book: each one Shelf has, once, named by order."""

    def __init__(self, novel: Novel):
        self.novel = novel
        self.files: dict[str, tuple[str, str] | None] = {}  # src -> (file, media type)
        self.data: list[tuple[str, str, bytes]] = []

    def _read(self, src: str) -> tuple[bytes, str] | None:
        parts = urlsplit(src)
        query = parse_qs(parts.query)
        if m := STORED_RE.match(parts.path):
            if int(m.group(1)) != self.novel.id:
                return None
            path = images_dir(self.novel.id) / m.group(2)
            data = path.read_bytes() if path.is_file() else b""
        elif parts.path == f"/novel/{self.novel.id}/archive-image" and "url" in query:
            return read_archived_image(self.novel, query["url"][0])
        elif parts.path == "/epub-image" and "book" in query and "path" in query:
            book = query["book"][0]
            if ".." in Path(book).parts or not book.lower().endswith(".epub"):
                return None
            # Only the novel's own e-book, or a volume in its folder.
            if book != self.novel.wacz_path and not (
                    self.novel.wacz_path.endswith("/")
                    and book.rsplit("/", 1)[0] + "/" == self.novel.wacz_path):
                return None
            try:
                with zipfile.ZipFile(settings.library_dir / book) as z:
                    data = z.read(query["path"][0])
            except (KeyError, zipfile.BadZipFile, OSError):
                return None
        else:
            return None
        mime = _sniff(data) if data else None
        return (data, mime) if mime else None

    def file(self, src: str) -> str | None:
        if src not in self.files:
            found = self._read(src)
            if found is None:
                self.files[src] = None
            else:
                data, mime = found
                name = f"images/i{len(self.data) + 1:04d}.{IMAGE_EXT[mime]}"
                self.data.append((name, mime, data))
                self.files[src] = (name, mime)
        f = self.files[src]
        return f[0] if f else None


def _xhtml_body(fragment: str, images: _Images) -> str:
    """A chapter's HTML as XHTML, its images pointing into the book."""
    fragment = BAD_XML_RE.sub("", fragment).strip()
    if not fragment:
        return ""
    root = lxml.html.fragment_fromstring(fragment, create_parent="div")
    for img in list(root.iter("img")):
        name = images.file(img.get("src", ""))
        if name is None:
            img.drop_tree()
        else:
            img.set("src", name)
            img.set("alt", img.get("alt", ""))
    for el in root.iter():  # attributes XHTML has no use for, and comments
        if not isinstance(el.tag, str):
            continue
        for attr in list(el.attrib):
            if ":" in attr or not re.match(r"^[a-zA-Z_][\w.-]*$", attr):
                del el.attrib[attr]
    out = [_x(root.text)] if root.text else []
    out += [etree.tostring(child, method="xml", encoding="unicode", with_tail=True)
            for child in root if isinstance(child.tag, str) or child.tail]
    return "".join(s if not s.startswith("<!--") else "" for s in out)


def _language(novel: Novel) -> str:
    lang = (novel.language or "").strip()
    return lang if LANG_RE.match(lang) else "en"


def _slug(title: str) -> str:
    s = re.sub(r'[\\/:?"<>|\x00-\x1f]+', " ", title.replace("*", "_")).strip(" .")
    return re.sub(r"\s+", " ", s)[:120] or "novel"


def export_filename(novel: Novel) -> str:
    return f"{_slug(novel.title)}.epub"


def export_epub(session: Session, novel: Novel, dest: Path, trim_rules=()) -> int:
    """Write the novel as an EPUB to `dest`, with the novel's trim rules applied as the
    reader applies them. Returns the number of chapters."""
    lang = _language(novel)
    rules = compile_rules(list(trim_rules))
    images = _Images(novel)
    ids = session.exec(select(Chapter.id).where(
        Chapter.novel_id == novel.id, Chapter.hidden == False)  # noqa: E712
        .order_by(Chapter.position)).all()
    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'shelf:{novel.id}:{novel.title}')}"
    modified = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        # The mimetype file comes first, uncompressed.
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0" encoding="utf-8"?>\n'
                   '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:'
                   'container"><rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>\n')
        z.writestr("OEBPS/style.css", CSS)

        manifest = ['<item id="css" href="style.css" media-type="text/css"/>',
                    '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
                    'properties="nav"/>',
                    '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>']
        spine: list[str] = []

        def add(item_id: str, name: str, content: str, linear: bool = True):
            z.writestr(f"OEBPS/{name}", content)
            manifest.append(f'<item id="{item_id}" href="{name}" '
                            'media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="{item_id}"' + ("" if linear else ' linear="no"')
                         + "/>")

        cover = settings.covers_dir / novel.cover_file if novel.cover_file else None
        cover_data = cover.read_bytes() if cover and cover.is_file() else b""
        cover_mime = _sniff(cover_data) if cover_data else None
        if cover_mime:
            name = f"cover.{IMAGE_EXT[cover_mime]}"
            z.writestr(f"OEBPS/{name}", cover_data)
            manifest.append(f'<item id="cover-image" href="{name}" media-type="{cover_mime}" '
                            'properties="cover-image"/>')
            add("cover", "cover.xhtml", _page(
                novel.title, f'<div class="cover"><img src="{name}" alt="Cover"/></div>',
                lang, "cover"))

        byline = f"<p>by {_x(', '.join(novel.authors))}</p>" if novel.authors else ""
        description = (f'<div class="description">'
                       f"{_xhtml_body(novel.description_html, images)}</div>"
                       if novel.description_html else "")
        add("title", "title.xhtml", _page(
            novel.title, f'<section class="title-page" epub:type="titlepage">'
                         f"<h1>{_x(novel.title)}</h1>{byline}{description}</section>", lang))

        terms = list(session.exec(select(GlossaryTerm).where(
            GlossaryTerm.novel_id == novel.id)))
        terms.sort(key=lambda t: (t.category.casefold(), t.term.casefold()))
        compiled = compile_terms(terms)

        def glossary_link(tid: int, text: str, first: bool) -> str:
            return (f'<a class="glossref" href="glossary.xhtml#g{tid}">{text}</a>'
                    if first else text)

        entries: list[_Entry] = []
        section, n_sections = "", 0
        for i, chapter_id in enumerate(ids, 1):
            ch = session.get(Chapter, chapter_id)
            if ch.season_label != section:
                section = ch.season_label
                if section:
                    n_sections += 1
                    name = f"s{n_sections:03d}.xhtml"
                    add(f"s{n_sections:03d}", name, _page(
                        section, f'<h1 class="section">{_x(section)}</h1>', lang))
                    entries.append(_Entry(name, section, ""))
            content = apply_rules(ch.content_html, rules)[0] if rules else ch.content_html
            content = resolve_images(session, novel, content)
            content = mark_terms(content, compiled, glossary_link)[0]
            body = _xhtml_body(content, images).replace(
                '<a class="glossref" href=', '<a epub:type="noteref" href=')
            title = ch.title or f"Chapter {i}"
            name = f"c{i:05d}.xhtml"
            add(f"c{i:05d}", name, _page(title, (
                f'<section epub:type="chapter"><h1>{_x(title)}</h1>\n{body}\n</section>'),
                lang))
            entries.append(_Entry(name, title, section))
            session.expunge(ch)  # keep memory flat on long novels

        if terms:
            add("glossary", "glossary.xhtml", _page("Glossary", _glossary(terms), lang))
            entries.append(_Entry("glossary.xhtml", "Glossary", ""))

        for k, (name, mime, data) in enumerate(images.data, 1):
            z.writestr(f"OEBPS/{name}", data, compress_type=zipfile.ZIP_STORED)
            manifest.append(f'<item id="img{k:04d}" href="{name}" media-type="{mime}"/>')

        z.writestr("OEBPS/nav.xhtml", _nav(novel.title, entries, lang))
        z.writestr("OEBPS/toc.ncx", _ncx(novel.title, book_id, entries))

        meta = [f'<dc:identifier id="book-id">{book_id}</dc:identifier>',
                f"<dc:title>{_x(novel.title)}</dc:title>",
                f"<dc:language>{lang}</dc:language>",
                f'<meta property="dcterms:modified">{modified}</meta>',
                f'<meta name="generator" content="Shelf {__version__}"/>']
        meta += [f"<dc:creator>{_x(a)}</dc:creator>" for a in novel.authors]
        meta += [f"<dc:subject>{_x(g)}</dc:subject>" for g in novel.genres]
        if novel.description_html:
            text = lxml.html.fromstring(BAD_XML_RE.sub("", novel.description_html)).text_content()
            meta.append(f"<dc:description>{_x(text.strip())}</dc:description>")
        if novel.english_publisher or novel.original_publisher:
            publisher = (novel.english_publisher or novel.original_publisher)[0]
            meta.append(f"<dc:publisher>{_x(publisher)}</dc:publisher>")
        if novel.year and novel.year.strip().isdigit():
            meta.append(f"<dc:date>{novel.year.strip()}</dc:date>")
        if cover_mime:
            meta.append('<meta name="cover" content="cover-image"/>')
        z.writestr("OEBPS/content.opf", (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            f'unique-identifier="book-id" xml:lang="{lang}">\n'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            + "\n".join(meta) + "\n</metadata>\n<manifest>\n" + "\n".join(manifest)
            + '\n</manifest>\n<spine toc="ncx">\n' + "\n".join(spine)
            + "\n</spine>\n</package>\n"))
    return len(ids)


def _glossary(terms: list[GlossaryTerm]) -> str:
    out, category = ['<section epub:type="glossary"><h1>Glossary</h1>'], None
    for t in terms:
        if t.category != category:
            if category is not None:
                out.append("</dl>")
            category = t.category
            out.append((f"<h2>{_x(category)}</h2>" if category else "") + "<dl>")
        paragraphs = "".join(f"<p>{_x(line)}</p>" for line in t.description.splitlines()
                             if line.strip())
        out.append(f'<dt id="g{t.id}">{_x(t.term)}</dt><dd>{paragraphs or "<p></p>"}</dd>')
    out.append("</dl></section>")
    return "".join(out)


def _grouped(entries: list[_Entry]) -> list[tuple[_Entry, list[_Entry]]]:
    """Section pages with their chapters; chapters outside a section stand alone."""
    out: list[tuple[_Entry, list[_Entry]]] = []
    for e in entries:
        if e.section and out and out[-1][0].title == e.section and not out[-1][0].section \
                and out[-1][0].file.startswith("s"):
            out[-1][1].append(e)
        else:
            out.append((e, []))
    return out


def _nav(title: str, entries: list[_Entry], lang: str) -> str:
    items = []
    for head, children in _grouped(entries):
        inner = "".join(f'<li><a href="{c.file}">{_x(c.title)}</a></li>' for c in children)
        items.append(f'<li><a href="{head.file}">{_x(head.title)}</a>'
                     + (f"<ol>{inner}</ol>" if inner else "") + "</li>")
    return _page(title, '<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>\n'
                        + "\n".join(items) + "\n</ol></nav>", lang)


def _ncx(title: str, book_id: str, entries: list[_Entry]) -> str:
    points, order = [], 0

    def point(e: _Entry, inner: str = "") -> str:
        nonlocal order
        order += 1
        return (f'<navPoint id="p{order}" playOrder="{order}"><navLabel><text>{_x(e.title)}'
                f'</text></navLabel><content src="{e.file}"/>{inner}</navPoint>')

    for head, children in _grouped(entries):
        # playOrder follows reading order: the section page, then its chapters.
        outer = point(head)
        inner = "".join(point(c) for c in children)
        points.append(outer[:-len("</navPoint>")] + inner + "</navPoint>")
    depth = 2 if any(children for _h, children in _grouped(entries)) else 1
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
            f'<head><meta name="dtb:uid" content="{book_id}"/>'
            f'<meta name="dtb:depth" content="{depth}"/></head>\n'
            f"<docTitle><text>{_x(title)}</text></docTitle>\n<navMap>\n"
            + "\n".join(points) + "\n</navMap>\n</ncx>\n")


def export_to_temp(session: Session, novel: Novel, trim_rules=()) -> Path:
    """The novel as an EPUB in a temporary file (the caller removes it)."""
    fd, name = tempfile.mkstemp(suffix=".epub", prefix="shelf-export-")
    os.close(fd)
    path = Path(name)
    try:
        export_epub(session, novel, path, trim_rules)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path

