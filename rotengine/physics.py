"""Fire, gas, explosions and throwing: the messy physical layer.

Fire and gas are numpy fields over the voxel grid, stepped once per second:
* Fire (0-10 per voxel) feeds on flammable walls and floors, eats their HP
  (burn a wooden floor out and whoever stands on it falls), spreads to
  flammable neighbours, lights up the dark, gives off smoke, and sets people
  alight.
* Gases (smoke, tear gas, ...) are JSON "gas" types with a concentration per
  voxel. They spread into open neighbouring voxels, rise through holes in
  floors, thin out over time, block sight when thick (smoke) and do things
  to people standing in them (tear gas).

Explosions combine blast (crushing, falling off with distance, with the
knockback that implies), fragments (piercing hits whose odds fall with
distance and cover), flash, gas and fire, and wreck terrain nearby.

Throwing uses the same projectile idea as shooting: a Throwing roll against
range, and a miss scatters. Throwing a *person* reuses knockback: they fly
until they hit something, and hitting something hurts.
"""
from __future__ import annotations

import functools
from typing import TYPE_CHECKING

import numpy as np

from . import combat, flight, perception, training
from .dice import Dice, check

if TYPE_CHECKING:
    from .creature import Creature, Item
    from .sim import Sim
    from .world import Pos

FIRE_MAX = 10.0
FIRE_DAMAGE_EVERY = 3.0   # 1d burn per this much fire intensity in your voxel
FIRE_SPREAD = 80.0        # chance per second to catch from a neighbour: heat / this x flammability
ON_FIRE_MS = 6000


# -- fields ------------------------------------------------------------------
class Fields:
    """Fire and gas state for one sim."""

    def __init__(self, sim: "Sim"):
        self.sim = sim
        w = sim.world
        shape = (w.depth, w.height, w.width)
        self.fire = np.zeros(shape, np.float32)
        self.gas: dict[str, np.ndarray] = {}
        self._flammable_cache: tuple[int, np.ndarray, np.ndarray] | None = None
        self.fire_light: np.ndarray | None = None

    @property
    def active(self) -> bool:
        return bool(self.fire.any()) or any(g.any() for g in self.gas.values())

    def add_gas(self, gas_id: str, pos: "Pos", amount: float, radius: int = 0) -> None:
        w = self.sim.world
        g = self.gas.setdefault(gas_id, np.zeros(self.fire.shape, np.float32))
        x, y, z = pos
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                p = (x + dx, y + dy, z)
                if w.in_bounds(p) and w.passable(p):
                    g[z, y + dy, x + dx] += amount / (1 + dx * dx + dy * dy)

    def ignite(self, pos: "Pos", intensity: float = 4.0) -> None:
        x, y, z = pos
        if self.sim.world.in_bounds(pos):
            self.fire[z, y, x] = max(self.fire[z, y, x], intensity)

    def burning(self, pos: "Pos") -> float:
        x, y, z = pos
        return float(self.fire[z, y, x])

    def concentration(self, gas_id: str, pos: "Pos") -> float:
        g = self.gas.get(gas_id)
        if g is None:
            return 0.0
        x, y, z = pos
        return float(g[z, y, x])

    # -- stepping ------------------------------------------------------------
    def _flammability(self) -> tuple[np.ndarray, np.ndarray]:
        w = self.sim.world
        if self._flammable_cache is None or self._flammable_cache[0] != w.version:
            flam = np.array([m.get("flammable", 0.0) for m in w.mats], np.float32)
            # a finished floor burns far less readily than raw timber
            floor_flam = np.array([m.get("floor_flammable", m.get("flammable", 0.0)) for m in w.mats], np.float32)
            self._flammable_cache = (w.version, flam[w.fill], floor_flam[w.floor])
        return self._flammable_cache[1], self._flammable_cache[2]

    def step(self) -> None:
        """One second of fire and gas."""
        if self.fire.any():
            self._step_fire()
        for gas_id in list(self.gas):
            self._step_gas(gas_id)
        self._update_fog()

    def _step_fire(self) -> None:
        sim, w, rng = self.sim, self.sim.world, self.sim.rng
        fill_f, floor_f = self._flammability()
        fuel = np.maximum(fill_f, floor_f)
        fire = self.fire
        # grow where there's fuel, die down where there isn't
        fire[:] = np.where(fuel > 0, np.minimum(FIRE_MAX, fire + 1.0 * fuel * (fire > 0)),
                           np.maximum(0.0, fire - 1.5))
        burning = np.argwhere(fire > 0)
        spread_to: list[tuple[int, int, int, float]] = []
        for z, y, x in burning:
            z, y, x = int(z), int(y), int(x)
            heat = float(fire[z, y, x])
            pos = (x, y, z)
            # consume what's burning
            if fill_f[z, y, x] > 0 and w.damage_fill(pos, int(heat) + w.fill_mat(pos).get("dr", 0)):
                sim.terrain_changed()
            if floor_f[z, y, x] > 0 and w.damage_floor(pos, int(heat // 2) + (w.floor_mat(pos) or {}).get("dr", 0)):
                sim.terrain_changed()
            # smoke rises from it
            self.add_gas("smoke", pos, heat * 0.6)
            if heat < 3:
                continue
            for dz, dy, dx in ((0, 0, 1), (0, 0, -1), (0, 1, 0), (0, -1, 0), (1, 0, 0),
                               (0, 1, 1), (0, 1, -1), (0, -1, 1), (0, -1, -1)):
                q = (x + dx, y + dy, z + dz)
                if not w.in_bounds(q):
                    continue
                qz, qy, qx = q[2], q[1], q[0]
                f = max(fill_f[qz, qy, qx], floor_f[qz, qy, qx])
                if f > 0 and fire[qz, qy, qx] == 0 and rng.random() < heat / FIRE_SPREAD * f:
                    spread_to.append((qx, qy, qz, 2.0))
        for x, y, z, heat in spread_to:
            fire[z, y, x] = max(fire[z, y, x], heat)
        self._update_fire_light()

    def _update_fire_light(self) -> None:
        """Firelight: bright on the flames, fading over 4 tiles."""
        fire = self.fire
        if not fire.any():
            self.fire_light = None
            return
        light = np.clip(fire / 6.0, 0, 1)
        out = light.copy()
        for r in range(1, 5):
            fade = max(0.0, 1.0 - r / 5)
            for dy, dx in ((r, 0), (-r, 0), (0, r), (0, -r), (r, r), (-r, -r), (r, -r), (-r, r)):
                shifted = np.zeros_like(light)
                ys = slice(max(0, dy), light.shape[1] + min(0, dy))
                yd = slice(max(0, -dy), light.shape[1] + min(0, -dy))
                xs = slice(max(0, dx), light.shape[2] + min(0, dx))
                xd = slice(max(0, -dx), light.shape[2] + min(0, -dx))
                shifted[:, ys, xs] = light[:, yd, xd]
                out = np.maximum(out, shifted * fade)
        self.fire_light = out

    def _step_gas(self, gas_id: str) -> None:
        spec = self.sim.content.get("gas", gas_id)
        g = self.gas[gas_id]
        w = self.sim.world
        open_ = ~w._solid[w.fill]
        g *= open_
        k = spec.get("spread", 0.15)
        for _ in range(2):
            total = np.zeros_like(g)
            count = np.zeros_like(g)
            for axis, shift in ((1, 1), (1, -1), (2, 1), (2, -1)):
                total += np.roll(g, shift, axis=axis)
                count += np.roll(open_, shift, axis=axis)
            avg = np.where(count > 0, total / np.maximum(count, 1), 0)
            g += k * (avg - g) * open_
            if spec.get("rises") and g.shape[0] > 1:
                # up through holes: anything above a voxel with no floor overhead
                hole = (w.floor[1:] == 0) & open_[1:]
                up = g[:-1] * 0.3 * hole
                g[:-1] -= up
                g[1:] += up
        g *= (1.0 - spec.get("decay", 0.05))
        g[g < 0.5] = 0.0
        if not g.any():
            del self.gas[gas_id]

    def _update_fog(self) -> None:
        w = self.sim.world
        fog = None
        for gas_id, g in self.gas.items():
            opacity = self.sim.content.get("gas", gas_id).get("opacity", 0.0)
            if opacity:
                fog = g * opacity if fog is None else fog + g * opacity
        w.fog = fog.tolist() if fog is not None else None

    # -- effects on creatures ------------------------------------------------------
    def affect(self, c: "Creature") -> None:
        """Once a second: fire and gas do things to whoever is standing in them."""
        sim = self.sim
        if c.dead:
            return
        heat = self.burning(c.pos)
        if heat > 0:
            dmg = Dice(max(1, int(heat // FIRE_DAMAGE_EVERY)), 6).roll(sim.rng)
            with sim.focus(c.pos):
                sim.log(f"{c.name} is in the flames!")
                combat.deal_damage(sim, c, dmg, "burn", knockback_ok=False)
                if not c.dead and not c.has_status("on_fire") and not check(sim.rng, 10 - int(heat // 3)).success:
                    sim.apply_status(c, "on_fire", ON_FIRE_MS)
                    sim.log(f"{c.name} catches fire!")
        if c.has_status("on_fire") and not c.dead:
            if c.has_status("prone") and check(sim.rng, 12).success:
                c.statuses.pop("on_fire", None)  # stop, drop and roll
                with sim.focus(c.pos):
                    sim.log(f"{c.name} rolls on the ground and puts the flames out.")
            else:
                with sim.focus(c.pos):
                    combat.deal_damage(sim, c, Dice(1, 6).roll(sim.rng), "burn", knockback_ok=False)
                self.ignite(c.pos, 2.0)
        for gas_id in self.gas:
            conc = self.concentration(gas_id, c.pos)
            spec = sim.content.get("gas", gas_id)
            status = spec.get("status")
            if status and conc >= spec.get("status_at", 20) and not c.has_trait(spec.get("immune_trait", "-")):
                sim.apply_status(c, status, 1500)


# -- explosions ------------------------------------------------------------------
def explode(sim: "Sim", pos: "Pos", spec: dict, source: "Creature | None" = None,
            name: str = "explosion") -> None:
    """Something goes off at pos. spec keys (all optional):
    damage (dice), radius, fragments {dice, type, count, radius}, terrain (dice
    or number, walls/floors within 1), flash {radius, stun_ms}, gas {id, amount,
    radius}, fire {radius, intensity}, noise (kind)."""
    world = sim.world
    with sim.focus(pos):
        sim.log(f"The {name} goes off!")
    radius = spec.get("radius", 0)
    blast = Dice.parse(spec["damage"]) if "damage" in spec else None
    frag = spec.get("fragments")
    flash = spec.get("flash")
    reach = max(radius, frag.get("radius", 0) if frag else 0, flash.get("radius", 0) if flash else 0)
    for c in list(sim.creatures):
        if c.dead:
            continue
        d = sim.distance_pos(c.pos, pos)
        if d > reach or not (d == 0 or world.has_los(pos, c.pos)):
            continue
        with sim.focus(pos, c.pos):
            if blast is not None and d <= radius:
                raw = int(blast.roll(sim.rng) / (1 + d))
                if raw > 0:
                    sim.log(f"  The blast hits {c.name}.")
                    # the blast pushes away from the centre (from a tile back if right on it)
                    origin = pos if d else (pos[0] - 1, pos[1], pos[2])
                    combat.deal_damage(sim, c, raw, "crush", source=source, origin=origin)
            if frag and d <= frag["radius"] and not c.dead:
                p_hit = min(0.6, 2.0 / (d + 1) ** 2) * (0.5 if c.has_status("prone") else 1.0)
                hits = sum(sim.rng.random() < p_hit for _ in range(frag.get("count", 10)))
                if hits:
                    sim.log(f"  {c.name} is hit by {hits} fragment{'s' * (hits > 1)}.")
                    fd = Dice.parse(frag["dice"])
                    for _ in range(hits):
                        if c.dead:
                            break
                        combat.deal_damage(sim, c, fd.roll(sim.rng), frag.get("type", "pierce"),
                                           source=source, knockback_ok=False)
            if flash and d <= flash["radius"] and c.conscious and combat.arc(c, pos) != "rear":
                if not check(sim.rng, c.stat("CON") - (4 if d <= 2 else 0)).success:
                    sim.log(f"  {c.name} is blinded and deafened.")
                    sim.apply_status(c, "stunned", flash.get("stun_ms", 3000))
                sim.apply_status(c, "dazzled", flash.get("dazzle_ms", 8000))
        if source is not None and not c.dead and d <= 2:
            perception.notice_attacker(sim, c, source)
    terrain = spec.get("terrain")
    if terrain:
        amount = (lambda: Dice.parse(terrain).roll(sim.rng)) if isinstance(terrain, str) else (lambda: int(terrain))
        sim.damage_terrain(pos, 1, amount, [0, 1])
        x, y, z = pos
        if z > 0 and world.damage_floor(pos, amount()):
            sim.terrain_changed()
    if "gas" in spec:
        g = spec["gas"]
        sim.fields.add_gas(g["id"], pos, g.get("amount", 60), g.get("radius", 1))
        sim.fields._update_fog()
    if "fire" in spec:
        f = spec["fire"]
        r = f.get("radius", 1)
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                p = (pos[0] + dx, pos[1] + dy, pos[2])
                if world.in_bounds(p) and world.passable(p):
                    sim.fields.ignite(p, f.get("intensity", 6))
        sim.fields._update_fire_light()
    perception.emit_noise(sim, source, pos, spec.get("noise", "explosion"))


def arm(sim: "Sim", item: "Item", fuse_ms: int, source: "Creature | None") -> None:
    """Start an explosive's fuse. It goes off wherever the item is by then:
    on the floor, in someone's hand, or in their pocket."""
    item.armed = True
    # a partial of a module-level function (not a closure), so a saved game can pickle it
    sim.schedule_event(sim.time + fuse_ms, functools.partial(_detonate, sim, item, source))


def _detonate(sim: "Sim", item: "Item", source: "Creature | None") -> None:
    pos = item_position(sim, item)
    if pos is None:
        return
    in_air = flight.flying(sim, item)
    if in_air is not None:
        in_air.done = True
        sim.flights[:] = [f for f in sim.flights if not f.done]
    sim.items[:] = [(p, i) for p, i in sim.items if i is not item]
    for c in sim.creatures:
        if item in c.carried:
            c.carried.remove(item)
        if c.wielded is item:
            c.wielded = None
    explode(sim, pos, item.data["explosive"], source, item.name)


def item_position(sim: "Sim", item: "Item") -> "Pos | None":
    for p, i in sim.items:
        if i is item:
            return p
    f = flight.flying(sim, item)
    if f is not None:
        return f.pos  # goes off in mid-air
    for c in sim.creatures:
        if item in c.carried or c.wielded is item:
            return c.pos
    return None


# -- throwing ---------------------------------------------------------------------
def throw_range(c: "Creature", weight: float) -> int:
    """How far c can throw something of this weight (tiles)."""
    st = c.stat("ST")
    return max(1, int(st * 1.5 / max(1.0, weight)))


def fling_distance(thrower: "Creature", body: "Creature") -> int:
    """How far a grabbed body flies. Humans barely manage a tile (a judo
    throw); the Hulk throws soldiers across the room."""
    return max(1, (thrower.stat("ST") - 2 * body.stat("ST")) // 4)


def throw_item(sim: "Sim", c: "Creature", item: "Item", target: "Pos") -> "Pos":
    """Throw an item at a tile. Returns where it's headed (it may not get
    there: walls and floors stop it on the way)."""
    world = sim.world
    dist = sim.distance_pos(c.pos, target)
    combat.face(c, target)
    skill = c.skill("throwing") - c.action_penalty("ranged") + combat.range_penalty(dist)
    roll = check(sim.rng, skill)
    training.practice(sim, c, "throwing", skill, roll.success)
    with sim.focus(c.pos, target):
        sim.log(f"{c.name} throws {item.the}{'' if roll.success else ' (a bad throw)'}.")
    land = target
    if not roll.success:
        scatter = min(4, 1 + (-roll.margin) // 3)
        land = (target[0] + sim.rng.randint(-scatter, scatter), target[1] + sim.rng.randint(-scatter, scatter),
                target[2])
    flight.launch_item(sim, c, item, land)  # it flies there (see flight.py)
    return land


def fling(sim: "Sim", c: "Creature", body: "Creature", direction: tuple[int, int]) -> None:
    """Throw someone you're holding. They fly like knockback: into walls,
    other people, through windows and off ledges."""
    from .actions import release
    release(sim, c)
    hurl(sim, c, body, direction)


def hurl(sim: "Sim", c: "Creature", body: "Creature", direction: tuple[int, int]) -> None:
    tiles = fling_distance(c, body)
    dx, dy = direction
    origin = (body.pos[0] - dx, body.pos[1] - dy, body.pos[2])
    with sim.focus(c.pos, body.pos):
        sim.log(f"{c.name} hurls {body.name}!")
        combat.knockback(sim, body, origin, tiles, on_land=functools.partial(_hurled_landing, sim, body, tiles))


def _hurled_landing(sim: "Sim", body: "Creature", tiles: int) -> None:
    """Coming down hard at the end of a hurl."""
    if not body.dead:
        with sim.focus(body.pos):
            combat.deal_damage(sim, body, Dice(max(1, tiles // 2), 6).roll(sim.rng), "crush", knockback_ok=False)
        body.add_status("prone", None)
    perception.emit_noise(sim, None, body.pos, "crash")
