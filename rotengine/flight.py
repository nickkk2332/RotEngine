"""Things in flight: thrown items and flung bodies travel through world time.

A body knocked back or hurled, and anything thrown, moves one tile at a time
on scheduled events instead of arriving instantly. Everything else keeps
happening meanwhile, so a speedster can hit a man, send him flying, run round
behind where he'll land and hit him again as he comes down.

* **Bodies** fly at 8 + 2 x (tiles) tiles per second: a one-tile shove takes
  about a tenth of a second, the Hulk's ten-tile hurl about a third. In the
  air you can't act and defend at -4 (the "airborne" status). Each tile is
  resolved as you reach it: walls you slam into (or through), people you
  crash into, windows, ledges.
* **Items** fly at 20 tiles per second along the throw's line. Windows
  shatter as they pass, walls and floors stop them, and whoever is standing
  where they come down gets hit. A grenade's fuse keeps burning in the air
  (see physics.item_position); a Molotov bursts where it lands.

Steps are scheduled as functools.partial of module-level functions, so a
saved game with something in the air pickles.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from . import perception
from .dice import Dice, check

if TYPE_CHECKING:
    from .creature import Creature, Item
    from .sim import Sim
    from .world import Pos

ITEM_SPEED = 20.0            # tiles per second
BODY_BASE_SPEED = 8.0        # + 2 per tile of launch
BODY_SPEED_PER_TILE = 2.0


@dataclass
class Flight:
    kind: str                            # "body" or "item"
    obj: object                          # the Creature or Item
    pos: "Pos"
    step_ms: int
    dx: int = 0                          # bodies: direction and tiles left
    dy: int = 0
    tiles: int = 0
    total: int = 0
    path: list = field(default_factory=list)  # items: the tiles still to cross
    thrower: "Creature | None" = None
    on_land: Callable | None = None
    done: bool = False


def active(sim: "Sim") -> list[Flight]:
    return [f for f in sim.flights if not f.done]


def flying(sim: "Sim", obj) -> Flight | None:
    return next((f for f in sim.flights if f.obj is obj and not f.done), None)


def _start(sim: "Sim", f: Flight) -> None:
    old = flying(sim, f.obj)
    if old is not None:
        old.done = True  # a new shove replaces the old one
    sim.flights.append(f)
    sim.flight_started = True
    sim.schedule_event(sim.time + f.step_ms, functools.partial(_step, sim, f))


def _step(sim: "Sim", f: Flight) -> None:
    if f.done:
        return
    keep_going = _body_step(sim, f) if f.kind == "body" else _item_step(sim, f)
    if keep_going:
        sim.schedule_event(sim.time + f.step_ms, functools.partial(_step, sim, f))
    else:
        f.done = True
        sim.flights[:] = [x for x in sim.flights if not x.done]
        if f.kind == "body":
            _body_land(sim, f)
        else:
            _item_land(sim, f)


# -- bodies ------------------------------------------------------------------------
def launch_body(sim: "Sim", target: "Creature", direction: tuple[int, int], tiles: int,
                on_land: Callable | None = None) -> None:
    """Send a creature (or a corpse) flying `tiles` tiles in `direction`."""
    if tiles <= 0:
        return
    dx, dy = direction
    speed = BODY_BASE_SPEED + BODY_SPEED_PER_TILE * tiles
    step_ms = max(15, int(1000 / speed))
    target.aim_target = None
    if not target.dead:
        target.add_status("airborne", sim.time + step_ms * tiles + 50)
    _start(sim, Flight("body", target, target.pos, step_ms, dx, dy, tiles, tiles, on_land=on_land))


def _body_step(sim: "Sim", f: Flight) -> bool:
    """Move the body one tile. Returns False when the flight is over."""
    from .combat import deal_damage
    target, world = f.obj, sim.world
    if f.tiles <= 0:
        return False
    x, y, z = target.pos
    nxt = (x + f.dx, y + f.dy, z)
    remaining = f.tiles
    f.tiles -= 1
    if not world.in_bounds(nxt):
        return False
    with sim.focus(target.pos, nxt):
        if world.fill_mat(nxt).get("solid"):
            mat = world.fill_mat(nxt)
            slam = Dice(remaining, 6).roll(sim.rng)
            broke = world.damage_fill(nxt, slam * 2)
            sim.log(f"  {target.name} {'smashes through' if broke else 'slams into'} the {mat['name']}!")
            perception.emit_noise(sim, None, nxt, "crash")
            deal_damage(sim, target, slam, "crush", knockback_ok=False)
            if not broke:
                return False
            sim.terrain_changed()
        blocker = sim.creature_at(nxt)
        if blocker is not None and blocker is not target:
            slam = Dice(remaining, 6).roll(sim.rng)
            sim.log(f"  {target.name} crashes into {blocker.name}!")
            deal_damage(sim, target, slam, "crush", knockback_ok=False)
            deal_damage(sim, blocker, slam, "crush", knockback_ok=False)
            if not blocker.has_status("prone"):
                blocker.add_status("prone", None)
            return False
    target.pos = f.pos = nxt
    return world.supported(nxt) and f.tiles > 0


def _body_land(sim: "Sim", f: Flight) -> None:
    target = f.obj
    target.statuses.pop("airborne", None)
    sim.make_room(target)
    if not target.dead and not target.has_status("prone"):
        if not check(sim.rng, target.stat("DEX") - (f.total - 1)).success:
            target.add_status("prone", None)
    if f.on_land is not None:
        f.on_land()
    sim.check_fall(target)


# -- items -------------------------------------------------------------------------
def launch_item(sim: "Sim", thrower: "Creature", item: "Item", land: "Pos") -> None:
    """Throw an item at a tile (the landing spot is already decided, scatter
    included). It flies there tile by tile."""
    path = list(sim.world.line(thrower.pos, land))
    step_ms = max(10, int(1000 / ITEM_SPEED))
    _start(sim, Flight("item", item, thrower.pos, step_ms, path=path, thrower=thrower))


def _item_step(sim: "Sim", f: Flight) -> bool:
    world = sim.world
    if not f.path:
        return False
    p = f.path.pop(0)
    if not world.in_bounds(p):
        return False
    if not world.passable(p):
        mat = world.fill_mat(p)
        if mat.get("transparent") and world.damage_fill(p, 10):  # through the window
            with sim.focus(p):
                sim.log(f"The {mat['name']} shatters!")
            sim.terrain_changed()
            perception.emit_noise(sim, None, p, "glass")
        else:
            return False
    slab = world.crossing(f.pos, p)
    if slab is not None and world.floor_mat(slab) is not None:
        return False
    f.pos = p
    return bool(f.path)


def _item_land(sim: "Sim", f: Flight) -> None:
    from .combat import attack_dice, deal_damage
    from .physics import explode
    item, world, c = f.obj, sim.world, f.thrower
    x, y, z = f.pos
    while z > 0 and not world.supported((x, y, z)):  # come to rest on a floor
        z -= 1
    land = (x, y, z)
    hit = sim.creature_at(land)
    if hit is not None and hit is not c and "thrown" in item.data:
        with sim.focus(land):
            dice = attack_dice(c, {"damage": item.data["thrown"]})
            sim.log(f"  {item.the[0].upper()}{item.the[1:]} hits {hit.name}.")
            deal_damage(sim, hit, dice.roll(sim.rng), item.data["thrown"]["type"], source=c)
    if "explosive" in item.data and item.data.get("throwable", {}).get("fuse_ms", 3000) == 0:
        explode(sim, land, item.data["explosive"], c, item.name)  # a Molotov bursts on impact
        return
    sim.drop(land, item)
    perception.emit_noise(sim, None, land, "thud")


def finish(sim: "Sim", max_ms: int = 5000) -> None:
    """Run the world until nothing is in the air (tests, and anything that
    needs a throw resolved before carrying on)."""
    end = sim.time + max_ms
    while active(sim) and sim.time < end:
        nxt = min((t for t, _, uid in sim._queue if uid < -1), default=None)
        if nxt is None:
            break
        sim._loop(lambda: True, nxt)
