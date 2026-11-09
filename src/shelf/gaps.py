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
    "encrypted": "captured only in encrypted form",
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
    rows: Iterable[tuple[int | None, str, float | None]],
    unreadable: Mapping[int, str] | None = None,
) -> list[Gap]:
    """Gaps per season, from (season, season_label, number) rows. Side stories are skipped.

    `unreadable` numbers carry no season, so they're used only for a novel without seasons.
    """
    from .extract import SIDE_SEASON

    seasons: dict[int | None, tuple[str, list[float | None]]] = {}
    for season, label, number in rows:
        if season == SIDE_SEASON:
            continue
        seasons.setdefault(season, (label, []))[1].append(number)
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
