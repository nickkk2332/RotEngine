"""Field of view: which voxels a creature can see from where it stands.

Uses the same 3D line of sight as combat, so "I can see it" and "I can shoot
it" always agree. Walls and floors are seen (their near face), and nothing
behind them is. Brute force over the voxels in range: fine for arena-sized
maps; the roguelike mode will want a shadowcaster behind the same function.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .world import Pos, World


def visible_from(world: "World", eye: "Pos", radius: int = 60, glowing=()) -> set["Pos"]:
    """glowing: positions bright enough to see through smoke (flames)."""
    ex, ey, ez = eye
    seen = {eye}
    for z in range(world.depth):
        for y in range(max(0, ey - radius), min(world.height, ey + radius + 1)):
            for x in range(max(0, ex - radius), min(world.width, ex + radius + 1)):
                p = (x, y, z)
                if p != eye and world.has_los(eye, p):
                    seen.add(p)
    # A wall is seen if it borders open ground you can see. (Lines to wall
    # corners graze other wall tiles, which otherwise leaves gaps.)
    walls = set()
    for x, y, z in seen:
        if world.fill_mat((x, y, z)).get("solid"):
            continue
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                q = (x + dx, y + dy, z)
                if q not in seen and world.in_bounds(q) and world.fill_mat(q).get("solid"):
                    walls.add(q)
    for p in glowing:
        if p not in seen and world.has_los(eye, p, fog=False):
            seen.add(p)
    return seen | walls
