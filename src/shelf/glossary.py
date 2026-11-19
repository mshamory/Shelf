"""A novel's glossary: names, places, techniques and other terms with a description,
marked in chapters where they appear.

A term matches its own name as written, and any aliases: one per line, plain text, or a
regular expression between slashes (`/Spirit Stones?/`). Plain names match whole words
and any spacing between words."""

from __future__ import annotations

import html as html_lib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from .db import GlossaryTerm

# A tag, quoted attribute values (which may hold ">") included.
TAG_RE = re.compile(r"""<(?:[^>"']|"[^"]*"|'[^']*')*>""")


class InvalidTerm(ValueError):
    pass


def split_aliases(text: str) -> list[str]:
    return list(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()))


def _is_word_char(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


@dataclass
class _Pattern:
    rx: re.Pattern
    lower: bool  # matched against the lowercased text (a term that ignores case)
    left_edge: bool  # must not follow a letter or digit
    fallback: re.Pattern | None = None  # when lowercasing changes the text's length


def _literal(text: str, ignore_case: bool) -> _Pattern:
    """A plain name: any spacing between words; whole words, so "Lin" doesn't match inside
    "Linley" (edges that aren't letters or digits, like CJK, match anywhere). The pattern
    starts with the text itself, which Python's regex engine finds fast; the left edge is
    checked after a match."""
    def source(t: str) -> str:
        src = re.sub(r"(\\\s)+", r"\\s+", re.escape(t))
        return src + (r"(?![A-Za-z0-9])" if _is_word_char(text[-1]) else "")

    left = _is_word_char(text[0])
    if ignore_case:
        return _Pattern(re.compile(source(text.lower())), True, left,
                        re.compile(source(text), re.IGNORECASE))
    return _Pattern(re.compile(source(text)), False, left)


class Matcher:
    """Finds a term (its name and aliases) in text."""

    def __init__(self, patterns: list[_Pattern]):
        self.patterns = patterns

    def spans(self, text: str, lowered: str | None) -> Iterable[tuple[int, int]]:
        """`lowered` is text.lower() when it's as long as text, else None."""
        for p in self.patterns:
            if p.lower:
                rx, subject = (p.rx, lowered) if lowered is not None else (p.fallback, text)
            else:
                rx, subject = p.rx, text
            for m in rx.finditer(subject):
                start, end = m.span()
                if end > start and not (p.left_edge and start and _is_word_char(
                        text[start - 1])):
                    yield start, end

    def search(self, text: str) -> bool:
        lowered = text.lower()
        return next(iter(self.spans(text, lowered if len(lowered) == len(text) else None)),
                    None) is not None


def compile_term(name: str, aliases: Iterable[str], ignore_case: bool) -> Matcher:
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        raise InvalidTerm("The term is empty")
    patterns = [_literal(name, ignore_case)]
    for alias in aliases:
        if len(alias) > 2 and alias.startswith("/") and alias.endswith("/"):
            try:
                rx = re.compile(alias[1:-1], re.IGNORECASE if ignore_case else 0)
            except re.error as e:
                raise InvalidTerm(f"Invalid regular expression {alias}: {e}") from e
            if rx.search(""):
                raise InvalidTerm(f"{alias} matches empty text")
            patterns.append(_Pattern(rx, False, False))
        elif alias := re.sub(r"\s+", " ", alias).strip():
            patterns.append(_literal(alias, ignore_case))
    return Matcher(patterns)


@dataclass
class CompiledTerm:
    id: int
    rx: Matcher


def compile_terms(terms: Iterable[GlossaryTerm]) -> list[CompiledTerm]:
    out = []
    for t in terms:
        try:
            out.append(CompiledTerm(t.id, compile_term(t.term, t.aliases, t.ignore_case)))
        except InvalidTerm:
            continue  # validated on save
    return out


SEP = "\x00"  # between pieces of text: no term matches across a tag


def _matches(text: str, terms: list[CompiledTerm]) -> list[tuple[int, int, int]]:
    """Non-overlapping (start, end, term id): the earliest, then the longest, wins."""
    lowered = text.lower()
    lowered = lowered if len(lowered) == len(text) else None
    found = [(start, start - end, t.id) for t in terms for start, end in t.rx.spans(text, lowered)
             if SEP not in text[start:end]]
    out, pos = [], 0
    for start, neg_len, tid in sorted(found):
        if start >= pos:
            out.append((start, start - neg_len, tid))
            pos = start - neg_len
    return out


def mark_terms(html: str, terms: list[CompiledTerm],
               wrap=None) -> tuple[str, list[int]]:
    """The chapter's HTML with each mention of a term wrapped (by default in a
    `<span class="gloss" data-gloss="id">`), and the ids of the terms found, in order of
    first mention. Only text is matched, never tags or attributes, and a mention split by
    a tag ("Lin <em>Dong</em>") isn't."""
    if not terms or not html:
        return html, []
    wrap = wrap or (lambda tid, text, first: f'<span class="gloss" data-gloss="{tid}">'
                                            f"{text}</span>")
    # The text between tags, unescaped and joined, so each term scans the chapter once.
    tags, texts, last = [], [], 0
    for m in TAG_RE.finditer(html):
        texts.append(html_lib.unescape(html[last:m.start()]).replace(SEP, ""))
        tags.append(m.group(0))
        last = m.end()
    texts.append(html_lib.unescape(html[last:]).replace(SEP, ""))
    found = _matches(SEP.join(texts), terms)
    if not found:
        return html, []

    out, seen, k, base = [], {}, 0, 0  # k: the piece of text matches are in
    pieces: list[list[tuple[int, int, int]]] = [[] for _ in texts]
    for start, end, tid in found:
        while start >= base + len(texts[k]) + 1:
            base += len(texts[k]) + 1
            k += 1
        pieces[k].append((start - base, end - base, tid))
    original = TAG_RE.split(html)  # the text as it was, for pieces without a mention
    for i, text in enumerate(texts):
        if not pieces[i]:
            out.append(original[i])
        else:
            pos = 0
            for start, end, tid in pieces[i]:
                out.append(html_lib.escape(text[pos:start], quote=False))
                out.append(wrap(tid, html_lib.escape(text[start:end], quote=False),
                                tid not in seen))
                seen.setdefault(tid, None)
                pos = end
            out.append(html_lib.escape(text[pos:], quote=False))
        if i < len(tags):
            out.append(tags[i])
    return "".join(out), list(seen)


def plain_text(html: str) -> str:
    return html_lib.unescape(TAG_RE.sub(" ", html))


def mentions(rx: Matcher, chapters: Iterable[str]) -> int:
    """How many of the chapters mention the term."""
    return sum(1 for html in chapters if rx.search(plain_text(html)))
