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

from . import actions, combat, effects, flight, grapple, perception, physics
from .dice import check

if TYPE_CHECKING:
    from .creature import Creature
    from .sim import Sim
    from .world import Pos

MAX_PATH_NODES = 3000
STUCK_WAIT_MS = 2000
AIM_HOLD_MS = 300
MAX_AIM_HOLDS = 4
FAILED_PATH_MEMORY_MS = 2000
IDLE_MS = 1500
LOOK_MS = 1000


def take_turn(sim: "Sim", c: "Creature") -> int:
    if c.has_status("yielded"):
        return _first_aid(sim, c, allies=False) or IDLE_MS
    if _gives_up(sim, c):
        return IDLE_MS
    if c.grappled_by is not None:
        return _held_turn(sim, c)
    if c.grappling is not None:
        return _grapple_turn(sim, c)
    flee = _flee_danger(sim, c)
    if flee is not None:
        return flee
    known = [e for e in sim.enemies_of(c) if perception.aware_of(c, e)]
    visible = [e for e in known if sim.world.has_los(c.pos, e.pos)]
    if c.has_status("prone") and not _good_firing_position(sim, c, known, visible):
        cost = actions.stand_up(sim, c)
        if cost is not None:
            return cost
    if not visible:
        aid = _first_aid(sim, c, allies=not known)
        if aid is not None:
            return aid
    if not known:
        return _calm_turn(sim, c)
    target = _choose_target(sim, c, known, visible)
    if c.target is not target:
        c.aim_target = None
    c.target = target
    goal = _believed_pos(c, target, visible)
    combat.face(c, goal)

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
        cost = actions.pick_up(sim, c, weapons_only=True)  # replace a lost weapon (the Hulk doesn't want a rifle)
        if cost is not None:
            return cost

    brute = _brute_moves(sim, c, target, visible)
    if brute is not None:
        return brute

    grenade = _grenade_throw(sim, c, target, goal, visible, known)
    if grenade is not None:
        return grenade

    if target in visible:
        plan = combat.best_attack_plan(sim, c, target)
        if plan is not None and plan.value > 0:
            c.aim_holds = 0
            return actions.attack(sim, c, target, plan)

        # Lined up a shot and a teammate stepped into it: keep the aim and give
        # them a moment to clear rather than walking off and starting over.
        if (c.aim_target == target.uid and c.aim_holds < MAX_AIM_HOLDS
                and combat.friendly_in_line(sim, c, target)):
            c.aim_holds += 1
            return AIM_HOLD_MS
    elif sim.distance_pos(c.pos, goal) <= 1:
        # Got to where they were last seen, and they're gone.
        aw = perception.awareness(c, target)
        aw.level = perception.SUSPICIOUS + 20
        c.investigate, c.search_turns = goal, 4
        return _look_around(sim, c)

    # Can't hurt the target from here. Shooters look for a firing position;
    # everyone else closes in on whoever they can reach. Failing both, just get
    # eyes on the target (a leap or a better angle may open up from there).
    step = None
    sees = lambda p: sim.world.has_los(p, goal)  # noqa: E731
    shooter = _has_usable_ranged(c)
    if shooter:
        step = _step_toward(sim, c, goal, sees, kind="los")
    if step is None:
        others = sorted((e for e in known if e is not target), key=lambda e: (sim.distance(c, e), e.uid))
        for e in [target] + others:
            step = _step_toward(sim, c, _believed_pos(c, e, visible))
            if step is not None:
                c.target = e
                break
    if step is None and not shooter and c.powers:
        step = _step_toward(sim, c, goal, sees, kind="los")  # a leap may open up from there
    if step is None:
        # No way in: kick in a door, smash a flimsy wall, or blow it open.
        step = _step_toward(sim, c, goal, breach=True)
        if step is not None and not sim.world.passable(step):
            return _breach(sim, c, step)
    if step is None:
        return STUCK_WAIT_MS  # nothing reachable: hold position and re-think later
    return actions.step(sim, c, step) or STUCK_WAIT_MS


def _danger_radius(item) -> int:
    ex = item.data.get("explosive", {})
    return max(ex.get("radius", 0) + 3, ex.get("fragments", {}).get("radius", 0) // 2 + 1,
               ex.get("flash", {}).get("radius", 0))


def _flee_danger(sim: "Sim", c: "Creature") -> int | None:
    """Get away from a live grenade or charge you can see, or out of the flames."""
    # live explosives on the floor, and ones still in the air (headed for where they'll land)
    live = [(p, item) for p, item in sim.items if item.armed]
    live += [(f.path[-1] if f.path else f.pos, f.obj) for f in flight.active(sim)
             if f.kind == "item" and getattr(f.obj, "armed", False)]
    threats = [p for p, item in live if sim.distance_pos(p, c.pos) <= _danger_radius(item)
               and (p == c.pos or sim.world.has_los(c.pos, p) or not sim.world.passable(p))]
    burning = sim.fields.burning(c.pos) > 0
    if not threats and not burning:
        return None
    best, best_score = None, None
    for nxt in sim.world.neighbors(c.pos, doors=True):
        if not sim.is_free(nxt, ignore=c) or sim.fields.burning(nxt) > 0:
            continue
        score = min((sim.distance_pos(nxt, t) for t in threats), default=0) - 5 * sim.fields.burning(nxt)
        if best_score is None or score > best_score:
            best, best_score = nxt, score
    here = min((sim.distance_pos(c.pos, t) for t in threats), default=0) - 5 * sim.fields.burning(c.pos)
    if best is None or (best_score <= here and not burning):  # nowhere better to be: get down
        return actions.go_prone(sim, c) if threats and not c.has_status("prone") else None
    return actions.step(sim, c, best)


def _breach(sim: "Sim", c: "Creature", pos: "Pos") -> int | None:
    """Get through the wall or locked door at pos: plant a charge if you
    have one (then run), otherwise kick or smash it."""
    charges = [i for i in c.carried if i.data.get("plantable")]
    if charges:
        cost = actions.plant(sim, c, charges[0], pos)
        if cost is not None:
            return cost
    return actions.smash(sim, c, pos) or STUCK_WAIT_MS


GRENADE_EVERY_MS = 6000


def _grenade_throw(sim: "Sim", c: "Creature", target: "Creature", goal: "Pos",
                   visible: list["Creature"], known: list["Creature"]) -> int | None:
    """Toss a grenade when it's worth it: the target is dug in behind cover
    or out of sight, or several enemies bunch up, and no friend is near
    where it will land."""
    items = [i for i in actions.throwables(c) if "explosive" in i.data]
    if not items or sim.time < getattr(c, "next_grenade", 0):
        return None
    dist = sim.distance_pos(c.pos, goal)
    item = items[0]
    blast = item.data.get("explosive", {})
    radius = max(blast.get("radius", 0), blast.get("fragments", {}).get("radius", 0) // 2, 2)
    if dist <= radius + 1 or dist > physics.throw_range(c, item.data.get("weight", 1)):
        return None
    if not sim.world.has_los(c.pos, goal):  # needs a clear line (an open door, a window)
        return None
    if any(a.team == c.team and not a.dead and sim.distance_pos(a.pos, goal) <= radius + 1
           for a in sim.creatures):
        return None
    hidden = target not in visible or combat.cover(sim, c.pos, target)[0] <= -2 or "fire" in blast
    bunched = sum(sim.distance_pos(e.pos, goal) <= radius for e in known) >= 2
    if not (hidden or bunched):
        return None
    c.next_grenade = sim.time + GRENADE_EVERY_MS
    return actions.throw(sim, c, item, goal)


def _gives_up(sim: "Sim", c: "Creature") -> bool:
    """Collapsed, or deep in the red, with enemies about: a WIS roll (plus
    pain resistance, minus pain) each turn to keep fighting. Fail and they
    give up: they stop fighting and nobody bothers with them any more.
    Creatures with "fearless": true never do."""
    if c.template.get("fearless") or not sim.enemies_of(c):
        return False
    if not (c.has_status("collapsed") or c.hp < -c.max_hp / 2):
        return False
    if check(sim.rng, c.stat("WIS") + int(c.trait_sum("pain_resist")) - c.pain()).success:
        return False
    c.add_status("yielded", None)
    with sim.focus(c.pos):
        sim.log(f"{c.name} gives up.")
    return True


def _held_turn(sim: "Sim", c: "Creature") -> int:
    """Someone has hold of c. A choke is a race: early on, hand-fight it and
    hit back (a knife or a pistol into whoever's behind you); once the air's
    running out, it's all-out struggling."""
    g = c.grappled_by
    neck = grapple.silenced(c)
    air = c.body.oxygen
    trained = c.skill("wrestling") >= 10
    if neck and air > 40 and sim.rng.random() < (0.7 if trained else 0.35):
        return grapple.fight_grip(sim, c)  # hands to the arm on your throat (the untrained mostly thrash)
    if not neck or air > 30:
        w = c.wielded
        if w is not None and w.data.get("two_handed") and not grapple.weapon_pinned(c):
            sidearm = next((i for i in c.carried if i.attacks and not i.data.get("two_handed")), None)
            if sidearm is not None and sim.rng.random() < 0.5:
                return actions.wield(sim, c, sidearm)  # can't swing a rifle round: go for the knife
        plan = combat.best_attack_plan(sim, c, g, allow_aim=False)
        if plan is not None and plan.value > 0.5 and sim.rng.random() < 0.4:
            return actions.attack(sim, c, g, plan)
    return grapple.struggle(sim, c)


def _grapple_turn(sim: "Sim", c: "Creature") -> int:
    """Holding someone. Most NPCs just let go; creatures with a "grapple"
    style (brutes like the Hulk) wrench or crush whatever they've got hold
    of: the arm comes off (and becomes a club), the head comes off."""
    t = c.grappling
    style = c.template.get("grapple")
    if not style or not t.active or t.team == c.team:
        return grapple.release(sim, c) or IDLE_MS
    options = grapple.moves(c)
    for move in ("wrench", "squeeze", "wrest"):
        if move in options:
            return getattr(grapple, move)(sim, c) or grapple.release(sim, c) or IDLE_MS
    return grapple.release(sim, c) or IDLE_MS


def _brute_grip(sim: "Sim", c: "Creature", t: "Creature") -> str:
    """Where a brute grabs: the gun arm (to tear it off and keep it), the
    neck, or the body."""
    targets = grapple.grab_targets(t)
    style = c.template["grapple"]
    if c.wielded is None and sim.rng.random() < style.get("tear", 0.5):
        for hand in t.body.parts.values():
            arm = hand.data.get("parent")
            if "grasp" in hand.tags and hand.data.get("primary") and arm in targets:
                return arm
    # the neck if it's a fair chance, else a bear hug
    neck = next((p for p in targets if p != grapple.WEAPON and t.body.part(p).data.get("choke")), None)
    if neck is not None and grapple.grab_odds(sim, c, t, neck) >= 0.4:
        return neck
    return "torso" if "torso" in targets else targets[0]


def _brute_moves(sim: "Sim", c: "Creature", target: "Creature", visible: list["Creature"]) -> int | None:
    """Grab someone in reach (creatures with a "grapple" style), or throw
    whatever's in hand that's made for throwing (a severed arm) at someone
    too far away to hit."""
    if target not in visible:
        return None
    style = c.template.get("grapple")
    near = sim.in_melee_reach(c.pos, target.pos) and c.pos[2] == target.pos[2]
    if (style and near and target.conscious and target.grappled_by is None
            and grapple.grab_targets(target) and sim.rng.random() < style.get("chance", 0.3)):
        cost = grapple.grab(sim, c, target, _brute_grip(sim, c, target))
        if cost is not None:
            return cost
    held = c.wielded
    if (held is not None and "thrown" in held.data and not near
            and sim.distance_pos(c.pos, target.pos) <= physics.throw_range(c, held.data.get("weight", 1))
            and sim.rng.random() < 0.5):
        return actions.throw(sim, c, held, target.pos)
    return None


def _believed_pos(c: "Creature", e: "Creature", visible: list["Creature"]) -> "Pos":
    """Where c thinks e is: where it is if c can see it, else last seen."""
    if e in visible:
        return e.pos
    aw = c.awareness.get(e.uid)
    return aw.last_pos if aw is not None and aw.last_pos is not None else e.pos


def _calm_turn(sim: "Sim", c: "Creature") -> int:
    """Nobody to fight that c knows of: check out anything suspicious, walk
    the patrol, or stand watch."""
    if c.investigate is not None:
        spot = c.investigate
        if sim.distance_pos(c.pos, spot) <= 1:
            c.search_turns -= 1
            if c.search_turns <= 0:
                c.investigate = None
            return _look_around(sim, c)
        step = _step_toward(sim, c, spot, lambda p: sim.distance_pos(p, spot) <= 1, kind="investigate")
        if step is not None:
            return actions.step(sim, c, step) or IDLE_MS
        c.investigate = None
        return IDLE_MS
    if c.patrol:
        wp = tuple(c.patrol[c.patrol_i % len(c.patrol)])
        if c.pos == wp:
            c.patrol_i += 1
            return _look_around(sim, c) if sim.rng.random() < 0.3 else 300
        step = _step_toward(sim, c, wp, lambda p: p == wp, kind="patrol")
        if step is not None:
            return actions.step(sim, c, step) or IDLE_MS
        c.patrol_i += 1
        return IDLE_MS
    if c.pos != c.post:
        step = _step_toward(sim, c, c.post, lambda p: p == c.post, kind="post")
        if step is not None:
            return actions.step(sim, c, step) or IDLE_MS
    if c.alarmed or sim.rng.random() < 0.25:
        return _look_around(sim, c)
    if c.post_facing:
        c.facing = c.post_facing
    return IDLE_MS


def _look_around(sim: "Sim", c: "Creature") -> int:
    ring = combat._RING
    i = ring.index(c.facing)
    c.facing = ring[(i + sim.rng.choice((-2, -1, 1, 2, 4))) % 8]
    return LOOK_MS


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
    if c.body.bleed_rate <= 0.05 and any(p.dislocated for p in c.body.parts.values()):
        return actions.reset_joint(sim, c, c)  # nothing bleeding: put the shoulder back in
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


def _threat(e: "Creature") -> int:
    """How much e can still hurt you: 2 on their feet and able, 1 down but
    still armed (collapsed with a gun, say), 0 helpless (stunned, doubled
    over, down and unarmed): finish those later."""
    if not e.can_act or (e.has_status("prone") and e.wielded is None):
        return 0
    if e.has_status("prone") or e.has_status("collapsed"):
        return 1
    return 2


def _is_threat(e: "Creature") -> bool:
    return _threat(e) > 0


def _choose_target(sim: "Sim", c: "Creature", enemies: list["Creature"],
                   visible: list["Creature"]) -> "Creature":
    pool = visible or enemies
    best = max(_threat(e) for e in pool)
    if c.target is not None and c.target in pool and _threat(c.target) >= best:
        return c.target
    return min(pool, key=lambda e: (-_threat(e), sim.distance(c, e), e.uid))


def can_reach(sim: "Sim", c: "Creature", target: "Creature") -> bool:
    return sim.in_melee_reach(c.pos, target.pos) or _step_toward(sim, c, target.pos) is not None


def _step_toward(sim: "Sim", c: "Creature", goal: "Pos",
                 done: "Callable[[Pos], bool] | None" = None, kind: str = "reach",
                 breach: bool = False) -> "Pos | None":
    """A* over walkable voxels toward goal; returns the first step.

    By default the search ends in melee reach of goal; `done` can replace
    that test (e.g. "has line of sight to the goal"), with `kind` naming it.
    Failed searches are the expensive ones, so they're remembered for a
    couple of seconds (or until the terrain changes)."""
    key = (c.pos, goal, kind + ("+breach" if breach else ""), sim.world.version)
    failed_at = sim.path_failures.get(key)
    if failed_at is not None and sim.time - failed_at < FAILED_PATH_MEMORY_MS:
        return None
    step = _astar(sim, c, goal, done, breach)
    if step is None:
        sim.path_failures[key] = sim.time
        if len(sim.path_failures) > 5000:
            sim.path_failures.clear()
    return step


def _astar(sim: "Sim", c: "Creature", goal: "Pos",
           done: "Callable[[Pos], bool] | None", breach: bool = False) -> "Pos | None":
    if done is None:
        def done(p: "Pos") -> bool:
            return sim.in_melee_reach(p, goal)

    world = sim.world
    occupied = {o.pos for o in sim.creatures if not o.dead and o is not c}  # step around the fallen
    fire = sim.fields.fire
    if fire.any():  # and around the flames
        occupied |= {(int(x), int(y), int(z)) for z, y, x in zip(*(fire > 2).nonzero())}
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
        for nxt in world.neighbors(cur, doors=True, breach=breach):
            if nxt in occupied:
                continue
            ng = g + (1 if not breach or world.passable(nxt) else 8)
            if ng < cost.get(nxt, 1 << 30):
                cost[nxt] = ng
                came[nxt] = cur
                heapq.heappush(frontier, (ng + h(nxt), ng, nxt))
    return None
