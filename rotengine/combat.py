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
* Crushing blows cause knockback in proportion to damage vs the target's ST,
  and a knocked-back body can go through walls, windows and off ledges.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from . import effects
from .body import Injury
from .dice import Dice, check, p_success

if TYPE_CHECKING:
    from .creature import Creature, Item
    from .sim import Sim
    from .world import Pos


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
    base = st_damage(attacker.stat("ST"), dmg["st"])
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


# -- planning ------------------------------------------------------------------
@dataclass
class AttackPlan:
    attack: dict
    item: "Item | None"
    dice: Dice
    skill: int                 # final effective skill
    location: str | None       # None = wherever it lands
    deceptive: int             # levels of deceptive attack (-2 skill / -1 enemy defense each)
    value: float               # expected injury per second

    @property
    def time_ms(self) -> int:
        return self.attack.get("time_ms", 1000)


def base_skill(sim: "Sim", attacker: "Creature", target: "Creature", attack: dict,
               item: "Item | None") -> int | None:
    """Effective skill before hit location / deception, or None if impossible."""
    dist = sim.distance(attacker, target)
    skill = attacker.skill(attack["skill"]) + attack.get("skill_mod", 0)
    skill += int(attacker.status_sum("attack_mod")) - attacker.shock
    if attack["kind"] == "melee":
        if not sim.in_melee_reach(attacker.pos, target.pos, attack.get("reach", 1)):
            return None
    else:
        if dist > attack.get("range", 100) or item is not None and item.ammo == 0:
            return None
        if not sim.world.has_los(attacker.pos, target.pos):
            return None
        skill += attack.get("acc", 0) + range_penalty(dist)
    return skill


def best_attack_plan(sim: "Sim", attacker: "Creature", target: "Creature",
                     surprise: bool = False, skill_bonus: int = 0,
                     damage_bonus: int = 0) -> AttackPlan | None:
    best: AttackPlan | None = None
    body = target.body
    cap = max(1.0, target.hp + target.max_hp)  # overkill is worthless
    parts = body.targetable()
    total_weight = sum(p.data.get("weight", 0) for p in parts) or 1
    for attack, item in attacker.attacks():
        skill0 = base_skill(sim, attacker, target, attack, item)
        if skill0 is None:
            continue
        skill0 += skill_bonus
        dice = attack_dice(attacker, attack)
        dice = Dice(dice.n, dice.sides, dice.add + damage_bonus)
        dtype = sim.content.get("damage_type", attack["damage"]["type"])
        defended = not surprise and target.can_act
        defense = target.best_defense(attack["kind"])[1] if defended else None
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
        time_s = attack.get("time_ms", 1000) / 1000 * attacker.trait_product("action_time_mult", attack["kind"])
        for loc, penalty, exp in options:
            if exp <= 0:
                continue
            for dec in range(0, 6):
                skill = skill0 + penalty - 2 * dec
                if skill < 3 or (dec and defense is None):
                    break
                p_hit = p_success(skill)
                p_def = p_success(defense - dec) if defense is not None else 0.0
                hits = min(shots, 1 + max(0.0, skill - 10.5) / attack.get("recoil", 1)) if shots > 1 else 1
                value = p_hit * (1 - p_def) * hits * exp / time_s
                if best is None or value > best.value:
                    best = AttackPlan(attack, item, dice, skill, loc, dec, value)
    return best


# -- resolution ------------------------------------------------------------
def resolve_attack(sim: "Sim", attacker: "Creature", target: "Creature", plan: AttackPlan,
                   surprise: bool = False) -> int:
    """Carry out an attack; returns the time it took in ms."""
    attack, item = plan.attack, plan.item
    ranged = attack["kind"] == "ranged"
    shots = attack.get("rof", 1)
    if item is not None and item.ammo is not None:
        shots = min(shots, item.ammo)
        item.ammo -= shots
    where = f" (aiming for the {target.body.part(plan.location).name})" if plan.location else ""
    verb = attack.get("verb", attack["name"])
    roll = check(sim.rng, plan.skill)
    time_ms = int(plan.time_ms * attacker.trait_product("action_time_mult", attack["kind"]))
    if not roll.success:
        sim.log(f"{attacker.name} {verb} {target.name}{where} and misses "
                f"(rolled {roll.roll} vs {plan.skill}).")
        if ranged:
            _stray(sim, attacker, target, plan)
        return time_ms

    hits = 1
    if ranged and shots > 1:
        hits = min(shots, 1 + roll.margin // attack.get("recoil", 1))
    if not surprise and target.can_act and not roll.critical:
        name, value = target.best_defense(attack["kind"])
        d = check(sim.rng, value - plan.deceptive)
        if d.success:
            blocked = min(hits, 1 + d.margin)
            hits -= blocked
            if hits == 0:
                sim.log(f"{attacker.name} {verb} {target.name}{where}, but {target.name} {name}.")
                return time_ms
    tag = " (critical!)" if roll.critical else " (unaware!)" if surprise else ""
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
    for _ in range(hits):
        if target.dead:
            break
        raw = max(1, plan.dice.roll(sim.rng) - cover_dr)
        loc = plan.location if plan.location and not target.body.part(plan.location).destroyed else None
        deal_damage(sim, target, raw, attack["damage"]["type"], loc, source=attacker,
                    origin=attacker.pos)
    return time_ms


def _stray(sim: "Sim", attacker: "Creature", target: "Creature", plan: AttackPlan) -> None:
    """A missed shot keeps going and chews up whatever it hits."""
    ax, ay, az = attacker.pos
    tx, ty, tz = target.pos
    far = (tx + (tx - ax) * 3, ty + (ty - ay) * 3, tz)
    for p in sim.world.line(target.pos, far):
        if not sim.world.in_bounds(p):
            return
        if sim.world.fill_mat(p).get("solid"):
            mat = sim.world.fill_mat(p)
            if sim.world.damage_fill(p, plan.dice.roll(sim.rng)):
                sim.log(f"A stray round punches through the {mat['name']}.")
                sim.terrain_changed()
                continue
            return


def deal_damage(sim: "Sim", target: "Creature", raw: int, dtype_id: str, part_id: str | None = None,
                source: "Creature | None" = None, origin: "Pos | None" = None,
                knockback_ok: bool = True) -> Injury | None:
    dtype = sim.content.get("damage_type", dtype_id)
    if part_id is None or target.body.part(part_id).destroyed:
        part_id = target.body.roll_location(sim.rng)
    dr = target.dr(part_id, dtype_id)
    inj = target.body.wound(part_id, raw, dr, dtype)
    armor = f" - DR {dr}" if dr else ""
    if target.dead:
        pass  # corpses still take wounds (and still fly), but nothing more to report
    elif not inj.penetrated:
        sim.log(f"  {raw} {dtype_id} to {target.name}'s {inj.part.name} doesn't get through (DR {dr}).")
    else:
        notes = []
        if inj.newly_destroyed:
            notes.append(inj.part.data.get("destroy_text", "destroyed") + "!")
            notes += [f"{n} lost" for n in inj.lost]
        elif inj.newly_crippled:
            notes.append("crippled!")
        sim.log(f"  {raw} {dtype_id}{armor} to the {inj.part.name} -> {inj.injury} injury. "
                f"{target.name}: {round(target.hp)}/{target.max_hp} HP" + (f"; {', '.join(notes)}" if notes else ""))
        pain = inj.injury / max(1.0, target.max_hp / 10)
        if not target.has_trait("high_pain_threshold"):
            target.shock = min(4, target.shock + int(pain))
        _after_injury(sim, target, inj)
        if not target.dead:
            sim.fire_hooks(target, "on_damaged", source, {"damage": inj.injury})
        if source is not None and not source.dead and target.dead:
            sim.fire_hooks(source, "on_kill", target)
    if knockback_ok and dtype.get("knockback") and origin is not None:
        tiles = raw // max(1, target.stat("ST") - 2)
        if tiles >= 1:
            knockback(sim, target, origin, tiles)
    return inj


def _after_injury(sim: "Sim", c: "Creature", inj: Injury) -> None:
    part = inj.part
    if inj.newly_destroyed and part.data.get("fatal_if_destroyed"):
        kill(sim, c)
        return
    if inj.newly_crippled:
        if "grasp" in part.tags and c.wielded is not None and (
                part.data.get("primary") or c.wielded.data.get("two_handed")):
            sim.log(f"  {c.name} drops the {c.wielded.name}.")
            c.wielded = None
        if "stance" in part.tags and not c.has_status("prone"):
            sim.log(f"  {c.name} collapses.")
            c.add_status("prone", None)
    check_hp_thresholds(sim, c)
    if c.dead or not c.conscious:
        return
    kd = part.data.get("knockdown_mod", 0)
    if inj.injury > c.max_hp / 2:
        r = check(sim.rng, c.stat("CON") + kd + int(c.trait_sum("knockdown_bonus")))
        if r.success:
            return
        if r.margin <= -5 or r.fumble:
            knock_out(sim, c)
        else:
            sim.log(f"  {c.name} is knocked down and stunned.")
            c.add_status("prone", None)
            c.add_status("stunned", sim.time + 2000)


def check_hp_thresholds(sim: "Sim", c: "Creature") -> None:
    """GURPS-style death checks each time HP crosses another -1xHP."""
    if c.dead:
        return
    mh = c.max_hp
    if c.hp <= -5 * mh:
        kill(sim, c)
        return
    below = math.floor(-c.hp / mh) if c.hp < 0 else 0
    while c.death_checks < below:
        c.death_checks += 1
        if not check(sim.rng, c.stat("CON")).success:
            kill(sim, c)
            return


def knock_out(sim: "Sim", c: "Creature") -> None:
    if c.conscious:
        sim.log(f"  {c.name} falls unconscious.")
        c.add_status("unconscious", None)
        c.add_status("prone", None)


def kill(sim: "Sim", c: "Creature") -> None:
    if not c.dead:
        c.dead = True
        c.statuses.clear()
        c.body.bleed_rate = 0.0
        sim.log(f"  {c.name} dies.")


def knockback(sim: "Sim", target: "Creature", origin: "Pos", tiles: int) -> None:
    if tiles <= 0:
        return
    dx = (target.pos[0] > origin[0]) - (target.pos[0] < origin[0])
    dy = (target.pos[1] > origin[1]) - (target.pos[1] < origin[1])
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
    if not target.dead and not target.has_status("prone"):
        if not check(sim.rng, target.stat("DEX") - (tiles - 1)).success:
            target.add_status("prone", None)
    sim.check_fall(target)


def use_power(sim: "Sim", c: "Creature", power: dict, target: "Creature | None") -> int:
    cost = power.get("cost", {})
    c.stamina -= cost.get("stamina", 0)
    if c.stamina < 0:
        c.body.hp += c.stamina  # overexertion eats into HP
        c.stamina = 0
    sim.log(f"{c.name} uses {power['name']}!")
    ctx = effects.Ctx(sim, c, target)
    effects.run(power["effects"], ctx)
    if ctx.vars.get("_abort"):
        sim.log(f"  ...but the {power['name']} fizzles.")
    return int(power.get("time_ms", 1000))
