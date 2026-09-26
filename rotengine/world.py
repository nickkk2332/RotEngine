"""The voxel world: z-levels, destructible terrain, and structural collapse.

Dwarf-Fortress-style cells: each (x, y, z) voxel has
  * a *fill* material (air, a wall, glass, stairs...) that blocks or allows movement
  * a *floor* material, the slab you stand on, which is also the ceiling of z-1.
Walls and floors both have HP and DR, so bullets penetrate drywall, fists go
through brick, and a knocked-back body can go through a window and fall two
storeys.

Arrays are numpy [z, y, x] so later systems (FOV, fluids, gas, heat) can be
vectorised. Positions passed around the game are (x, y, z) tuples.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Iterator

import numpy as np

Pos = tuple[int, int, int]

DIRS8 = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]
AIR = 0


class World:
    def __init__(self, materials: dict[str, dict], width: int, height: int, depth: int):
        if "air" not in materials:
            raise ValueError("materials must define 'air'")
        self.mats: list[dict] = [materials["air"]] + [m for k, m in materials.items() if k != "air"]
        self.mat_index = {m["id"]: i for i, m in enumerate(self.mats)}
        self.width, self.height, self.depth = width, height, depth
        shape = (depth, height, width)
        self.fill = np.zeros(shape, np.uint16)
        self.floor = np.zeros(shape, np.uint16)
        self.fill_hp = np.zeros(shape, np.int32)
        self.floor_hp = np.zeros(shape, np.int32)
        self._structural = np.array([bool(m.get("solid") or m.get("supports")) for m in self.mats])
        self._opaque = np.array([bool(m.get("solid") and not m.get("transparent")) for m in self.mats])

    # -- editing -----------------------------------------------------------
    def set_fill(self, pos: Pos, mat: str) -> None:
        x, y, z = pos
        i = self.mat_index[mat]
        self.fill[z, y, x] = i
        self.fill_hp[z, y, x] = self.mats[i].get("hp", 0)

    def set_floor(self, pos: Pos, mat: str | None) -> None:
        x, y, z = pos
        i = self.mat_index[mat] if mat else AIR
        self.floor[z, y, x] = i
        self.floor_hp[z, y, x] = self.mats[i].get("hp", 0)

    # -- queries -----------------------------------------------------------
    def in_bounds(self, pos: Pos) -> bool:
        x, y, z = pos
        return 0 <= x < self.width and 0 <= y < self.height and 0 <= z < self.depth

    def fill_mat(self, pos: Pos) -> dict:
        x, y, z = pos
        return self.mats[self.fill[z, y, x]]

    def floor_mat(self, pos: Pos) -> dict | None:
        x, y, z = pos
        i = self.floor[z, y, x]
        return self.mats[i] if i else None

    def passable(self, pos: Pos) -> bool:
        return self.in_bounds(pos) and not self.fill_mat(pos).get("solid")

    def supported(self, pos: Pos) -> bool:
        x, y, z = pos
        return bool(self.floor[z, y, x]) or bool(self.fill_mat(pos).get("supports"))

    def standable(self, pos: Pos) -> bool:
        return self.passable(pos) and self.supported(pos)

    def neighbors(self, pos: Pos) -> Iterator[Pos]:
        """Walkable moves from pos: 8 horizontal plus climbing stairs."""
        x, y, z = pos
        for dx, dy in DIRS8:
            q = (x + dx, y + dy, z)
            if not self.standable(q):
                continue
            if dx and dy and not (self.passable((x + dx, y, z)) or self.passable((x, y + dy, z))):
                continue  # no squeezing diagonally between two walls
            yield q
        if self.fill_mat(pos).get("climbable"):
            for dz in (1, -1):
                q = (x, y, z + dz)
                if self.in_bounds(q) and self.passable(q) and self.fill_mat(q).get("climbable"):
                    yield q

    # -- lines, sight and projectiles --------------------------------------
    @staticmethod
    def line(a: Pos, b: Pos) -> list[Pos]:
        """Voxels from a (exclusive) to b (inclusive)."""
        d = [b[i] - a[i] for i in range(3)]
        n = max(abs(v) for v in d)
        return [tuple(int(math.floor(a[i] + d[i] * s / n + 0.5)) for i in range(3))
                for s in range(1, n + 1)]

    def _crossing(self, p: Pos, q: Pos) -> Pos | None:
        """The floor slab crossed moving p -> q, if the move changes z."""
        if q[2] > p[2]:
            return q
        if q[2] < p[2]:
            return p
        return None

    def obstacles(self, a: Pos, b: Pos) -> list[tuple[str, Pos]] | None:
        """Solid-but-see-through things between a and b (glass, grates, glass
        floors), or None if something opaque is in the way."""
        found: list[tuple[str, Pos]] = []
        prev = a
        for p in self.line(a, b):
            slab = self._crossing(prev, p)
            if slab is not None:
                fm = self.floor_mat(slab)
                if fm is not None:
                    if not fm.get("transparent"):
                        return None
                    found.append(("floor", slab))
            if p != b:
                x, y, z = p
                mi = self.fill[z, y, x]
                if self._opaque[mi]:
                    return None
                if self.mats[mi].get("solid"):
                    found.append(("fill", p))
            prev = p
        return found

    def first_opaque(self, a: Pos, b: tuple[float, float, float], exclude: Pos) -> Pos | None:
        """First opaque wall voxel on the segment from a's centre to an
        arbitrary point b (used for partial cover), ignoring a and `exclude`."""
        n = int(max(abs(b[i] - a[i]) for i in range(3)) * 3) + 1
        for s in range(1, n + 1):
            p = tuple(int(math.floor(a[i] + (b[i] - a[i]) * s / n + 0.5)) for i in range(3))
            if p == a or p == exclude:
                continue
            if not self.in_bounds(p):
                return None
            if self._opaque[self.fill[p[2], p[1], p[0]]]:
                return p
        return None

    def has_los(self, a: Pos, b: Pos) -> bool:
        return self.obstacles(a, b) is not None

    # -- destruction -------------------------------------------------------
    def damage_fill(self, pos: Pos, amount: int) -> bool:
        """Apply damage to a wall voxel; True if it was destroyed."""
        x, y, z = pos
        mat = self.fill_mat(pos)
        if not (mat.get("solid") or mat.get("supports")) or mat.get("indestructible"):
            return False
        pen = amount - mat.get("dr", 0)
        if pen <= 0:
            return False
        self.fill_hp[z, y, x] -= pen
        if self.fill_hp[z, y, x] <= 0:
            self.set_fill(pos, "air")
            return True
        return False

    def damage_floor(self, pos: Pos, amount: int) -> bool:
        x, y, z = pos
        mat = self.floor_mat(pos)
        if mat is None or z == 0 or mat.get("indestructible"):
            return False  # z=0 floors are bedrock
        pen = amount - mat.get("dr", 0)
        if pen <= 0:
            return False
        self.floor_hp[z, y, x] -= pen
        if self.floor_hp[z, y, x] <= 0:
            self.set_floor(pos, None)
            return True
        return False

    def settle(self) -> list[tuple[str, Pos]]:
        """Remove everything no longer connected to the ground.

        Walls support walls above/beside them; floors hang off adjacent floors
        and rest on or attach to walls. Anything not connected to z=0 falls.
        Returns what collapsed as ("fill"|"floor", (x, y, z)).
        """
        fill_s = self._structural[self.fill]
        floor_s = self.floor != AIR
        seen_fill = np.zeros_like(fill_s)
        seen_floor = np.zeros_like(floor_s)
        q: deque[tuple[int, int, int, int]] = deque()  # (kind, z, y, x); kind 0=fill 1=floor
        for y, x in zip(*np.nonzero(fill_s[0])):
            seen_fill[0, y, x] = True
            q.append((0, 0, y, x))
        for y, x in zip(*np.nonzero(floor_s[0])):
            seen_floor[0, y, x] = True
            q.append((1, 0, y, x))
        D, H, W = fill_s.shape

        def visit(kind: int, z: int, y: int, x: int) -> None:
            if not (0 <= z < D and 0 <= y < H and 0 <= x < W):
                return
            mask, seen = (fill_s, seen_fill) if kind == 0 else (floor_s, seen_floor)
            if mask[z, y, x] and not seen[z, y, x]:
                seen[z, y, x] = True
                q.append((kind, z, y, x))

        while q:
            kind, z, y, x = q.popleft()
            if kind == 0:
                for dz, dy, dx in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
                    visit(0, z + dz, y + dy, x + dx)
                visit(1, z + 1, y, x)          # the floor resting on top of this wall
                visit(1, z, y, x)              # the floor under this wall
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    visit(1, z, y + dy, x + dx)  # floors bolted to the wall's side
            else:
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    visit(1, z, y + dy, x + dx)
                    visit(0, z, y + dy, x + dx)
                visit(0, z, y, x)
                visit(0, z - 1, y, x)

        collapsed: list[tuple[str, Pos]] = []
        for z, y, x in zip(*np.nonzero(fill_s & ~seen_fill)):
            self.set_fill((int(x), int(y), int(z)), "air")
            collapsed.append(("fill", (int(x), int(y), int(z))))
        for z, y, x in zip(*np.nonzero(floor_s & ~seen_floor)):
            self.set_floor((int(x), int(y), int(z)), None)
            collapsed.append(("floor", (int(x), int(y), int(z))))
        return collapsed
