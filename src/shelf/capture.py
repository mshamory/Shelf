"""Chapters and novels sent from the browser extension.

The extension sends the page exactly as the browser displayed it, with elements the page
hid with CSS already removed (only the browser knows the computed styles). Here the chapter
is extracted like any archived page, numbered, and stored as a "browser" chapter, which
re-imports of the novel's archive leave alone.
"""

import gzip
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from sqlmodel import Session, select

from .db import BROWSER_PREFIX, Chapter, Novel, save_version, utcnow
from .extract import (
    MIN_CHAPTER_CHARS,
    SIDE_SEASON,
    ChapterSpan,
    ExtractedChapter,
    chapter_number,
    classify,
    extract_content,
    is_error_page,
    is_placeholder,
    page_title,
    remove_hidden,
    text_length,
    url_path_text,
)
from .gaps import covered, same_chapter
from .library import (
    BARE_TITLE_RE,
    _is_part,
    _promote_heading,
    _strip_series_prefix,
    _url_key,
    captures_dir,
    resequence,
    title_pattern_numbers,
)
from .wacz import normalize_url

TITLE_SPLIT_RE = re.compile(r"\s+[|–—\-»·]\s+")
SITE_NAME_RE = re.compile(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)',
                          re.I)


class CaptureError(ValueError):
    pass


@dataclass
class CaptureResult:
    chapter: Chapter
    action: str  # "added" | "replaced" (an archive chapter) | "updated" (an earlier capture)
    replaced_title: str = ""
    # The chapter replaced came from another page (matched by number, not URL).
    other_page: bool = False


def _site_words(url: str, html: str) -> set[str]:
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    words = {host.casefold(), host.split(".")[0].casefold()}
    m = SITE_NAME_RE.search(html[:20000])
    if m:
        words.add(m.group(1).strip().casefold())
    return {w for w in words if w}


def clean_title(raw: str, url: str, html: str, series: str) -> str:
    """"Series - Chapter 3: X | Site" -> "Chapter 3: X"."""
    title = _strip_series_prefix(re.sub(r"\s+", " ", raw).strip(), series)
    parts = [p.strip() for p in TITLE_SPLIT_RE.split(title) if p.strip()]
    site = _site_words(url, html)
    squashed = {re.sub(r"\W", "", w) for w in site}
    parts = [p for p in parts
             if p.casefold() not in site and re.sub(r"\W", "", p.casefold()) not in squashed
             and p.casefold() != series.casefold()]
    numbered = [p for p in parts if chapter_number(p) is not None]
    if numbered:
        # "Chapter 3 | Chapter 3: X" -> the fuller one
        return max(numbered, key=len)
    return " - ".join(parts) or title


def extract_page(url: str, html: str, series: str) -> ExtractedChapter:
    """The chapter in a page sent from the browser."""
    own_title = page_title(html)
    if is_error_page(own_title, 200):
        raise CaptureError(f"This looks like an error or bot-check page ({own_title!r})")
    content = extract_content(remove_hidden(html), url)
    n = text_length(content) if content else 0
    if not content or n < MIN_CHAPTER_CHARS:
        raise CaptureError("No chapter text found on this page")
    if is_placeholder(content):
        raise CaptureError("The chapter hasn't finished loading (the page still shows a "
                           "placeholder). Wait until the text is visible and try again.")
    title = clean_title(own_title, url, html, series)
    heading = "" if title.strip() else _main_heading(html, series)
    if heading:
        # No <title> (the site sets it from script): the chapter's name is its <h1>.
        title = heading
    elif not title or BARE_TITLE_RE.fullmatch(title) or chapter_number(title) is None:
        title, content = _promote_heading(title if chapter_number(title) else "", content)
    chapter = ExtractedChapter(url=normalize_url(url), title=title or own_title or url,
                               html=content, text_length=text_length(content), ts="")
    classify(chapter, url_path_text(url))
    parts = [p for p in urlsplit(url).path.split("/") if p]
    if chapter.number is None and parts and parts[-1].isdigit() and not _dated(parts):
        chapter.number = float(parts[-1])  # /series/name/1009 (not a blog's /2016/09/26/1274/)
    if heading and chapter.number is not None and chapter_number(heading) is None:
        n = chapter.number
        chapter.title = f"Chapter {int(n) if n == int(n) else n}: {heading}"
    return chapter


def _main_heading(html: str, series: str) -> str:
    """The page's one <h1>, unless it's just the series or site name."""
    heads = [re.sub(r"\s+", " ", h.text()).strip()
             for h in HTMLParser(remove_hidden(html)).css("h1")]
    heads = [h for h in heads if h and len(h) <= 200 and h.casefold() != series.casefold()]
    return heads[0] if len(heads) == 1 else ""


def _season_for(chapter: ExtractedChapter, existing: list[Chapter]) -> tuple[int | None, str]:
    """Fit the detected season to how the novel's chapters are already split."""
    if chapter.season == SIDE_SEASON:
        return SIDE_SEASON, next((c.season_label for c in existing if c.season == SIDE_SEASON),
                                 "Side stories")
    split = any(c.season not in (None, SIDE_SEASON) for c in existing)
    if not split:
        return None, ""
    season = chapter.season or 1
    label = next((c.season_label for c in existing if c.season == season), "")
    word = next((c.season_label.rsplit(" ", 1)[0] for c in existing
                 if c.season not in (None, SIDE_SEASON) and c.season_label), "Season")
    return season, label or f"{word} {season}"


def _save_raw(novel_id: int, html: str) -> str:
    data = html.encode("utf-8")
    name = hashlib.sha1(data).hexdigest()[:16] + ".html.gz"
    folder = captures_dir(novel_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(gzip.compress(data))
    return str((folder / name).relative_to(folder.parent.parent))


def _fit_range(ec: ExtractedChapter, existing: list[Chapter]) -> None:
    """"Chapter 3-4" is part 4 of chapter 3 in a novel that numbers parts that way."""
    if ec.range_kind == "dash" and any(c.number is not None and _is_part(c.number)
                                       for c in existing):
        span = ChapterSpan(ec.number, ec.number_end, ec.range_kind).as_part()
        ec.number, ec.number_end, ec.range_kind = span.number, span.end, span.range_kind


def find_target(chapters: list[Chapter], url: str, number: float | None,
                season: int | None, number_end: float | None = None) -> Chapter | None:
    """The existing chapter a capture replaces: same URL, else a chapter with one of its
    numbers in the same season."""
    key = _url_key(url)
    for c in chapters:
        if _url_key(c.url) == key:
            return c
    for c in chapters:
        if (c.season or 0) == (season or 0) and same_chapter(number, number_end, c.number,
                                                             c.number_end):
            return c
    return None


def _pattern_numbers(chapters: list[Chapter], ec: ExtractedChapter | None = None):
    """Numbers from the titles' shared pattern ("OVRMMO 106: …", see
    library.title_pattern_numbers) for the unnumbered chapters, and for `ec` if it's
    unnumbered too (set on it directly). Returns (chapter, (number, prefix)) to apply."""
    loose = [c for c in chapters if c.auto_number is None]
    items = [(c.auto_title or c.title, c.url, c.auto_season == SIDE_SEASON) for c in loose]
    new = ec is not None and ec.number is None
    if new:
        items.append((ec.title, ec.url, ec.season == SIDE_SEASON))
    found = title_pattern_numbers(items)
    if new:
        f = found.pop()
        if f is not None:
            ec.number, ec.number_end = f[0], None
            if ec.season == SIDE_SEASON:
                ec.season, ec.season_word = None, ""
    return [(c, f) for c, f in zip(loose, found, strict=True) if f is not None]


def _apply_pattern_numbers(found) -> None:
    for c, (n, _prefix) in found:
        c.auto_number, c.auto_number_end = n, None
        if not c.custom_number:
            c.number, c.number_end = n, None
        if c.auto_season == SIDE_SEASON:  # a numbered "Extra Edition" in the story's order
            c.auto_season, c.auto_season_label = None, ""
            if not c.custom_season:
                c.season, c.season_label = None, ""


def add_chapter(session: Session, novel: Novel, url: str, html: str, title: str = "",
                number: float | None = None, dry_run: bool = False) -> CaptureResult:
    existing = list(novel.chapters)
    ec = extract_page(url, html, novel.title)
    numbered_by_pattern = _pattern_numbers(existing, ec)
    _fit_range(ec, existing)
    if number is not None:
        if number != ec.number:
            ec.number_end = None
        ec.number = number
    season, label = _season_for(ec, existing)
    custom_title = bool(title.strip())
    final_title = title.strip() or ec.title
    target = find_target(existing, ec.url, ec.number, season, ec.number_end)
    action = "added" if target is None else (
        "updated" if target.origin == "browser" else "replaced")

    if target is None:
        ch = Chapter(novel_id=novel.id, position=len(existing))
    else:
        ch = target
    replaced_title = target.title if target is not None else ""
    other_page = target is not None and _url_key(target.url) != _url_key(ec.url)
    if dry_run:
        preview = Chapter(novel_id=novel.id, position=0, number=ec.number,
                          number_end=ec.number_end, season=season,
                          season_label=label, title=final_title, url=ec.url,
                          content_html=ec.html, char_count=ec.text_length, origin="browser")
        return CaptureResult(preview, action, replaced_title, other_page)

    if target is not None and (other_page or target.content_html != ec.html):
        save_version(session, target, f"Replaced by a capture of {ec.url}")
    ch.url = ec.url
    ch.content_html, ch.char_count = ec.html, ec.text_length
    ch.captured_at = utcnow().strftime("%Y%m%d%H%M%S")
    ch.origin = "browser"
    ch.capture_file = _save_raw(novel.id, html)
    ch.auto_title, ch.auto_number, ch.auto_number_end = ec.title, ec.number, ec.number_end
    ch.auto_season, ch.auto_season_label = season, label
    if custom_title or not ch.custom_title:
        ch.title, ch.custom_title = final_title, custom_title
    if not ch.custom_number:
        ch.number, ch.number_end = ec.number, ec.number_end
        ch.custom_number = number is not None
    if not ch.custom_season:
        ch.season, ch.season_label = season, label
    if target is None:
        session.add(ch)
        existing.append(ch)
    _apply_pattern_numbers([(c, f) for c, f in numbered_by_pattern if c is not ch])
    resequence(existing)
    if ch.number is not None and ch.number == int(ch.number) and novel.unreadable_chapters:
        filled = set(covered(ch.number, ch.number_end))
        novel.unreadable_chapters = {k: v for k, v in novel.unreadable_chapters.items()
                                     if int(k) not in filled}
    novel.import_error = None
    session.commit()
    session.refresh(ch)
    return CaptureResult(ch, action, replaced_title, other_page)


def create_novel(session: Session, title: str) -> Novel:
    novel = Novel(wacz_path=f"{BROWSER_PREFIX}new", title=title.strip() or "Untitled novel",
                  metadata_source="manual")
    session.add(novel)
    session.flush()
    novel.wacz_path = f"{BROWSER_PREFIX}{novel.id}"
    session.commit()
    session.refresh(novel)
    return novel


def novel_by_nu_url(session: Session, nu_url: str) -> Novel | None:
    from .novelupdates import MetadataError, normalize_series_url

    def norm(url: str) -> str | None:
        try:
            return normalize_series_url(url)
        except MetadataError:
            return None

    key = norm(nu_url)
    if key is None:
        return None
    for novel in session.exec(select(Novel).where(Novel.nu_url != None)).all():  # noqa: E711
        if norm(novel.nu_url) == key:
            return novel
    return None


def match_score(page_url: str, chapter_urls: list[str]) -> int:
    """How well a page URL fits a novel's chapters: length of the shared path prefix
    on the same host (0 = different site)."""
    page = urlsplit(normalize_url(page_url))
    page_parts = [p for p in page.path.split("/") if p]
    best = 0
    for u in chapter_urls:
        other = urlsplit(u)
        if other.hostname != page.hostname:
            continue
        parts = [p for p in other.path.split("/") if p]
        shared = 1
        for a, b in zip(page_parts, parts, strict=False):
            if a != b:
                break
            shared += 1
        if _dated(page_parts) and _dated(parts):
            # Blogs file every post under its date (/2026/09/07/…): the date says nothing
            # about the series, the post name ("safe-city-ch-134") does.
            same = _slug_stem(page_parts) and _slug_stem(page_parts) == _slug_stem(parts)
            shared = len(page_parts) if same else 1
        best = max(best, shared)
    return best


def _dated(parts: list[str]) -> bool:
    """A path like /2026/09/07/post-name: folders that are all numbers."""
    return len(parts) >= 2 and all(p.isdigit() for p in parts[:-1])


def _slug_stem(parts: list[str]) -> str:
    """A post name without its numbers: "safe-city-ch-134" -> "safe-city-ch"."""
    return re.sub(r"[\W_]*\d+[\W_]*", "-", parts[-1].casefold()).strip("-") if parts else ""


def _words(text: str) -> str:
    return " " + " ".join(re.findall(r"[^\W_]+", text.casefold())) + " "


def _names(novel: Novel) -> list[str]:
    """The novel's title and alternative names, as words; short ones ("ED") are too vague."""
    names = [novel.title, *(a for a in novel.alt_names or [] if isinstance(a, str))]
    return [w for w in (_words(n) for n in names) if len(w.replace(" ", "")) >= 4]


def identify_novel(url: str, page_title: str, scored: list[tuple[Novel, int]]) -> Novel | None:
    """The novel a page surely belongs to, or None when it isn't clear.

    A novel counts when its chapters sit in the same folder as the page (same host and
    path up to the last segment), or when its title or an alternative name appears in the
    page title or the URL. Only a single best candidate is returned.
    """
    parts = [p for p in urlsplit(url).path.split("/") if p]
    in_title = _words(page_title)
    in_url = _words(urlsplit(url).path)
    best: list[tuple[tuple[bool, bool], Novel]] = []
    for novel, score in scored:
        by_folder = score > 0 and score >= len(parts)
        by_name = any(n in in_title or n in in_url for n in _names(novel))
        if by_folder or by_name:
            best.append(((by_folder and by_name, by_folder), novel))
    best.sort(key=lambda b: b[0], reverse=True)
    if len(best) == 1 or (len(best) > 1 and best[0][0] > best[1][0]):
        return best[0][1]
    return None


def refresh_captures(session: Session, novel: Novel) -> int:
    """Re-extract chapters sent from the browser from their saved pages (after extraction
    improves). Edited titles/numbers/seasons are kept. Returns how many were updated."""
    from .config import settings

    chapters = list(novel.chapters)
    updated = 0
    for ch in chapters:
        if ch.origin != "browser" or not ch.capture_file:
            continue
        path = settings.data_dir / ch.capture_file
        try:
            html = gzip.decompress(path.read_bytes()).decode("utf-8", errors="replace")
            ec = extract_page(ch.url, html, novel.title)
        except (OSError, CaptureError):
            continue
        others = [c for c in chapters if c is not ch]
        _fit_range(ec, others)
        season, label = _season_for(ec, others)
        ch.content_html, ch.char_count = ec.html, ec.text_length
        ch.auto_title, ch.auto_number, ch.auto_number_end = ec.title, ec.number, ec.number_end
        ch.auto_season, ch.auto_season_label = season, label
        if not ch.custom_title:
            ch.title = ec.title
        if not ch.custom_number:
            ch.number, ch.number_end = ec.number, ec.number_end
        if not ch.custom_season:
            ch.season, ch.season_label = season, label
        updated += 1
    if updated:
        _apply_pattern_numbers(_pattern_numbers(chapters))
        resequence(chapters)
    return updated
