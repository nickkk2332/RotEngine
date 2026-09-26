import random

import pytest

from rotengine.dice import Check, Dice, p_success


def test_parse_and_mean():
    d = Dice.parse("2d6+2")
    assert (d.n, d.sides, d.add) == (2, 6, 2)
    assert d.mean == 9
    assert Dice.parse("d6-1") == Dice(1, 6, -1)
    with pytest.raises(ValueError):
        Dice.parse("banana")


def test_distribution_sums_to_one():
    assert sum(Dice(3, 6).distribution().values()) == pytest.approx(1.0)


def test_3d6_curve():
    assert p_success(10) == pytest.approx(0.5)
    assert p_success(3) == pytest.approx(4 / 216)      # a 3 or 4 always succeeds
    assert p_success(25) == pytest.approx(1 - 4 / 216)  # a 17 or 18 always fails
    # the bell curve: +2 in the middle is worth far more than +2 at the edge
    assert p_success(11) - p_success(9) > p_success(17) - p_success(15)


def test_criticals():
    assert Check(4, 3).success and Check(4, 3).critical
    assert Check(6, 16).critical and not Check(6, 15).critical
    assert Check(17, 15).fumble and not Check(17, 16).fumble
    assert not Check(17, 30).success


def test_roll_range():
    rng = random.Random(1)
    rolls = [Dice(3, 6).roll(rng) for _ in range(500)]
    assert min(rolls) >= 3 and max(rolls) <= 18
