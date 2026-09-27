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

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from . import effects, grapple, perception
from .body import Injury
from .creature import Item
from .dice import Dice, check, p_success

if TYPE_CHECKING:
    from .creature import Creature, Item
    from .sim import Sim
    from .world import Pos

REACTION_MS = 1000          # a defense occupies this much of the defender's own time
TEMPO_DEFENSE = 2           # defense bonus per doubling of defender tempo over attacker tempo
STACKED_DEFENSE_PENALTY = 2  # per earlier defense still inside the window
SIDE_PENALTY = 2
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


def attack_dice(attacker: "Creature", attack: dict) -> Dice:
    dmg = attack["damage"]
    if "dice" in dmg:
        return Dice.parse(dmg["dice"])
    # Momentum: limbs moving N times faster hit harder (traits opt in).
    st = attacker.stat("ST") * attacker.tempo ** attacker.trait_sum("momentum_exponent")
    base = st_damage(round(st), dmg["st"])
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
    if attack["kind"] == "melee":
        if not sim.in_melee_reach(attacker.pos, target.pos, attack.get("reach", 1)):
            return None
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
        options += [(p.id, p.data.get("hit_penalty", 0), per_part[p.id]) for p in parts]
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
    with sim.focus(attacker.pos, target.pos):
        took = _resolve(sim, attacker, target, plan, surprise, attack, item, ranged, time_ms)
    attacker.noisy_until = sim.time + 2000
    noise = attack.get("noise", "gunshot" if ranged else "melee")
    perception.emit_noise(sim, attacker, attacker.pos, noise)
    perception.notice_attacker(sim, target, attacker)
    return took


def _resolve(sim, attacker, target, plan, surprise, attack, item, ranged, time_ms) -> int:
    shots = attack.get("rof", 1)
    if item is not None and item.ammo is not None:
        shots = min(shots, item.ammo)
        item.ammo -= shots
    if ranged:
        attacker.aim_target = None
    where = f" (aiming for the {target.body.part(plan.location).name})" if plan.location else ""
    verb = attack.get("verb", attack["name"])
    roll = check(sim.rng, plan.skill)

    if not roll.success:
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
        hits = min(shots, 1 + roll.margin // attack.get("recoil", 1))
    strays = shots - hits
    defense = None if roll.critical else defense_against(sim, attacker, target, attack["kind"], surprise)
    if defense is not None:
        name, value = defense
        spend_defense(sim, target)
        d = check(sim.rng, value - plan.deceptive)
        if d.success:
            blocked = min(hits, max(1, 1 + d.margin))
            hits -= blocked
            strays += blocked if ranged else 0
            if hits == 0:
                sim.log(f"{attacker.name} {verb} {target.name}{where}, but {target.name} {name}.")
                if ranged:
                    _stray_rounds(sim, attacker, target, plan.dice, strays)
                return time_ms
    tag = " (critical!)" if roll.critical else " (unaware!)" if surprise else ""
    if not surprise and defense is None and not roll.critical and target.conscious:
        tag = " (never saw it coming!)" if not _noticed(sim, target, attacker) else " (from behind!)"
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
            if p != target.pos:
                other = sim.creature_at(p, include_down=True)
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
        if inj.internal:
            notes.append("internal bleeding")
        sim.log(f"  {raw} {dtype_id}{armor} to the {inj.part.name} -> {inj.injury} injury. "
                f"{target.name}: {round(target.hp)}/{target.max_hp} HP" + (f"; {', '.join(notes)}" if notes else ""))
        shock = int(inj.injury / max(1.0, target.max_hp / 10) * target.trait_product("pain_mult"))
        target.shock = min(4, target.shock + shock)
        target.aim_target = None
        _after_injury(sim, target, inj, prev_hp)
        if (target.conscious and inj.injury >= target.max_hp / 3 and not grapple.silenced(target)
                and not target.has_trait("high_pain_threshold")):
            perception.emit_noise(sim, target, target.pos, "scream")
        if not target.dead:
            sim.fire_hooks(target, "on_damaged", source, {"damage": inj.injury})
        if source is not None and not source.dead and target.dead:
            sim.fire_hooks(source, "on_kill", target)
    if knockback_ok and dtype.get("knockback") and origin is not None:
        tiles = raw // max(1, target.stat("ST") - 2)
        if tiles >= 1:
            knockback(sim, target, origin, tiles)
    return inj


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
    if inj.newly_crippled or inj.newly_destroyed:
        check_grip(sim, c)
        if not c.body.functional_with("stance") and c.body.total_with("stance") and not c.has_status("prone"):
            sim.log(f"  {c.name} collapses.")
            c.add_status("prone", None)
        elif "stance" in part.tags and not c.has_status("prone"):
            sim.log(f"  {c.name} goes down.")
            c.add_status("prone", None)
    if not c.conscious:
        return

    # Falling below 0 HP (or another -HP) with this hit: stay conscious?
    below = math.floor(-c.hp / mh) + 1 if c.hp <= 0 else 0
    was_below = math.floor(-prev_hp / mh) + 1 if prev_hp <= 0 else 0
    if below > was_below and not check(sim.rng, c.stat("CON") - (below - 1)).success:
        knock_out(sim, c, "from the trauma")
        return

    if inj.injury > mh / 2:
        kd = part.data.get("knockdown_mod", 0)
        r = check(sim.rng, c.stat("CON") + kd + int(c.trait_sum("knockdown_bonus")))
        if not r.success:
            if r.margin <= -5 or r.fumble:
                knock_out(sim, c, "from the blow")
                return
            sim.log(f"  {c.name} is knocked down and stunned.")
            c.add_status("prone", None)
            sim.apply_status(c, "stunned", 2000)
    if (inj.injury >= mh / 3 or inj.newly_fractured or inj.newly_destroyed) \
            and not c.has_status("stunned") and not c.has_status("agony"):
        r = check(sim.rng, c.stat("WIS") + int(c.trait_sum("pain_resist")))
        if not r.success:
            sim.log(f"  {c.name} doubles over in agony.")
            sim.apply_status(c, "agony", 2000 if r.margin > -5 else 4000)


def check_grip(sim: "Sim", c: "Creature") -> None:
    if c.wielded is not None and not c.can_grip(c.wielded):
        sim.log(f"  {c.name} drops {c.wielded.the}.")
        sim.drop(c.pos, c.wielded)
        c.wielded = None


def knock_out(sim: "Sim", c: "Creature", why: str = "") -> None:
    if c.conscious:
        sim.log(f"  {c.name} falls unconscious{' ' + why if why else ''}.")
        c.add_status("unconscious", None)
        c.add_status("prone", None)


def kill(sim: "Sim", c: "Creature", cause: str) -> None:
    if not c.dead:
        c.dead = True
        c.death_cause = cause
        c.statuses.clear()
        c.body.bleed_rate = c.body.internal_bleed = 0.0
        sim.log(f"  {c.name} dies ({cause}).")


def knockback(sim: "Sim", target: "Creature", origin: "Pos", tiles: int) -> None:
    """Momentum doesn't care about armor: knockback uses the damage rolled,
    not what got through (a riot shield still gets shoved)."""
    if tiles <= 0:
        return
    dx = _sign(target.pos[0] - origin[0])
    dy = _sign(target.pos[1] - origin[1])
    if dx == dy == 0:
        dx, dy = sim.rng.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
    sim.log(f"  {target.name} is hurled back {tiles} tile{'s' * (tiles > 1)}!")
    world = sim.world
    for i in range(tiles):
        x, y, z = target.pos
        nxt = (x + dx, y + dy, z)
        remaining = tiles - i
        if not world.in_bounds(nxt):
            break
        if world.fill_mat(nxt).get("solid"):
            mat = world.fill_mat(nxt)
            slam = Dice(remaining, 6).roll(sim.rng)
            broke = world.damage_fill(nxt, slam * 2)
            sim.log(f"  {target.name} {'smashes through' if broke else 'slams into'} the {mat['name']}!")
            perception.emit_noise(sim, None, nxt, "crash")
            deal_damage(sim, target, slam, "crush", knockback_ok=False)
            if not broke:
                break
            sim.terrain_changed()
        blocker = sim.creature_at(nxt)
        if blocker is not None:
            slam = Dice(remaining, 6).roll(sim.rng)
            sim.log(f"  {target.name} crashes into {blocker.name}!")
            deal_damage(sim, target, slam, "crush", knockback_ok=False)
            deal_damage(sim, blocker, slam, "crush", knockback_ok=False)
            if not blocker.has_status("prone"):
                blocker.add_status("prone", None)
            break
        target.pos = nxt
        if not world.supported(nxt):
            break
    target.aim_target = None
    sim.make_room(target)
    if not target.dead and not target.has_status("prone"):
        if not check(sim.rng, target.stat("DEX") - (tiles - 1)).success:
            target.add_status("prone", None)
    sim.check_fall(target)


FIZZLE_COOLDOWN_MS = 2000


def use_power(sim: "Sim", c: "Creature", power: dict, target: "Creature | None") -> int:
    """Pay, run the effects, start the cooldown. A power whose effects abort
    (no room to land, target out of range) fizzles and can't be retried for a
    couple of seconds, so the AI doesn't burn itself out retrying."""
    c.stamina -= power.get("cost", {}).get("stamina", 0)
    sim.log(f"{c.name} uses {power.get('name', power['id'])}!")
    if power.get("noise"):
        perception.emit_noise(sim, c, c.pos, power["noise"])
    ctx = effects.Ctx(sim, c, target)
    effects.run(power["effects"], ctx)
    cooldown = power.get("cooldown_ms", 0)
    if ctx.vars.get("_abort"):
        sim.log(f"  ...but the {power.get('name', power['id'])} fizzles.")
        cooldown = max(cooldown, FIZZLE_COOLDOWN_MS)
    if cooldown:
        c.cooldowns[power["id"]] = sim.time + cooldown / c.tempo
    return int(power.get("time_ms", 1000))
