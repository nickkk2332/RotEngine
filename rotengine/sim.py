"""The simulation: time scheduling, the world tick, and glue between systems.

Time is continuous (milliseconds of world time) rather than turn-based. Every
action has a cost in the actor's *own* time, which is divided by the actor's
tempo to get world time. A human with tempo 1 acts once per second of
fighting; a tempo-10 speedster gets ten actions, ten reaction windows and ten
times the recovery in that same second. A global 1-second tick handles the
body: bleeding, hypoxia, clotting, fainting and waking, stamina.

The simulation never touches the screen. The ASCII UI, the arena batch
runner and the tests all drive the same Sim, and a seed makes it fully
reproducible.
"""
from __future__ import annotations

import heapq
import itertools
import random
from typing import Callable

from . import ai, combat, effects
from .content import Content
from .creature import Creature, Item
from .dice import Dice, check
from .world import DIRS8, Pos, World

TICK_MS = 1000
CARDIAC_ARREST_HYPOXIA = 0.5   # brain damage per second with no circulation (~3.5 min to death)


class Sim:
    def __init__(self, content: Content, world: World, seed: int | None = None,
                 echo: Callable[[str], None] | None = None):
        self.content = content
        self.world = world
        self.rng = random.Random(seed)
        self.time = 0
        self.creatures: list[Creature] = []
        self.items: list[tuple[Pos, Item]] = []  # things lying on the ground
        self.lines: list[str] = []
        self.echo = echo
        self.fighting = True  # False during the aftermath: nobody left to fight
        self._queue: list[tuple[int, int, int]] = []  # (time, seq, uid); uid -1 = world tick
        self._seq = itertools.count()
        self._by_uid: dict[int, Creature] = {}
        self._terrain_dirty = False
        heapq.heappush(self._queue, (TICK_MS, next(self._seq), -1))

    # -- bookkeeping -------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"[{self.time / 1000:6.2f}s] {msg}"
        self.lines.append(line)
        if self.echo:
            self.echo(line)

    def spawn(self, template_id: str, team: str, pos: Pos, name: str | None = None) -> Creature:
        c = Creature(len(self._by_uid), self.content.get("creature", template_id), self.content,
                     team, pos, name)
        self.creatures.append(c)
        self._by_uid[c.uid] = c
        self._schedule(c, self.time + int(self.rng.randint(0, 300) / c.tempo))
        return c

    def _schedule(self, c: Creature, at: int) -> None:
        c.next_time = at
        heapq.heappush(self._queue, (at, next(self._seq), c.uid))

    def apply_status(self, c: Creature, status_id: str, duration_ms: float) -> None:
        """Timed status. 'subjective' statuses (stun, agony) run on the
        creature's own clock, so a speedster shakes them off faster."""
        if self.content.get("status", status_id).get("subjective"):
            duration_ms /= c.tempo
        c.add_status(status_id, self.time + duration_ms)

    def expire_statuses(self, c: Creature) -> None:
        """Drop timed statuses that have run out. Called whenever a creature
        acts or defends, so a 250 ms stun really is 250 ms."""
        for sid, until in list(c.statuses.items()):
            if until is not None and until <= self.time:
                del c.statuses[sid]

    def drop(self, pos: Pos, item: Item) -> None:
        self.items.append((pos, item))

    # -- spatial queries -------------------------------------------------------
    @staticmethod
    def distance_pos(a: Pos, b: Pos) -> int:
        return max(abs(a[0] - b[0]), abs(a[1] - b[1])) + 2 * abs(a[2] - b[2])

    def distance(self, a: Creature, b: Creature) -> int:
        return self.distance_pos(a.pos, b.pos)

    def in_melee_reach(self, a: Pos, b: Pos, reach: int = 1) -> bool:
        """Same level within reach, or one level apart across a staircase."""
        dxy = max(abs(a[0] - b[0]), abs(a[1] - b[1]))
        dz = abs(a[2] - b[2])
        if dz == 0:
            return dxy <= reach
        return dz == 1 and dxy <= 1 and (self.world.fill_mat(a).get("climbable", False)
                                          or self.world.fill_mat(b).get("climbable", False))

    def creature_at(self, pos: Pos, include_down: bool = False) -> Creature | None:
        """The creature occupying pos. Downed bodies don't block movement
        but can still catch a stray bullet (include_down)."""
        for c in self.creatures:
            if c.pos == pos and (c.active or include_down and not c.dead):
                return c
        return None

    def enemies_of(self, c: Creature) -> list[Creature]:
        return [o for o in self.creatures if o.team != c.team and o.active]

    def enemies_within(self, c: Creature, radius: float) -> list[Creature]:
        return [o for o in self.enemies_of(c) if self.distance(c, o) <= radius]

    def spot_near(self, target: Creature, behind_from: Pos | None = None) -> Pos | None:
        x, y, z = target.pos
        spots = [(x + dx, y + dy, z) for dx, dy in DIRS8]
        spots = [p for p in spots if self.world.standable(p) and self.creature_at(p) is None]
        if not spots:
            return None
        if behind_from is None:
            return self.rng.choice(spots)
        return max(spots, key=lambda p: (self.distance_pos(p, behind_from), self.rng.random()))

    # -- movement and falling ------------------------------------------------
    def move_creature(self, c: Creature, pos: Pos) -> None:
        combat.face(c, pos)
        c.pos = pos
        c.aim_target = None
        self.check_fall(c)

    def check_fall(self, c: Creature) -> None:
        x, y, z = c.pos
        levels = 0
        while z > 0 and not self.world.supported((x, y, z)):
            z -= 1
            levels += 1
            if self.world.fill_mat((x, y, z)).get("solid"):
                z += 1  # landed on top of rubble / a wall stub
                levels -= 1
                break
        if levels <= 0:
            return
        c.pos = (x, y, z)
        self.log(f"{c.name} falls {levels} storey{'s' if levels > 1 else ''}!")
        combat.deal_damage(self, c, Dice(2 * min(levels, 10), 6).roll(self.rng), "crush",
                           knockback_ok=False)
        c.add_status("prone", None)

    # -- terrain ---------------------------------------------------------------
    def terrain_changed(self) -> None:
        self._terrain_dirty = True

    def damage_terrain(self, center: Pos, radius: int, amount: Callable[[], int],
                       z_offsets: list[int]) -> None:
        cx, cy, cz = center
        broken = 0
        for dz in z_offsets:
            for y in range(cy - radius, cy + radius + 1):
                for x in range(cx - radius, cx + radius + 1):
                    p = (x, y, cz + dz)
                    if not self.world.in_bounds(p):
                        continue
                    if self.world.damage_fill(p, amount()):
                        broken += 1
                    if dz > 0 and self.world.damage_floor(p, amount()):
                        broken += 1
        if broken:
            self.log(f"{broken} sections of terrain are torn apart.")
            self.terrain_changed()

    def _settle(self) -> None:
        self._terrain_dirty = False
        collapsed = self.world.settle()
        if collapsed:
            self.log(f"The structure gives way: {len(collapsed)} sections collapse!")
        for kind, (x, y, z) in collapsed:
            if kind != "floor":
                continue
            below = self.creature_at((x, y, z - 1), include_down=True)
            if below is not None:
                self.log(f"Debris crashes down on {below.name}.")
                combat.deal_damage(self, below, Dice(3, 6).roll(self.rng), "crush", knockback_ok=False)
        for c in self.creatures:
            self.check_fall(c)

    # -- hooks -------------------------------------------------------------
    def fire_hooks(self, c: Creature, hook: str, target: Creature | None = None,
                   vars: dict | None = None) -> None:
        for d in c.traits + c.status_defs():
            fx = d.get("hooks", {}).get(hook)
            if fx:
                effects.run(fx, effects.Ctx(self, c, target, dict(vars or {})))

    # -- main loop ---------------------------------------------------------
    def active_teams(self) -> set[str]:
        return {c.team for c in self.creatures if c.active}

    def run(self, max_ms: int = 180_000) -> str | None:
        """Run until one team is left standing. Returns the winner, or None for
        a timeout / mutual wipe."""
        self._loop(lambda: len(self.active_teams()) > 1, max_ms)
        teams = self.active_teams()
        return next(iter(teams)) if len(teams) == 1 else None

    def run_aftermath(self, seconds: float) -> None:
        """Keep the clock running after the fight: the wounded bleed out, pass
        out or get patched up by whoever is still standing."""
        self.fighting = False
        self._loop(lambda: True, self.time + int(seconds * 1000))

    def _loop(self, keep_going: Callable[[], bool], until_ms: int) -> None:
        while self._queue and keep_going():
            at, _, uid = self._queue[0]
            if at > until_ms:
                return
            heapq.heappop(self._queue)
            self.time = at
            if uid == -1:
                self._tick()
                heapq.heappush(self._queue, (at + TICK_MS, next(self._seq), -1))
            else:
                c = self._by_uid[uid]
                if c.dead or c.next_time != at:
                    continue
                own_ms = self._act(c)
                self._schedule(c, at + max(1, int(own_ms / c.tempo)))
            if self._terrain_dirty:
                self._settle()

    def _act(self, c: Creature) -> int:
        self.expire_statuses(c)
        if not c.can_act:
            ends = [t for sid, t in c.statuses.items()
                    if t is not None and self.content.get("status", sid).get("prevents_action")]
            if ends and c.conscious:
                return max(1, int((min(ends) - self.time) * c.tempo))
            return 1000  # out cold: waking is decided on the world tick
        if c.hp <= 0:
            below = int(-c.hp // c.max_hp)
            if not check(self.rng, c.stat("CON") - below).success:
                combat.knock_out(self, c, "from the trauma")
                return 1000
        if c.stamina <= -c.max_stamina / 2:
            combat.knock_out(self, c, "from exhaustion")
            return 1000
        cost = ai.take_turn(self, c)
        c.shock = 0
        return cost

    def _tick(self) -> None:
        """One second of physiology for everyone."""
        second = self.time // TICK_MS
        for c in self.creatures:
            if c.dead:
                continue
            self.expire_statuses(c)
            self._blood(c, second)
            if c.dead:
                continue
            resting = not c.conscious or not self.fighting
            c.stamina = min(c.max_stamina, c.stamina + (0.5 if resting else 0.1) * c.tempo)
            if c.has_status("unconscious") and second % 5 == 0 and self._can_wake(c):
                if check(self.rng, c.stat("CON")).success:
                    del c.statuses["unconscious"]
                    self.log(f"{c.name} comes to.")
            self.fire_hooks(c, "on_second")

    def _blood(self, c: Creature, second: int) -> None:
        b = c.body
        arrest = c.has_status("cardiac_arrest")
        b.blood = max(0.0, b.blood - b.total_bleed * (0.2 if arrest else 1.0))  # no pump, little pressure
        rate = 0.04 * (50 - b.blood) if b.blood < 50 else 0.0
        if arrest:
            rate = max(rate, CARDIAC_ARREST_HYPOXIA)
            if c.conscious and self.rng.random() < 0.15:
                combat.knock_out(self, c, "as the heart gives out")
        b.hypoxia += rate
        if b.hypoxia >= 100:
            cause = "bled out" if b.blood < 50 and not arrest else "brain death"
            combat.kill(self, c, cause)
            return
        if c.conscious:
            if b.blood < 60:
                combat.knock_out(self, c, "from blood loss")
            elif b.blood < 70 and second % 10 == 0 and not check(self.rng, c.stat("CON")).success:
                combat.knock_out(self, c, "from blood loss")
        if b.bleed_rate > 0 and second % 10 == 0:
            # arterial bleeding (severed limbs, cut throats) rarely clots on its own
            r = check(self.rng, c.stat("CON") - (4 if b.bleed_rate > 1.0 else 0))
            if r.success:
                b.bleed_rate = 0.0 if r.critical else b.bleed_rate / 2
            if b.bleed_rate < 0.02:
                b.bleed_rate = 0.0

    def _can_wake(self, c: Creature) -> bool:
        b = c.body
        return (c.hp > 0 and b.blood >= 60 and c.stamina > 0
                and not c.has_status("cardiac_arrest") and b.hypoxia < 50)
