"""Skill growth through use.

Rolling a skill when it matters teaches you something. How much depends on
how hard the roll was: a sure thing (95%+) or a hopeless one (under 2%)
teaches nothing, a coin flip teaches the most, and a failure teaches half
what a success does. Each level costs more than the last:
5 x (1 + levels above 10) points, so going from 10 to 11 takes a handful of
real fights and 17 to 18 takes a career.

Only creatures that "learn" keep score: the player, and any creature
template with "learns": true.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .dice import p_success

if TYPE_CHECKING:
    from .creature import Creature
    from .sim import Sim

COST_BASE = 5
EASY, HOPELESS = 0.95, 0.02


def learns(c: "Creature") -> bool:
    return c.controller == "player" or bool(c.template.get("learns"))


def cost(level: int) -> float:
    return COST_BASE * (1 + max(0, level - 10))


def practice(sim: "Sim", c: "Creature", skill: str, target: int, success: bool) -> None:
    """c just rolled `skill` against `target`."""
    if not learns(c) or c.dead:
        return
    p = p_success(target)
    if p >= EASY or p < HOPELESS:
        return
    gain = 2 * (1 - p) * (1.0 if success else 0.5)
    c.practice[skill] = c.practice.get(skill, 0.0) + gain
    level = c.skill(skill)
    if c.practice[skill] >= cost(level):
        c.practice[skill] -= cost(level)
        c.skills[skill] = level + 1
        sim.log(f"{c.name}'s {skill.replace('_', ' ')} improves to {level + 1}.", private_to=c.uid)


def progress(c: "Creature", skill: str) -> float:
    """How far to the next level, 0..1 (for the UI)."""
    return min(1.0, c.practice.get(skill, 0.0) / cost(c.skill(skill)))
