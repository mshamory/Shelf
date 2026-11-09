"""Detect chapters that are probably missing from an archive.

A chapter counts as present if any captured page carries its (integer) number, so
"Chapter 12.5" or "Chapter 207-2" don't create gaps and part-chapters don't fill them.
Hidden chapters still count as present: hiding means "don't show", not "not captured".
"""

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Gap:
    start: int
    end: int  # inclusive
    season: int | None = None
    season_label: str = ""

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    @property
    def label(self) -> str:
        return str(self.start) if self.start == self.end else f"{self.start}–{self.end}"

    @property
    def full_label(self) -> str:
        return f"{self.season_label} {self.label}" if self.season_label else self.label


def find_gaps(
    numbers: Iterable[float | None], season: int | None = None, season_label: str = ""
) -> list[Gap]:
    """Ranges of whole chapter numbers absent between the lowest and highest present."""
    present = sorted({int(n) for n in numbers if n is not None and n >= 0})
    gaps = []
    for prev, cur in zip(present, present[1:], strict=False):
        if cur > prev + 1:
            gaps.append(Gap(prev + 1, cur - 1, season, season_label))
    return gaps


def find_season_gaps(rows: Iterable[tuple[int | None, str, float | None]]) -> list[Gap]:
    """Gaps per season, from (season, season_label, number) rows. Side stories are skipped."""
    from .extract import SIDE_SEASON

    seasons: dict[int | None, tuple[str, list[float | None]]] = {}
    for season, label, number in rows:
        if season == SIDE_SEASON:
            continue
        seasons.setdefault(season, (label, []))[1].append(number)
    gaps: list[Gap] = []
    for season in sorted(seasons, key=lambda s: s or 0):
        label, numbers = seasons[season]
        gaps += find_gaps(numbers, season, label)
    return gaps


def missing_count(gaps: Iterable[Gap]) -> int:
    return sum(g.size for g in gaps)


def gaps_before(chapters, gaps: list[Gap]) -> dict[int, list[Gap]]:
    """Attach each gap to the id of the first listed chapter numbered after it.

    `chapters` are rows with `.id` and `.number`, in reading order.
    """
    out: dict[int, list[Gap]] = {}
    pending = sorted(gaps, key=lambda g: (g.season or 0, g.start))
    for ch in chapters:
        if ch.number is None:
            continue
        here = (getattr(ch, "season", None) or 0, ch.number)
        while pending and (pending[0].season or 0, pending[0].end) < here:
            out.setdefault(ch.id, []).append(pending.pop(0))
    return out
