"""Remove unwanted text (watermarks, translator asides, ads) from chapter HTML.

Rules either cut just the matching text or drop every block (paragraph, list item,
heading, ...) whose text matches. Blocks left empty by a text cut are dropped too.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

import lxml.html
from lxml import etree

from .db import TrimRule

MODES = {"text": "Remove matching text", "paragraph": "Remove paragraphs containing it"}
BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
              "figcaption", "td", "th", "div", "span"}
# Blocks that a paragraph rule may remove: innermost text containers only, so a rule
# can never take out a whole wrapper with the chapter in it.
PARAGRAPH_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "figcaption",
                  "blockquote", "td", "th", "div"}


class InvalidRule(ValueError):
    pass


def compile_rule(pattern: str, is_regex: bool, ignore_case: bool) -> re.Pattern:
    if not pattern.strip():
        raise InvalidRule("The pattern is empty")
    source = pattern if is_regex else re.escape(pattern.strip())
    if not is_regex:
        # Match regardless of how whitespace was laid out in the HTML.
        source = re.sub(r"(\\\s)+", r"\\s+", source)
    try:
        rx = re.compile(source, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise InvalidRule(f"Invalid regular expression: {e}") from e
    if rx.search(""):
        raise InvalidRule("The pattern matches empty text, which would remove nothing useful")
    return rx


@dataclass
class CompiledRule:
    id: int | None
    rx: re.Pattern
    mode: str


def compile_rules(rules: Iterable[TrimRule]) -> list[CompiledRule]:
    out = []
    for r in rules:
        if not r.enabled:
            continue
        try:
            out.append(CompiledRule(r.id, compile_rule(r.pattern, r.is_regex, r.ignore_case),
                                    r.mode))
        except InvalidRule:
            continue  # validated on save; skip anything that slipped through
    return out


def _has_block_child(el) -> bool:
    return any(isinstance(c.tag, str) and c.tag in PARAGRAPH_TAGS for c in el.iterdescendants())


def _is_empty(el) -> bool:
    return not el.text_content().strip() and el.find(".//img") is None


def apply_rules(html: str, rules: list[CompiledRule]) -> tuple[str, int]:
    """Return (trimmed html, number of removals)."""
    if not rules or not html.strip():
        return html, 0
    root = lxml.html.fragment_fromstring(html, create_parent="div")
    removed = 0

    para_rules = [r for r in rules if r.mode == "paragraph"]
    if para_rules:
        for el in list(root.iter(*PARAGRAPH_TAGS)):
            if el is root or el.getparent() is None or _has_block_child(el):
                continue
            text = el.text_content()
            if any(r.rx.search(text) for r in para_rules):
                el.drop_tree()  # keeps the element's tail text in place
                removed += 1

    text_rules = [r for r in rules if r.mode == "text"]
    if text_rules:
        touched = []
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue  # comments etc.
            for attr in ("text", "tail"):
                value = getattr(el, attr)
                if not value:
                    continue
                new = value
                for r in text_rules:
                    new, n = r.rx.subn("", new)
                    removed += n
                if new != value:
                    setattr(el, attr, new)
                    touched.append(el if attr == "text" else el.getparent())
        # Drop blocks emptied by the cuts (e.g. a paragraph that was only a watermark).
        for el in touched:
            while (el is not None and el is not root and isinstance(el.tag, str)
                   and el.tag in BLOCK_TAGS and _is_empty(el)):
                parent = el.getparent()
                el.drop_tree()
                el = parent

    out = (root.text or "") + "".join(
        etree.tostring(c, encoding="unicode", method="html") for c in root
    )
    return out, removed


def count_matches(text: str, rule: CompiledRule) -> int:
    return len(rule.rx.findall(text)) if rule.mode == "text" else int(bool(rule.rx.search(text)))
