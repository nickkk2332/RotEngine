"""Dice and 3d6 success checks.

Almost every uncertain thing in the game is a 3d6 roll-under check. The bell
curve is the point: a +2 skill edge near the middle of the curve is huge, and
out at the tails a master almost never misses while a novice almost never
lands the hard shot.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from functools import lru_cache

_DICE_RE = re.compile(r"^\s*(\d*)\s*d\s*(\d+)\s*(?:([+-])\s*(\d+))?\s*$")


@dataclass(frozen=True)
class Dice:
    n: int
    sides: int = 6
    add: int = 0

    @classmethod
    def parse(cls, text: str) -> "Dice":
        m = _DICE_RE.match(text)
        if not m:
            raise ValueError(f"bad dice expression: {text!r}")
        n = int(m.group(1) or 1)
        add = int(m.group(4) or 0) * (-1 if m.group(3) == "-" else 1)
        return cls(n, int(m.group(2)), add)

    def roll(self, rng: random.Random) -> int:
        return sum(rng.randint(1, self.sides) for _ in range(self.n)) + self.add

    @property
    def mean(self) -> float:
        return self.n * (self.sides + 1) / 2 + self.add

    def distribution(self) -> dict[int, float]:
        """Exact probability of each total."""
        return {v + self.add: p for v, p in _sum_distribution(self.n, self.sides).items()}

    def __str__(self) -> str:
        if not self.add:
            return f"{self.n}d{self.sides}"
        return f"{self.n}d{self.sides}{self.add:+d}"


@lru_cache(maxsize=None)
def _sum_distribution(n: int, sides: int) -> dict[int, float]:
    dist = {0: 1.0}
    for _ in range(n):
        nxt: dict[int, float] = {}
        for total, p in dist.items():
            for face in range(1, sides + 1):
                nxt[total + face] = nxt.get(total + face, 0.0) + p / sides
        dist = nxt
    return dist


def roll_3d6(rng: random.Random) -> int:
    return rng.randint(1, 6) + rng.randint(1, 6) + rng.randint(1, 6)


def _is_critical(roll: int, target: int) -> bool:
    return roll <= 4 or (roll == 5 and target >= 15) or (roll == 6 and target >= 16)


def _is_fumble(roll: int, target: int) -> bool:
    return roll == 18 or (roll == 17 and target <= 15) or roll >= target + 10


@dataclass(frozen=True)
class Check:
    roll: int
    target: int

    @property
    def critical(self) -> bool:
        return _is_critical(self.roll, self.target)

    @property
    def fumble(self) -> bool:
        return _is_fumble(self.roll, self.target)

    @property
    def success(self) -> bool:
        if self.critical:
            return True
        return self.roll <= self.target and self.roll < 17

    @property
    def margin(self) -> int:
        return self.target - self.roll


def check(rng: random.Random, target: int) -> Check:
    return Check(roll_3d6(rng), target)


@lru_cache(maxsize=None)
def p_success(target: int) -> float:
    """Chance that a 3d6 check against `target` succeeds (crit rules included)."""
    return sum(p for roll, p in _sum_distribution(3, 6).items() if Check(roll, target).success)
