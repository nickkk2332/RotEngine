"""Actions: the verbs a creature can perform, shared by the AI and the player.

Each action either does something and returns its cost in the actor's *own*
milliseconds (the sim divides by tempo), or returns None when it isn't
possible right now, having changed nothing. Keeping these in one place means
the player and the AI play by exactly the same rules.
"""
from __future__ import annotations

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
    return [i for i in c.carried if "throwable" in i.data]


def throw(sim: "Sim", c: "Creature", item, target: "Pos") -> int | None:
    """Throw a carried item at a tile. Grenades are armed as they leave your hand."""
    if item not in c.carried or not c.body.functional_with("grasp"):
        return None
    weight = item.data.get("weight", 1)
    if sim.distance_pos(c.pos, target) > physics.throw_range(c, weight) or target == c.pos:
        return None
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
    sim.log(f"{c.name} draws the {nxt.name}.")
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
    penalty = c.action_penalty("melee") + (3 if getattr(g, "rear_hold", False) else 0) + c.choked
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
