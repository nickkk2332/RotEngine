"""Actions: the verbs a creature can perform, shared by the AI and the player.

Each action either does something and returns its cost in the actor's *own*
milliseconds (the sim divides by tempo), or returns None when it isn't
possible right now, having changed nothing. Keeping these in one place means
the player and the AI play by exactly the same rules.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import combat
from .dice import check

if TYPE_CHECKING:
    from .combat import AttackPlan
    from .creature import Creature
    from .sim import Sim
    from .world import Pos

MOVE_EXERTION = 0.05
FIRST_AID_MS = 5000
STAND_MS = 1000
GO_PRONE_MS = 500
PICKUP_MS = 1000


def step(sim: "Sim", c: "Creature", to: "Pos") -> int | None:
    """Walk (or crawl) one tile to an adjacent free tile, or climb stairs."""
    if to not in set(sim.world.neighbors(c.pos)) or not sim.is_free(to, ignore=c):
        return None
    sim.move_creature(c, to)
    c.exert(MOVE_EXERTION)
    return 2000 if c.has_status("prone") else int(1000 / c.move_per_second)


def step_dir(sim: "Sim", c: "Creature", dx: int, dy: int) -> int | None:
    x, y, z = c.pos
    return step(sim, c, (x + dx, y + dy, z))


def climb(sim: "Sim", c: "Creature", dz: int) -> int | None:
    """Go up (dz=1) or down (dz=-1) the stairs you're standing on."""
    x, y, z = c.pos
    return step(sim, c, (x, y, z + dz))


def wait(sim: "Sim", c: "Creature", ms: int = 500) -> int:
    return ms


def attack(sim: "Sim", c: "Creature", target: "Creature", plan: "AttackPlan") -> int:
    return combat.resolve_attack(sim, c, target, plan)


def reload(sim: "Sim", c: "Creature") -> int | None:
    item = c.wielded
    if item is None or "reload_ms" not in item.data or item.ammo == item.data.get("magazine"):
        return None
    item.ammo = item.data["magazine"]
    sim.log(f"{c.name} reloads the {item.name}.")
    return item.data["reload_ms"]


def stand_up(sim: "Sim", c: "Creature") -> int | None:
    if not c.has_status("prone"):
        return None
    if c.body.total_with("stance") and not c.body.functional_with("stance"):
        return None  # no working legs: you crawl
    del c.statuses["prone"]
    sim.log(f"{c.name} gets back up.")
    return STAND_MS


def go_prone(sim: "Sim", c: "Creature") -> int | None:
    if c.has_status("prone"):
        return None
    c.add_status("prone", None)
    sim.log(f"{c.name} drops flat.")
    return GO_PRONE_MS


def bandage(sim: "Sim", c: "Creature", patient: "Creature") -> int | None:
    """First aid on external bleeding: yours (at -2) or someone in reach.
    Small bleeds are easy, arteries hard; internal bleeding is out of reach."""
    if not c.body.functional_with("grasp") or patient.dead or patient.body.bleed_rate <= 0.05:
        return None
    if patient is not c and not sim.in_melee_reach(c.pos, patient.pos):
        return None
    bleed = patient.body.bleed_rate
    severity = 3 if bleed < 0.5 else 0 if bleed < 1.5 else -3
    skill = c.skill("first_aid") + severity - c.action_penalty() - (2 if patient is c else 0)
    r = check(sim.rng, skill)
    who = "their own wounds" if patient is c else f"{patient.name}'s wounds"
    if r.success:
        patient.body.bleed_rate = 0.0 if r.critical or r.margin >= 5 else bleed * 0.25
        note = " The internal bleeding needs a surgeon." if patient.body.internal_bleed > 0.01 else ""
        sim.log(f"{c.name} binds {who} (bleeding {bleed:.1f} -> {patient.body.bleed_rate:.1f}%/s).{note}")
    else:
        sim.log(f"{c.name} fumbles with {who}.")
    return FIRST_AID_MS


def pick_up(sim: "Sim", c: "Creature") -> int | None:
    """Pick up a weapon from your tile (or next to you) if your hands are free."""
    if c.wielded is not None:
        return None
    for i, (pos, item) in enumerate(sim.items):
        if (pos == c.pos or sim.in_melee_reach(c.pos, pos)) and item.attacks and c.can_grip(item):
            sim.items.pop(i)
            c.wielded = item
            sim.log(f"{c.name} picks up the {item.name}.")
            return PICKUP_MS
    return None


def power_blocked(sim: "Sim", c: "Creature", power: dict) -> str | None:
    """Why a power can't be used right now, or None if it can."""
    if c.stamina < power.get("cost", {}).get("stamina", 0):
        return "not enough stamina"
    if c.cooldowns.get(power["id"], 0) > sim.time:
        return "not ready yet"
    return None


def use_power(sim: "Sim", c: "Creature", power: dict, target: "Creature | None") -> int | None:
    if power_blocked(sim, c, power):
        return None
    return combat.use_power(sim, c, power, target)
