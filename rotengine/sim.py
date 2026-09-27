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
from contextlib import contextmanager
from typing import Callable, Iterator

from . import actions, ai, combat, effects, perception, physics
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
        # per line: (positions involved, uid it's private to or None). The UI
        # shows the player only what happened where they could see it.
        self.line_meta: list[tuple[tuple, int | None]] = []
        self._focus: list[Pos] = []
        self.echo = echo
        self.ambient_light = 1.0
        self.start_aware = True  # new arrivals know where their enemies are (arena fights)
        self._light: tuple[int, float, list] | None = None
        self.fighting = True  # False during the aftermath: nobody left to fight
        self._queue: list[tuple[int, int, int]] = []  # (time, seq, uid); uid -1 = world tick
        self._seq = itertools.count()
        self._by_uid: dict[int, Creature] = {}
        self._terrain_dirty = False
        self.path_failures: dict[tuple, int] = {}  # AI memo of recently failed searches
        self.awaiting: Creature | None = None  # player-controlled creature whose turn it is
        self.fields = physics.Fields(self)     # fire and gas
        self._events: dict[int, Callable[[], None]] = {}  # timed events (fuses), by negative id
        self._event_ids = itertools.count(2)
        heapq.heappush(self._queue, (TICK_MS, next(self._seq), -1))

    # -- bookkeeping -------------------------------------------------------
    def log(self, msg: str, private_to: int | None = None) -> None:
        line = f"[{self.time / 1000:6.2f}s] {msg}"
        self.lines.append(line)
        self.line_meta.append((tuple(self._focus), private_to))
        if self.echo and private_to is None:
            self.echo(line)

    @contextmanager
    def focus(self, *positions: Pos) -> Iterator[None]:
        """Tag log lines written inside this block with where they happen."""
        self._focus.extend(positions)
        try:
            yield
        finally:
            del self._focus[len(self._focus) - len(positions):]

    def light_map(self) -> list:
        key = (self.world.version, self.ambient_light)
        if self._light is None or self._light[:2] != key:
            self._light = (*key, perception.compute_light(self.world, self.ambient_light))
        return self._light[2]

    def spawn(self, template_id: str, team: str, pos: Pos, name: str | None = None) -> Creature:
        c = Creature(len(self._by_uid), self.content.get("creature", template_id), self.content,
                     team, pos, name)
        if self.start_aware:
            for o in self.creatures:
                if o.team != c.team:
                    c.awareness[o.uid] = perception.Awareness(perception.AWARE, o.pos, self.time)
                    o.awareness[c.uid] = perception.Awareness(perception.AWARE, c.pos, self.time)
        self.creatures.append(c)
        self._by_uid[c.uid] = c
        self._schedule(c, self.time + int(self.rng.randint(0, 300) / c.tempo))
        return c

    def _schedule(self, c: Creature, at: int) -> None:
        c.next_time = at
        heapq.heappush(self._queue, (at, next(self._seq), c.uid))

    def schedule_event(self, at: float, fn: Callable[[], None]) -> None:
        """Run fn at world time `at` (a grenade's fuse, say)."""
        eid = -next(self._event_ids)
        self._events[eid] = fn
        heapq.heappush(self._queue, (int(at), next(self._seq), eid))

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
                if sid == "unconscious" and not c.dead:
                    with self.focus(c.pos):
                        self.log(f"{c.name} comes to.")

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

    def is_free(self, pos: Pos, ignore: Creature | None = None) -> bool:
        """Standable, and no living body (standing or down) already there.
        Corpses can be stood on."""
        return self.world.standable(pos) and not any(
            o.pos == pos and not o.dead and o is not ignore for o in self.creatures)

    def make_room(self, c: Creature) -> None:
        """If c shares its tile with another living body (it woke up under
        someone, or landed on them), shift it to the nearest free tile."""
        if c.dead or not any(o is not c and not o.dead and o.pos == c.pos for o in self.creatures):
            return
        x, y, z = c.pos
        for r in (1, 2, 3):
            ring = [(x + dx, y + dy, z) for dx in range(-r, r + 1) for dy in range(-r, r + 1)
                    if max(abs(dx), abs(dy)) == r]
            free = [p for p in ring if self.is_free(p, ignore=c)]
            if free:
                c.pos = self.rng.choice(free)
                return

    def enemies_of(self, c: Creature) -> list[Creature]:
        return [o for o in self.creatures if o.team != c.team and o.active]

    def enemies_within(self, c: Creature, radius: float) -> list[Creature]:
        return [o for o in self.enemies_of(c) if self.distance(c, o) <= radius]

    def spot_near(self, target: Creature, behind_from: Pos | None = None) -> Pos | None:
        x, y, z = target.pos
        spots = [(x + dx, y + dy, z) for dx, dy in DIRS8]
        spots = [p for p in spots if self.is_free(p)]
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
        perception.emit_noise(self, None, c.pos, "thud")
        dmg = Dice(2 * min(levels, 10), 6).roll(self.rng)
        under = next((o for o in self.creatures if o is not c and not o.dead and o.pos == c.pos), None)
        if under is not None:  # landing on someone: they break the fall, painfully
            self.log(f"{c.name} lands on {under.name}!")
            combat.deal_damage(self, under, dmg // 2, "crush", knockback_ok=False)
            under.add_status("prone", None)
            dmg -= dmg // 2
        combat.deal_damage(self, c, dmg, "crush", knockback_ok=False)
        c.add_status("prone", None)
        self.make_room(c)

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
            self.log(f"{broken} section{'s' if broken != 1 else ''} of terrain {'are' if broken != 1 else 'is'} torn apart.")
            self.terrain_changed()

    def _settle(self) -> None:
        self._terrain_dirty = False
        collapsed = self.world.settle()
        if collapsed:
            self.log(f"The structure gives way: {len(collapsed)} sections collapse!")
            perception.emit_noise(self, None, collapsed[0][1], "collapse")
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
        return self.winner()

    def winner(self) -> str | None:
        teams = self.active_teams()
        return next(iter(teams)) if len(teams) == 1 else None

    # -- interactive play --------------------------------------------------
    def advance(self, max_ms: int = 180_000) -> str:
        """Run the world until a player-controlled creature needs a decision.
        Returns "player" (see self.awaiting), "over" (one side left) or
        "timeout". A downed or stunned player is simply skipped past."""
        if self.awaiting is not None:
            return "player"
        stop = self._loop(lambda: len(self.active_teams()) > 1, max_ms, stop_for_player=True)
        if stop == "player":
            return "player"
        return "over" if len(self.active_teams()) <= 1 else "timeout"

    def player_act(self, own_ms: int) -> None:
        """The awaited player creature took an action costing own_ms of its
        own time. (Actions come from rotengine.actions, like the AI's.)"""
        c = self.awaiting
        if c is None:
            raise RuntimeError("no player turn is pending")
        self.awaiting = None
        c.shock = 0
        self._schedule(c, self.time + max(1, int(own_ms / c.tempo)))
        if self._terrain_dirty:
            self._settle()

    def run_aftermath(self, seconds: float) -> None:
        """Keep the clock running after the fight: the wounded bleed out, pass
        out or get patched up by whoever is still standing."""
        self.fighting = False
        self._loop(lambda: True, self.time + int(seconds * 1000))

    def _loop(self, keep_going: Callable[[], bool], until_ms: int,
              stop_for_player: bool = False) -> str:
        while self._queue and keep_going():
            at, seq, uid = self._queue[0]
            if at > until_ms:
                return "time"
            heapq.heappop(self._queue)
            self.time = at
            if uid == -1:
                self._tick()
                heapq.heappush(self._queue, (at + TICK_MS, next(self._seq), -1))
            elif uid < -1:
                fn = self._events.pop(uid, None)
                if fn is not None:
                    fn()
            else:
                c = self._by_uid[uid]
                if c.dead or c.next_time != at:
                    continue
                if stop_for_player and c.controller == "player":
                    with self.focus(c.pos):
                        own_ms = self._pre_act(c)
                    if own_ms is None:  # conscious and able: hand over to the player
                        heapq.heappush(self._queue, (at, seq, uid))
                        self.awaiting = c
                        return "player"
                else:
                    with self.focus(c.pos):
                        own_ms = self._act(c)
                self._schedule(c, at + max(1, int(own_ms / c.tempo)))
            if self._terrain_dirty:
                self._settle()
        return "done"

    def _act(self, c: Creature) -> int:
        cost = self._pre_act(c)
        if cost is not None:
            return cost
        cost = ai.take_turn(self, c)
        c.shock = 0
        return cost

    def _pre_act(self, c: Creature) -> int | None:
        """The start of any creature's turn, AI or player: statuses run out,
        and the stunned, the fading and the exhausted lose their turn.
        Returns the own-ms cost if the turn is used up, else None."""
        self.expire_statuses(c)
        actions.check_grapple(self, c)
        perception.perceive(self, c)
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
        return None

    def _tick(self) -> None:
        """One second of physiology for everyone."""
        second = self.time // TICK_MS
        if self.fields.active:
            self.fields.step()
        for c in self.creatures:
            if c.dead:
                continue
            with self.focus(c.pos):
                self._tick_one(c, second)

    def _tick_one(self, c: Creature, second: int) -> None:
        self.expire_statuses(c)
        if self.fields.active or c.has_status("on_fire"):
            self.fields.affect(c)
            if c.dead:
                return
        perception.decay(self, c)
        self._blood(c, second)
        if c.dead:
            return
        resting = not c.conscious or not self.fighting
        c.stamina = min(c.max_stamina, c.stamina + (0.5 if resting else 0.1) * c.tempo)
        if (c.statuses.get("unconscious", 0) is None and second % 5 == 0 and self._can_wake(c)
                and check(self.rng, c.stat("CON")).success):
            del c.statuses["unconscious"]  # (timed knockouts, like a choke, wear off by themselves)
            self.log(f"{c.name} comes to.")
            self.make_room(c)
        self.fire_hooks(c, "on_second")

    def _blood(self, c: Creature, second: int) -> None:
        b = c.body
        arrest = c.has_status("cardiac_arrest")
        b.blood = max(0.0, b.blood - b.total_bleed * (0.2 if arrest else 1.0))  # no pump, little pressure
        rate = 0.04 * (50 - b.blood) if b.blood < 50 else 0.0
        smothered = c.status_sum("hypoxia")  # can't breathe: a broken neck, a crushed windpipe
        if smothered:
            rate = max(rate, smothered)
        if arrest:
            rate = max(rate, CARDIAC_ARREST_HYPOXIA)
            if c.conscious and self.rng.random() < 0.15:
                combat.knock_out(self, c, "as the heart gives out")
        b.hypoxia += rate
        if b.hypoxia >= 100:
            cause = "bled out" if b.blood < 50 and not arrest else "brain death"
            for d in c.status_defs():
                cause = d.get("death_text", cause) if d.get("hypoxia") else cause
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
                and not c.has_status("cardiac_arrest") and not c.status_sum("hypoxia") and b.hypoxia < 50)
