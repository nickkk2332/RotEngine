"""Baseline combat AI.

Deliberately simple: use a power if its JSON ai_condition says so, reload when
empty, patch up bleeding when nobody is shooting at you, otherwise take the
attack (or aim) with the best expected value (see combat.best_attack_plan),
or close the distance. The cleverness is in the expected-value planner rather
than here, so modded creatures fight sensibly without new code.
"""
from __future__ import annotations

import heapq
from typing import TYPE_CHECKING, Callable

from . import combat, effects
from .dice import check

if TYPE_CHECKING:
    from .creature import Creature
    from .sim import Sim
    from .world import Pos

MAX_PATH_NODES = 3000
MOVE_EXERTION = 0.05
FIRST_AID_MS = 5000


def take_turn(sim: "Sim", c: "Creature") -> int:
    enemies = sim.enemies_of(c)
    visible = [e for e in enemies if sim.world.has_los(c.pos, e.pos)]
    if c.has_status("prone") and not _good_firing_position(sim, c, enemies, visible):
        if c.body.functional_with("stance") or not c.body.total_with("stance"):
            del c.statuses["prone"]
            sim.log(f"{c.name} gets back up.")
            return 1000
    if not visible:
        aid = _first_aid(sim, c, allies=not enemies)
        if aid is not None:
            return aid
    if not enemies:
        return 1000
    target = _choose_target(sim, c, enemies, visible)
    if c.target is not target:
        c.aim_target = None
    c.target = target
    combat.face(c, target.pos)

    for power in c.powers:
        if c.stamina < power.get("cost", {}).get("stamina", 0):
            continue
        if c.cooldowns.get(power["id"], 0) > sim.time:
            continue
        ctx = effects.Ctx(sim, c, target)
        if effects.test(power.get("ai_condition", False), ctx):
            return combat.use_power(sim, c, power, target)

    item = c.wielded
    if item is not None and item.ammo == 0 and "reload_ms" in item.data:
        item.ammo = item.data["magazine"]
        sim.log(f"{c.name} reloads the {item.name}.")
        return item.data["reload_ms"]

    plan = combat.best_attack_plan(sim, c, target)
    if plan is not None and plan.value > 0:
        return combat.resolve_attack(sim, c, target, plan)

    # Can't hurt the target from here. Shooters look for a firing position;
    # everyone else closes in on whoever they can reach. Failing both, just get
    # eyes on the target (a leap or a better angle may open up from there).
    step = None
    if _has_usable_ranged(c):
        step = _step_toward(sim, c, target.pos, lambda p: sim.world.has_los(p, target.pos))
    if step is None:
        for goal in [target] + sorted((e for e in enemies if e is not target),
                                      key=lambda e: (sim.distance(c, e), e.uid)):
            step = _step_toward(sim, c, goal.pos)
            if step is not None:
                c.target = goal
                break
    if step is None:
        step = _step_toward(sim, c, target.pos, lambda p: sim.world.has_los(p, target.pos))
    if step is None:
        return 500
    return _move(sim, c, step)


def _good_firing_position(sim: "Sim", c: "Creature", enemies: list["Creature"],
                          visible: list["Creature"]) -> bool:
    """Lying flat is a fine place to shoot from (and a smaller target), as
    long as nobody is close enough to stomp on you."""
    return (bool(visible) and _has_usable_ranged(c)
            and all(sim.distance(c, e) > 3 for e in enemies))


def _move(sim: "Sim", c: "Creature", step: "Pos") -> int:
    sim.move_creature(c, step)
    c.exert(MOVE_EXERTION)
    return 2000 if c.has_status("prone") else int(1000 / c.move_per_second)


def _first_aid(sim: "Sim", c: "Creature", allies: bool) -> int | None:
    """Bandage the worst external bleeding in reach: your own while nobody is
    in sight, your friends' once the fighting is over."""
    if not c.body.functional_with("grasp"):
        return None
    patients = [c] if c.body.bleed_rate > 0.05 else []
    if allies:
        patients += [a for a in sim.creatures if a.team == c.team and a is not c
                     and not a.dead and a.body.bleed_rate > 0.05]
    if not patients:
        return None
    patient = max(patients, key=lambda a: (a.body.bleed_rate, -sim.distance(c, a)))
    if patient is not c and not sim.in_melee_reach(c.pos, patient.pos):
        step = _step_toward(sim, c, patient.pos)
        return _move(sim, c, step) if step is not None else None
    bleed = patient.body.bleed_rate
    severity = 3 if bleed < 0.5 else 0 if bleed < 1.5 else -3  # pressure on a nick vs. an artery
    skill = c.skill("first_aid") + severity - c.action_penalty() - (2 if patient is c else 0)
    r = check(sim.rng, skill)
    who = "their own wounds" if patient is c else f"{patient.name}'s wounds"
    if r.success:
        before = patient.body.bleed_rate
        patient.body.bleed_rate = 0.0 if r.critical or r.margin >= 5 else before * 0.25
        note = " The internal bleeding needs a surgeon." if patient.body.internal_bleed > 0.01 else ""
        sim.log(f"{c.name} binds {who} (bleeding {before:.1f} -> {patient.body.bleed_rate:.1f}%/s).{note}")
    else:
        sim.log(f"{c.name} fumbles with {who}.")
    return FIRST_AID_MS


def _has_usable_ranged(c: "Creature") -> bool:
    return any(a["kind"] == "ranged" and (item is None or item.ammo != 0 or "reload_ms" in item.data)
               for a, item in c.attacks())


def _is_threat(e: "Creature") -> bool:
    """Down, disarmed and unable to stand: finish later, deal with the armed first."""
    return not (e.has_status("prone") and e.wielded is None and not e.body.functional_with("stance"))


def _choose_target(sim: "Sim", c: "Creature", enemies: list["Creature"],
                   visible: list["Creature"]) -> "Creature":
    pool = visible or enemies
    if c.target is not None and c.target in pool and (
            _is_threat(c.target) or not any(_is_threat(e) for e in pool)):
        return c.target
    return min(pool, key=lambda e: (not _is_threat(e), sim.distance(c, e), e.uid))


def can_reach(sim: "Sim", c: "Creature", target: "Creature") -> bool:
    return sim.in_melee_reach(c.pos, target.pos) or _step_toward(sim, c, target.pos) is not None


def _step_toward(sim: "Sim", c: "Creature", goal: "Pos",
                 done: "Callable[[Pos], bool] | None" = None) -> "Pos | None":
    """A* over walkable voxels toward goal; returns the first step.

    By default the search ends in melee reach of goal; `done` can replace
    that test (e.g. "has line of sight to the goal")."""
    if done is None:
        def done(p: "Pos") -> bool:
            return sim.in_melee_reach(p, goal)

    world = sim.world
    occupied = {o.pos for o in sim.creatures if o.active and o is not c}
    start = c.pos

    def h(p: "Pos") -> int:
        return sim.distance_pos(p, goal)

    frontier = [(h(start), 0, start)]
    came: dict = {start: None}
    cost = {start: 0}
    expanded = 0
    while frontier and expanded < MAX_PATH_NODES:
        _, g, cur = heapq.heappop(frontier)
        expanded += 1
        if cur != start and done(cur):
            while came[cur] != start:
                cur = came[cur]
            return cur
        for nxt in world.neighbors(cur):
            if nxt in occupied:
                continue
            ng = g + 1
            if ng < cost.get(nxt, 1 << 30):
                cost[nxt] = ng
                came[nxt] = cur
                heapq.heappush(frontier, (ng + h(nxt), ng, nxt))
    return None
