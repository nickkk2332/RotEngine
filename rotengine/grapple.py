"""Grappling: you grab a *thing*, then work on what you've got hold of.

A hold is a grip on one part of someone (their neck, their right arm, their
body) or on the weapon in their hands. What you can do depends on the grip:

* **neck**: choke (a sleeper hold: out in seconds, harmless if you let go),
  strangle (squeeze the throat: slower to put them out, but it hurts, and a
  strong grip crushes the windpipe), or twist (snap the neck).
* **an arm, hand, leg or foot**: wrench (break the joint, or tear the part
  off if you're strong enough), squeeze (crush it), throw them down
  (a leg grip trips at +2), take the weapon out of that hand.
* **the body**: bear hug (squeeze the ribs), take down, hurl.
* **their weapon**: wrest it away. Grabbing for a weapon is dangerous: miss
  a blade and it cuts your hand, miss a gun and it may go off in your face;
  and a blade ripped free through your grip slices it.

While you hold an arm (or the hand, or the weapon itself) they can't use the
weapon in it. Changing grip means grabbing again; if that fails you keep the
old grip. All of this is ordinary JSON: parts with "grab" can be grabbed,
"choke" parts can be choked, parts with a by_type "wrench" entry can be
wrenched, and the squeeze and wrench damage types decide the rest.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

from . import combat, perception, physics
from .dice import check, p_success

if TYPE_CHECKING:
    from .creature import Creature
    from .sim import Sim

GRAB_MS = 1000
CHOKE_MS = 1000
WRENCH_MS = 1000
SQUEEZE_MS = 1000
TAKEDOWN_MS = 1000
DISARM_MS = 1000
THROW_MS = 1000
CHOKE_HYPOXIA = 3.0            # brain damage per second of choking someone already out
KNOCKOUT_MS = (20_000, 60_000)  # how long a choke keeps someone under
WEAPON = "weapon"              # the hold id for a grip on someone's weapon
WEAPON_GRAB_PENALTY = {"melee": -3, "ranged": -2, "long": -1}
LEG_TRIP_BONUS = 2


# -- what can be grabbed ----------------------------------------------------------
def grab_targets(t: "Creature") -> list[str]:
    """Everything on t you could grab: parts marked "grab", and the weapon."""
    parts = [p.id for p in t.body.parts.values() if p.data.get("grab") and not p.destroyed]
    if t.wielded is not None and t.conscious:
        parts.append(WEAPON)
    return parts


def grab_penalty(t: "Creature", what: str) -> int:
    if what == WEAPON:
        w = t.wielded
        if w is None:
            return 0
        if any(a.get("kind") == "melee" for a in w.attacks):
            return WEAPON_GRAB_PENALTY["melee"]
        return WEAPON_GRAB_PENALTY["long" if w.data.get("two_handed") else "ranged"]
    return t.body.part(what).data.get("hit_penalty", 0)


def grab_skill(c: "Creature", t: "Creature", what: str, unaware: bool = False) -> int:
    """Wrestling at the location's penalty, halved against someone who
    doesn't see it coming (they aren't guarding their throat)."""
    pen = grab_penalty(t, what)
    return c.skill("wrestling") - c.action_penalty("melee") + (int(pen / 2) if unaware else pen)


def grab_odds(sim: "Sim", c: "Creature", t: "Creature", what: str) -> float:
    """Chance a grab lands: the Wrestling roll, then their defense (if they
    saw you coming). Out cold or dead, it just works."""
    if not t.conscious:
        return 1.0
    d = combat.defense_against(sim, c, t, "melee")
    p = p_success(grab_skill(c, t, what, unaware=d is None))
    return p if d is None else p * (1 - p_success(d[1]))


def hold_name(t: "Creature", hold: str | None) -> str:
    if hold == WEAPON:
        return t.wielded.name if t.wielded is not None else "weapon"
    if hold is None or hold not in t.body.parts:
        return "body"
    return "body" if hold == "torso" else t.body.part(hold).name


def weapon_danger(t: "Creature") -> str:
    """What happens if you fumble a grab for t's weapon."""
    w = t.wielded
    if w is None:
        return ""
    if _blade(w) is not None:
        return "a miss cuts you"
    if _gun(w) is not None:
        return "a miss may get you shot"
    return ""


def _blade(w) -> dict | None:
    return next((a for a in w.attacks if a.get("kind") == "melee"
                 and a["damage"].get("type") in ("cut", "impale")), None)


def _gun(w) -> dict | None:
    if w.ammo == 0:
        return None
    return next((a for a in w.attacks if a.get("kind") == "ranged"), None)


# -- grabbing ---------------------------------------------------------------------
def grab(sim: "Sim", c: "Creature", target: "Creature", what: str = "torso") -> int | None:
    """Get hold of a part of someone next to you, or the weapon in their
    hands. From behind, or on someone who never saw you coming, there's no
    defending against it. Already holding them, this changes your grip (and
    a miss leaves the old grip where it was)."""
    regrip = c.grappling is target
    if (target is c or not c.body.functional_with("grasp") or c.pos[2] != target.pos[2]
            or not sim.in_melee_reach(c.pos, target.pos) or what not in grab_targets(target)
            or (c.grappling is not None and not regrip)
            or (target.grappled_by is not None and not regrip)
            or (regrip and what == c.hold)):
        return None
    combat.face(c, target.pos)
    name = hold_name(target, what)
    if not target.conscious:  # a body: just take hold of it (to drag it somewhere dark)
        with sim.focus(c.pos, target.pos):
            whose = "body" if target.dead else "limp form"
            sim.log(f"{c.name} takes hold of {target.name}'s {whose if what == 'torso' else name}.")
        _hold(c, target, what, rear=True)
        return GRAB_MS
    c.exert(0.3)
    defense = combat.defense_against(sim, c, target, "melee")
    roll = check(sim.rng, grab_skill(c, target, what, unaware=defense is None))
    perception.emit_noise(sim, c, c.pos, "struggle")
    with sim.focus(c.pos, target.pos):
        missed = not roll.success
        if missed:
            sim.log(f"{c.name} grabs for {target.name}'s {name} and misses.")
        elif defense is not None:
            combat.spend_defense(sim, target)
            if check(sim.rng, defense[1]).success:
                sim.log(f"{c.name} grabs for {target.name}'s {name}, but {target.name} twists away.")
                missed = True
        if missed:
            perception.notice_attacker(sim, target, c)
            if what == WEAPON and defense is not None:
                _weapon_bites(sim, c, target)
            return GRAB_MS
        rear = defense is None
        if what == WEAPON:
            sim.log(f"{c.name} grabs {target.name}'s {name}!")
        elif target.body.part(what).data.get("choke"):
            sim.log(f"{c.name} gets an arm around {target.name}'s neck from behind." if rear
                    else f"{c.name} gets {target.name} by the throat.")
        elif what == "torso":
            sim.log(f"{c.name} gets hold of {target.name}{' from behind' if rear else ''}.")
        else:
            sim.log(f"{c.name} grabs {target.name}'s {name}{' from behind' if rear else ''}.")
    _hold(c, target, what, rear=rear or (regrip and c.rear_hold))
    perception.notice_attacker(sim, target, c)  # they know now (a grip on the throat keeps them quiet)
    return GRAB_MS


def _hold(c: "Creature", t: "Creature", what: str, rear: bool) -> None:
    if c.grappling is t and c.hold != what:
        t.choked = 0  # the grip on the throat is gone
    c.grappling, t.grappled_by, c.hold = t, c, what
    c.rear_hold = rear  # taken from behind: much harder to get out of
    c.add_status("grappling", None)
    t.add_status("grappled", None)


def _weapon_bites(sim: "Sim", c: "Creature", t: "Creature") -> None:
    """A grab for a weapon that fails: blades cut the reaching hand, and a
    gun can go off at point-blank range."""
    w = t.wielded
    if w is None or not t.can_act:
        return
    blade = _blade(w)
    if blade is not None:
        hands = c.body.functional_with("grasp")
        hand = next((h for h in hands if h.data.get("primary")), hands[0] if hands else None)
        raw = combat.attack_dice(t, blade).roll(sim.rng)
        sim.log(f"  {c.name}'s hand closes on the edge of {w.the}.")
        combat.deal_damage(sim, c, raw, blade["damage"]["type"], hand.id if hand else None, source=t,
                           knockback_ok=False)
        return
    if _gun(w) is not None:
        plans = [p for p in combat.attack_plans(sim, t, c, allow_aim=False)
                 if p.item is w and p.attack.get("kind") == "ranged"]
        if plans:
            sim.log(f"  {t.name} fires as {c.name} grabs for {w.the}!")
            combat.resolve_attack(sim, t, c, max(plans, key=lambda p: p.value))


# -- what you can do with a grip ------------------------------------------------------
def hold_part(c: "Creature"):
    """The part c is holding, or None (holding a weapon, or nobody)."""
    t = c.grappling
    if t is None or c.hold in (None, WEAPON) or c.hold not in t.body.parts:
        return None
    return t.body.part(c.hold)


def moves(c: "Creature") -> list[str]:
    """What c can do with the grip it has: a subset of choke, strangle,
    wrench, squeeze, takedown, disarm, wrest, hurl."""
    t = c.grappling
    if t is None:
        return []
    if c.hold == WEAPON:
        return ["wrest"] if t.wielded is not None else []
    part = hold_part(c)
    if part is None:
        return []
    out = []
    if part.data.get("choke") and not t.dead:
        out += ["choke", "strangle"]
    if "wrench" in part.data.get("by_type", {}):
        out.append("wrench")
    if not part.data.get("choke"):
        out.append("squeeze")
    if t.conscious and not t.has_status("prone"):
        out.append("takedown")
    if t.wielded is not None and _controls_weapon_hand(t, part):
        out.append("disarm")
    out.append("hurl")
    return out


def _controls_weapon_hand(t: "Creature", part) -> bool:
    """Holding the hand that grips the weapon, or the arm it hangs from."""
    for hand in t.body.parts.values():
        if "grasp" in hand.tags and (hand.data.get("primary") or t.wielded.data.get("two_handed")):
            if part.id in (hand.id, hand.data.get("parent")):
                return True
    return False


def restrained(t: "Creature") -> set[str]:
    """Parts of t someone has hold of (with everything hanging off them), or
    {"weapon"}. A held arm can't swing the sword in its hand."""
    g = t.grappled_by
    if g is None or g.hold is None:
        return set()
    if g.hold == WEAPON:
        return {WEAPON}
    out = {g.hold}
    grew = True
    while grew:
        grew = False
        for p in t.body.parts.values():
            if p.data.get("parent") in out and p.id not in out:
                out.add(p.id)
                grew = True
    return out


def silenced(t: "Creature") -> bool:
    """A grip on the throat keeps someone from crying out."""
    g = t.grappled_by
    return g is not None and g.hold in t.body.parts and bool(t.body.part(g.hold).data.get("choke"))


def _contest(sim: "Sim", c: "Creature", t: "Creature", resist: int | None = None, bonus: int = 0) -> bool:
    """Doing something to someone you hold: your ST or Wrestling (+2 for the
    leverage, +3 more from behind) against their ST or Wrestling. Someone out
    cold doesn't resist."""
    if not t.conscious:
        return True
    mine = check(sim.rng, max(c.stat("ST"), c.skill("wrestling")) + 2 + (3 if c.rear_hold else 0) + bonus
                 - c.action_penalty("melee"))
    base = resist if resist is not None else max(t.stat("ST"), t.skill("wrestling"))
    theirs = check(sim.rng, base - t.action_penalty("melee") - t.choked)
    return mine.success and (not theirs.success or mine.margin > theirs.margin)


def _technique(c: "Creature") -> int:
    return max(0, (c.skill("wrestling") - 12) // 2)


def wrench_dice(c: "Creature"):
    """Swing damage from ST (with a speedster's momentum), plus technique:
    +1 per 2 points of Wrestling above 12."""
    return combat.attack_dice(c, {"damage": {"st": "swing", "add": _technique(c)}})


def squeeze_dice(c: "Creature"):
    """Grip strength: thrust damage from ST, plus technique."""
    return combat.attack_dice(c, {"damage": {"st": "thrust", "add": _technique(c)}})


def odds(c: "Creature", t: "Creature", part, dtype_id: str = "wrench") -> tuple[float, int | None, float]:
    """(average injury per successful wrench or squeeze, how many more to
    break the part or None, chance one tears it off)."""
    dtype = c.content.get("damage_type", dtype_id)
    spec = t.body.spec(part, dtype_id)
    dr, mult, mh = t.dr(part.id, dtype_id), t.body.wound_multiplier(part, dtype), t.max_hp
    dice = wrench_dice(c) if dtype_id == "wrench" else squeeze_dice(c)
    avg = p_tear = 0.0
    for raw, p in dice.distribution().items():
        pen = raw - dr
        injury = max(1, math.floor(pen * mult)) if pen > 0 else 0
        avg += injury * p
        if (dtype.get("dismembers") and spec.get("destroy_at") is not None
                and injury >= mh * spec["destroy_at"]):
            p_tear += p
    tries = None
    if spec.get("fracture_at") is not None and not part.fractured and avg > 0:
        tries = max(1, math.ceil((mh * spec["fracture_at"] - part.damage) / avg))
    return avg, tries, p_tear


def choke(sim: "Sim", c: "Creature") -> int | None:
    """Sleeper hold on the neck you're holding. The one being choked can't
    cry out; each second they roll CON (worse every second) or go limp for
    half a minute or so. Keep squeezing after that and you're killing them."""
    t = c.grappling
    if t is None or t.dead or "choke" not in moves(c):
        return None
    t.choked += 1
    perception.emit_noise(sim, c, c.pos, "sneak")
    with sim.focus(c.pos, t.pos):
        _choke_round(sim, c, t, resist=2, verb="tightens the chokehold on")
    return CHOKE_MS


def strangle(sim: "Sim", c: "Creature") -> int | None:
    """Two hands on the throat. Slower to put them out than a proper choke
    (+2 to their CON), but every second crushes the throat too: a strong
    grip crushes the windpipe (they suffocate), a monstrous one the neck."""
    t = c.grappling
    if t is None or t.dead or "strangle" not in moves(c):
        return None
    t.choked += 1
    perception.emit_noise(sim, c, c.pos, "sneak")
    with sim.focus(c.pos, t.pos):
        _choke_round(sim, c, t, resist=4, verb="throttles")
        if not t.dead:
            combat.deal_damage(sim, t, squeeze_dice(c).roll(sim.rng), "squeeze", c.hold, source=c,
                               knockback_ok=False)
    if t.dead:
        release(sim, c)
    return CHOKE_MS


def _choke_round(sim: "Sim", c: "Creature", t: "Creature", resist: int, verb: str) -> None:
    if t.conscious:
        if not check(sim.rng, t.stat("CON") - 2 * t.choked + resist).success:
            sim.log(f"{t.name} goes limp in {c.name}'s grip.")
            t.statuses.pop("prone", None)
            t.add_status("unconscious", sim.time + sim.rng.randint(*KNOCKOUT_MS))
            t.add_status("prone", None)
        else:
            sim.log(f"{c.name} {verb} {t.name}.")
        return
    t.body.hypoxia += CHOKE_HYPOXIA
    if t.body.hypoxia >= 100:
        combat.kill(sim, t, "strangled")
        release(sim, c)
    elif t.choked % 5 == 0:
        sim.log(f"{c.name} keeps squeezing {t.name}'s throat...")


def wrench(sim: "Sim", c: "Creature", part_id: str | None = None) -> int | None:
    """Joint lock, limb break, neck snap, on the part you're holding. Armor
    doesn't help; the joint's own strength (its wrench DR) and natural
    toughness do. A normal person breaks an arm in a couple of goes and
    needs several to snap a neck. Someone strong enough to do the part's
    whole destroy threshold in one pull tears it off, and is left holding
    it instead of them."""
    t = c.grappling
    part = hold_part(c)
    if t is None or part is None or "wrench" not in moves(c) or part_id not in (None, part.id):
        return None
    combat.face(c, t.pos)
    c.exert(0.4)
    perception.emit_noise(sim, c, c.pos, "struggle")
    spec = t.body.spec(part, "wrench")
    was_dead = t.dead
    with sim.focus(c.pos, t.pos):
        if not _contest(sim, c, t):
            sim.log(f"{c.name} strains at {t.name}'s {part.name}, but {t.name} fights it.")
            return WRENCH_MS
        verb = spec.get("verb", "wrenches {target}'s {part}").format(target=t.name, part=part.name)
        sim.log(f"{c.name} {verb}.")
        inj = combat.deal_damage(sim, t, wrench_dice(c).roll(sim.rng), "wrench", part.id,
                                 source=c, knockback_ok=False)
        if was_dead and inj.newly_destroyed:
            sim.log(f"  {inj.spec.get('destroy_text', 'torn off').capitalize()}.")
        if part.destroyed:  # it came off in their hands
            release(sim, c)
            item = inj.severed
            if item is not None and c.wielded is None and c.can_grip(item):
                sim.items[:] = [(p, i) for p, i in sim.items if i is not item]
                c.wielded = item
                sim.log(f"{c.name} is left holding {item.name}.")
    return WRENCH_MS


def squeeze(sim: "Sim", c: "Creature") -> int | None:
    """Crush what you're holding: a hand, an arm, the ribs (a bear hug).
    Grip strength (thrust from ST) against the part's squeeze DR; armor
    doesn't help. Most people can barely hurt a forearm this way; the Hulk
    turns a hand to paste."""
    t = c.grappling
    part = hold_part(c)
    if t is None or part is None or "squeeze" not in moves(c):
        return None
    c.exert(0.4)
    perception.emit_noise(sim, c, c.pos, "struggle")
    with sim.focus(c.pos, t.pos):
        if not _contest(sim, c, t):
            sim.log(f"{c.name} squeezes {t.name}'s {hold_name(t, c.hold)}, but {t.name} braces against it.")
            return SQUEEZE_MS
        sim.log(f"{c.name} crushes {t.name} in a bear hug." if part.id == "torso"
                else f"{c.name} crushes {t.name}'s {part.name}.")
        combat.deal_damage(sim, t, squeeze_dice(c).roll(sim.rng), "squeeze", part.id, source=c,
                           knockback_ok=False)
    return SQUEEZE_MS


def takedown(sim: "Sim", c: "Creature") -> int | None:
    """Throw the person you hold to the ground and keep hold. A leg grip
    trips them (+2). Pinned under you, they struggle at -2."""
    t = c.grappling
    part = hold_part(c)
    if t is None or "takedown" not in moves(c):
        return None
    c.exert(0.4)
    perception.emit_noise(sim, c, c.pos, "struggle")
    trip = part is not None and "stance" in part.tags
    with sim.focus(c.pos, t.pos):
        if not _contest(sim, c, t, bonus=LEG_TRIP_BONUS if trip else 0):
            sim.log(f"{c.name} tries to throw {t.name} down, but {t.name} keeps their feet.")
            return TAKEDOWN_MS
        sim.log(f"{c.name} {'yanks' if trip else 'slams'} {t.name} {'off their feet' if trip else 'into the ground'}.")
        t.add_status("prone", None)
        dice = combat.attack_dice(c, {"damage": {"st": "thrust", "add": 0}})
        combat.deal_damage(sim, t, dice.roll(sim.rng), "crush", "torso", source=c, knockback_ok=False)
    perception.emit_noise(sim, None, t.pos, "thud")
    return TAKEDOWN_MS


def disarm(sim: "Sim", c: "Creature") -> int | None:
    """Twist the weapon out of the hand (or arm) you're holding. They resist
    with ST or their skill with it. It falls at their feet."""
    t = c.grappling
    if t is None or "disarm" not in moves(c):
        return None
    w = t.wielded
    c.exert(0.3)
    with sim.focus(c.pos, t.pos):
        if not _contest(sim, c, t, resist=_weapon_resist(t)):
            sim.log(f"{t.name} hangs on to {w.the}.")
            return DISARM_MS
        t.wielded = None
        sim.drop(t.pos, w)
        sim.log(f"{c.name} twists {w.the} out of {t.name}'s hand.")
    return DISARM_MS


def wrest(sim: "Sim", c: "Creature") -> int | None:
    """Tear away the weapon you've got hold of. No leverage bonus here: it's
    your grip against theirs. Win and it's yours (if you have a hand free)."""
    t = c.grappling
    if t is None or "wrest" not in moves(c):
        return None
    w = t.wielded
    c.exert(0.4)
    perception.emit_noise(sim, c, c.pos, "struggle")
    with sim.focus(c.pos, t.pos):
        if not _contest(sim, c, t, resist=_weapon_resist(t), bonus=-2):
            sim.log(f"{c.name} and {t.name} wrestle over {w.the}.")
            return DISARM_MS
        t.wielded = None
        release(sim, c)
        if c.wielded is None and c.can_grip(w):
            c.wielded = w
            sim.log(f"{c.name} tears {w.the} out of {t.name}'s hands and keeps it.")
        else:
            sim.drop(t.pos, w)
            sim.log(f"{c.name} tears {w.the} out of {t.name}'s hands; it clatters to the floor.")
    return DISARM_MS


def _weapon_resist(t: "Creature") -> int:
    w = t.wielded
    skill = max((t.skill(a["skill"]) for a in w.attacks), default=0) if w is not None else 0
    return max(t.stat("ST"), skill)


def hurl(sim: "Sim", c: "Creature", direction: tuple[int, int]) -> int | None:
    """Throw the person you're holding (by any grip on their body)."""
    body = c.grappling
    if body is None or direction == (0, 0) or c.hold == WEAPON:
        return None
    physics.fling(sim, c, body, direction)
    c.exert(0.5)
    return THROW_MS


def release(sim: "Sim", c: "Creature") -> int | None:
    t = c.grappling
    if t is None:
        return None
    c.grappling = None
    c.hold = None
    c.statuses.pop("grappling", None)
    if t.grappled_by is c:
        t.grappled_by = None
        t.statuses.pop("grappled", None)
    t.choked = 0
    return 200


def struggle(sim: "Sim", c: "Creature") -> int | None:
    """Try to break a hold: your ST against theirs (they have the leverage).
    Held from behind is -3, pinned -2; every second of choking saps you
    another -1. Someone holding your weapon has no leverage, but rip a
    blade free through their grip and it cuts their hand."""
    g = c.grappled_by
    if g is None:
        return None
    on_weapon = g.hold == WEAPON
    pinned = c.has_status("prone") and not g.has_status("prone")
    penalty = (c.action_penalty("melee") + (3 if g.rear_hold else 0) + c.choked
               + (2 if pinned else 0))
    base = _weapon_resist(c) if on_weapon else max(c.stat("ST"), c.skill("wrestling"))
    mine = check(sim.rng, base - penalty)
    theirs = check(sim.rng, max(g.stat("ST"), g.skill("wrestling")) + (0 if on_weapon else 2))
    perception.emit_noise(sim, c, c.pos, "struggle")
    with sim.focus(c.pos, g.pos):
        if mine.success and (not theirs.success or mine.margin > theirs.margin):
            w = c.wielded
            blade = _blade(w) if on_weapon and w is not None else None
            if on_weapon:
                sim.log(f"{c.name} rips {w.the} free of {g.name}'s grip!")
            else:
                sim.log(f"{c.name} breaks free of {g.name}!")
            release(sim, g)
            if blade is not None:
                hands = g.body.functional_with("grasp")
                if hands:
                    raw = combat.attack_dice(c, blade).roll(sim.rng) // 2
                    sim.log(f"  The edge slices through {g.name}'s fingers.")
                    combat.deal_damage(sim, g, raw, blade["damage"]["type"], hands[0].id, source=c,
                                       knockback_ok=False)
        else:
            sim.log(f"{c.name} struggles in {g.name}'s grip.")
    return 1000


def check_grapple(sim: "Sim", c: "Creature") -> None:
    """Holds break when the two get separated, the holder can't hold on, the
    part being held is gone, or the weapon being held has been dropped."""
    for holder, held in ((c, c.grappling), (c.grappled_by, c)):
        if holder is None or held is None:
            continue
        lost_grip = (holder.hold == WEAPON and held.wielded is None) or (
            holder.hold in held.body.parts and held.body.part(holder.hold).destroyed)
        if (holder.dead or not holder.conscious or lost_grip
                or not sim.in_melee_reach(holder.pos, held.pos) or not holder.body.functional_with("grasp")):
            release(sim, holder)
