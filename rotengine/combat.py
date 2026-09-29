"""Attack resolution: 3d6 to hit, 3d6 to defend, then armor, wounding and the
body's reaction.

Things that fall out of these rules rather than being special-cased:
* Skill is margin. A skill-20 gunman can take -3 for the vitals, trade 2 more
  skill to make the target's dodge 1 worse, and still hit on most rolls.
  best_attack_plan works this out for every creature, so experts fight like
  experts without per-character AI.
* Damage has to get past DR before it counts, so it scales by threshold rather
  than by percentage. A rifle round against a DR 25 hide mostly does nothing,
  and a ST 60 haymaker against a DR 4 vest does not care about the vest.
* Defending takes time. Every defense opens a reaction window (1 s of the
  defender's own time), and each further defense inside it is at -2. Being
  mobbed, or attacked by something ten times faster than you, overwhelms you
  without a special rule.
* Relative speed matters. Defense is +2 per doubling of the defender's tempo
  over the attacker's, and -2 per halving.
* Facing matters. Attacks from the side are at -2 to defend, and from behind
  there is no defense at all.
* Bullets are physical. Misses and dodged rounds keep flying, hit
  bystanders, chew through walls, and hit the cover a target is hiding behind.
"""
from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from . import effects, flight, grapple, perception, training
from .body import Injury
from .creature import Item
from .dice import Dice, check, p_success

if TYPE_CHECKING:
    from .creature import Creature, Item
    from .sim import Sim
    from .world import Pos

REACTION_MS = 1000          # a defense occupies this much of the defender's own time
SURPRISE_MS = 900           # attacked by someone you never saw: this long to gather yourself
TEMPO_DEFENSE = 2           # defense bonus per doubling of defender tempo over attacker tempo
STACKED_DEFENSE_PENALTY = 2  # per earlier defense still inside the window
SIDE_PENALTY = 2
HELPLESS_BONUS = 5          # melee against someone unconscious (and called shots cost half)
AIM_MS = 1000
BYSTANDER_HIT = 9           # 3d6 roll to hit someone standing in a stray round's path
_RING = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]


# -- numbers ---------------------------------------------------------------
def st_damage(st: int, kind: str) -> Dice:
    """Muscle-powered damage. Superlinear enough that ST 60 is a wrecking
    ball, gentle enough that ST 10 vs ST 12 is a real but small edge."""
    mean = (0.35 if kind == "thrust" else 0.55) * st - 2
    mean = max(0.5, mean)
    n = max(1, round(mean / 3.5))
    return Dice(n, 6, math.floor(mean - 3.5 * n + 0.5))


def striking_st(c: "Creature") -> float:
    """The strength behind a blow: ST, times the momentum of a speedster
    (tempo^momentum_exponent)."""
    return c.stat("ST") * c.tempo ** c.trait_sum("momentum_exponent")


def attack_dice(attacker: "Creature", attack: dict) -> Dice:
    dmg = attack["damage"]
    if "dice" in dmg:
        return Dice.parse(dmg["dice"])
    # Momentum: limbs moving N times faster hit harder (traits opt in).
    base = st_damage(round(striking_st(attacker)), dmg["st"])
    return Dice(base.n, base.sides, base.add + dmg.get("add", 0))


def range_penalty(dist: float) -> int:
    """Roughly GURPS's speed/range table: -2 at 5 tiles, -4 at 10, -8 at 50."""
    if dist <= 2:
        return 0
    return -max(0, math.floor(6 * math.log10(dist) - 2 + 0.5))


@lru_cache(maxsize=65536)
def expected_injury(dice: Dice, dr: int, mult: float, cap: float) -> float:
    total = 0.0
    for raw, p in dice.distribution().items():
        pen = max(1, raw) - dr
        if pen > 0:
            total += p * min(cap, max(1, math.floor(pen * mult)))
    return total


def _sign(v: int) -> int:
    return (v > 0) - (v < 0)


def face(c: "Creature", toward: "Pos") -> None:
    d = (_sign(toward[0] - c.pos[0]), _sign(toward[1] - c.pos[1]))
    if d != (0, 0):
        c.facing = d


def arc(defender: "Creature", from_pos: "Pos") -> str:
    """'front', 'side' or 'rear' relative to where the defender is facing."""
    d = (_sign(from_pos[0] - defender.pos[0]), _sign(from_pos[1] - defender.pos[1]))
    if d == (0, 0):
        return "front"
    steps = abs(_RING.index(d) - _RING.index(defender.facing))
    steps = min(steps, 8 - steps)
    return "front" if steps <= 1 else "side" if steps == 2 else "rear"


def cover(sim: "Sim", shooter: "Pos", target: "Creature") -> tuple[int, "Pos | None"]:
    """How much of the target hides behind solid terrain, seen from shooter.
    Casts rays at the corners of the target's tile: -1 per blocked corner,
    -4 when all but the middle is hidden. Returns (penalty, blocking voxel)."""
    tx, ty, tz = target.pos
    blocked, where = 0, None
    for ox, oy in ((-0.45, -0.45), (0.45, -0.45), (-0.45, 0.45), (0.45, 0.45)):
        hit = sim.world.first_opaque(shooter, (tx + ox, ty + oy, tz), exclude=target.pos)
        if hit is not None:
            blocked += 1
            where = hit
    return (-4 if blocked >= 3 else -blocked), where


# -- defense -------------------------------------------------------------------
def defense_against(sim: "Sim", attacker: "Creature", target: "Creature", kind: str,
                    surprise: bool = False) -> tuple[str, int] | None:
    """The target's defense roll target, or None if it can't defend at all."""
    if surprise or not target.conscious or not _noticed(sim, target, attacker):
        return None
    sim.expire_statuses(target)
    side = arc(target, attacker.pos)
    ratio = target.tempo / attacker.tempo
    if side == "rear" and ratio >= 4:
        side = "front" if ratio >= 16 else "side"  # enough time to glance back
    if side == "rear":
        return None
    name, value = target.best_defense(kind)
    value -= target.defense_penalty()
    # Seeing it coming: a tempo-8 speedster watches a trigger pull in slow
    # motion (+6), and a normal person barely registers the speedster (-6).
    value += round(TEMPO_DEFENSE * math.log2(ratio))
    if side == "side":
        value -= SIDE_PENALTY
    if sim.time < target.defense_until:
        value -= STACKED_DEFENSE_PENALTY * target.defenses_in_window
    return name, value


def _noticed(sim: "Sim", target: "Creature", attacker: "Creature") -> bool:
    """You can only defend against someone you know is there. NPCs track that
    with awareness; a player notices whatever they can make out."""
    if target.controller == "player":
        return perception.can_make_out(sim, target, attacker)
    return perception.aware_of(target, attacker)


def spend_defense(sim: "Sim", target: "Creature") -> None:
    if sim.time >= target.defense_until:
        target.defenses_in_window = 0
        target.defense_until = sim.time + int(REACTION_MS / target.tempo)
    target.defenses_in_window += 1


_spend_defense = spend_defense  # old name


# -- planning ------------------------------------------------------------------
@dataclass
class AttackPlan:
    attack: dict
    item: "Item | None"
    dice: Dice
    skill: int                 # final effective skill
    location: str | None       # None = wherever it lands
    deceptive: int             # levels of deceptive attack (-2 skill / -1 enemy defense each)
    value: float               # expected injury per second of the attacker's time
    aim_first: bool = False    # spend time aiming before this shot
    time_ms: int = 1000
    p_hit: float = 0.0         # chance the attack roll succeeds
    p_defended: float = 0.0    # chance the target's defense then stops it
    injury: float = 0.0        # expected injury per landed hit at that location

    @property
    def p_land(self) -> float:
        return self.p_hit * (1 - self.p_defended)


def base_skill(sim: "Sim", attacker: "Creature", target: "Creature", attack: dict,
               item: "Item | None") -> int | None:
    """Effective skill before hit location / deception, or None if impossible."""
    skill = (attacker.skill(attack["skill"]) + attack.get("skill_mod", 0)
             - attacker.action_penalty(attack["kind"]))
    if attacker.grappled_by is target:  # hitting back at whoever's holding you
        if item is not None and item.data.get("two_handed"):
            return None  # no bringing a rifle (or a bat) to bear on someone wrapped around you
        if target.rear_hold:
            skill -= 2   # blind, backwards, over your shoulder
    if attack["kind"] == "melee":
        if not sim.in_melee_reach(attacker.pos, target.pos, attack.get("reach", 1)):
            return None
        skill += int(target.status_sum("melee_target_mod"))  # someone on the floor is easier to hit
        if not target.conscious:
            skill += HELPLESS_BONUS  # out cold: hard to miss, though it's still your swing
        return skill
    dist = sim.distance(attacker, target)
    if dist > attack.get("range", 100) or item is not None and item.ammo == 0:
        return None
    if not sim.world.has_los(attacker.pos, target.pos):
        return None
    if attacker.aim_target == target.uid:
        skill += attack.get("acc", 0)
    skill += int(target.status_sum("ranged_target_mod"))  # e.g. lying flat
    return skill + range_penalty(dist) + cover(sim, attacker.pos, target)[0]


def friendly_in_line(sim: "Sim", shooter: "Creature", target: "Creature") -> bool:
    for p in sim.world.line(shooter.pos, target.pos)[:-1]:
        c = sim.creature_at(p)
        if c is not None and c.team == shooter.team:
            return True
    return False


def attack_plans(sim: "Sim", attacker: "Creature", target: "Creature",
                 surprise: bool = False, skill_bonus: int = 0, damage_bonus: int = 0,
                 allow_aim: bool = True) -> list[AttackPlan]:
    """Every way the attacker could go at the target right now: attack x
    hit location x feint level x (aim first or not), with odds. The AI takes
    the best; the player's attack menu shows them all."""
    return [AttackPlan(*c) for c in _candidates(sim, attacker, target, surprise, skill_bonus,
                                                 damage_bonus, allow_aim)]


def best_attack_plan(sim: "Sim", attacker: "Creature", target: "Creature",
                     surprise: bool = False, skill_bonus: int = 0,
                     damage_bonus: int = 0, allow_aim: bool = True) -> AttackPlan | None:
    best = max(_candidates(sim, attacker, target, surprise, skill_bonus, damage_bonus, allow_aim),
               key=lambda c: c[6], default=None)
    return AttackPlan(*best) if best else None


def _candidates(sim, attacker, target, surprise, skill_bonus, damage_bonus, allow_aim):
    if allow_aim and (attacker.shock or sim.enemies_within(attacker, 2)):
        allow_aim = False  # just hurt, or someone's in your face: no time to line up a shot
    body = target.body
    cap = max(1.0, target.hp + target.max_hp)  # overkill is worthless
    parts = body.targetable()
    total_weight = sum(p.data.get("weight", 0) for p in parts) or 1
    for attack, item in attacker.attacks():
        skill0 = base_skill(sim, attacker, target, attack, item)
        if skill0 is None:
            continue
        ranged = attack["kind"] == "ranged"
        if ranged and friendly_in_line(sim, attacker, target):
            continue
        skill0 += skill_bonus
        dice = attack_dice(attacker, attack)
        dice = Dice(dice.n, dice.sides, dice.add + damage_bonus)
        dtype = sim.content.get("damage_type", attack["damage"]["type"])
        d = defense_against(sim, attacker, target, attack["kind"], surprise)
        defense = d[1] if d else None
        per_part = {}
        for p in parts:
            part_cap = cap
            if p.data.get("cripple_at") is not None:
                part_cap = min(cap, math.floor(target.max_hp * p.data["cripple_at"]) + 1)
            per_part[p.id] = expected_injury(dice, target.dr(p.id, dtype["id"]),
                                             body.wound_multiplier(p, dtype), part_cap)
        options = [(None, 0, sum(per_part[p.id] * p.data.get("weight", 0) for p in parts) / total_weight)]
        helpless = attack["kind"] == "melee" and not target.conscious
        options += [(p.id, int(p.data.get("hit_penalty", 0) / (2 if helpless else 1)), per_part[p.id])
                    for p in parts]
        shots = attack.get("rof", 1)
        if item is not None and item.ammo is not None:
            shots = min(shots, item.ammo)
        speed_mult = attacker.trait_product("action_time_mult", attack["kind"])
        shot_ms = attack.get("time_ms", 1000) * speed_mult
        variants = [(skill0, shot_ms, False)]
        if allow_aim and ranged and attack.get("acc", 0) and attacker.aim_target != target.uid:
            variants.append((skill0 + attack["acc"], shot_ms + AIM_MS * speed_mult, True))
        for skill_v, ms, aim in variants:
            for loc, penalty, exp in options:
                if exp <= 0:
                    continue
                for dec in range(0, 6):
                    skill = skill_v + penalty - 2 * dec
                    if skill < 3 or (dec and defense is None):
                        break
                    p_hit = p_success(skill)
                    p_def = p_success(defense - dec) if defense is not None else 0.0
                    hits = min(shots, 1 + max(0.0, skill - 10.5) / attack.get("recoil", 1)) if shots > 1 else 1
                    value = p_hit * (1 - p_def) * hits * exp / (ms / 1000)
                    yield (attack, item, dice, skill, loc, dec, value, aim, int(ms), p_hit, p_def, exp)


# -- resolution ------------------------------------------------------------
def resolve_attack(sim: "Sim", attacker: "Creature", target: "Creature", plan: AttackPlan,
                   surprise: bool = False) -> int:
    """Carry out a planned attack (or the aiming before it); returns the time
    it took in the attacker's own ms."""
    attack, item = plan.attack, plan.item
    ranged = attack["kind"] == "ranged"
    speed_mult = attacker.trait_product("action_time_mult", attack["kind"])
    face(attacker, target.pos)
    if plan.aim_first:
        attacker.aim_target = target.uid
        sim.log(f"{attacker.name} takes aim at {target.name}.")
        return int(AIM_MS * speed_mult)

    time_ms = int(attack.get("time_ms", 1000) * speed_mult)
    attacker.exert(attack.get("fatigue", 0.05 if ranged else 0.3))
    if ranged and sim.timed_shots:
        # the rounds are in the air for a few milliseconds (flight.py); the
        # shot is settled when they get there, and if the target isn't where
        # it was aimed any more, they fly on past
        shots = _spend_ammo(attack, item)
        attacker.aim_target = None
        speed = attack.get("velocity", DEFAULT_VELOCITY)
        unseen = not _noticed(sim, target, attacker)  # judged now: the bang comes with the bullet
        flight.launch_tracer(sim, attacker, target.pos, speed, functools.partial(
            _rounds_arrive, sim, attacker, target, plan, surprise, target.pos, attacker.pos, shots, time_ms,
            unseen))
        took = time_ms
    else:
        with sim.focus(attacker.pos, target.pos):
            took = _resolve(sim, attacker, target, plan, surprise, attack, item, ranged, time_ms)
    attacker.noisy_until = sim.time + 2000
    noise = attack.get("noise", "gunshot" if ranged else "melee")
    perception.emit_noise(sim, attacker, attacker.pos, noise)
    perception.notice_attacker(sim, target, attacker)
    return took


DEFAULT_VELOCITY = 400.0   # tiles per second for a ranged attack without its own "velocity"


PARRY_JARRED = 1.5      # a blow this many times your strength jars the arm that stops it
PARRY_SMASHED = 3.0     # ... and this many times, a parry doesn't stop it at all


def _weapon_arm(c: "Creature"):
    hand = next((p for p in c.body.parts.values() if p.data.get("primary") and "grasp" in p.tags), None)
    if hand is None:
        return None
    return c.body.parts.get(hand.data.get("parent"), hand)


def _parry_holds(sim: "Sim", attacker: "Creature", target: "Creature", plan: AttackPlan) -> bool:
    """Parrying something far stronger than you. Half again your strength and the
    parry holds, but the force goes into your arms and may tear the weapon
    from your hand; three times and it just smashes through your guard."""
    ratio = striking_st(attacker) / max(1.0, target.stat("ST"))
    if ratio < PARRY_JARRED:
        return True
    arm = _weapon_arm(target)
    w = target.wielded
    if w is not None and not check(sim.rng, target.stat("ST") + 2 - round(4 * (ratio - 1))).success:
        sim.log(f"  The blow tears {w.the} out of {target.name}'s hand!")
        target.wielded = None
        sim.drop(target.pos, w)
    if ratio >= PARRY_SMASHED:
        sim.log(f"  {target.name} gets a guard up, but the blow smashes straight through it!")
        return False
    jar = round(plan.dice.roll(sim.rng) * (1 - 1 / ratio))
    sim.log(f"  {target.name} parries, but the force of it jars their "
            f"{arm.name if arm else 'arms'}.")
    if jar > 0 and arm is not None:
        deal_damage(sim, target, jar, "crush", arm.id, source=attacker, knockback_ok=False)
    return True


def _spend_ammo(attack: dict, item) -> int:
    shots = attack.get("rof", 1)
    if item is not None and item.ammo is not None:
        shots = min(shots, item.ammo)
        item.ammo -= shots
    return shots


class _Spot:
    """Where someone was (for rounds that arrive after they've gone)."""

    def __init__(self, pos):
        self.pos = pos


def _rounds_arrive(sim, attacker, target, plan, surprise, aimed_at, fired_from, shots, time_ms,
                   unseen=False) -> None:
    """The rounds get where they were aimed. Settle the shot as if from where
    the shooter stood when they fired; if the target has moved off that
    tile (someone very fast), they fly on past as strays."""
    here = attacker.pos
    attacker.pos = fired_from
    try:
        with sim.focus(fired_from, aimed_at):
            if target.dead:  # someone else got him first: the rounds fly on
                _stray_rounds(sim, attacker, _Spot(aimed_at), plan.dice, shots)
            elif target.pos != aimed_at:
                verb = plan.attack.get("verb", plan.attack["name"])
                sim.cue(aimed_at, "miss")
                sim.log(f"{attacker.name} {verb} {target.name}, but {target.name} isn't there any more.")
                _stray_rounds(sim, attacker, _Spot(aimed_at), plan.dice, shots)
            else:
                _resolve(sim, attacker, target, plan, surprise, plan.attack, plan.item, True, time_ms, shots,
                         unseen)
    finally:
        attacker.pos = here


def _resolve(sim, attacker, target, plan, surprise, attack, item, ranged, time_ms, shots=None,
             unseen=None) -> int:
    if shots is None:
        shots = _spend_ammo(attack, item)
    if unseen is None:
        unseen = not _noticed(sim, target, attacker)
    if ranged:
        attacker.aim_target = None
    where = f" (aiming for the {target.body.part(plan.location).name})" if plan.location else ""
    if where and surprise:
        where, surprise_tag = where[:-1] + "; unaware!)", ""
    else:
        surprise_tag = " (unaware!)"
    verb = attack.get("verb", attack["name"])
    roll = check(sim.rng, plan.skill)
    training.practice(sim, attacker, attack["skill"], plan.skill, roll.success)

    if unseen and target.conscious:
        sim.give_pause(target, SURPRISE_MS)  # hit or miss, it takes a moment to understand what happened
    if not roll.success:
        sim.cue(target.pos, "miss")
        sim.log(f"{attacker.name} {verb} {target.name}{where} and misses "
                f"(rolled {roll.roll} vs {plan.skill}).")
        if ranged:
            penalty, blocker = cover(sim, attacker.pos, target)
            if blocker is not None and roll.margin >= penalty:
                _hit_cover(sim, attacker, target, plan, blocker)
                shots -= 1
            _stray_rounds(sim, attacker, target, plan.dice, shots)
        return time_ms

    hits = 1
    if ranged and shots > 1:
        hits = min(shots, 1 + max(0, roll.margin) // attack.get("recoil", 1))
    strays = shots - hits
    defense = None if roll.critical or unseen else defense_against(sim, attacker, target, attack["kind"], surprise)
    if defense is not None:
        name, value = defense
        spend_defense(sim, target)
        d = check(sim.rng, value - plan.deceptive)
        held = d.success and (name != "parries" or _parry_holds(sim, attacker, target, plan))
        if held:
            blocked = min(hits, max(1, 1 + d.margin))
            hits -= blocked
            strays += blocked if ranged else 0
            if hits == 0:
                sim.cue(target.pos, "block")
                sim.log(f"{attacker.name} {verb} {target.name}{where}, but {target.name} {name}.")
                if ranged:
                    _stray_rounds(sim, attacker, target, plan.dice, strays)
                return time_ms
    sim.cue(target.pos, "crit" if roll.critical else "hit")
    tag = " (critical!)" if roll.critical else surprise_tag if surprise else ""
    if not surprise and defense is None and not roll.critical and target.conscious:
        tag = " (never saw it coming!)" if unseen else " (from behind!)"
    sim.log(f"{attacker.name} {verb} {target.name}{where}{tag}" + (f" - {hits} hits" if hits > 1 else "") + ".")

    cover_dr = 0
    if ranged:
        for kind, pos in sim.world.obstacles(attacker.pos, target.pos) or []:
            mat = sim.world.fill_mat(pos) if kind == "fill" else sim.world.floor_mat(pos)
            cover_dr += mat.get("dr", 0)
            broke = (sim.world.damage_fill(pos, plan.dice.roll(sim.rng)) if kind == "fill"
                     else sim.world.damage_floor(pos, plan.dice.roll(sim.rng)))
            if broke:
                sim.log(f"The {mat['name']} shatters!")
                sim.terrain_changed()
                perception.emit_noise(sim, None, pos, "glass")
    for _ in range(hits):
        raw = max(1, plan.dice.roll(sim.rng) - cover_dr)
        loc = plan.location if plan.location and not target.body.part(plan.location).destroyed else None
        deal_damage(sim, target, raw, attack["damage"]["type"], loc, source=attacker,
                    origin=attacker.pos)
    if ranged and strays:
        _stray_rounds(sim, attacker, target, plan.dice, strays)
    return time_ms


def _hit_cover(sim: "Sim", attacker: "Creature", target: "Creature", plan: AttackPlan,
               blocker: "Pos") -> None:
    """The round hits what the target is hiding behind, and may go through it."""
    mat = sim.world.fill_mat(blocker)
    raw = max(1, plan.dice.roll(sim.rng))
    broke = sim.world.damage_fill(blocker, raw)
    through = raw - mat.get("dr", 0)
    if broke:
        sim.terrain_changed()
    if through > 0:
        sim.log(f"  The round punches through the {mat['name']} into {target.name}!")
        deal_damage(sim, target, through, plan.attack["damage"]["type"], source=attacker,
                    origin=attacker.pos)
    else:
        sim.log(f"  The round smacks into the {mat['name']} {target.name} is hiding behind.")


def _stray_rounds(sim: "Sim", shooter: "Creature", target: "Creature", dice: Dice, rounds: int) -> None:
    """Rounds that missed or were dodged keep going: into bystanders, through
    thin walls, and eventually into something solid."""
    if rounds <= 0:
        return
    sx, sy, sz = shooter.pos
    tx, ty, tz = target.pos
    far = (tx + (tx - sx) * 4, ty + (ty - sy) * 4, tz + (tz - sz) * 4)
    path = sim.world.line(shooter.pos, far)
    world = sim.world
    for _ in range(rounds):
        dmg = dice.roll(sim.rng)
        prev = shooter.pos
        for p in path:
            if not world.in_bounds(p) or dmg <= 0:
                break
            slab = world.crossing(prev, p)
            prev = p
            if slab is not None and world.floor_mat(slab) is not None:
                fmat = world.floor_mat(slab)
                if world.damage_floor(slab, dmg):
                    sim.log(f"  A stray round punches through the {fmat['name']} floor.")
                    sim.terrain_changed()
                dmg -= fmat.get("dr", 0)
                if dmg <= 0 or world.floor_mat(slab) is not None:
                    break
            other = sim.creature_at(p, include_down=True)
            if other is not target:
                if other is not None and other is not shooter and check(sim.rng, BYSTANDER_HIT).success:
                    sim.log(f"  A stray round hits {other.name}!")
                    deal_damage(sim, other, dmg, "pierce", source=shooter, origin=shooter.pos)
                    break
            mat = sim.world.fill_mat(p)
            if mat.get("solid"):
                if sim.world.damage_fill(p, dmg):
                    sim.log(f"  A stray round punches through the {mat['name']}.")
                    sim.terrain_changed()
                dmg -= mat.get("dr", 0)
                if dmg <= 0 or sim.world.fill_mat(p).get("solid"):
                    break


def deal_damage(sim: "Sim", target: "Creature", raw: int, dtype_id: str, part_id: str | None = None,
                source: "Creature | None" = None, origin: "Pos | None" = None,
                knockback_ok: bool = True) -> Injury | None:
    dtype = sim.content.get("damage_type", dtype_id)
    if part_id not in target.body.parts or target.body.part(part_id).destroyed:
        part_id = target.body.roll_location(sim.rng)
    dr = target.dr(part_id, dtype_id)
    prev_hp = target.hp
    inj = target.body.wound(part_id, raw, dr, dtype)
    armor = f" - DR {dr}" if dr else ""
    if inj.newly_destroyed and inj.spec.get("sever_item"):
        inj.severed = _sever(sim, target, inj)
    if target.dead:
        pass  # corpses still take wounds (and still fly), but nothing more to report
    elif not inj.penetrated:
        sim.log(f"  {raw} {dtype_id} to {target.name}'s {inj.part.name} doesn't get through (DR {dr}).")
    else:
        p = inj.spec
        notes = []
        if inj.newly_destroyed:
            notes.append(p.get("destroy_text", "destroyed") + "!")
            notes += [f"{n} lost" for n in inj.lost]
        elif inj.newly_fractured:
            notes.append(p.get("fracture_text", "bone broken") + "!")
        elif inj.newly_crippled:
            notes.append("crippled!")
        if inj.dislocatable and not inj.newly_fractured and sim.rng.random() < dtype.get("dislocates", 0):
            inj.part.dislocated = inj.newly_dislocated = True
            inj.part.note = p.get("dislocate_text", "dislocated")
            notes.append(inj.part.note + "!")
        if inj.internal:
            notes.append("internal bleeding")
        sim.log(f"  {raw} {dtype_id}{armor} to the {inj.part.name} -> {inj.injury} injury. "
                f"{target.name}: {round(target.hp)}/{target.max_hp} HP" + (f"; {', '.join(notes)}" if notes else ""))
        _organ_wounds(sim, target, inj, dtype)
        shock = int(inj.injury / max(1.0, target.max_hp / 10) * target.trait_product("pain_mult"))
        target.shock = min(4, target.shock + shock)
        target.aim_target = None
        _after_injury(sim, target, inj, prev_hp)
        if (target.conscious and inj.injury >= target.max_hp / 3 and not grapple.silenced(target)
                and not target.has_trait("high_pain_threshold")):
            perception.emit_noise(sim, target, target.pos, "scream")
        if target.dead:
            target.body.bleed_rate = target.body.internal_bleed = 0.0  # a corpse doesn't bleed out
        elif target.conscious:
            sim.fire_hooks(target, "on_damaged", source, {"damage": inj.injury})
        if source is not None and not source.dead and target.dead:
            sim.fire_hooks(source, "on_kill", target)
    if knockback_ok and dtype.get("knockback") and origin is not None:
        tiles = knockback_tiles(raw, target, source)
        if tiles >= 1:
            knockback(sim, target, origin, tiles)
    return inj


ORGAN_VERBS = {"pierce": "The round tears through", "large_pierce": "It tears through",
               "impale": "The point goes into", "cut": "The blade opens", "crush": "The blow crushes",
               "burn": "It sears", "squeeze": "The crush ruptures", "wrench": "It tears"}
RIB_PUNCTURE_AT = 0.25  # a blunt blow this big (x HP) on broken ribs can drive one inward


def _organ_wounds(sim: "Sim", c: "Creature", inj: Injury, dtype: dict) -> None:
    """What the wound did underneath: which organ it reached, and what that
    means. Also broken ribs driven in by another heavy blow."""
    body, mh = c.body, c.max_hp
    organs = inj.spec.get("organs") or []
    if inj.reaches_organs and organs:
        organ = sim.rng.choices(organs, [o.get("weight", 1) for o in organs])[0]
        if organ["id"] != "miss":
            verb = ORGAN_VERBS.get(inj.damage_type, "It reaches")
            _hit_organ(sim, c, organ, inj.reaches_organs, f"{verb} {c.name}'s {organ['name']}")
    # Broken ribs and another heavy blow to the chest: a rib goes in.
    ribs = next((p for p in body.parts.values() if p.data.get("fracture_punctures")), None)
    if (ribs is not None and dtype.get("fractures") and ribs.fractured and inj.injury >= mh * RIB_PUNCTURE_AT
            and inj.part.id in (ribs.id, *ribs.data["fracture_punctures"].get("under", []))):
        spec = ribs.data["fracture_punctures"]
        if sim.rng.random() < spec.get("chance", 0.3):
            everything = {o["id"]: o for p in body.parts.values() for o in p.data.get("organs", [])}
            pool = [everything[i] for i in spec["organs"] if i in everything]
            if pool:
                organ = sim.rng.choices(pool, [o.get("weight", 1) for o in pool])[0]
                _hit_organ(sim, c, organ, mh * 0.3, f"A broken rib is driven into {c.name}'s {organ['name']}")


def _hit_organ(sim: "Sim", c: "Creature", organ: dict, deep: float, how: str) -> None:
    from .body import organ_hit
    organ_hit(c.body, organ, deep)
    effect = organ.get("effect", "")
    status = organ.get("status")
    if status and sim.rng.random() < organ.get("chance", 1.0):
        c.add_status(status, None)
        if sim.content.get("status", status).get("no_stand") and not c.has_status("prone"):
            c.add_status("prone", None)
        lungs = [k for k in c.body.organs if "lung" in k]
        if status == "punctured_lung" and len(lungs) >= 2:
            c.add_status("both_lungs", None)
            effect = "both lungs are gone: drowning in the air"
    elif status:
        effect = organ.get("else", effect)
    sim.log(f"  {how}" + (f": {effect}" if effect else "") + "!")


def _sever(sim: "Sim", c: "Creature", inj: Injury):
    """A part that comes off leaves something behind: an arm, a leg, a head.
    It lies where it fell, and it can be picked up, swung and thrown."""
    data = sim.content.get("item", inj.spec["sever_item"])
    item = Item(data)
    item.name = f"{c.name}'s {inj.spec.get('sever_name', inj.part.name)}"
    sim.drop(c.pos, item)
    return item


def _after_injury(sim: "Sim", c: "Creature", inj: Injury, prev_hp: float) -> None:
    part = inj.part
    spec = inj.spec
    mh = c.max_hp
    if inj.newly_destroyed:
        if spec.get("fatal_if_destroyed"):
            kill(sim, c, spec.get("death_text", f"{part.name} destroyed"))
            return
        status = spec.get("destroy_status")
        if status:
            c.add_status(status, None)
    if inj.newly_fractured and spec.get("fracture_status"):
        status = spec["fracture_status"]
        c.add_status(status, None)
        if sim.content.get("status", status).get("knocks_out"):
            knock_out(sim, c, "")
    if c.body.hypoxia >= 100:
        kill(sim, c, "brain destroyed")
        return
    if c.hp <= -5 * mh:
        kill(sim, c, "torn apart")
        return
    if inj.newly_crippled or inj.newly_destroyed or inj.newly_dislocated:
        check_grip(sim, c)
        if not c.body.functional_with("stance") and c.body.total_with("stance") and not c.has_status("prone"):
            sim.log(f"  {c.name} collapses.")
            c.add_status("prone", None)
        elif "stance" in part.tags and not c.has_status("prone"):
            sim.log(f"  {c.name} goes down.")
            c.add_status("prone", None)
    if not c.conscious:
        return
    if c.hp <= -SHOCK_OUT * mh:  # past what anyone stays awake through
        knock_out(sim, c, "from the sheer trauma")
        return

    # Knockdown. A big hit puts you down and stuns you. Only a blunt blow
    # ("concussive" damage) to the head (a "concussion" part) can knock you
    # out, and not for long: a cut to the scalp hurts, it doesn't switch you off.
    head = bool(part.data.get("concussion"))
    concussive = head and bool(sim.content.get("damage_type", inj.damage_type).get("concussive"))
    if inj.injury > mh * 2 / 3 or (concussive and inj.injury >= mh / 4):
        kd = part.data.get("knockdown_mod", 0)
        r = check(sim.rng, c.stat("CON") + kd + int(c.trait_sum("knockdown_bonus")))
        if not r.success:
            if concussive and (r.margin <= -5 or r.fumble):
                knock_out(sim, c, "from the blow to the head", sim.rng.randint(*CONCUSSION_MS))
                return
            if r.margin >= -STAGGER_MARGIN or c.has_status("prone"):  # rocked, but still on your feet
                sim.log(f"  {c.name} {'is stunned' if c.has_status('prone') else 'staggers, stunned'}.")
            else:
                sim.log(f"  {c.name} is knocked down and stunned.")
                c.add_status("prone", None)
            sim.apply_status(c, "stunned", 3000 if head else 2000)

    # Being in the red isn't a knockout. It's pain: going into the red, or a
    # big wound once you're there, is a WIS roll. Fail and your legs go:
    # collapsed, but awake, and you can still shoot and fight from the
    # floor, badly. (You go out from blood loss, a blow to the head, lack of
    # air, or trauma past -4 x HP.)
    in_red = c.hp <= 0
    maimed = inj.newly_destroyed and inj.part.data.get("cripple_at") is not None  # a limb gone
    if c.hp <= 0 < prev_hp or (in_red and inj.injury >= mh / 3) or maimed:
        r = check(sim.rng, c.stat("WIS") + int(c.trait_sum("pain_resist")) - int(-c.hp // mh)
                  - (LIMB_LOSS_SHOCK if maimed else 0))
        if not r.success:
            collapse(sim, c, "from the pain")
    if (inj.injury >= (mh / 4 if in_red else mh / 2) or inj.newly_fractured or inj.newly_destroyed
            or inj.newly_dislocated) \
            and not c.has_status("stunned") and not c.has_status("agony"):
        r = check(sim.rng, c.stat("WIS") + int(c.trait_sum("pain_resist")))
        if not r.success:
            sim.log(f"  {c.name} doubles over in agony.")
            sim.apply_status(c, "agony", 1500 if r.margin > -5 else 3000)


SHOCK_OUT = 4        # at -4 x HP nobody stays conscious (-5 x HP is torn apart)
LIMB_LOSS_SHOCK = 4  # losing an arm or a leg: the WIS roll to stay on your feet is this much harder
STAGGER_MARGIN = 2   # a knockdown roll missed by this much or less: stunned where you stand
CONCUSSION_MS = (15_000, 90_000)  # how long a knockout blow to the head keeps you under
SHOCK_BASE = 4       # stays_conscious rolls CON + this, minus how bad it is


def consciousness_penalty(c: "Creature") -> int:
    """How hard it is to stay awake right now: -2 per full HP below 0,
    -2 below 70% blood and -4 below 60%, plus graying out from lack of air."""
    trauma = int(2 * max(0.0, -c.hp) / c.max_hp)
    b = c.body.blood
    blood = 4 if b < 60 else 2 if b < 70 else 0
    return trauma + blood + c.air_penalty()


def stays_conscious(sim: "Sim", c: "Creature") -> bool:
    """A CON roll to stay awake through blood loss, or to come round: CON + 4
    + knockdown bonuses, minus consciousness_penalty. A lost cause past
    -4 x HP, below 50% blood or out of air."""
    if c.hp <= -SHOCK_OUT * c.max_hp or c.body.blood < 50 or c.body.oxygen <= 10:
        return False
    target = c.stat("CON") + SHOCK_BASE + int(c.trait_sum("knockdown_bonus")) - consciousness_penalty(c)
    return check(sim.rng, target).success


def collapse(sim: "Sim", c: "Creature", why: str = "") -> None:
    """Down and can't get up, but still conscious (see the 'collapsed' status)."""
    if c.conscious and not c.has_status("collapsed"):
        if c.has_status("prone"):
            sim.log(f"  {c.name} can't get up{' ' + why if why else ''}.")
        else:
            sim.log(f"  {c.name} collapses{' ' + why if why else ''}.")
        c.add_status("collapsed", None)
        c.add_status("prone", None)


def check_grip(sim: "Sim", c: "Creature") -> None:
    if c.wielded is not None and not c.can_grip(c.wielded):
        sim.log(f"  {c.name} drops {c.wielded.the}.")
        sim.drop(c.pos, c.wielded)
        c.wielded = None


def knock_out(sim: "Sim", c: "Creature", why: str = "", duration_ms: int | None = None) -> None:
    """Out cold: for duration_ms (a blow to the head, a choke), or until the
    body recovers enough to come round (see Sim._can_wake)."""
    if c.conscious:
        sim.log(f"  {c.name} falls unconscious{' ' + why if why else ''}.")
        c.add_status("unconscious", None if duration_ms is None else sim.time + duration_ms)
        c.add_status("prone", None)
        grapple.release(sim, c)  # limp hands let go
    elif not c.dead and duration_ms is None:
        c.statuses["unconscious"] = None  # out already, now for a reason a timer won't fix


def kill(sim: "Sim", c: "Creature", cause: str) -> None:
    if not c.dead:
        c.dead = True
        c.death_cause = cause
        grapple.release(sim, c)
        if c.grappled_by is not None and c.grappled_by.hold not in c.body.parts:
            grapple.release(sim, c.grappled_by)  # a grip on a dead man's weapon is just the weapon
        c.statuses.clear()
        c.body.bleed_rate = c.body.internal_bleed = 0.0
        sim.log(f"  {c.name} dies ({cause}).")


KNOCKBACK_EXPONENT = 0.75  # how much a strength gap multiplies the shove


def knockback_tiles(raw: int, target: "Creature", source: "Creature | None") -> int:
    """How far a blow sends someone: the damage rolled over their mass (ST),
    multiplied by how much stronger the hitter is. Two average people barely
    shove each other; the Hulk puts a soldier through the far wall."""
    mass = max(1.0, target.stat("ST"))
    push = raw / mass
    if source is not None:
        push *= max(1.0, striking_st(source) / mass) ** KNOCKBACK_EXPONENT
    return int(push)


def knockback(sim: "Sim", target: "Creature", origin: "Pos", tiles: int,
              on_land=None, thrower: "Creature | None" = None) -> None:
    """Momentum doesn't care about armor: knockback uses the damage rolled,
    not what got through (a riot shield still gets shoved). The body flies
    tile by tile through world time (see flight.py)."""
    if tiles <= 0:
        return
    dx = _sign(target.pos[0] - origin[0])
    dy = _sign(target.pos[1] - origin[1])
    if dx == dy == 0:
        dx, dy = sim.rng.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
    sim.log(f"  {target.name} is {'thrown' if thrower else 'hurled back'} {tiles} tile{'s' * (tiles > 1)}!")
    flight.launch_body(sim, target, (dx, dy), tiles, on_land, thrower)


FIZZLE_COOLDOWN_MS = 2000


def use_power(sim: "Sim", c: "Creature", power: dict, target: "Creature | None", dest: "Pos | None" = None) -> int:
    """Pay, run the effects, start the cooldown. A power whose effects abort
    (no room to land, target out of range) fizzles and can't be retried for a
    couple of seconds, so the AI doesn't burn itself out retrying."""
    c.stamina -= power.get("cost", {}).get("stamina", 0)
    sim.log(f"{c.name} uses {power.get('name', power['id'])}!")
    if power.get("noise"):
        perception.emit_noise(sim, c, c.pos, power["noise"])
    ctx = effects.Ctx(sim, c, target)
    if dest is not None:
        ctx.vars["dest"] = dest  # the player aimed it at a tile
    effects.run(power["effects"], ctx)
    cooldown = power.get("cooldown_ms", 0)
    if ctx.vars.get("_abort"):
        sim.log(f"  ...but the {power.get('name', power['id'])} fizzles.")
        cooldown = max(cooldown, FIZZLE_COOLDOWN_MS)
    if cooldown:
        c.cooldowns[power["id"]] = sim.time + cooldown / c.tempo
    return int(power.get("time_ms", 1000))
