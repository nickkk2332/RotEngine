"""Baseline combat AI.

Deliberately simple: use a power if its JSON ai_condition says so, reload when
empty, otherwise take the attack with the best expected value (see
combat.best_attack_plan), or close the distance. The cleverness is in the
expected-value planner rather than here, so modded creatures fight sensibly
without new code.
"""
from __future__ import annotations

import heapq
from typing import TYPE_CHECKING, Callable

from . import combat, effects

if TYPE_CHECKING:
    from .creature import Creature
    from .sim import Sim
    from .world import Pos

MAX_PATH_NODES = 3000


def take_turn(sim: "Sim", c: "Creature") -> int:
    if c.has_status("prone"):
        if c.body.functional_with("stance") or not c.body.total_with("stance"):
            del c.statuses["prone"]
            sim.log(f"{c.name} gets back up.")
            return 1000
    enemies = sim.enemies_of(c)
    if not enemies:
        return 1000
    target = _choose_target(sim, c, enemies)
    c.target = target

    for power in c.powers:
        if c.stamina < power.get("cost", {}).get("stamina", 0):
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
    sim.move_creature(c, step)
    return 2000 if c.has_status("prone") else int(1000 / c.move_per_second)


def _has_usable_ranged(c: "Creature") -> bool:
    return any(a["kind"] == "ranged" and (item is None or item.ammo != 0 or "reload_ms" in item.data)
               for a, item in c.attacks())


def _choose_target(sim: "Sim", c: "Creature", enemies: list["Creature"]) -> "Creature":
    if c.target is not None and c.target.active and sim.world.has_los(c.pos, c.target.pos):
        return c.target
    visible = [e for e in enemies if sim.world.has_los(c.pos, e.pos)]
    return min(visible or enemies, key=lambda e: (sim.distance(c, e), e.uid))


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
