"""Detect chapters that are probably missing from an archive.

A chapter counts as present if any captured page carries its (integer) number, so
"Chapter 12.5" or "Chapter 207-2" don't create gaps and part-chapters don't fill them.
Hidden chapters still count as present: hiding means "don't show", not "not captured".

Some archives show that a chapter exists without holding its text (an encrypted or locked
API response). Those chapters get a reason, and are listed even past the last chapter.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

REASONS = {
    "": "not captured",
    "encrypted": "captured only in encrypted form, never displayed",
    "locked": "locked; only a preview was captured",
    "preview": "only a preview was captured",
}


@dataclass(frozen=True)
class Gap:
    start: int
    end: int  # inclusive
    season: int | None = None
    season_label: str = ""
    reason: str = ""  # "" (not captured), or a key of REASONS

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    @property
    def label(self) -> str:
        return str(self.start) if self.start == self.end else f"{self.start}–{self.end}"

    @property
    def full_label(self) -> str:
        return f"{self.season_label} {self.label}" if self.season_label else self.label

    @property
    def description(self) -> str:
        return REASONS.get(self.reason, REASONS[""])


def covered(number: float | None, end: float | None = None) -> range:
    """Whole chapter numbers a chapter covers: 12 -> 12; "Chapter 96-97" -> 96, 97."""
    if number is None or number < 0:
        return range(0)
    start = int(number)
    return range(start, max(start, int(end)) + 1 if end is not None else start + 1)


def same_chapter(a: float | None, a_end: float | None, b: float | None,
                 b_end: float | None) -> bool:
    """Whether two numbered chapters are the same chapter: parts must match exactly
    (228.1 isn't 228.2 or 228), whole numbers and ranges overlap ("96-97" includes 97)."""
    if a is None or b is None:
        return False
    if a != int(a) or b != int(b):
        return a == b
    return bool(set(covered(a, a_end)) & set(covered(b, b_end)))


def find_gaps(
    numbers: Iterable[float | None], season: int | None = None, season_label: str = "",
    unreadable: Mapping[int, str] | None = None,
) -> list[Gap]:
    """Ranges of whole chapter numbers absent between the lowest and highest present.

    `unreadable` maps chapter numbers known to exist without text to a reason; ranges are
    split where the reason changes, and such chapters after the last present one are added.
    """
    unreadable = unreadable or {}
    present = sorted({int(n) for n in numbers if n is not None and n >= 0})
    if not present:
        return []
    absent = [n for prev, cur in zip(present, present[1:], strict=False)
              for n in range(prev + 1, cur)]
    absent += sorted(n for n in unreadable if n > present[-1])
    gaps: list[Gap] = []
    for n in absent:
        reason = unreadable.get(n, "")
        last = gaps[-1] if gaps else None
        if last and last.end == n - 1 and last.reason == reason:
            gaps[-1] = Gap(last.start, n, season, season_label, reason)
        else:
            gaps.append(Gap(n, n, season, season_label, reason))
    return gaps


def find_season_gaps(
    rows: Iterable[tuple],
    unreadable: Mapping[int, str] | None = None,
) -> list[Gap]:
    """Gaps per season, from (season, season_label, number[, number_end]) rows. Side stories
    are skipped.

    `unreadable` numbers carry no season, so they're used only for a novel without seasons.
    """
    from .extract import SIDE_SEASON

    seasons: dict[int | None, tuple[str, list[float | None]]] = {}
    for season, label, number, *rest in rows:
        if season == SIDE_SEASON:
            continue
        seasons.setdefault(season, (label, []))[1].extend(
            covered(number, rest[0] if rest else None) or [number])
    single = list(seasons) == [None]
    gaps: list[Gap] = []
    for season in sorted(seasons, key=lambda s: s or 0):
        label, numbers = seasons[season]
        gaps += find_gaps(numbers, season, label, unreadable if single else None)
    return gaps


def reason_counts(gaps: Iterable[Gap]) -> list[tuple[str, int]]:
    """(description, chapter count) per reason, in REASONS order."""
    counts: dict[str, int] = {}
    for g in gaps:
        counts[g.reason] = counts.get(g.reason, 0) + g.size
    return [(REASONS[r], counts[r]) for r in REASONS if r in counts]


def missing_count(gaps: Iterable[Gap]) -> int:
    return sum(g.size for g in gaps)


def gaps_before(chapters, gaps: list[Gap]) -> dict[int | None, list[Gap]]:
    """Attach each gap to the id of the first listed chapter numbered after it.

    `chapters` are rows with `.id` and `.number`, in reading order. Gaps after the last
    chapter are listed under None.
    """
    out: dict[int, list[Gap]] = {}
    pending = sorted(gaps, key=lambda g: (g.season or 0, g.start))
    for ch in chapters:
        if ch.number is None:
            continue
        here = (getattr(ch, "season", None) or 0, ch.number)
        while pending and (pending[0].season or 0, pending[0].end) < here:
            out.setdefault(ch.id, []).append(pending.pop(0))
    if pending:
        out[None] = pending
    return out
