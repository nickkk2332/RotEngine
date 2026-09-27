"""Perception and stealth: who notices whom, and what they do about it.

Every creature keeps an awareness meter per enemy (0-100). At the start of
its turn it looks: for each enemy in line of sight and not behind it, a 3d6
Perception roll (WIS) against the enemy's Stealth. Light, distance, being in
the corner of the eye, cover, lying flat, moving and shooting all modify the
roll, and winning it fills the meter. At SUSPICIOUS it goes to look; at
AWARE (100) it has spotted you: it knows where you are, shouts an alarm, and
fights. Unseen, the meter drains and the enemy is hunted at its last known
position rather than tracked by magic.

Noise does the rest: gunshots, footsteps, breaking glass, screams and shouts
spread out from where they happen, are muffled by walls, and pull listeners
toward them. Anyone who has *not* noticed an attacker gets no defense against
them, so a knife from the dark is a knife from the dark.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from .dice import check

if TYPE_CHECKING:
    from .creature import Creature
    from .sim import Sim
    from .world import Pos, World

AWARE = 100.0        # spotted: knows where you are
SUSPICIOUS = 30.0    # something's off: goes to look
FORGET_AFTER_MS = 20_000
PERIPHERAL = -4      # seen out of the corner of the eye (side arc)

# How far a noise carries (tiles) and what a listener makes of it.
NOISE = {
    "footstep": 4, "sneak": 1, "melee": 5, "struggle": 4, "thud": 6, "suppressed": 7,
    "scream": 12, "crash": 12, "glass": 12, "alarm": 16, "collapse": 30, "gunshot": 40,
}
DESCRIBE = {
    "footstep": "footsteps", "sneak": "a faint scuff", "melee": "a scuffle", "struggle": "a struggle",
    "thud": "a heavy thud", "suppressed": "a muffled shot", "scream": "a scream", "crash": "a crash",
    "glass": "glass breaking", "alarm": "a shout of alarm", "collapse": "something collapsing",
    "gunshot": "gunfire",
}
SUSPICION_FROM_NOISE = {"footstep": 25, "sneak": 15, "melee": 50, "struggle": 45, "suppressed": 45,
                        "gunshot": 70, "scream": 60, "crash": 50, "glass": 50, "thud": 40}


@dataclass
class Awareness:
    level: float = 0.0
    last_pos: "Pos | None" = None
    last_seen: float = -1e12   # world ms when last actually seen


def awareness(c: "Creature", other: "Creature") -> Awareness:
    aw = c.awareness.get(other.uid)
    if aw is None:
        aw = c.awareness[other.uid] = Awareness()
    return aw


def aware_of(c: "Creature", other: "Creature") -> bool:
    aw = c.awareness.get(other.uid)
    return aw is not None and aw.level >= AWARE


def make_all_aware(sim: "Sim") -> None:
    """Arena fights: everyone starts knowing where everyone is."""
    for c in sim.creatures:
        for o in sim.creatures:
            if o.team != c.team:
                c.awareness[o.uid] = Awareness(AWARE, o.pos, sim.time)


def state(c: "Creature") -> str:
    """'combat', 'searching' or 'calm', for the AI and the UI."""
    if any(a.level >= AWARE for a in c.awareness.values()):
        return "combat"
    if c.investigate is not None or c.alarmed:
        return "searching"
    return "calm"


def perception(c: "Creature") -> int:
    base = c.stat("WIS") + int(c.trait_sum("perception_bonus"))
    return base + {"calm": -1, "searching": 1, "combat": 2}[state(c)]


def stealth(sim: "Sim", c: "Creature") -> int:
    s = c.skill("stealth") + int(c.trait_sum("stealth_bonus"))
    if not c.sneaking:
        s -= 4
    if sim.time < c.noisy_until:
        s -= 6  # you just fired a gun
    return s


# -- light -------------------------------------------------------------------
def compute_light(world: "World", ambient: float) -> list:
    """Light level 0..1 per voxel: the scenario's ambient light, brightened
    near light sources (materials with "light": radius) that can see you."""
    light = np.full((world.depth, world.height, world.width), float(ambient))
    lit = np.array([m.get("light", 0) for m in world.mats])[world.fill]
    for z, y, x in zip(*np.nonzero(lit)):
        r = int(lit[z, y, x])
        src = (int(x), int(y), int(z))
        for ty in range(max(0, y - r), min(world.height, y + r + 1)):
            for tx in range(max(0, x - r), min(world.width, x + r + 1)):
                d = math.hypot(tx - x, ty - y)
                if d > r:
                    continue
                p = (tx, ty, int(z))
                if d < 1.5 or world.has_los(src, p):
                    light[z, ty, tx] = max(light[z, ty, tx], 1.0 - 0.75 * d / r)
    return light.tolist()


def light_at(sim: "Sim", pos: "Pos") -> float:
    x, y, z = pos
    return sim.light_map()[z][y][x]


def light_word(level: float) -> str:
    return "bright light" if level >= 0.7 else "dim light" if level >= 0.35 else "darkness"


def _light_mod(level: float) -> int:
    """Being well lit makes you conspicuous; darkness hides you."""
    return 2 if level >= 0.7 else -2 if level >= 0.35 else -6 if level >= 0.05 else -10


# -- sight ---------------------------------------------------------------------
def _in_view(sim: "Sim", observer: "Creature", target: "Creature") -> bool:
    from .combat import arc
    return arc(observer, target.pos) != "rear" and sim.world.has_los(observer.pos, target.pos)


def sight_mods(sim: "Sim", observer: "Creature", target: "Creature") -> int | None:
    """Modifier to spot target, or None if it can't be seen at all."""
    from .combat import arc, cover, range_penalty
    if not sim.world.has_los(observer.pos, target.pos):
        return None
    side = arc(observer, target.pos)
    if side == "rear":
        return None
    dist = sim.distance(observer, target)
    # a person is a big thing to notice: distance counts half as much as for a shot
    mods = range_penalty(dist) // 2 + (3 if dist <= 2 else 1 if dist <= 5 else 0)
    if side == "side":
        mods += PERIPHERAL
    mods += _light_mod(light_at(sim, target.pos) + (0.4 if observer.has_trait("night_vision") else 0.0))
    mods += cover(sim, observer.pos, target)[0]
    if target.has_status("prone"):
        mods -= 2
    if sim.time - target.last_moved < 1000:
        mods += 2
    return mods


def can_make_out(sim: "Sim", observer: "Creature", target: "Creature") -> bool:
    """Whether target is visible enough to see at all (used by the player's
    view): in line of sight, and not lost in the dark at a distance."""
    if not sim.world.has_los(observer.pos, target.pos):
        return False
    level = light_at(sim, target.pos) + (0.4 if observer.has_trait("night_vision") else 0.0)
    return level >= 0.05 or sim.distance(observer, target) <= 2


def perceive(sim: "Sim", c: "Creature") -> None:
    """c looks around at the start of its turn."""
    if not c.conscious or c.controller == "player":
        return
    now = sim.time
    for t in sim.creatures:
        if t is c or t.dead and t.team != c.team:
            continue
        if t.team == c.team:
            if not t.active and t.uid not in c.known_bodies:
                mods = sight_mods(sim, c, t)  # (already includes -2 for lying down)
                if mods is not None and check(sim.rng, perception(c) + mods - 1).success:
                    _found_body(sim, c, t)
            continue
        if not t.active:
            continue
        aw = awareness(c, t)
        if aw.level >= AWARE:  # already tracking them: any look in their direction keeps it fresh
            if _in_view(sim, c, t):
                aw.last_pos, aw.last_seen = t.pos, now
            continue
        mods = sight_mods(sim, c, t)
        if mods is None:
            continue
        obs = check(sim.rng, perception(c) + mods)
        if not obs.success:
            continue
        hidden = check(sim.rng, stealth(sim, t))
        if hidden.success and hidden.margin > obs.margin:
            gain = 10  # a glimpse of something
        else:
            gain = 25 + 20 * max(0, obs.margin - (hidden.margin if hidden.success else -1))
            aw.last_seen = now
        aw.level = min(AWARE, aw.level + gain)
        aw.last_pos = t.pos
        if aw.level >= AWARE:
            spotted(sim, c, t)
        elif aw.level >= SUSPICIOUS:
            c.investigate, c.search_turns = t.pos, 4


def spotted(sim: "Sim", c: "Creature", t: "Creature") -> None:
    aw = awareness(c, t)
    aw.level, aw.last_pos, aw.last_seen = AWARE, t.pos, sim.time
    c.alarmed = True
    sim.log(f"{c.name} spots {t.name}!")
    if c.grappled_by is None:  # a chokehold keeps you quiet
        emit_noise(sim, c, c.pos, "alarm", about=t)


def notice_attacker(sim: "Sim", c: "Creature", attacker: "Creature") -> None:
    """Being attacked tells you exactly where the attacker is."""
    if c.conscious and not c.dead and attacker.team != c.team and not aware_of(c, attacker):
        aw = awareness(c, attacker)
        aw.level, aw.last_pos, aw.last_seen = AWARE, attacker.pos, sim.time
        c.alarmed = True
        if c.controller != "player" and c.grappled_by is None:
            emit_noise(sim, c, c.pos, "alarm", about=attacker)


def _found_body(sim: "Sim", c: "Creature", body: "Creature") -> None:
    c.known_bodies.add(body.uid)
    if c.alarmed and state(c) == "combat":
        return
    sim.log(f"{c.name} finds {body.name}{' dead' if body.dead else ' down'} and raises the alarm!")
    c.alarmed = True
    c.investigate, c.search_turns = body.pos, 6
    if c.grappled_by is None:
        emit_noise(sim, c, c.pos, "alarm")


def decay(sim: "Sim", c: "Creature", seconds: float = 1.0) -> None:
    """Awareness fades while an enemy stays out of sight."""
    for aw in c.awareness.values():
        unseen = sim.time - aw.last_seen
        if aw.level >= AWARE:
            if unseen > FORGET_AFTER_MS:
                aw.level = SUSPICIOUS + 20  # lost them: back to searching
        elif aw.level > 0 and unseen > 2000:
            aw.level = max(0.0, aw.level - 4 * seconds)


# -- noise ----------------------------------------------------------------------
def direction_word(frm: "Pos", to: "Pos") -> str:
    dx, dy, dz = to[0] - frm[0], to[1] - frm[1], to[2] - frm[2]
    if dz > 0:
        return "from above"
    if dz < 0:
        return "from below"
    if abs(dx) < 2 and abs(dy) < 2:
        return "right next to you"
    ns = "north" if dy < -abs(dx) / 2 else "south" if dy > abs(dx) / 2 else ""
    ew = "west" if dx < -abs(dy) / 2 else "east" if dx > abs(dy) / 2 else ""
    return f"to the {ns}{'-' if ns and ew else ''}{ew}"


def emit_noise(sim: "Sim", source: "Creature | None", pos: "Pos", kind: str,
               loudness: float | None = None, about: "Creature | None" = None) -> None:
    """A sound happens at pos. Everyone in earshot may hear it; walls halve
    how far it carries."""
    loud = NOISE.get(kind, 5) if loudness is None else loudness
    for c in sim.creatures:
        if c is source or not c.conscious:
            continue
        d = sim.distance_pos(c.pos, pos)
        if d > loud:
            continue
        reach = loud if d <= loud / 2 or sim.world.has_los(c.pos, pos) else loud / 2
        if d > reach:
            continue
        if not check(sim.rng, perception(c) + 2 + int(reach - d) // 2).success:
            continue
        _hear(sim, c, source, pos, kind, about)


def _hear(sim, c, source, pos, kind, about) -> None:
    if c.controller == "player":
        if source is not None and can_make_out(sim, c, source):
            return  # you can see who made it; the log already shows what they did
        if kind not in ("footstep", "sneak") and (source is None or source.team != c.team or kind == "alarm"):
            sim.log(f"You hear {DESCRIBE.get(kind, 'something')} {direction_word(c.pos, pos)}.",
                    private_to=c.uid)
        return
    friendly = source is not None and source.team == c.team
    if kind == "alarm" and friendly:
        c.alarmed = True
        if about is not None:  # "over there!": they know where the intruder was
            aw = awareness(c, about)
            aw.level, aw.last_pos = max(aw.level, AWARE), about.pos
            aw.last_seen = max(aw.last_seen, sim.time - 1)
        else:
            c.investigate, c.search_turns = pos, 6
        return
    if friendly:
        if kind in ("scream", "gunshot", "struggle") and state(c) != "combat":
            c.investigate, c.search_turns = pos, 4
        return
    if source is not None:
        aw = awareness(c, source)
        if aw.level >= AWARE:
            aw.last_pos = pos  # heard where they are now
            return
        aw.level = min(AWARE - 10, aw.level + SUSPICION_FROM_NOISE.get(kind, 30))
        aw.last_pos = pos
    if state(c) != "combat":
        c.investigate, c.search_turns = pos, 4
