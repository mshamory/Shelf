"""Turn archived HTML pages into clean, ordered chapters.

Main-content extraction is done by `trafilatura`; sanitising by `nh3`.
This module only adds the novel-specific heuristics: chapter numbering,
ordering, title clean-up and filtering of non-chapter pages.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import nh3
import trafilatura
from selectolax.lexbor import LexborHTMLParser as HTMLParser

ALLOWED_TAGS = {
    "p", "br", "hr", "em", "strong", "i", "b", "u", "s", "del", "sub", "sup", "small",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "li", "pre", "code",
    "table", "thead", "tbody", "tr", "th", "td", "img", "figure", "figcaption", "span",
}
ALLOWED_ATTRS = {"img": {"src", "alt", "title"}, "td": {"colspan", "rowspan"},
                 "th": {"colspan", "rowspan"}}

ERROR_TITLE_RE = re.compile(
    r"^\s*(?:error|404|403|500|502|503)\b|"
    r"\b(?:page not found|not found|access denied|just a moment|attention required|"
    r"error \d{3}|\d{3} error|making sure you(?:'|’)re not a bot|checking your browser|"
    r"verify(?:ing)? (?:you are|that you are) (?:a )?human|ddos-guard)\b",
    re.I,
)
TITLE_SEPARATORS = re.compile(r"\s+[|–—\-:»·]\s+")

# "Chapter 12", "Ch. 12.5", "Ep 3", "第12章", "c12", "/chapter-12/"
_NUM = r"(\d+(?:\.\d+)?)"
CHAPTER_PATTERNS = [
    # In titles, "Chapter 207-2" is part 2 of chapter 207 and "Chapter 96-97" (or "Ch. 7 & 8",
    # "Chapters 7 to 9") covers several chapters; see chapter_span.
    re.compile(rf"\b(?:chapters?|chpaters?|chaps?|chs?|episodes?|eps?)\s*[.#:\-]?\s*{_NUM}"
               r"(?:[-~](\d{1,4})\b(?!\.\d)"
               r"|\s*(?:&|\+|~|\band\b|\bto\b)\s*(?:(?:chapter|ch)\.?\s*)?(\d{1,4})\b(?!\.\d))?",
               re.I),
    re.compile(rf"第\s*{_NUM}\s*[章话話回]"),
    re.compile(rf"(?:^|[/\-_])(?:c|ch|chapter|episode|ep)[\-_]?{_NUM}(?:[/\-_.]|$)", re.I),
]

# A season marker must come before the chapter marker: "Part 2 Chapter 16", "S2 Ep 3".
# ("Chapter 12 Part 2" is a part of chapter 12, not a season.)
SEASON_RE = re.compile(
    r"\b(?:(season|part|book|volume|vol|arc)\.?\s*|(s))(\d+)\b(?!\.\d)", re.I
)
SEASON_WORDS = {"s": "Season", "vol": "Volume"}
# Unnumbered extras: "Side Story 1-1", "Extra 3", "Special Chapter", ...
SIDE_RE = re.compile(
    r"\b(?:side[\s\-]*stor(?:y|ies)|extras?|special|bonus|omake|ss)\b"
    r"(?:\s*[.#:\-]?\s*(\d+)(?:-(\d{1,2}))?)?",
    re.I,
)
SIDE_SEASON = 1_000_000  # sorts after every real season
SIDE_LABEL = "Side stories"

MIN_CHAPTER_CHARS = 300

# Pages captured before the site's script filled in the chapter ("Verifying access…").
PLACEHOLDER_RE = re.compile(
    r"\b(verifying (your )?access|checking (your )?access|decrypting|loading (the )?chapter"
    r"|unlock (this|the) chapter|this chapter is locked|purchase (this|the) chapter"
    r"|log ?in to (read|unlock)|sign in to (read|unlock))\b",
    re.I,
)
PLACEHOLDER_MAX_CHARS = 1500


@dataclass
class ExtractedChapter:
    url: str
    title: str
    html: str
    text_length: int
    number: float | None = None
    ts: str = ""
    season: int | None = None  # detected season/part/book number, or SIDE_SEASON
    season_word: str = ""  # "Part", "Season", "Book", ...
    season_label: str = ""  # final grouping label ("Part 2"); empty when not split
    number_hint: float | None = None  # number from structured data (e.g. an XHR payload)
    number_end: float | None = None  # last chapter covered by a "Chapter 96-97" post
    range_kind: str = ""  # see ChapterSpan
    duplicate: bool = False  # another capture of the same number was preferred
    source: str = "html"  # "html" page or "xhr" JSON payload


def detect_charset(body: bytes, content_type: str = "") -> str:
    m = re.search(r"charset=([\w\-]+)", content_type, re.I)
    if m:
        return m.group(1)
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w\-]+)", body[:4096], re.I)
    return m.group(1).decode() if m else "utf-8"


def decode_html(body: bytes, content_type: str = "") -> str:
    try:
        return body.decode(detect_charset(body, content_type), errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


HIDDEN_STYLE_RE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px|em|rem|%)?\s*(?:;|$)|"
    r"opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)|(?:top|left|right|bottom)\s*:\s*-\d{3,}px|"
    r"clip\s*:\s*rect\(\s*0",
    re.I,
)


def is_hidden_style(declarations: str) -> bool:
    if HIDDEN_STYLE_RE.search(declarations):
        return True
    # "Visually hidden" boxes: 1px wide/high with overflow clipped.
    return bool(re.search(r"overflow\s*:\s*hidden", declarations, re.I)
                and re.search(r"(?:width|height)\s*:\s*[01](?:px)?\s*(?:;|$)", declarations, re.I))


_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")


def remove_hidden(html: str) -> str:
    """Drop elements a browser wouldn't show (anti-scraping junk, screen-reader-only text).

    Handles inline styles, the `hidden`/`aria-hidden` attributes and classes that a
    <style> block in the content hides. Sanitising removes styles, which would otherwise
    reveal them.
    """
    if not re.search(r"aria-hidden|\bhidden\b|style", html, re.I):
        return html
    import lxml.html
    from lxml import etree

    root = lxml.html.fragment_fromstring(html, create_parent="div")
    hidden_classes: set[str] = set()
    for style in root.iter("style"):
        for selectors, declarations in _CSS_RULE_RE.findall(style.text or ""):
            if is_hidden_style(declarations):
                for sel in selectors.split(","):
                    m = re.fullmatch(r"\s*(?:[a-z0-9]*)\.([\w-]+)\s*", sel, re.I)
                    if m:
                        hidden_classes.add(m.group(1))
    for el in list(root.iter()):
        if el is root or not isinstance(el.tag, str) or el.getparent() is None:
            continue
        if (el.tag in ("style", "script") or el.get("aria-hidden") == "true"
                or el.get("hidden") is not None or is_hidden_style(el.get("style") or "")
                or hidden_classes.intersection((el.get("class") or "").split())):
            el.drop_tree()
    return (root.text or "") + "".join(
        etree.tostring(c, encoding="unicode", method="html") for c in root)


# Invisible watermarks: runs of zero-width characters, variation selectors (U+FE00–FE0F,
# U+E0100–E01EF) or tag characters (U+E0000–E007F). Single ones are kept (emoji use them).
ZERO_WIDTH_RUN_RE = re.compile(
    "[\u200b\u200c\u200d\u2060\ufeff\ufe00-\ufe0f\U000e0000-\U000e007f"
    "\U000e0100-\U000e01ef]{3,}"
)


def sanitize(html: str) -> str:
    html = ZERO_WIDTH_RUN_RE.sub("", html)
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        url_schemes={"http", "https", "data"},
        link_rel=None,
    )


def _strip_wrapper(html: str) -> str:
    tree = HTMLParser(html)
    body = tree.body
    return body.inner_html if body is not None else html


def extract_content(html: str, url: str | None = None) -> str | None:
    """Return sanitised HTML of the page's main content, or None."""
    best: str | None = None
    # Recall mode keeps paragraph structure best on typical WordPress/translator sites;
    # fall back to the default (balanced) mode if it finds nothing.
    for opts in ({"favor_recall": True}, {}):
        best = trafilatura.extract(
            html,
            url=url,
            output_format="html",
            include_formatting=True,
            include_images=True,
            include_links=False,
            include_comments=False,
            include_tables=True,
            deduplicate=False,
            **opts,
        )
        if best:
            break
    if not best:
        return None
    cleaned = sanitize(_strip_wrapper(best))
    # Drop empty paragraphs left behind by sanitising.
    cleaned = re.sub(r"<p>\s*(?:<br>\s*)*</p>", "", cleaned)
    return strip_trailing_widgets(cleaned).strip() or None


# End-of-chapter boxes that main-content extraction can pick up along with the chapter:
# author/translator cards, donation and follow prompts.
WIDGET_MARKER_RE = re.compile(
    r"^\s*(?:meet|about) the (?:author|translator|editor)s?\b"
    r"|^\s*(?:support|follow) (?:me|us|the (?:author|translator|team))\b"
    r"|^\s*(?:buy me a coffee|become a patron|donate)\b",
    re.I,
)
WIDGET_MAX_CHARS = 600  # a widget is short; anything longer after the marker is content
_BLOCK_RE = re.compile(r"<(p|h[1-6]|li|div|blockquote)\b[^>]*>(.*?)</\1>", re.S)


def strip_trailing_widgets(html: str) -> str:
    """Cut an author card / donation box at the very end of a chapter (and the avatar or
    blank lines just before it)."""
    tail_start = max(0, len(html) - 4000)
    for m in reversed(list(_BLOCK_RE.finditer(html, tail_start))):
        text = re.sub(r"<[^>]+>", "", m.group(2))
        if not WIDGET_MARKER_RE.search(text):
            continue
        after = re.sub(r"<[^>]+>", "", html[m.start():])
        if len(after.strip()) > WIDGET_MAX_CHARS:
            break
        head = html[: m.start()]
        # Avatar images, line breaks and empty blocks right before the marker.
        head = re.sub(r"(?:\s*(?:<img\b[^>]*>|<br>|<(p|div)>\s*</\1>))+\s*$", "", head)
        return head
    return html


def text_length(html: str) -> int:
    tree = HTMLParser(html)
    node = tree.body or tree.root
    return len((node.text(separator=" ") if node else "").strip())


def page_title(html: str) -> str:
    tree = HTMLParser(html)
    for sel in ('meta[property="og:title"]', 'meta[name="twitter:title"]'):
        node = tree.css_first(sel)
        if node and node.attributes.get("content"):
            return node.attributes["content"].strip()
    node = tree.css_first("title")
    return node.text(strip=True) if node else ""


# "Chapter 191 Part 2", "Chapter 191 (Part 2)": part 2 of chapter 191, like "191-2".
PART_AFTER_RE = re.compile(r"\s*[:\-–—(]?\s*\(?part\s*(\d{1,2})\b", re.I)
# A range wider than this is more likely a typo or a compilation than one post.
MAX_RANGE_SPAN = 20


@dataclass(frozen=True)
class ChapterSpan:
    number: float
    end: float | None = None  # last chapter covered, for "Chapter 96-97"
    # "dash" when the range came from "N-M", which some novels use for parts ("Chapter 3-4"
    # = part 4 of chapter 3); "word" for "N & M", "N to M", which are always ranges.
    range_kind: str = ""

    def as_part(self) -> ChapterSpan:
        """Read a dash range as a part instead: 3-4 -> 3.004."""
        if self.range_kind != "dash" or self.end is None or self.end > 99:
            return self
        return ChapterSpan(self.number + int(self.end) / 1000)


def _span(m: re.Match, pat: re.Pattern) -> ChapterSpan | None:
    try:
        number = float(m.group(1))
    except ValueError:
        return None
    dash = m.group(2) if pat.groups > 1 else None
    word = m.group(3) if pat.groups > 2 else None
    second = dash or word
    if second is None:
        return ChapterSpan(number)
    end = int(second)
    if number == int(number) and number < end <= number + MAX_RANGE_SPAN:
        return ChapterSpan(number, float(end), "dash" if dash else "word")
    if dash and len(dash) <= 2 and end <= number:
        return ChapterSpan(number + end / 1000)  # part
    return ChapterSpan(number)


def chapter_span(*candidates: str) -> ChapterSpan | None:
    """The chapter (or range of chapters) named by the first candidate that names one."""
    for cand in candidates:
        if not cand:
            continue
        for pat in CHAPTER_PATTERNS:
            m = pat.search(cand)
            if m:
                span = _span(m, pat)
                if span is None:
                    continue
                part = PART_AFTER_RE.match(cand, m.end())
                if part and span.end is None and span.number == int(span.number):
                    span = ChapterSpan(span.number + int(part.group(1)) / 1000)
                return span
    return None


def chapter_number(*candidates: str) -> float | None:
    span = chapter_span(*candidates)
    return span.number if span else None


def _chapter_match(text: str):
    for pat in CHAPTER_PATTERNS:
        m = pat.search(text)
        if m:
            return m
    return None


def chapter_season(*candidates: str) -> tuple[int, str] | None:
    """Season marker preceding the chapter marker, e.g. ("Part 2 Chapter 16") -> (2, "Part")."""
    for cand in candidates:
        if not cand:
            continue
        m = _chapter_match(cand)
        if m is None:
            continue
        found = list(SEASON_RE.finditer(cand[: m.start()]))
        if found:
            m = found[-1]
            word, n = m.group(1) or m.group(2), int(m.group(3))
            word = SEASON_WORDS.get(word.lower(), word.capitalize())
            return n, word
        return None  # the first candidate with a chapter marker decides
    return None


def side_story_number(title: str) -> float | None | bool:
    """For an unnumbered side story: its number (or True if unnumbered); False otherwise."""
    m = SIDE_RE.search(title or "")
    if not m:
        return False
    if m.group(1):
        n = float(m.group(1))
        return n + int(m.group(2)) / 1000 if m.group(2) else n
    return True


def classify(chapter: ExtractedChapter, path_text: str) -> None:
    """Fill in number/season for a chapter from its title and URL path."""
    span = chapter_span(chapter.title, path_text)
    chapter.number = span.number if span else None
    chapter.number_end = span.end if span else None
    chapter.range_kind = span.range_kind if span else ""
    if chapter.number_hint is not None:
        if chapter.number_hint != chapter.number:
            chapter.number_end, chapter.range_kind = None, ""
        chapter.number = chapter.number_hint
    if chapter.number is not None:
        season = chapter_season(chapter.title, path_text)
        if season:
            chapter.season, chapter.season_word = season
        return
    side = side_story_number(chapter.title)
    if side is not False:
        chapter.season, chapter.season_word = SIDE_SEASON, SIDE_LABEL
        chapter.number = side if side is not True else None


def url_path_text(url: str) -> str:
    return unquote(urlparse(url).path)


def is_error_page(title: str, status: int) -> bool:
    if not 200 <= status < 300:
        return True
    # "Chapter 404" is a chapter, not an error page.
    return chapter_number(title or "") is None and bool(ERROR_TITLE_RE.search(title or ""))


def is_placeholder(html: str) -> bool:
    """A short page whose text says the chapter is still loading, locked or being verified."""
    if text_length(html) > PLACEHOLDER_MAX_CHARS:
        return False
    text = re.sub(r"<[^>]+>", " ", html)
    return bool(PLACEHOLDER_RE.search(text))


_SEP_CHARS = " |–—-:»·"


def strip_common_affixes(titles: list[str]) -> list[str]:
    """Remove site/series segments shared by most titles.

    "Series - Chapter 1.1 - Novel Live - Reading Novel Free" -> "Chapter 1.1". Segments are
    compared case-insensitively and stripped repeatedly, so stacked suffixes all go.
    """
    if len(titles) < 2:
        return titles
    out = [re.sub(r"\s+", " ", t).strip() for t in titles]
    # Only titles that have segments can share one; mixed sources (e.g. "Chapter 3: X" from
    # an API next to "Series - Chapter 4 - Site" pages) shouldn't dilute the vote.
    segmented = sum(1 for t in out if len(TITLE_SEPARATORS.split(t)) > 1)
    threshold = max(2, int(len(titles) * 0.6)) if segmented == len(out) else \
        max(2, int(segmented * 0.6))
    for _ in range(5):
        changed = False
        last, first = Counter(), Counter()
        for t in out:
            parts = TITLE_SEPARATORS.split(t)
            if len(parts) > 1:
                last[parts[-1].casefold()] += 1
                first[parts[0].casefold()] += 1
        suffix = next((x for x, n in last.most_common(1) if n >= threshold), None)
        prefix = next((x for x, n in first.most_common(1) if n >= threshold), None)
        if prefix and chapter_number(prefix) is not None:
            prefix = None
        for i, t in enumerate(out):
            parts = TITLE_SEPARATORS.split(t)
            if len(parts) > 1 and suffix and parts[-1].casefold() == suffix:
                t = t[: len(t) - len(parts[-1])].rstrip(_SEP_CHARS)
                changed = True
            if len(parts) > 1 and prefix and parts[0].casefold() == prefix:
                t = t[len(parts[0]):].lstrip(_SEP_CHARS)
                changed = True
            elif prefix and t.casefold().startswith(prefix + " ") and \
                    chapter_number(t[len(prefix):]) is not None:
                # Prefix glued on without a separator: "Series Part 2 Chapter 4: Title".
                t = t[len(prefix):].lstrip(_SEP_CHARS)
                changed = True
            out[i] = t.strip() or "Untitled"
        if not changed:
            break
    return _strip_shared_suffix(out)


def _strip_shared_suffix(titles: list[str]) -> list[str]:
    """Drop a series name repeated after every chapter label ("Ch 3: Series" -> "Ch 3")."""
    if len(titles) < 2:
        return titles
    suffix = titles[0]
    for t in titles[1:]:
        while suffix and not t.endswith(suffix):
            suffix = suffix[1:]
    # Only cut at a separator so we never split words, and keep something meaningful.
    m = re.search(r"\s*[:|–—\-]\s+(?=\S)", suffix)
    if not m or len(suffix) - m.end() < 4:
        return titles
    suffix = suffix[m.start():]
    stripped = [t[: -len(suffix)].rstrip(" |–—-:") for t in titles]
    if all(chapter_number(s) is not None for s in stripped):
        return stripped
    return titles


def assign_seasons(chapters: list[ExtractedChapter]) -> bool:
    """Decide whether detected seasons are real and label them. Returns True if split.

    Seasons only matter when chapter numbering restarts: "Part 2 Chapter 1" after
    "Chapter 1". Volumes with continuous numbering ("Volume 3, Chapter 304") stay one list.
    Chapters without a marker belong to the first season.
    """
    main = [c for c in chapters if c.season != SIDE_SEASON]
    seen: dict[int, set[int]] = {}
    for c in main:
        if c.number is not None:
            seen.setdefault(int(c.number), set()).add(c.season or 1)
    split = any(len(s) > 1 for s in seen.values())
    words = Counter(c.season_word for c in main if c.season is not None)
    word = words.most_common(1)[0][0] if words else "Season"
    for c in chapters:
        if c.season == SIDE_SEASON:
            c.season_label = SIDE_LABEL
        elif split:
            if c.season is None and c.number is not None:
                c.season = 1
            c.season_label = f"{word} {c.season}" if c.season is not None else ""
        else:
            c.season, c.season_label = None, ""
    return split


def order_chapters(chapters: list[ExtractedChapter]) -> list[ExtractedChapter]:
    """Order by season, then chapter number; unnumbered chapters keep capture order."""
    assign_seasons(chapters)
    numbered = sum(c.number is not None for c in chapters)
    if numbered >= max(1, len(chapters) // 2):
        # Unnumbered pages (prologues, ...) sort by capture time among themselves,
        # before numbered ones if they were captured first. Side stories go last.
        return sorted(
            chapters,
            key=lambda c: (
                c.season or 0,
                c.number if c.number is not None else float("-inf"),
                c.ts,
            ),
        )
    return sorted(chapters, key=lambda c: c.ts)
