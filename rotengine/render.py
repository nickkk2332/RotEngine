"""Plain-text rendering of one z-level. The real UI will use python-tcod, but
it will read the same World and Sim.

Glyphs: creatures by their template glyph; '&' someone down (unconscious);
'%' a corpse; items on the ground by their own glyph."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sim import Sim

DOWN_GLYPH = "&"
CORPSE_GLYPH = "%"


def render_level(sim: "Sim", z: int) -> str:
    world = sim.world
    # later layers win: items, then corpses, then the downed, then the standing
    marks: dict[tuple[int, int], str] = {}
    for pos, item in sim.items:
        if pos[2] == z:
            marks[pos[:2]] = item.data.get("glyph", "(")
    for layer in ("dead", "down", "active"):
        for c in sim.creatures:
            state = "dead" if c.dead else "active" if c.active else "down"
            if c.pos[2] == z and state == layer:
                marks[c.pos[:2]] = {"dead": CORPSE_GLYPH, "down": DOWN_GLYPH}.get(state, c.glyph)
    rows = []
    for y in range(world.height):
        row = []
        for x in range(world.width):
            if (x, y) in marks:
                row.append(marks[(x, y)])
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
