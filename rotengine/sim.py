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

import functools
import heapq
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
ARREST_OXYGEN_DRAIN = 10.0     # % oxygen per second with no heartbeat: ~9 s of consciousness
BREATH_RECOVERY = 25.0         # % oxygen per second back once you can breathe again
OXYGEN_BLACKOUT = 10.0         # below this you're out


class Sim:
    def __init__(self, content: Content, world: World, seed: int | None = None,
                 echo: Callable[[str], None] | None = None, start_time: int = 0):
        self.content = content
        self.world = world
        self.rng = random.Random(seed)
        self.time = start_time  # a roguelike run's clock carries on from level to level
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
        self.healing = False     # bodies recover over time (roguelike mode; arena fights are too short)
        self.endless = False     # roguelike: the world runs on as long as a player lives
        self._light: tuple[int, float, list] | None = None
        self.fighting = True  # False during the aftermath: nobody left to fight
        self._queue: list[tuple[int, int, int]] = []  # (time, seq, uid); uid -1 = world tick
        self._seq_n = 0  # plain ints, not itertools.count (which doesn't pickle on newer Pythons)
        self._by_uid: dict[int, Creature] = {}
        self._terrain_dirty = False
        self.path_failures: dict[tuple, int] = {}  # AI memo of recently failed searches
        self.awaiting: Creature | None = None  # player-controlled creature whose turn it is
        self.fields = physics.Fields(self)     # fire and gas
        self._events: dict[int, Callable[[], None]] = {}  # timed events (fuses), by negative id
        self.flights: list = []           # things in the air (flight.py)
        self.timed_shots = True           # bullets take time to arrive (flight.launch_tracer)
        self.fx: list[tuple] = []         # (pos, kind) visual cues for the UI: hit, miss, block, crit
        self.fx_seq = 0
        self.flight_started = False       # set when something takes off (the UI animates it)
        self._event_n = 1
        heapq.heappush(self._queue, (start_time + TICK_MS, self._next_seq(), -1))

    def __setstate__(self, state: dict) -> None:
        # saves from before these existed
        state.setdefault("flights", [])
        state.setdefault("flight_started", False)
        state.setdefault("timed_shots", True)
        state.setdefault("fx", [])
        state.setdefault("fx_seq", 0)
        for old, new, start in (("_seq", "_seq_n", 0), ("_event_ids", "_event_n", 1)):
            if old in state:  # an itertools.count: carry on from where it got to
                state[new] = next(state.pop(old)) - (0 if new == "_seq_n" else 1)
            state.setdefault(new, start)
        self.__dict__.update(state)

    def _next_seq(self) -> int:
        self._seq_n += 1
        return self._seq_n

    # -- bookkeeping -------------------------------------------------------
    def cue(self, pos: Pos, kind: str) -> None:
        """A visual cue for whoever's watching (the UI flashes the tile)."""
        self.fx.append((pos, kind))
        self.fx_seq += 1  # how many cues ever: the UI reads by this, so trimming doesn't replay any
        if len(self.fx) > 200:
            del self.fx[:100]

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

    def adopt(self, c: Creature, pos: Pos) -> Creature:
        """Bring a creature from another level into this one (the player
        going downstairs): same body, gear and skills, fresh knowledge."""
        c.uid = len(self._by_uid)
        c.pos = c.post = pos
        c.awareness, c.known_bodies = {}, set()
        c.target = c.aim_target = c.investigate = None
        c.grappling = c.grappled_by = c.hold = None
        c.statuses.pop("grappled", None)
        c.statuses.pop("grappling", None)
        c.choked, c.defenses_in_window, c.defense_until = 0, 0, 0
        c.patrol, c.alarmed = [], False
        self.creatures.append(c)
        self._by_uid[c.uid] = c
        self._schedule(c, self.time + 1)
        for item in [c.wielded, *c.carried]:  # a fuse still burning comes along
            if item is not None and item.armed and getattr(item, "fuse_at", None) is not None:
                self.schedule_event(max(self.time + 1, item.fuse_at),
                                    functools.partial(physics._detonate, self, item, c))
        return c

    def _schedule(self, c: Creature, at: int) -> None:
        c.next_time = at
        heapq.heappush(self._queue, (at, self._next_seq(), c.uid))

    def schedule_event(self, at: float, fn: Callable[[], None]) -> None:
        """Run fn at world time `at` (a grenade's fuse, say)."""
        self._event_n += 1
        eid = -self._event_n
        self._events[eid] = fn
        heapq.heappush(self._queue, (int(at), self._next_seq(), eid))

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
                if sid == "unconscious" and not c.dead:
                    if not self._can_wake(c):
                        c.statuses[sid] = None  # the knock's worn off, but they're in no state to come round
                        continue
                    del c.statuses[sid]
                    with self.focus(c.pos):
                        self.log(f"{c.name} comes to.")
                    self.make_room(c)
                    continue
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

    def resting_place(self, pos: Pos) -> Pos:
        """Where something dropped at pos comes to rest: down through open air
        to the first floor, or on top of rubble or a wall stub."""
        x, y, z = pos
        while z > 0 and not self.world.supported((x, y, z)):
            if self.world.fill_mat((x, y, z - 1)).get("solid"):
                break
            z -= 1
        return (x, y, z)

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
        self.items[:] = [(self.resting_place(p), i) for p, i in self.items]  # the floor went: so do they

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
    def advance_for(self, ms: int) -> str:
        """Run the world forward at most `ms` (the UI's animation frames):
        "player" if it's the player's turn, "over" if the fight or run is
        over, else "time"."""
        if self.awaiting is not None:
            return "player"
        if self.endless:
            alive = lambda: any(c.controller == "player" and not c.dead for c in self.creatures)  # noqa: E731
        else:
            alive = lambda: len(self.active_teams()) > 1  # noqa: E731
        until = self.time + ms
        stop = self._loop(alive, until, stop_for_player=True)
        if stop == "player":
            return "player"
        if not alive():
            return "over"
        if stop == "time":
            self.time = until  # nothing happened in this slice: the clock still runs
        return "time"

    def advance(self, max_ms: int = 180_000, stop_on_flight: bool = False) -> str:
        """Run the world until a player-controlled creature needs a decision.
        Returns "player" (see self.awaiting), "over" (one side left) or
        "timeout". A downed or stunned player is simply skipped past."""
        if self.awaiting is not None:
            return "player"
        if not stop_on_flight:
            self.flight_started = False
        if self.endless:  # roguelike: nothing ends until you do
            alive = lambda: any(c.controller == "player" and not c.dead for c in self.creatures)  # noqa: E731
            stop = self._loop(alive, self.time + max_ms, stop_for_player=True, stop_on_flight=stop_on_flight)
            return stop if stop in ("player", "flight") else "over" if not alive() else "timeout"
        stop = self._loop(lambda: len(self.active_teams()) > 1, self.time + max_ms, stop_for_player=True,
                          stop_on_flight=stop_on_flight)
        if stop in ("player", "flight"):
            return stop
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
              stop_for_player: bool = False, stop_on_flight: bool = False) -> str:
        while self._queue and keep_going():
            if stop_on_flight and self.flight_started:
                self.flight_started = False
                return "flight"  # something took off: the UI wants to show it
            at, seq, uid = self._queue[0]
            if at > until_ms:
                return "time"
            heapq.heappop(self._queue)
            self.time = at
            if uid == -1:
                self._tick()
                heapq.heappush(self._queue, (at + TICK_MS, self._next_seq(), -1))
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
        if c.stamina <= -c.max_stamina / 2:
            combat.collapse(self, c, "from exhaustion")
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
        self._breathe(c)
        if c.dead:
            return
        self._blood(c, second)
        if c.dead:
            return
        resting = not c.conscious or not self.fighting
        if self.endless and not resting:  # roguelike: catching your breath between fights
            if c.controller == "player":
                resting = not any(perception.aware_of(o, c) for o in self.enemies_of(c))
            else:
                resting = perception.state(c) != "combat"
        if self.healing:
            for what in c.body.recover(1, c.stat("CON")):
                self.log(f"{c.name}'s {what}.", private_to=c.uid if c.controller == "player" else None)
        c.stamina = min(c.max_stamina, c.stamina + (0.5 if resting else 0.1) * c.tempo)
        if (c.statuses.get("unconscious", 0) is None and second % 5 == 0 and self._can_wake(c)
                and combat.stays_conscious(self, c)):
            del c.statuses["unconscious"]  # (timed knockouts, like a choke, wear off by themselves)
            self.log(f"{c.name} comes to.")
            self.make_room(c)
        self.fire_hooks(c, "on_second")

    def _breathe(self, c: Creature) -> None:
        """Oxygen out and in, once a second. No heartbeat: the brain's supply
        is gone in seconds. Not breathing (a broken neck, a crushed
        windpipe): you last as long as you can hold your breath (CON).
        Someone's hands on your throat: the choke itself drains you (see
        grapple.choke). Otherwise you get your breath back quickly. At 10%
        you black out; at 0 the brain starts to die."""
        b = c.body
        arrest = c.has_status("cardiac_arrest")
        not_breathing = c.status_sum("hypoxia")  # the status's brain-damage rate once the air is gone
        choked = c.airway_blocked_until > self.time
        if arrest:
            b.oxygen -= ARREST_OXYGEN_DRAIN
        elif not_breathing:
            b.oxygen -= c.apnea_rate
        elif not choked:
            b.oxygen = min(100.0, b.oxygen + BREATH_RECOVERY)
        b.oxygen = max(0.0, b.oxygen)
        if b.oxygen <= OXYGEN_BLACKOUT and c.conscious:
            combat.knock_out(self, c, "as the heart gives out" if arrest else "as the world goes dark")
        if b.oxygen <= 0 and not choked:
            b.hypoxia += CARDIAC_ARREST_HYPOXIA if arrest else not_breathing
        if b.hypoxia >= 100:
            self._brain_death(c)

    def _brain_death(self, c: Creature) -> None:
        b = c.body
        cause = "bled out" if b.blood < 50 and b.oxygen > 0 else "brain death"
        for d in c.status_defs():
            cause = d.get("death_text", cause) if d.get("hypoxia") else cause
        combat.kill(self, c, cause)

    def _blood(self, c: Creature, second: int) -> None:
        b = c.body
        arrest = c.has_status("cardiac_arrest")
        b.blood = max(0.0, b.blood - b.total_bleed * (0.2 if arrest else 1.0))  # no pump, little pressure
        if b.blood < 50:
            b.hypoxia += 0.04 * (50 - b.blood)
        if b.hypoxia >= 100:
            self._brain_death(c)
            return
        if c.conscious:
            if b.blood < 50:
                combat.knock_out(self, c, "from blood loss")
            elif second % 10 == 0 and b.blood < 70 and not combat.stays_conscious(self, c):
                combat.knock_out(self, c, "from blood loss")
            elif second % 10 == 5 and c.has_status("collapsed"):
                self._try_to_rise(c)
        if b.bleed_rate > 0 and second % 10 == 0:
            # arterial bleeding (severed limbs, cut throats) rarely clots on its own
            r = check(self.rng, c.stat("CON") - (4 if b.bleed_rate > 1.0 else 0))
            if r.success:
                b.bleed_rate = 0.0 if r.critical else b.bleed_rate / 2
            if b.bleed_rate < 0.02:
                b.bleed_rate = 0.0

    def _try_to_rise(self, c: Creature) -> None:
        """Collapsed: a WIS roll (minus pain) to get your legs back. Exhaustion
        passes once you've got some breath back."""
        if c.stamina <= 0:
            return
        if check(self.rng, c.stat("WIS") + int(c.trait_sum("pain_resist")) - c.pain()).success:
            del c.statuses["collapsed"]
            self.log(f"{c.name} gets their legs back under them.")

    def _can_wake(self, c: Creature) -> bool:
        """Coming round (a stays_conscious roll every 5 s) needs the things
        that put you under to have eased."""
        b = c.body
        return (c.hp > -combat.SHOCK_OUT * c.max_hp and b.blood >= 55 and b.oxygen >= 50
                and c.airway_blocked_until <= self.time
                and not c.has_status("cardiac_arrest") and not c.status_sum("hypoxia") and b.hypoxia < 50)
