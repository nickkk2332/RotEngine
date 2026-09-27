"""Actions: the verbs a creature can perform, shared by the AI and the player.

Each action either does something and returns its cost in the actor's *own*
milliseconds (the sim divides by tempo), or returns None when it isn't
possible right now, having changed nothing. Keeping these in one place means
the player and the AI play by exactly the same rules.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

from . import combat, perception, physics
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
SWAP_MS = 1000
GRAB_MS = 1000
CHOKE_MS = 1000
WRENCH_MS = 1000
TAKEDOWN_MS = 1000
DISARM_MS = 1000
CHOKE_HYPOXIA = 3.0          # brain damage per second of choking someone already out
KNOCKOUT_MS = (20_000, 60_000)  # how long a choke keeps someone under
DOOR_MS = 500
THROW_MS = 1000
PLANT_MS = 3000
CHARGE_FUSE_MS = 5000


def step(sim: "Sim", c: "Creature", to: "Pos") -> int | None:
    """Walk (or crawl) one tile to an adjacent free tile, or climb stairs.
    Makes noise (less when sneaking or crawling). Someone in your grip gets
    dragged along into the tile you left."""
    if c.grappled_by is not None:
        return None
    victim = c.grappling
    if to not in set(sim.world.neighbors(c.pos, doors=True)):
        return None
    if sim.world.is_closed_door(to):
        return open_door(sim, c, to)  # walking into a door opens it
    if not sim.is_free(to, ignore=victim or c):
        return None
    if to == getattr(victim, "pos", None):
        return None
    old = c.pos
    sim.move_creature(c, to)
    c.exert(MOVE_EXERTION * (3 if victim else 1))
    c.last_moved = sim.time
    quiet = c.sneaking or c.has_status("prone")
    perception.emit_noise(sim, c, c.pos, "sneak" if quiet else "footstep")
    cost = 2000 if c.has_status("prone") else int(1000 / c.move_per_second)
    if victim is not None:
        victim.pos = old
        sim.check_fall(victim)
        cost *= 2
    return cost


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


def pick_up(sim: "Sim", c: "Creature", weapons_only: bool = False) -> int | None:
    """Pick something up from your tile or next to you. A live grenade comes
    first (so you can throw it back); then a weapon, if your hands are free;
    then anything else you could throw."""
    if not c.body.functional_with("grasp"):
        return None
    near = [(i, item) for i, (pos, item) in enumerate(sim.items)
            if pos == c.pos or sim.in_melee_reach(c.pos, pos)]

    def take(i: int, item, wield: bool) -> int:
        sim.items.pop(i)
        if wield:
            c.wielded = item
        else:
            c.carried.append(item)
        with sim.focus(c.pos):
            sim.log(f"{c.name} picks up {item.the}.")
        return PICKUP_MS

    if not weapons_only:
        for i, item in near:
            if item.armed and "throwable" in item.data:
                return take(i, item, False)
    if c.wielded is None:
        for i, item in near:
            if item.attacks and c.can_grip(item) and not (weapons_only and item.data.get("gore")):
                return take(i, item, True)
    if not weapons_only:
        for i, item in near:
            if "throwable" in item.data and not item.data.get("plantable"):
                return take(i, item, False)
    return None



# -- doors and terrain ------------------------------------------------------------
def open_door(sim: "Sim", c: "Creature", pos: "Pos") -> int | None:
    mat = sim.world.fill_mat(pos)
    if not mat.get("door") or not mat.get("solid") or not sim.in_melee_reach(c.pos, pos):
        return None
    with sim.focus(c.pos, pos):
        if mat.get("locked"):
            sim.log(f"{c.name} tries the {mat['name']}: locked.")
            return DOOR_MS
        sim.world.swap_fill(pos, mat["door"])
        sim.log(f"{c.name} opens the {mat['name']}.")
    perception.emit_noise(sim, c, pos, "door", loudness=1 if c.sneaking else None)
    return DOOR_MS


def close_door(sim: "Sim", c: "Creature", pos: "Pos") -> int | None:
    mat = sim.world.fill_mat(pos)
    if (not mat.get("door") or mat.get("solid") or not sim.in_melee_reach(c.pos, pos)
            or sim.creature_at(pos, include_down=True) is not None or any(p == pos for p, _ in sim.items)):
        return None
    sim.world.swap_fill(pos, mat["door"])
    with sim.focus(c.pos, pos):
        sim.log(f"{c.name} closes the {sim.world.fill_mat(pos)['name']}.")
    perception.emit_noise(sim, c, pos, "door", loudness=1 if c.sneaking else None)
    return DOOR_MS


def smash(sim: "Sim", c: "Creature", pos: "Pos") -> int | None:
    """Hit a wall, door or window next to you with your best melee attack.
    Force concentrated on a structure counts double (like a body slammed into it)."""
    mat = sim.world.fill_mat(pos)
    if not mat.get("solid") or not sim.in_melee_reach(c.pos, pos) or pos[2] != c.pos[2]:
        return None
    melee = [(a, i) for a, i in c.attacks() if a["kind"] == "melee"]
    if not melee:
        return None
    attack, _ = max(melee, key=lambda ai: combat.attack_dice(c, ai[0]).mean)
    raw = combat.attack_dice(c, attack).roll(sim.rng)
    force = raw * (2 if attack["damage"]["type"] == "crush" else 1)
    combat.face(c, pos)
    c.exert(0.4)
    broke = sim.world.damage_fill(pos, force)
    with sim.focus(c.pos, pos):
        if broke:
            sim.log(f"{c.name} smashes through the {mat['name']}!")
            sim.terrain_changed()
        elif force > mat.get("dr", 0):
            sim.log(f"{c.name} {attack.get('verb', 'hits')} the {mat['name']}; it's giving way.")
        else:
            sim.log(f"{c.name} {attack.get('verb', 'hits')} the {mat['name']} to no effect.")
    perception.emit_noise(sim, c, pos, "crash")
    return int(attack.get("time_ms", 1000))


# -- throwing and explosives ----------------------------------------------------
def throwables(c: "Creature") -> list:
    """What c could throw: carried grenades and the like, and whatever's in
    hand if it's throwable (a severed arm, say)."""
    out = [i for i in c.carried if "throwable" in i.data]
    if c.wielded is not None and "throwable" in c.wielded.data:
        out.append(c.wielded)
    return out


def throw(sim: "Sim", c: "Creature", item, target: "Pos") -> int | None:
    """Throw a carried item at a tile. Grenades are armed as they leave your hand."""
    if item not in throwables(c) or not c.body.functional_with("grasp"):
        return None
    weight = item.data.get("weight", 1)
    if sim.distance_pos(c.pos, target) > physics.throw_range(c, weight) or target == c.pos:
        return None
    if item is c.wielded:
        c.wielded = None
    else:
        c.carried.remove(item)
    fuse = item.data["throwable"].get("fuse_ms", 3000)
    if "explosive" in item.data and fuse > 0 and not item.armed:
        physics.arm(sim, item, fuse, c)
    land = physics.throw_item(sim, c, item, target)
    if "explosive" in item.data and fuse == 0:  # molotovs: burst on impact
        sim.items[:] = [(p, i) for p, i in sim.items if i is not item]
        physics.explode(sim, land, item.data["explosive"], c, item.name)
    c.exert(0.2)
    return THROW_MS


def hurl(sim: "Sim", c: "Creature", direction: tuple[int, int]) -> int | None:
    """Throw the person you're holding."""
    body = c.grappling
    if body is None or direction == (0, 0):
        return None
    physics.fling(sim, c, body, direction)
    c.exert(0.5)
    return THROW_MS


def plant(sim: "Sim", c: "Creature", item, pos: "Pos") -> int | None:
    """Fix a charge to a wall or door next to you; it blows a few seconds later."""
    if item not in c.carried or not item.data.get("plantable") or not sim.in_melee_reach(c.pos, pos):
        return None
    if not sim.world.fill_mat(pos).get("solid"):
        return None
    c.carried.remove(item)
    sim.items.append((pos, item))
    physics.arm(sim, item, item.data.get("fuse_ms", CHARGE_FUSE_MS), c)
    with sim.focus(c.pos, pos):
        sim.log(f"{c.name} plants a {item.name} on the {sim.world.fill_mat(pos)['name']}.")
    return PLANT_MS


def toggle_sneak(sim: "Sim", c: "Creature") -> int:
    c.sneaking = not c.sneaking
    return 100


def swap_weapon(sim: "Sim", c: "Creature") -> int | None:
    """Put away what's in hand and draw the next carried weapon."""
    options = [i for i in c.carried if i.attacks and c.can_grip(i)]
    if not options:
        return None
    nxt = options[0]
    c.carried.remove(nxt)
    if c.wielded is not None:
        c.carried.append(c.wielded)
    c.wielded = nxt
    sim.log(f"{c.name} draws {nxt.the}.")
    return SWAP_MS


# -- grappling ---------------------------------------------------------------
def grab(sim: "Sim", c: "Creature", target: "Creature") -> int | None:
    """Get hold of someone next to you. From behind, or on someone who never
    saw you coming, there's no defending against it."""
    if (c.grappling is not None or target is c or not c.body.functional_with("grasp")
            or not sim.in_melee_reach(c.pos, target.pos) or c.pos[2] != target.pos[2]
            or target.grappled_by is not None):
        return None
    combat.face(c, target.pos)
    if not target.conscious:  # a body: just take hold of it (to drag it somewhere dark)
        with sim.focus(c.pos, target.pos):
            sim.log(f"{c.name} takes hold of {target.name}'s {'body' if target.dead else 'limp form'}.")
        c.grappling, target.grappled_by = target, c
        c.add_status("grappling", None)
        target.add_status("grappled", None)
        return GRAB_MS
    c.exert(0.3)
    defense = combat.defense_against(sim, c, target, "melee")
    roll = check(sim.rng, c.skill("wrestling") - c.action_penalty("melee"))
    perception.emit_noise(sim, c, c.pos, "struggle")
    with sim.focus(c.pos, target.pos):
        if not roll.success:
            sim.log(f"{c.name} lunges for {target.name} and misses.")
            perception.notice_attacker(sim, target, c)
            return GRAB_MS
        if defense is not None:
            combat.spend_defense(sim, target)
            if check(sim.rng, defense[1]).success:
                sim.log(f"{c.name} grabs at {target.name}, who twists away.")
                perception.notice_attacker(sim, target, c)
                return GRAB_MS
        how = "" if defense is not None else (" from behind" if target.conscious else "")
        sim.log(f"{c.name} gets {target.name} in a hold{how}.")
    c.grappling, target.grappled_by = target, c
    c.rear_hold = defense is None  # taken from behind: much harder to get out of
    c.add_status("grappling", None)
    target.add_status("grappled", None)
    perception.notice_attacker(sim, target, c)  # they know now, but the hold keeps them quiet
    return GRAB_MS


def choke(sim: "Sim", c: "Creature") -> int | None:
    """Squeeze. The one being choked can't cry out; each second they roll
    CON (worse every second) or go limp for half a minute or so. Keep
    squeezing after that and you're killing them."""
    t = c.grappling
    if t is None or t.dead:
        return None
    t.choked += 1
    perception.emit_noise(sim, c, c.pos, "sneak")
    with sim.focus(c.pos, t.pos):
        if t.conscious:
            if not check(sim.rng, t.stat("CON") - 2 * t.choked + 2).success:
                sim.log(f"{t.name} goes limp in {c.name}'s chokehold.")
                t.statuses.pop("prone", None)
                t.add_status("unconscious", sim.time + sim.rng.randint(*KNOCKOUT_MS))
                t.add_status("prone", None)
            else:
                sim.log(f"{c.name} tightens the chokehold on {t.name}.")
        else:
            t.body.hypoxia += CHOKE_HYPOXIA
            if t.body.hypoxia >= 100:
                combat.kill(sim, t, "strangled")
                release(sim, c)
            elif t.choked % 5 == 0:
                sim.log(f"{c.name} keeps squeezing {t.name}'s throat...")
    return CHOKE_MS


def _hold_contest(sim: "Sim", c: "Creature", t: "Creature", resist: int | None = None) -> bool:
    """Doing something to someone you hold: your ST or Wrestling (+2 for the
    leverage, +3 more from behind) against their ST or Wrestling. Someone out
    cold doesn't resist."""
    if not t.conscious:
        return True
    mine = check(sim.rng, max(c.stat("ST"), c.skill("wrestling")) + 2 + (3 if c.rear_hold else 0)
                 - c.action_penalty("melee"))
    base = resist if resist is not None else max(t.stat("ST"), t.skill("wrestling"))
    theirs = check(sim.rng, base - t.action_penalty("melee") - t.choked)
    return mine.success and (not theirs.success or mine.margin > theirs.margin)


def wrenchable(t: "Creature") -> list:
    """Parts of t that can be twisted, bent the wrong way or torn off."""
    return [p for p in t.body.parts.values()
            if not p.destroyed and "wrench" in p.data.get("by_type", {})]


def wrench_dice(c: "Creature"):
    """Swing damage from ST (with a speedster's momentum), plus technique:
    +1 per 2 points of Wrestling above 12."""
    bonus = max(0, (c.skill("wrestling") - 12) // 2)
    return combat.attack_dice(c, {"damage": {"st": "swing", "add": bonus}})


def wrench_odds(c: "Creature", t: "Creature", part) -> tuple[float, int | None, float]:
    """(average injury per successful wrench, wrenches left to break it or
    None, chance a single wrench tears it off)."""
    dtype = c.content.get("damage_type", "wrench")
    spec = t.body.spec(part, "wrench")
    dr, mult, mh = t.dr(part.id, "wrench"), t.body.wound_multiplier(part, dtype), t.max_hp
    avg = p_tear = 0.0
    for raw, p in wrench_dice(c).distribution().items():
        pen = raw - dr
        injury = max(1, math.floor(pen * mult)) if pen > 0 else 0
        avg += injury * p
        if spec.get("destroy_at") is not None and injury >= mh * spec["destroy_at"]:
            p_tear += p
    tries = None
    if spec.get("fracture_at") is not None and not part.fractured and avg > 0:
        tries = max(1, math.ceil((mh * spec["fracture_at"] - part.damage) / avg))
    return avg, tries, p_tear


def wrench(sim: "Sim", c: "Creature", part_id: str) -> int | None:
    """Joint lock, limb break, neck snap. Armor doesn't help; the joint's own
    strength (its wrench DR) and natural toughness do. A normal person breaks
    an arm in a couple of goes and needs several to snap a neck. Someone
    strong enough to do the part's whole destroy threshold in one pull
    tears it off, and ends up holding it."""
    t = c.grappling
    if t is None:
        return None
    part = t.body.parts.get(part_id)
    if part is None or part not in wrenchable(t):
        return None
    combat.face(c, t.pos)
    c.exert(0.4)
    perception.emit_noise(sim, c, c.pos, "struggle")
    spec = t.body.spec(part, "wrench")
    was_dead = t.dead
    with sim.focus(c.pos, t.pos):
        if not _hold_contest(sim, c, t):
            sim.log(f"{c.name} goes for {t.name}'s {part.name}, but {t.name} fights it.")
            return WRENCH_MS
        verb = spec.get("verb", "wrenches {target}'s {part}").format(target=t.name, part=part.name)
        sim.log(f"{c.name} {verb}.")
        inj = combat.deal_damage(sim, t, wrench_dice(c).roll(sim.rng), "wrench", part_id,
                                 source=c, knockback_ok=False)
        if was_dead and inj.newly_destroyed:
            sim.log(f"  {inj.spec.get('destroy_text', 'torn off').capitalize()}.")
        item = inj.severed
        if item is not None and c.wielded is None and c.can_grip(item):
            sim.items[:] = [(p, i) for p, i in sim.items if i is not item]
            c.wielded = item
            sim.log(f"{c.name} is left holding {item.name}.")
    return WRENCH_MS


def takedown(sim: "Sim", c: "Creature") -> int | None:
    """Throw the person you hold to the ground and keep hold. Pinned under
    you, they struggle at -2."""
    t = c.grappling
    if t is None or not t.conscious or t.has_status("prone"):
        return None
    c.exert(0.4)
    perception.emit_noise(sim, c, c.pos, "struggle")
    with sim.focus(c.pos, t.pos):
        if not _hold_contest(sim, c, t):
            sim.log(f"{c.name} tries to throw {t.name} down, but {t.name} keeps their feet.")
            return TAKEDOWN_MS
        sim.log(f"{c.name} slams {t.name} into the ground.")
        t.add_status("prone", None)
        dice = combat.attack_dice(c, {"damage": {"st": "thrust", "add": 0}})
        combat.deal_damage(sim, t, dice.roll(sim.rng), "crush", "torso", source=c, knockback_ok=False)
    perception.emit_noise(sim, None, t.pos, "thud")
    return TAKEDOWN_MS


def disarm(sim: "Sim", c: "Creature") -> int | None:
    """Twist the weapon out of the hands of the person you hold. They resist
    with ST or their skill with it."""
    t = c.grappling
    w = t.wielded if t is not None else None
    if w is None:
        return None
    skill = max((t.skill(a["skill"]) for a in w.attacks), default=0)
    c.exert(0.3)
    with sim.focus(c.pos, t.pos):
        if not _hold_contest(sim, c, t, resist=max(t.stat("ST"), skill)):
            sim.log(f"{t.name} hangs on to {w.the}.")
            return DISARM_MS
        t.wielded = None
        sim.drop(t.pos, w)
        sim.log(f"{c.name} twists {w.the} out of {t.name}'s hands.")
    return DISARM_MS


def release(sim: "Sim", c: "Creature") -> int | None:
    t = c.grappling
    if t is None:
        return None
    c.grappling = None
    c.statuses.pop("grappling", None)
    if t.grappled_by is c:
        t.grappled_by = None
        t.statuses.pop("grappled", None)
    t.choked = 0
    return 200


def struggle(sim: "Sim", c: "Creature") -> int | None:
    """Try to break a hold: your ST against theirs (they have the leverage).
    Held from behind is -3; every second of choking saps you another -1."""
    g = c.grappled_by
    if g is None:
        return None
    pinned = c.has_status("prone") and not g.has_status("prone")
    penalty = (c.action_penalty("melee") + (3 if getattr(g, "rear_hold", False) else 0) + c.choked
               + (2 if pinned else 0))
    mine = check(sim.rng, max(c.stat("ST"), c.skill("wrestling")) - penalty)
    theirs = check(sim.rng, max(g.stat("ST"), g.skill("wrestling")) + 2)
    perception.emit_noise(sim, c, c.pos, "struggle")
    with sim.focus(c.pos, g.pos):
        if mine.success and (not theirs.success or mine.margin > theirs.margin):
            sim.log(f"{c.name} breaks free of {g.name}!")
            release(sim, g)
        else:
            sim.log(f"{c.name} struggles in {g.name}'s grip.")
    return 1000


def check_grapple(sim: "Sim", c: "Creature") -> None:
    """Holds break when the two get separated or the holder can't hold on."""
    for holder, held in ((c, c.grappling), (c.grappled_by, c)):
        if holder is None or held is None:
            continue
        if (holder.dead or not holder.conscious
                or not sim.in_melee_reach(holder.pos, held.pos) or not holder.body.functional_with("grasp")):
            release(sim, holder)


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
