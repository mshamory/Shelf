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
