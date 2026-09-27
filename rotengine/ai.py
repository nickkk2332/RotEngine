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

from . import actions, combat, effects

if TYPE_CHECKING:
    from .creature import Creature
    from .sim import Sim
    from .world import Pos

MAX_PATH_NODES = 3000
STUCK_WAIT_MS = 2000
AIM_HOLD_MS = 300
MAX_AIM_HOLDS = 4
FAILED_PATH_MEMORY_MS = 2000


def take_turn(sim: "Sim", c: "Creature") -> int:
    enemies = sim.enemies_of(c)
    visible = [e for e in enemies if sim.world.has_los(c.pos, e.pos)]
    if c.has_status("prone") and not _good_firing_position(sim, c, enemies, visible):
        cost = actions.stand_up(sim, c)
        if cost is not None:
            return cost
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
        if actions.power_blocked(sim, c, power):
            continue
        if effects.test(power.get("ai_condition", False), effects.Ctx(sim, c, target)):
            return actions.use_power(sim, c, power, target)

    if c.wielded is not None and c.wielded.ammo == 0:
        cost = actions.reload(sim, c)
        if cost is not None:
            return cost
    if c.wielded is None and c.template.get("equipment", {}).get("wield"):
        cost = actions.pick_up(sim, c)  # replace a lost weapon (the Hulk doesn't want a rifle)
        if cost is not None:
            return cost

    plan = combat.best_attack_plan(sim, c, target)
    if plan is not None and plan.value > 0:
        c.aim_holds = 0
        return actions.attack(sim, c, target, plan)

    # Lined up a shot and a teammate stepped into it: keep the aim and give
    # them a moment to clear rather than walking off and starting over.
    if (c.aim_target == target.uid and target in visible and c.aim_holds < MAX_AIM_HOLDS
            and combat.friendly_in_line(sim, c, target)):
        c.aim_holds += 1
        return AIM_HOLD_MS

    # Can't hurt the target from here. Shooters look for a firing position;
    # everyone else closes in on whoever they can reach. Failing both, just get
    # eyes on the target (a leap or a better angle may open up from there).
    step = None
    sees = lambda p: sim.world.has_los(p, target.pos)  # noqa: E731
    shooter = _has_usable_ranged(c)
    if shooter:
        step = _step_toward(sim, c, target.pos, sees, kind="los")
    if step is None:
        for goal in [target] + sorted((e for e in enemies if e is not target),
                                      key=lambda e: (sim.distance(c, e), e.uid)):
            step = _step_toward(sim, c, goal.pos)
            if step is not None:
                c.target = goal
                break
    if step is None and not shooter and c.powers:
        step = _step_toward(sim, c, target.pos, sees, kind="los")  # a leap may open up from there
    if step is None:
        return STUCK_WAIT_MS  # nothing reachable: hold position and re-think later
    return actions.step(sim, c, step) or STUCK_WAIT_MS


def _good_firing_position(sim: "Sim", c: "Creature", enemies: list["Creature"],
                          visible: list["Creature"]) -> bool:
    """Lying flat is a fine place to shoot from (and a smaller target), as
    long as nobody is close enough to stomp on you."""
    return (bool(visible) and _has_usable_ranged(c)
            and all(sim.distance(c, e) > 3 for e in enemies))


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
        return actions.step(sim, c, step) if step is not None else None
    return actions.bandage(sim, c, patient)


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
                 done: "Callable[[Pos], bool] | None" = None, kind: str = "reach") -> "Pos | None":
    """A* over walkable voxels toward goal; returns the first step.

    By default the search ends in melee reach of goal; `done` can replace
    that test (e.g. "has line of sight to the goal"), with `kind` naming it.
    Failed searches are the expensive ones, so they're remembered for a
    couple of seconds (or until the terrain changes)."""
    key = (c.pos, goal, kind, sim.world.version)
    failed_at = sim.path_failures.get(key)
    if failed_at is not None and sim.time - failed_at < FAILED_PATH_MEMORY_MS:
        return None
    step = _astar(sim, c, goal, done)
    if step is None:
        sim.path_failures[key] = sim.time
        if len(sim.path_failures) > 5000:
            sim.path_failures.clear()
    return step


def _astar(sim: "Sim", c: "Creature", goal: "Pos",
           done: "Callable[[Pos], bool] | None") -> "Pos | None":
    if done is None:
        def done(p: "Pos") -> bool:
            return sim.in_melee_reach(p, goal)

    world = sim.world
    occupied = {o.pos for o in sim.creatures if not o.dead and o is not c}  # step around the fallen
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
