"""Actions: the verbs a creature can perform, shared by the AI and the player.

Each action either does something and returns its cost in the actor's *own*
milliseconds (the sim divides by tempo), or returns None when it isn't
possible right now, having changed nothing. Keeping these in one place means
the player and the AI play by exactly the same rules.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import combat, effects, perception, physics, training
from .dice import check
from .grapple import (  # noqa: F401  (grappling verbs live in grapple.py)
    check_grapple, choke, disarm, grab, hurl, release, squeeze, strangle, struggle, takedown, wrench, wrest)

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
    if victim is not None and victim.conscious and victim.grappled_by is c:
        # hauling someone who's fighting you: a hold contest every step
        from .grapple import _contest
        c.exert(0.3)
        if not _contest(sim, c, victim):
            with sim.focus(c.pos, victim.pos):
                sim.log(f"{c.name} hauls at {victim.name}, who digs in.")
            perception.emit_noise(sim, c, c.pos, "struggle")
            return 1000
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
    if any(d.get("no_stand") for d in c.status_defs()):
        return None  # collapsed: your legs won't have it yet
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
    training.practice(sim, c, "first_aid", skill, r.success)
    who = "their own wounds" if patient is c else f"{patient.name}'s wounds"
    if r.success:
        patient.body.bleed_rate = 0.0 if r.critical or r.margin >= 5 else bleed * 0.25
        note = " The internal bleeding needs a surgeon." if patient.body.internal_bleed > 0.01 else ""
        sim.log(f"{c.name} binds {who} (bleeding {bleed:.1f} -> {patient.body.bleed_rate:.1f}%/s).{note}")
    else:
        sim.log(f"{c.name} fumbles with {who}.")
    return FIRST_AID_MS


def usable(c: "Creature") -> list:
    """Carried things with a "use" block (medicine)."""
    return [i for i in c.carried if "use" in i.data]


def cannot_use(sim: "Sim", c: "Creature", item) -> str | None:
    """Why c can't use this item right now, or None."""
    use = item.data.get("use")
    if use is None or item not in c.carried:
        return "that isn't something you can use"
    if not c.body.functional_with("grasp"):
        return "you have no working hand"
    if "needs" in use and not effects.test(use["needs"], effects.Ctx(sim, c, c)):
        return use.get("needs_text", "it wouldn't do anything")
    return None


def use_item(sim: "Sim", c: "Creature", item) -> int | None:
    """Use medicine (or anything with a "use" block) on yourself. With a
    "skill", the roll decides between "effects" and "fail_effects"; the item
    is used up either way unless it says "keep": true."""
    if cannot_use(sim, c, item):
        return None
    use = item.data["use"]
    ctx = effects.Ctx(sim, c, c)
    ok = True
    note = ""
    if "skill" in use:
        target = c.skill(use["skill"]) + use.get("mod", 0) - c.action_penalty()
        roll = check(sim.rng, target)
        training.practice(sim, c, use["skill"], target, roll.success)
        ok = roll.success
        note = "" if ok else f" (a botched job: rolled {roll.roll} vs {target})"
    with sim.focus(c.pos):
        sim.log(f"{c.name} uses {item.the}{note}.")
        effects.run(use["effects"] if ok else use.get("fail_effects", []), ctx)
    if not use.get("keep"):
        c.carried.remove(item)
    return int(use.get("time_ms", 1000))


def items_near(sim: "Sim", c: "Creature") -> list:
    """Items on your tile or next to you."""
    return [item for pos, item in sim.items if pos == c.pos or sim.in_melee_reach(c.pos, pos)]


def pick_up(sim: "Sim", c: "Creature", weapons_only: bool = False, which=None) -> int | None:
    """Pick something up from your tile or next to you: `which` item, or the
    most urgent. A live grenade comes first (so you can throw it back); then a
    weapon, if your hands are free (into your hands); then anything else
    (into your pack)."""
    if not c.body.functional_with("grasp"):
        return None
    near = [(i, item) for i, (pos, item) in enumerate(sim.items)
            if pos == c.pos or sim.in_melee_reach(c.pos, pos)]
    if which is not None:
        near = [(i, item) for i, item in near if item is which]
        if not near:
            return None

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
        for i, item in near:  # medicine, armor, charges, spare weapons: into the pack
            return take(i, item, False)
    return None


def drop_item(sim: "Sim", c: "Creature", item) -> int | None:
    if item is c.wielded:
        c.wielded = None
    elif item in c.carried:
        c.carried.remove(item)
    else:
        return None
    sim.drop(c.pos, item)
    with sim.focus(c.pos):
        sim.log(f"{c.name} drops {item.the}.")
    return 500


def wield(sim: "Sim", c: "Creature", item) -> int | None:
    """Take a carried weapon in hand (what you held goes in the pack)."""
    if item not in c.carried or not item.attacks or not c.can_grip(item):
        return None
    c.carried.remove(item)
    if c.wielded is not None:
        c.carried.append(c.wielded)
    c.wielded = item
    sim.log(f"{c.name} draws {item.the}.")
    return SWAP_MS


WEAR_MS = 8000


def wear(sim: "Sim", c: "Creature", item) -> int | None:
    """Put on carried armor (slow: don't do it with anyone watching). Takes
    off whatever it would replace on the same body parts."""
    if item not in c.carried or not item.armor:
        return None
    covers = set(item.armor["covers"])
    for old in [w for w in c.worn if set(w.armor["covers"]) & covers]:
        c.worn.remove(old)
        c.carried.append(old)
    c.carried.remove(item)
    c.worn.append(item)
    sim.log(f"{c.name} puts on {item.the}.")
    return WEAR_MS


def take_off(sim: "Sim", c: "Creature", item) -> int | None:
    if item not in c.worn:
        return None
    c.worn.remove(item)
    c.carried.append(item)
    sim.log(f"{c.name} takes off {item.the}.")
    return WEAR_MS // 2



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
