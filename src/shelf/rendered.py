"""Chapters from the text the browser displayed, as recorded in pages.jsonl.

Some sites send chapter text in a form only their own script can turn into prose (e.g.
encrypted) and fill the page in afterwards. The network capture then holds no readable
chapter, but ArchiveWeb.page / Browsertrix also save each page's rendered text. That text
is plain lines with site chrome (menus, settings, comments) and anti-scraping filler mixed
in, and an infinite-scroll page can hold several chapters. This module cuts it into chapters:

  - filler: zero-width characters, lines repeated many times on one page, long lines of
    random tokens with hardly any spaces
  - chrome: lines with letters that appear on many different pages of the archive (scene
    breaks like "***" recur too, but aren't chrome)
  - a chapter starts at a "Chapter N: Title" heading and ends at the next heading, a bare
    "Chapter N" label (the next chapter's header) or two chrome lines in a row (a single
    one may be a stock phrase that recurs in the story)
"""

import html
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .extract import ZERO_WIDTH_RUN_RE, sanitize, text_length
from .wacz import normalize_url

ZERO_WIDTH_RE = re.compile("[​‌‍⁠﻿]")
HEADING_RE = re.compile(
    r"^(?:chapter|ch\.?|episode|ep\.?)\s*(\d+(?:\.\d+)?)(?![\d.])\s*[:：.\-–—]\s*"
    r"(?=[^\n]*[^\W\d_])(\S.{0,200})$", re.I)
LABEL_RE = re.compile(r"^(?:chapter|ch\.?|episode|ep\.?)\s*\d+(?:\.\d+)?$", re.I)
MIN_PAGE_CHARS = 1000
MIN_RENDERED_CHARS = 1500  # stricter than for pages: this text has no structure to go on
PROSE_RE = re.compile(r"[.!?…\"”’»」』)~*]$")
REPEATED_LINE_MIN = 5  # identical lines on one page this often are filler
CHROME_MIN_PAGES = 3


@dataclass
class RenderedChapter:
    url: str
    title: str
    html: str
    text_length: int
    number: float
    ts: str
    page_url: str = ""  # the page whose text held it


def _is_noise(line: str) -> bool:
    """A long run of random tokens: almost no spaces."""
    return len(line) > 80 and line.count(" ") < len(line) * 0.02


def _clean_lines(text: str) -> list[str]:
    lines = [ZERO_WIDTH_RE.sub("", ZERO_WIDTH_RUN_RE.sub("", ln)).strip()
             for ln in text.splitlines()]
    counts = Counter(ln for ln in lines if len(ln) >= 20)
    return [ln for ln in lines if ln and counts.get(ln, 0) < REPEATED_LINE_MIN
            and not _is_noise(ln)]


def looks_like_prose(lines: list[str]) -> bool:
    """Most of the text is in lines that end a sentence (not a list of links or labels)."""
    total = sum(len(ln) for ln in lines)
    ended = sum(len(ln) for ln in lines if PROSE_RE.search(ln))
    return total > 0 and ended >= total * 0.6


def _chapter_url(page_url: str, number: float) -> str:
    parts = urlsplit(normalize_url(page_url))
    segments = parts.path.rstrip("/").split("/")
    n = int(number) if number == int(number) else number
    if segments and segments[-1].isdigit():
        segments[-1] = str(n)
        return urlunsplit(parts._replace(path="/".join(segments), query="", fragment=""))
    return urlunsplit(parts._replace(fragment=f"chapter-{n}"))


def split_chapters(lines: list[str], chrome: set[str]) -> list[tuple[float, str, list[str]]]:
    """(number, heading, body lines) for each chapter heading in a page's lines."""
    out = []
    i = 0
    while i < len(lines):
        m = HEADING_RE.match(lines[i])
        if not m or lines[i] in chrome:
            i += 1
            continue
        heading, body = lines[i], []
        i += 1
        while i < len(lines):
            ln = lines[i]
            if HEADING_RE.match(ln) or LABEL_RE.match(ln):
                break
            if ln in chrome and (i + 1 >= len(lines) or lines[i + 1] in chrome):
                break
            body.append(ln)
            i += 1
        while body and not re.search(r"\w", body[-1]):
            body.pop()  # stray counters/separators before the chrome
        out.append((float(m.group(1)), heading, body))
    return out


def chapters_from_page_texts(texts: list[tuple[str, str, str]]) -> list[RenderedChapter]:
    """Cut rendered page texts ((url, ts, text) triples) into chapters, best per number."""
    pages = [(url, ts, _clean_lines(t)) for url, ts, t in texts if len(t) >= MIN_PAGE_CHARS]
    seen_on = Counter(ln for _u, _t, lines in pages for ln in set(lines))
    chrome_min = max(CHROME_MIN_PAGES, int(len(pages) * 0.3))
    chrome = {ln for ln, n in seen_on.items()
              if n >= chrome_min and re.search(r"[^\W\d_]", ln)}

    best: dict[float, RenderedChapter] = {}
    for url, ts, lines in pages:
        for number, heading, body in split_chapters(lines, chrome):
            if not looks_like_prose(body):
                continue
            content = sanitize("".join(f"<p>{html.escape(ln)}</p>" for ln in body))
            n = text_length(content)
            if n < MIN_RENDERED_CHARS:
                continue
            prev = best.get(number)
            if prev is None or n > prev.text_length:
                best[number] = RenderedChapter(_chapter_url(url, number), heading, content,
                                               n, number, ts, url)
    return [best[k] for k in sorted(best)]
