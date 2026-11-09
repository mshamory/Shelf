from types import SimpleNamespace as Row

from shelf.gaps import Gap, find_gaps, gaps_before, missing_count


def test_find_gaps():
    assert find_gaps([1, 2, 3]) == []
    assert find_gaps([128, 130, None, 131, 135]) == [Gap(129, 129), Gap(132, 134)]
    # Part chapters neither create nor fill gaps.
    assert find_gaps([207.001, 207.002, 208, 209.5, 211]) == [Gap(210, 210)]
    assert missing_count(find_gaps([1, 5])) == 3
    assert Gap(129, 129).label == "129" and Gap(132, 134).label == "132–134"


def test_gaps_before_attaches_to_next_listed_chapter():
    rows = [Row(id=1, number=1), Row(id=2, number=None), Row(id=3, number=3),
            Row(id=5, number=6)]  # chapter 4/5 missing, 2 unnumbered
    gaps = find_gaps([1, 3, 6])
    assert gaps_before(rows, gaps) == {3: [Gap(2, 2)], 5: [Gap(4, 5)]}


def test_gaps_per_season():
    from shelf.extract import SIDE_SEASON
    from shelf.gaps import find_season_gaps

    rows = [(1, "Part 1", 1), (1, "Part 1", 3), (2, "Part 2", 1), (2, "Part 2", 2),
            (2, "Part 2", 5), (SIDE_SEASON, "Side stories", 1), (SIDE_SEASON, "Side stories", 9)]
    gaps = find_season_gaps(rows)
    assert [g.full_label for g in gaps] == ["Part 1 2", "Part 2 3–4"]
    chapters = [Row(id=i, number=n, season=s) for i, (s, _l, n) in enumerate(rows)]
    assert {k: [g.full_label for g in v] for k, v in gaps_before(chapters, gaps).items()} == {
        1: ["Part 1 2"], 4: ["Part 2 3–4"]}
