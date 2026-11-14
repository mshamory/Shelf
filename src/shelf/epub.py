"""Novels from EPUB e-books.

An EPUB is a zip of XHTML documents: the package file (OPF) lists them in reading order
(the spine) and holds the book's metadata; the table of contents (EPUB 3 nav document or
EPUB 2 NCX) names the chapters and says where each starts. Chapters follow the table of
contents: an entry starts a chapter at its document (or at an anchor inside it), and
documents without an entry continue the chapter before them (books often split long
chapters into several files). Without a table of contents, each document is a chapter.

Images stay in the book and are served from it. Books with DRM (encrypted content) are refused: Shelf
doesn't decrypt protected content.
"""

import html as html_lib
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit
from xml.etree import ElementTree as ET

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .extract import (
    MIN_CHAPTER_CHARS,
    SIDE_SEASON,
    ExtractedChapter,
    assign_seasons,
    chapter_number,
    classify,
    sanitize,
    text_length,
)
from .novelupdates import NovelMetadata

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "enc": "http://www.w3.org/2001/04/xmlenc#",
}
EPUB_NS = "http://www.idpf.org/2007/ops"
HTML_TYPES = {"application/xhtml+xml", "text/html", "application/x-dtbook+xml"}
# Font obfuscation (IDPF, Adobe) only scrambles embedded fonts; anything else is DRM.
FONT_OBFUSCATION = {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}
# A short numbered chapter is still a chapter; short unnumbered documents are title pages,
# copyright notices and the like.
MIN_NUMBERED_CHARS = 200
URL_SCHEME = "epub:"
CHAPTERISH_RE = re.compile(r"\b(?:chapter|episode|prologue|epilogue|interlude|act|part|"
                           r"side story|extra|afterword)\b|^\s*ch\.", re.I)
# A title naming a volume, book or part rather than a chapter: "Book 2 – Growing Up".
VOLUME_TITLE_RE = re.compile(r"^\s*(?:book|volume|vol\.|part|arc)\s*[\divxlc]+\b", re.I)


def _names_volume(title: str) -> bool:
    """ "Book 2 – Growing Up", not "Volume 6 - Chapter 32: The Sixth Investigator"."""
    return bool(VOLUME_TITLE_RE.match(title)) and not re.search(
        r"\b(?:chapter|chap|ch|episode|ep)\b", title, re.I)


FRONT_RE = re.compile(r"^\s*(?:title page|cover|copyright|(?:table of )?contents|untitled)\b",
                      re.I)
# Pages that aren't part of the story, whatever their length.
CONTENTS_RE = re.compile(
    r"^\s*(?:(?:table of )?contents|copyrights?(?: and credits)?|copyright page)\s*$"
    r"|\bnewsletter\b", re.I)


class EpubError(ValueError):
    pass


@dataclass
class TocEntry:
    title: str
    doc: str  # path inside the zip
    fragment: str
    group: str = ""  # top-level entry this one is nested under ("Volume 2")


@dataclass
class ParsedBook:
    title: str
    chapters: list[ExtractedChapter]
    metadata: NovelMetadata
    cover: tuple[bytes, str] | None = None
    skipped: list[str] = field(default_factory=list)


def _xml(z: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        return ET.fromstring(z.read(name))
    except KeyError as e:
        raise EpubError(f"{name} is missing from the e-book") from e
    except ET.ParseError as e:
        raise EpubError(f"{name} can't be read: {e}") from e


def _join(base: str, href: str) -> str:
    """A link from a document at `base` to a path inside the zip."""
    path = unquote(urlsplit(href).path)
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), path)) if path else base


def _check_drm(z: zipfile.ZipFile) -> None:
    if "META-INF/encryption.xml" not in z.namelist():
        return
    root = _xml(z, "META-INF/encryption.xml")
    for data in root.iter(f"{{{NS['enc']}}}EncryptedData"):
        method = data.find("enc:EncryptionMethod", NS)
        if method is None or method.get("Algorithm") not in FONT_OBFUSCATION:
            raise EpubError("This e-book is DRM-protected; Shelf doesn't decrypt protected "
                            "content")


def _text(el: ET.Element | None) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def _toc_from_nav(z: zipfile.ZipFile, nav_path: str) -> list[TocEntry]:
    tree = HTMLParser(_html(z.read(nav_path).decode("utf-8", errors="replace")))
    navs = tree.css("nav")
    toc = next((n for n in navs if "toc" in (n.attributes.get("epub:type") or "")), None)
    toc = toc or (navs[0] if navs else None)
    if toc is None:
        return []
    ol = toc.css_first("ol")
    entries: list[TocEntry] = []

    def walk(ol_node, group: str, depth: int) -> None:
        for li in ol_node.iter():
            if li.tag != "li":
                continue
            a = next((c for c in li.iter() if c.tag in ("a", "span")), None)
            title = re.sub(r"\s+", " ", a.text() if a is not None else "").strip()
            href = a.attributes.get("href") if a is not None and a.tag == "a" else None
            if href:
                parts = urlsplit(href)
                entries.append(TocEntry(title, _join(nav_path, href), unquote(parts.fragment),
                                        group))
            sub = next((c for c in li.iter() if c.tag == "ol"), None)
            if sub is not None:
                walk(sub, group or title if depth == 0 else group, depth + 1)

    if ol is not None:
        walk(ol, "", 0)
    return entries


def _toc_from_ncx(z: zipfile.ZipFile, ncx_path: str) -> list[TocEntry]:
    root = _xml(z, ncx_path)
    nav_map = root.find("ncx:navMap", NS)
    entries: list[TocEntry] = []

    def walk(node: ET.Element, group: str, depth: int) -> None:
        for point in node.findall("ncx:navPoint", NS):
            title = _text(point.find("ncx:navLabel/ncx:text", NS))
            content = point.find("ncx:content", NS)
            src = content.get("src") if content is not None else None
            if src:
                entries.append(TocEntry(title, _join(ncx_path, src),
                                        unquote(urlsplit(src).fragment), group))
            walk(point, group or title if depth == 0 else group, depth + 1)

    if nav_map is not None:
        walk(nav_map, "", 0)
    return entries


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
             "source", "track", "wbr"}
SELF_CLOSING_RE = re.compile(r"<([a-zA-Z][\w:-]*)(\s[^<>]*?)?/>")


def _html(markup: str) -> str:
    """XHTML as an HTML parser reads it: "<script src=…/>" or "<a id=…/>" would otherwise
    be left open, swallowing the rest of the page."""
    def expand(m: re.Match) -> str:
        tag = m.group(1)
        if tag.lower() in VOID_TAGS:
            return m.group(0)
        return f"<{tag}{m.group(2) or ''}></{tag}>"

    return SELF_CLOSING_RE.sub(expand, markup)


def _body(markup: str) -> str:
    tree = HTMLParser(markup)
    return tree.body.inner_html if tree.body is not None else markup


def _split_at_anchors(body: str, fragments: list[str]) -> list[str]:
    """The document's body cut where the elements with these ids start. Returns
    len(fragments) + 1 pieces: the part before the first anchor, then one per anchor."""
    cuts = []
    for frag in fragments:
        m = re.search(rf"<[a-zA-Z][^>]*\bid\s*=\s*[\"']{re.escape(frag)}[\"']", body)
        cuts.append(m.start() if m else None)
    pieces, last = [], 0
    for cut in cuts:
        if cut is None or cut < last:
            pieces.append("")  # anchor not found (or out of order): an empty piece
            continue
        pieces.append(body[last:cut])
        last = cut
    pieces.append(body[last:])
    return pieces


LEAF_DIV_RE = re.compile(
    r"<div\b[^>]*>((?:(?!<(?:p|div|h[1-6]|ul|ol|table|blockquote|figure)\b).)*?)</div>",
    re.S | re.I)


class _Images:
    """Image links into the book, served by Shelf from the EPUB file (see main.epub_image)
    instead of copied into every chapter."""

    def __init__(self, z: zipfile.ZipFile, book: str):
        self.names, self.book = set(z.namelist()), book

    def uri(self, path: str) -> str | None:
        if path not in self.names or not self.book:
            return None
        return "/epub-image?" + urlencode({"book": self.book, "path": path})


def _clean(fragment: str, doc: str, images: _Images) -> str:
    """Chapter HTML: images embedded, links into the book removed, then sanitised."""

    def img(m: re.Match) -> str:
        tag, src = m.group(0), html_lib.unescape(m.group(2))
        if urlsplit(src).scheme in ("http", "https", "data"):
            return tag
        uri = images.uri(_join(doc, src))
        return tag.replace(m.group(1), f'src="{html_lib.escape(uri)}"') if uri else ""

    fragment = re.sub(r"""<img\b[^>]*?(\bsrc\s*=\s*["']([^"']*)["'])[^>]*>""", img, fragment,
                      flags=re.I)
    # Many converted books use <div> for paragraphs; divs are dropped by the sanitiser, so
    # the innermost ones (holding text, not other blocks) become paragraphs.
    fragment = LEAF_DIV_RE.sub(r"<p>\1</p>", fragment)
    # Links to other parts of the book (footnotes, the contents page) lead nowhere here.
    fragment = re.sub(r"""<a\b[^>]*\bhref\s*=\s*["'](?!https?:)[^"']*["'][^>]*>(.*?)</a>""",
                      r"\1", fragment, flags=re.I | re.S)
    return sanitize(fragment)


def _heading(markup: str) -> str:
    tree = HTMLParser(markup)
    for sel in ("h1", "h2", "h3", "title"):
        node = tree.css_first(sel)
        if node is not None and (t := re.sub(r"\s+", " ", node.text()).strip()):
            return t
    return ""


LEADING_HEADING_RE = re.compile(
    r"\s*(?:<(?!h[1-6]\b|p\b|img\b|/)[^>]*>\s*)*<(h[1-3])\b[^>]*>(.*?)</\1>", re.S | re.I)


def _leading_heading(body: str) -> str:
    """The heading a document starts with (after wrapper elements), if any."""
    m = LEADING_HEADING_RE.match(body)
    if not m:
        return ""
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", "", m.group(2)))).strip()


# A block with no blocks inside it, and a chapter label as its whole text: "[Chapter 31]",
# "Chapter 31: The Pass".
INNER_BLOCK_RE = re.compile(
    r"<(p|div|h[1-6])\b[^>]*>((?:(?!<(?:p|div|h[1-6])\b).)*?)</\1>", re.S | re.I)
LABEL_RE = re.compile(r"^[\[(【]?\s*(?:chapter|chap\.|ch\.|episode)\s*\d", re.I)


def _split_at_labels(content: str) -> list[tuple[str, str]]:
    """Text holding several chapters marked only by label lines (no headings, no contents
    entries): (label, text) per chapter; the part before the first label has label "".
    Content with fewer than two labels comes back whole."""
    cuts, bare = [], []
    for m in INNER_BLOCK_RE.finditer(content):
        text = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", "", m.group(2)))).strip()
        if len(text) <= 100 and LABEL_RE.match(text):
            cuts.append((m.start(), m.end(), text.strip("[]()【】 ")))
        elif re.fullmatch(r"\d{1,4}", text):  # "2": a chapter, if they count up by one
            bare.append((m.start(), m.end(), f"Chapter {int(text)}"))
    if not cuts and len(bare) >= 2:
        nums = [int(label.split()[1]) for _s, _e, label in bare]
        if all(b == a + 1 for a, b in zip(nums, nums[1:], strict=False)):
            cuts = bare
    if len(cuts) < 2:
        return [("", content)]
    out = [("", content[:cuts[0][0]])]
    for (_start, end, label), nxt in zip(cuts, [*cuts[1:], (len(content),)], strict=True):
        out.append((label, content[end:nxt[0]]))
    if any(text_length(text) < MIN_CHAPTER_CHARS for _label, text in out[1:]):
        return [("", content)]  # a list of chapters (a contents page), not chapters
    if not re.sub(r"<[^>]+>", "", out[0][1]).strip():
        out.pop(0)
    return out


def _squash(text: str) -> str:
    """Letters and digits only: "Prelude:<br>What Remained" matches "Prelude: What…"."""
    return re.sub(r"\W+", "", text).casefold()


def _drop_title_heading(content: str, title: str) -> str:
    """The reader shows the title above the text: drop the same heading at the start."""
    want, text, end = _squash(title), "", 0
    # The heading can be split in two: the number, then the name ("284" / "Three People").
    for _ in range(2):
        m = BLOCK_RE.match(content, end)
        if not m:
            break
        text += _squash(html_lib.unescape(re.sub(r"<[^>]+>", "", m.group(2))))
        end = m.end()
        if text and text == want:
            return content[end:]
        if not want.startswith(text):
            break
    return content


BLOCK_RE = re.compile(r"\s*<(h[1-6]|p)\b[^>]*>(.*?)</\1>", re.S)


def _metadata(opf: ET.Element) -> NovelMetadata:
    md = opf.find("opf:metadata", NS)
    if md is None:
        return NovelMetadata()

    def all_of(tag: str) -> list[str]:
        return [t for el in md.findall(f"dc:{tag}", NS) if (t := _text(el))]

    desc_el = md.find("dc:description", NS)  # raw: blank lines separate paragraphs
    description = "".join(desc_el.itertext()).strip() if desc_el is not None else ""
    if description and "<" not in description:
        description = "".join(f"<p>{html_lib.escape(p.strip())}</p>"
                              for p in re.split(r"\n\s*\n", description) if p.strip())
    date = (all_of("date") or [""])[0]
    return NovelMetadata(
        title=(all_of("title") or [""])[0],
        authors=all_of("creator"),
        description_html=sanitize(description) if description else "",
        language=(all_of("language") or [""])[0],
        year=date[:4] if re.match(r"\d{4}", date) else "",
        tags=all_of("subject"),
        english_publisher=all_of("publisher"),
    )


def parse_epub(path: Path, book: str = "") -> ParsedBook:
    """`book` names the file for image links (its path in the library); without it,
    images are left out."""
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise EpubError("Not an EPUB file (not a zip archive)") from e
    with z:
        _check_drm(z)
        container = _xml(z, "META-INF/container.xml")
        rootfile = container.find(".//c:rootfile", NS)
        if rootfile is None or not rootfile.get("full-path"):
            raise EpubError("The e-book has no package file")
        opf_path = rootfile.get("full-path")
        opf = _xml(z, opf_path)

        manifest: dict[str, tuple[str, str, str]] = {}  # id -> (path, media type, properties)
        for item in opf.iterfind("opf:manifest/opf:item", NS):
            if item.get("href"):
                manifest[item.get("id", "")] = (
                    _join(opf_path, item.get("href")), item.get("media-type", ""),
                    item.get("properties", ""))
        spine_el = opf.find("opf:spine", NS)
        spine = [manifest[r.get("idref")][0] for r in opf.iterfind("opf:spine/opf:itemref", NS)
                 if r.get("idref") in manifest and manifest[r.get("idref")][1] in HTML_TYPES]
        if not spine:
            raise EpubError("The e-book lists no documents to read")

        nav = next((p for p, _t, props in manifest.values() if "nav" in props.split()), None)
        ncx_id = spine_el.get("toc") if spine_el is not None else None
        ncx = manifest.get(ncx_id, (None,))[0] if ncx_id else next(
            (p for p, t, _ in manifest.values() if t == "application/x-dtbncx+xml"), None)
        toc: list[TocEntry] = []
        if nav:
            toc = _toc_from_nav(z, nav)
        if not toc and ncx:
            toc = _toc_from_ncx(z, ncx)

        meta = _metadata(opf)
        cover = _cover(z, opf, manifest)
        images = _Images(z, book)
        found, skipped = _chapters(z, spine, toc, nav, images)
        chapters = [c for c, _g in found]
        _number(chapters, [g for _c, g in found])
        return ParsedBook(title=meta.title or path.stem, chapters=chapters, metadata=meta,
                          cover=cover, skipped=skipped)


def _cover(z: zipfile.ZipFile, opf: ET.Element, manifest) -> tuple[bytes, str] | None:
    cover = next(((p, t) for p, t, props in manifest.values()
                  if "cover-image" in props.split()), None)
    if cover is None:
        meta = next((m for m in opf.iterfind("opf:metadata/opf:meta", NS)
                     if m.get("name") == "cover"), None)
        if meta is not None and meta.get("content") in manifest:
            p, t, _ = manifest[meta.get("content")]
            cover = (p, t)
    if cover is None or not cover[1].startswith("image/"):
        return None
    try:
        return z.read(cover[0]), cover[1]
    except KeyError:
        return None


@dataclass
class _Draft:
    title: str
    doc: str
    fragment: str
    group: str
    parts: list[str] = field(default_factory=list)
    from_toc: bool = False
    listed_as: str = ""  # the contents' name for it, when its own heading was used instead


def _chapters(z, spine: list[str], toc: list[TocEntry], nav: str | None,
              images: _Images) -> tuple[list[tuple[ExtractedChapter, str]], list[str]]:
    """The book's chapters, each with the contents group it's under ("Volume 2"), and the
    titles of documents too short to be chapters."""
    by_doc: dict[str, list[TocEntry]] = {}
    group_names = {e.group for e in toc if e.group}
    for e in toc:
        entries = by_doc.setdefault(e.doc, [])
        # A volume and its first chapter often point at the same place: keep the chapter.
        if entries and entries[-1].fragment == e.fragment:
            entries[-1] = TocEntry(e.title, e.doc, e.fragment, e.group or entries[-1].title)
        else:
            entries.append(e)

    drafts: list[_Draft] = []
    for doc in spine:
        if doc == nav:
            continue  # the contents page
        try:
            markup = _html(z.read(doc).decode("utf-8", errors="replace"))
        except KeyError:
            continue
        raw = _body(markup)
        body = _clean(raw, doc, images)
        entries = by_doc.get(doc, []) if toc else [TocEntry("", doc, "")]
        if not entries:
            heading = _leading_heading(body)
            # A chapter continued in another file: no heading of its own, the chapter's
            # heading again, or the chapter so far is only its title page (the text then
            # starts with its first scene's heading, "1").
            if drafts and (not heading or heading.casefold() == drafts[-1].title.casefold()
                           or text_length("".join(drafts[-1].parts)) < MIN_CHAPTER_CHARS):
                prev = drafts[-1]
                # A volume's or arc's title page ("Book 2 – Growing Up"), then its first
                # chapter, which says what it is ("Chapter 1 – Magical Beast").
                if (heading and prev.from_toc and _chapter_heading(heading)
                        and text_length("".join(prev.parts)) < MIN_CHAPTER_CHARS
                        and (not _numbered(prev.title) or _names_volume(prev.title))):
                    if prev.title in group_names or _names_volume(prev.title):
                        prev.group = prev.group or prev.title
                    prev.title, prev.listed_as = heading, prev.title
                    prev.parts = []
                prev.parts.append(body)
            else:
                # Front matter, or a chapter the contents leave out (some list a whole
                # volume as one entry; each file then starts with its chapter's heading).
                group = drafts[-1].group if drafts else ""
                prev = drafts[-1] if drafts else None
                listed = prev.listed_as or prev.title if prev is not None else ""
                if heading and prev is not None and prev.from_toc and not (
                        _numbered(listed) and not _names_volume(listed)):
                    # The entry before was a volume, not a chapter: its first file has
                    # its own heading too. (After a chapter, this is one the contents
                    # leave out.)
                    own = _leading_heading("".join(prev.parts))
                    if own:
                        prev.group = group = prev.group or prev.listed_as or prev.title
                        prev.title = own
                    prev.from_toc = False
                drafts.append(_Draft(heading or _heading(markup), doc, "", group, [body]))
            continue
        # Cut before cleaning: the anchors are id attributes, which cleaning removes.
        pieces = [_clean(p, doc, images) if p else ""
                  for p in _split_at_anchors(raw, [e.fragment for e in entries if e.fragment])]
        before, rest = pieces[0], iter(pieces[1:])
        if entries[0].fragment:  # the text before the first anchor continues a chapter
            if drafts:
                drafts[-1].parts.append(before)
            elif re.sub(r"<[^>]+>", "", before).strip():
                drafts.append(_Draft(_heading(markup), doc, "", "", [before]))
        for i, e in enumerate(entries):
            # An entry without an anchor starts at the top of the document.
            piece = next(rest, "") if e.fragment else (before if i == 0 else "")
            title, group = e.title or _heading(piece or markup), e.group
            # A volume or arc listed by its name ("Book 2 – Growing Up") pointing at its
            # first chapter, which says what it is ("Chapter 1 – Magical Beast").
            own = _leading_heading(piece) if (
                title in group_names or _names_volume(title)) else ""
            if own and _chapter_heading(own):
                if title in group_names:
                    group = group or title
                title = own
            drafts.append(_Draft(title, doc, e.fragment, group, [piece], from_toc=True))

    pieces = []
    for d in drafts:
        title = d.title or "Untitled"
        url = f"{URL_SCHEME}{d.doc}" + (f"#{d.fragment}" if d.fragment else "")
        whole = "".join(d.parts)
        # Only text the book doesn't already structure ("Title Page", "Arc 2", a whole
        # book in one file) is split at label lines: real chapters often number their
        # scenes ("1", "2", …).
        named = chapter_number(title) is not None or CHAPTERISH_RE.search(title)
        split = [("", whole)] if named else _split_at_labels(whole)
        # A story split into its chapters ("The Warped": Chapter 1, 2…) is their section.
        group = d.group or ("" if len(split) == 1 or FRONT_RE.match(title) else title)
        for i, (label, content) in enumerate(split):
            if i == 0 and not label:
                pieces.append((title, url, content, d.group))
            else:
                pieces.append((label, f"{url}@{i}", content, group))
    kept, skipped = [], []
    for title, url, content, group in pieces:
        content = _drop_title_heading(content, title)
        ch = ExtractedChapter(url=url, title=title, html=content,
                              text_length=text_length(content), source="epub")
        classify(ch, "")
        enough = MIN_NUMBERED_CHARS if ch.number is not None else MIN_CHAPTER_CHARS
        if ch.text_length < enough or CONTENTS_RE.search(title) or _lists_chapters(content):
            skipped.append(title)
        else:
            kept.append((ch, group))
    return kept, skipped


LISTED_CHAPTER_RE = re.compile(r"\bchapter\s*\d+", re.I)


def _lists_chapters(content: str) -> bool:
    """A page that is a list of the chapters (a title page with the whole contents on it),
    not a chapter."""
    text = re.sub(r"<[^>]+>", " ", content)
    labels = len(LISTED_CHAPTER_RE.findall(text))
    return labels >= 20 and len(text) < 80 * labels


BARE_NUMBER_RE = re.compile(r"\s*[<\[(]?\s*(\d{1,4})\s*[>\])]?\s*")
_UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
          "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
          "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
NUMBER_WORDS = {w: i for i, w in enumerate(_UNITS)} | {
    (t if u == 0 else f"{t}-{_UNITS[u]}"): 10 * i + u
    for i, t in enumerate(_TENS) if t for u in range(10)}
WORD_CHAPTER_RE = re.compile(r"^\s*chapter\s+([a-z]+(?:[\s-][a-z]+)?)\b", re.I)


BOOK_CHAPTER_RE = re.compile(r"^\s*B(\d{1,3})\s*C(\d{1,4})\b", re.I)  # "B1C2: Resolution"


def _book_number(title: str) -> int | None:
    """Chapter numbers books write their own way: "1", "<1>", "Chapter One",
    "Chapter Twenty-Three", "B1C2" (book 1, chapter 2)."""
    if m := BARE_NUMBER_RE.fullmatch(title):
        return int(m.group(1))
    if m := BOOK_CHAPTER_RE.match(title):
        return int(m.group(2))
    if m := WORD_CHAPTER_RE.match(title):
        words = re.split(r"[\s-]+", m.group(1).casefold())
        for n in (2, 1):  # "Twenty-Three", else "One" (of "Chapter One The Beginning")
            if len(words) >= n and (v := NUMBER_WORDS.get("-".join(words[:n]))) is not None:
                return v
    return None


# A book's own running number before each title: "1. Scholar Meng Hao", "001 – Prologue".
LIST_NUMBER_RE = re.compile(r"^\s*(\d{1,4})\s*(?:[.:)]|\s[–—-])\s*\S")


def _numbered(title: str) -> bool:
    return chapter_number(title) is not None or _book_number(title) is not None


def _chapter_heading(heading: str) -> bool:
    """A heading naming a chapter ("Chapter 1 – Magical Beast", "B1C1: Qin Yu"), not a
    scene ("1") or a volume."""
    return _numbered(heading) and not BARE_NUMBER_RE.fullmatch(heading) and not (
        _names_volume(heading))


def _list_numbers(chapters: list[ExtractedChapter]) -> bool:
    """When most titles start with a running number, that is the chapter's number, even
    where the rest of the title names another ("1313. Chapter 1307B": the translator's
    count, which drifts from the book's)."""
    found = [(c, int(m.group(1))) for c in chapters if (m := LIST_NUMBER_RE.match(c.title))]
    if len(found) < 3 or len(found) < 0.8 * len(chapters):
        return False
    numbers = [n for _c, n in found]
    rising = sum(b > a for a, b in zip(numbers, numbers[1:], strict=False))
    if rising < 0.9 * (len(numbers) - 1):
        return False
    for c, n in found:
        c.number, c.number_end, c.season, c.season_word = float(n), None, None, ""
    return True


def _number(chapters: list[ExtractedChapter], groups: list[str]) -> None:
    """Numbers from titles, in the book's order. Side stories stay where the book puts
    them (unnumbered, so "Side Story 1" isn't taken for chapter 1). When numbering
    restarts (a new volume starts again at chapter 1), the table of contents' groups
    become sections."""
    from .library import _numbers_from_title_pattern  # library imports this module

    # A chapter with entries for its own headings nested under it is no group of chapters.
    groups = ["" if g and _numbered(g) and not _names_volume(g) else g for g in groups]

    for c in chapters:
        if c.season == SIDE_SEASON:
            c.season, c.season_word, c.number, c.number_end = None, "", None, None
        elif c.number is None and (n := _book_number(c.title)) is not None:
            c.number = float(n)
            if BARE_NUMBER_RE.fullmatch(c.title):
                c.title = f"Chapter {n}"
    # With the book's own running numbers, other titles are not chapters of that count
    # ("D001 – Prologue" of a digest version beside "001 – Prologue").
    if not _list_numbers(chapters):
        _numbers_from_title_pattern(chapters)
    if assign_seasons(chapters) and not _starts_over(chapters):
        return  # the titles say which part each chapter is in ("Volume 2 Chapter 1")
    numbers = [c.number for c in chapters if c.number is not None]
    restarts = any(b < a for a, b in zip(numbers, numbers[1:], strict=False))
    if not restarts or not any(groups):
        for c in chapters:
            c.season, c.season_label = None, ""
        _sections_at_restarts(chapters)
        return
    order: dict[str, int] = {}
    for c, g in zip(chapters, groups, strict=True):  # outside every group: no section
        c.season = order.setdefault(g, len(order) + 1) if g else None
        c.season_label = g


URL_VOLUME_RE = re.compile(r"(?<![a-z])vol(?:ume)?[ _.-]*(\d{1,3})(?!\d)", re.I)


def _starts_over(chapters: list[ExtractedChapter]) -> list[int]:
    """Where numbering starts again at chapter 1 within a section."""
    found, last = [], {}
    for i, c in enumerate(chapters):
        if c.number is None:
            continue
        key = c.season or 0
        if c.number <= 1 and last.get(key, 0) >= 5:
            found.append(i)
        last[key] = c.number_end or c.number
    return found


def _sections_at_restarts(chapters: list[ExtractedChapter]) -> None:
    """A book whose numbering starts again (volume by volume) with nothing else to tell
    its parts apart: a section from each restart, named for the volume its files are
    named for ("…/Volume_4_-_Chapter_1.xhtml"), else "Part 2"."""
    starts = _starts_over(chapters)
    if not starts:
        return
    first = next(i for i, c in enumerate(chapters) if c.number is not None)
    bounds = [first, *starts, len(chapters)]
    parts = [chapters[a:b] for a, b in zip(bounds, bounds[1:], strict=False)]
    volumes = []
    for part in parts:
        m = next((m for c in part if c.number is not None
                  and (m := URL_VOLUME_RE.search(c.url))), None)
        volumes.append(int(m.group(1)) if m else None)
    named = None not in volumes and len(set(volumes)) == len(volumes)
    for k, part in enumerate(parts, 1):
        label = f"Volume {volumes[k - 1]}" if named else f"Part {k}"
        for c in part:
            c.season, c.season_label = k, label
