"""Plain-text rendering of one z-level. The real UI will use python-tcod, but
it will read the same World and Sim."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sim import Sim


def render_level(sim: "Sim", z: int) -> str:
    world = sim.world
    bodies = {}
    for c in sim.creatures:
        if c.pos[2] != z:
            continue
        if c.active:
            bodies[c.pos[:2]] = c.glyph
        else:
            bodies.setdefault(c.pos[:2], "%")
    rows = []
    for y in range(world.height):
        row = []
        for x in range(world.width):
            if (x, y) in bodies:
                row.append(bodies[(x, y)])
                continue
            fill = world.fill_mat((x, y, z))
            if fill["id"] != "air":
                row.append(fill["glyph"])
            elif (floor := world.floor_mat((x, y, z))) is not None:
                row.append(floor.get("floor_glyph", "."))
            else:
                row.append(" ")
        rows.append("".join(row).rstrip())
    return "\n".join(rows)


def render_all(sim: "Sim") -> str:
    out = []
    for z in range(sim.world.depth - 1, -1, -1):
        out.append(f"--- z={z} ---")
        out.append(render_level(sim, z))
    return "\n".join(out)
