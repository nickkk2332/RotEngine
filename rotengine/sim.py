"""The simulation: time scheduling, the world tick, and glue between systems.

Time is continuous (milliseconds) rather than turn-based. Every action has a
cost, and a creature acts again when its cost has elapsed. That is how speed,
slow reloads and fast characters fall out without special cases. A global
1-second tick handles bleeding, status expiry and "every second" hooks.

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
from .creature import Creature
from .dice import Dice, check
from .world import DIRS8, Pos, World

TICK_MS = 1000


class Sim:
    def __init__(self, content: Content, world: World, seed: int | None = None,
                 echo: Callable[[str], None] | None = None):
        self.content = content
        self.world = world
        self.rng = random.Random(seed)
        self.time = 0
        self.creatures: list[Creature] = []
        self.lines: list[str] = []
        self.echo = echo
        self._queue: list[tuple[int, int, int]] = []  # (time, seq, uid); uid -1 = world tick
        self._seq = itertools.count()
        self._by_uid: dict[int, Creature] = {}
        self._terrain_dirty = False
        heapq.heappush(self._queue, (TICK_MS, next(self._seq), -1))

    # -- bookkeeping -------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"[{self.time / 1000:6.1f}s] {msg}"
        self.lines.append(line)
        if self.echo:
            self.echo(line)

    def spawn(self, template_id: str, team: str, pos: Pos, name: str | None = None) -> Creature:
        c = Creature(len(self._by_uid), self.content.get("creature", template_id), self.content,
                     team, pos, name)
        self.creatures.append(c)
        self._by_uid[c.uid] = c
        self._schedule(c, self.time + self.rng.randint(0, 300))
        return c

    def _schedule(self, c: Creature, at: int) -> None:
        c.next_time = at
        heapq.heappush(self._queue, (at, next(self._seq), c.uid))

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

    def creature_at(self, pos: Pos) -> Creature | None:
        for c in self.creatures:
            if c.pos == pos and c.active:
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
        c.pos = pos
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
            below = self.creature_at((x, y, z - 1))
            if below is not None:
                self.log(f"Debris crashes down on {below.name}.")
                combat.deal_damage(self, below, Dice(3, 6).roll(self.rng), "crush", knockback_ok=False)
        for c in self.creatures:
            if not c.dead:
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
        while self._queue and len(self.active_teams()) > 1:
            at, _, uid = heapq.heappop(self._queue)
            if at > max_ms:
                return None
            self.time = at
            if uid == -1:
                self._tick()
                heapq.heappush(self._queue, (at + TICK_MS, next(self._seq), -1))
            else:
                c = self._by_uid[uid]
                if c.dead or c.next_time != at:
                    continue
                self._schedule(c, at + max(50, self._act(c)))
            if self._terrain_dirty:
                self._settle()
        teams = self.active_teams()
        return next(iter(teams)) if len(teams) == 1 else None

    def _act(self, c: Creature) -> int:
        if not c.can_act:
            return 500
        if c.hp <= 0:
            below = int(-c.hp // c.max_hp)
            if not check(self.rng, c.stat("CON") - below).success:
                combat.knock_out(self, c)
                return 1000
        cost = ai.take_turn(self, c)
        c.shock = 0
        return cost

    def _tick(self) -> None:
        second = self.time // TICK_MS
        for c in self.creatures:
            if c.dead:
                continue
            for sid, until in list(c.statuses.items()):
                if until is not None and until <= self.time:
                    del c.statuses[sid]
            if c.body.bleed_rate > 0:
                c.body.hp -= c.body.bleed_rate
                combat.check_hp_thresholds(self, c)
                if c.dead:
                    self.log(f"  ({c.name} bled out.)")
                    continue
                if second % 10 == 0:
                    r = check(self.rng, c.stat("CON"))
                    if r.success:
                        c.body.bleed_rate = 0.0 if r.critical else c.body.bleed_rate / 2
                    if c.body.bleed_rate < 0.02:
                        c.body.bleed_rate = 0.0
            if c.has_status("unconscious") and c.hp > 0 and second % 5 == 0:
                if check(self.rng, c.stat("CON")).success:
                    del c.statuses["unconscious"]
                    self.log(f"{c.name} comes to.")
            c.stamina = min(c.max_stamina, c.stamina + 0.1)
            self.fire_hooks(c, "on_second")
