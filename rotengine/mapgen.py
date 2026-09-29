"""Level generation from JSON prefabs (CDDA-style mapgen with palettes).

A **prefab** is a room drawn in ASCII. Its characters mean whatever its
palettes say: the default tile legend, then any named "palette" objects,
then the prefab's own "legend". Besides terrain, a palette entry can mark

* "door_slot": a spot in the room's wall where a corridor may connect. Used
  slots become a door or an open doorway; unused ones stay wall.
* "monster": "level" (the floor's monster group) or a monster_group id, with
  an optional "chance": someone might be standing here.
* "item": "level" or an item_group id, with "chance": loot.
* "exit": a candidate spot for the way down.

A **dungeon** lists floors by depth: size, which prefab tags to draw rooms
from, how many rooms, the monster and item groups, light, and what rock and
corridors are made of.

The generator splits the level into a grid of cells, drops a prefab (maybe
mirrored or rotated) into some of them, joins the rooms with a spanning tree
plus a few loops of A*-carved corridors through the rock, and puts the exit
in the room farthest from the start. Levels that come out disconnected are
thrown away and rolled again.
"""
from __future__ import annotations

import heapq
import random
from dataclasses import dataclass, field

from .content import Content

Pos2 = tuple[int, int]
CELL_W, CELL_H = 18, 12
MAX_TRIES = 30


class MapgenError(Exception):
    pass


@dataclass
class Room:
    prefab: str
    x: int
    y: int
    w: int
    h: int
    slots: list[Pos2] = field(default_factory=list)
    floor: list[Pos2] = field(default_factory=list)   # open tiles inside
    exits: list[Pos2] = field(default_factory=list)

    @property
    def center(self) -> Pos2:
        return self.x + self.w // 2, self.y + self.h // 2

    def contains(self, p: Pos2) -> bool:
        return self.x <= p[0] < self.x + self.w and self.y <= p[1] < self.y + self.h


@dataclass
class Spawn:
    creature: str
    pos: Pos2
    team: str = "hostile"
    patrol: list[Pos2] = field(default_factory=list)
    facing: tuple[int, int] = (1, 0)


@dataclass
class LevelPlan:
    """A generated level, before it becomes a World."""
    name: str
    depth: int
    width: int
    height: int
    tiles: list[list[dict]]          # [y][x] -> {"fill": ..., "floor": ...}
    rooms: list[Room]
    start: Pos2
    exit: Pos2 | None
    spawns: list[Spawn]
    items: list[tuple[str, Pos2]]
    ambient_light: float = 1.0
    final: bool = False

    def ascii(self, content: Content) -> str:
        """Debug view: the level as characters."""
        rows = []
        for y in range(self.height):
            row = []
            for x in range(self.width):
                t = self.tiles[y][x]
                if (x, y) == self.start:
                    row.append("@")
                elif (x, y) == self.exit:
                    row.append(">")
                elif t.get("fill", "air") != "air":
                    row.append(content.get("material", t["fill"])["glyph"])
                elif t.get("floor"):
                    row.append(content.get("material", t["floor"]).get("floor_glyph", "."))
                else:
                    row.append(" ")
            rows.append("".join(row))
        for s in self.spawns:
            x, y = s.pos
            rows[y] = rows[y][:x] + "M" + rows[y][x + 1:]
        return "\n".join(rows)


# -- content helpers ------------------------------------------------------------
def floor_for(dungeon: dict, depth: int) -> dict:
    for f in dungeon["floors"]:
        lo, hi = f.get("depth", [1, 999])
        if lo <= depth <= hi:
            return f
    raise MapgenError(f"dungeon {dungeon['id']} has no floor for depth {depth}")


def in_depth(entry: dict, depth: int) -> bool:
    lo, hi = entry.get("depth", [1, 999])
    return lo <= depth <= hi


def pick_weighted(rng: random.Random, entries: list[dict]) -> dict | None:
    entries = [e for e in entries if e.get("weight", 10) > 0]
    if not entries:
        return None
    return rng.choices(entries, [e.get("weight", 10) for e in entries])[0]


def roll_range(rng: random.Random, r, default: int = 0) -> int:
    if r is None:
        return default
    if isinstance(r, int):
        return r
    return rng.randint(r[0], r[1])


def palette_for(content: Content, prefab: dict) -> dict[str, dict]:
    legend = dict(content.get("tile_legend", "default")["tiles"])
    for pid in prefab.get("palettes", []):
        legend.update(content.get("palette", pid)["tiles"])
    legend.update(prefab.get("legend", {}))
    return legend


def transform(rows: list[str], mirror_x: bool, mirror_y: bool, rotate: bool) -> list[str]:
    w = max(len(r) for r in rows)
    grid = [list(r.ljust(w)) for r in rows]
    if rotate:
        grid = [list(col) for col in zip(*grid)]
    if mirror_x:
        grid = [row[::-1] for row in grid]
    if mirror_y:
        grid = grid[::-1]
    return ["".join(r) for r in grid]


# -- generation -------------------------------------------------------------------
def generate(content: Content, dungeon_id: str, depth: int, seed: int) -> LevelPlan:
    """Generate one floor of a dungeon. Deterministic for a given seed."""
    dungeon = content.get("dungeon", dungeon_id)
    fdef = floor_for(dungeon, depth)
    for attempt in range(MAX_TRIES):
        rng = random.Random(seed * 1009 + attempt)
        plan = _try_generate(content, dungeon, fdef, depth, rng)
        if plan is not None:
            return plan
    raise MapgenError(f"couldn't generate {dungeon_id} depth {depth} in {MAX_TRIES} tries")


def _try_generate(content: Content, dungeon: dict, fdef: dict, depth: int,
                  rng: random.Random) -> LevelPlan | None:
    W, H = fdef.get("size", [72, 36])
    rock = fdef.get("rock", {"fill": "concrete", "floor": "concrete"})
    tiles = [[dict(rock) for _ in range(W)] for _ in range(H)]
    solid = [[True] * W for _ in range(H)]     # rock nobody has claimed
    claimed = [[False] * W for _ in range(H)]  # part of a room (walls included)

    # 1. rooms into cells
    cols, rows_ = max(2, (W - 2) // CELL_W), max(2, (H - 2) // CELL_H)
    cw, ch = (W - 2) // cols, (H - 2) // rows_
    cells = [(cx, cy) for cy in range(rows_) for cx in range(cols)]
    rng.shuffle(cells)
    n_rooms = min(len(cells), roll_range(rng, fdef.get("rooms"), len(cells)))
    tags = set(fdef.get("prefab_tags", []))
    prefabs = [p for p in content.all("prefab").values()
               if (not tags or tags & set(p.get("tags", []))) and in_depth(p, depth)]
    if not prefabs:
        raise MapgenError(f"no prefabs with tags {sorted(tags)} for depth {depth}")
    rooms: list[Room] = []
    monster_marks: list[tuple[Pos2, dict, int]] = []
    item_marks: list[tuple[Pos2, dict, int]] = []
    boss = fdef.get("boss")
    boss_tag = boss.get("prefab_tag") if boss else None
    if boss_tag:
        prefabs = prefabs + [p for p in content.all("prefab").values()
                             if boss_tag in p.get("tags", []) and p not in prefabs]
    for n, (cx, cy) in enumerate(cells[:n_rooms]):
        options = []
        # the first room of a boss floor is the boss's; no other room gets that prefab
        pool = [p for p in prefabs if (boss_tag in p.get("tags", [])) == (n == 0)] if boss_tag else prefabs
        for p in pool:
            for rot in ((False, True) if p.get("rotate", True) else (False,)):
                rows = transform(p["rows"], False, False, rot)
                if len(rows[0]) <= cw - 2 and len(rows) <= ch - 2:
                    options.append((p, rot))
        if not options:
            continue
        p, rot = rng.choices(options, [o[0].get("weight", 10) for o in options])[0]
        mx, my = (rng.random() < 0.5, rng.random() < 0.5) if p.get("mirror", True) else (False, False)
        rows = transform(p["rows"], mx, my, rot)
        w, h = len(rows[0]), len(rows)
        x0 = 1 + cx * cw + rng.randint(1, max(1, cw - w - 1))
        y0 = 1 + cy * ch + rng.randint(1, max(1, ch - h - 1))
        legend = palette_for(content, p)
        room = Room(p["id"], x0, y0, w, h)
        for dy, line in enumerate(rows):
            for dx, char in enumerate(line):
                x, y = x0 + dx, y0 + dy
                t = legend.get(char)
                if t is None:
                    raise MapgenError(f"prefab {p['id']}: no palette entry for {char!r}")
                claimed[y][x] = True
                solid[y][x] = False
                tiles[y][x] = {k: v for k, v in t.items() if k in ("fill", "floor")}
                if t.get("door_slot"):
                    room.slots.append((x, y))
                elif t.get("fill", "air") == "air" and t.get("floor"):
                    room.floor.append((x, y))
                if t.get("exit"):
                    room.exits.append((x, y))
                if t.get("monster"):
                    monster_marks.append(((x, y), t, len(rooms)))
                if t.get("item"):
                    item_marks.append(((x, y), t, len(rooms)))
        if room.slots and room.floor:
            rooms.append(room)
        elif boss_tag and n == 0:
            return None
    if len(rooms) < 2 or (boss_tag and rooms[0].prefab not in
                          {p["id"] for p in prefabs if boss_tag in p.get("tags", [])}):
        return None

    # 2. corridors: a spanning tree plus a few loops
    edges = _spanning_edges(rooms)
    extra = roll_range(rng, fdef.get("loops"), 1)
    others = [(a, b) for a in range(len(rooms)) for b in range(a + 1, len(rooms))
              if (a, b) not in edges and (b, a) not in edges]
    others.sort(key=lambda e: _dist(rooms[e[0]].center, rooms[e[1]].center))
    edges += others[:extra]
    used: set[Pos2] = set()
    links = _UnionFind(len(rooms))
    corridor = fdef.get("corridor", {"floor": "concrete"})
    for a, b in edges:
        path = _connect(rooms[a], rooms[b], claimed, W, H, used, tiles)
        if path is None:
            continue
        for x, y in path:
            if not claimed[y][x]:
                tiles[y][x] = dict(corridor)
                solid[y][x] = False
        links.union(a, b)
    if len({links.find(i) for i in range(len(rooms))}) > 1:
        return None
    door = fdef.get("door", {"fill": "door", "floor": "concrete"})
    doorway = fdef.get("doorway", {"floor": "concrete"})
    for x, y in used:
        tiles[y][x] = dict(door if rng.random() < fdef.get("door_chance", 0.6) else doorway)

    # 3. start and exit: the exit goes in the room farthest (by corridor) from the start
    if boss_tag:  # start as far from the boss as the level allows
        dist0 = _room_distances(0, edges, rooms, links)
        start_room = max(range(1, len(rooms)), key=lambda i: (dist0.get(i, -1), i))
    else:
        start_room = rng.randrange(len(rooms))
    dist = _room_distances(start_room, edges, rooms, links)
    far = max(range(len(rooms)), key=lambda i: (dist.get(i, -1), i))
    start = rng.choice(rooms[start_room].floor)
    exit_pos = None
    final = bool(fdef.get("final"))
    if not final:
        er = rooms[far]
        choices = er.exits or [p for p in er.floor if p != start]
        exit_pos = rng.choice(choices)
        tiles[exit_pos[1]][exit_pos[0]] = {"floor": fdef.get("exit_floor", "stairwell_down")}

    # 4. people and loot
    spawns: list[Spawn] = []
    taken = {start} | ({exit_pos} if exit_pos else set())
    near_start = lambda p: _dist(p, start) < 8  # noqa: E731
    level_monsters = fdef.get("monsters", {})
    for pos, t, ri in monster_marks:
        if ri == start_room or pos in taken or near_start(pos) or rng.random() >= t.get("chance", 0.7):
            continue
        group = level_monsters.get("group") if t["monster"] == "level" else t["monster"]
        cid = _pick_creature(content, group, depth, rng)
        if cid:
            spawns.append(Spawn(cid, pos, level_monsters.get("team", "hostile"), facing=_rand_facing(rng)))
            taken.add(pos)
    for _ in range(roll_range(rng, level_monsters.get("extra"), 0)):
        ri = rng.choice([i for i in range(len(rooms)) if i != start_room])
        free = [p for p in rooms[ri].floor if p not in taken and not near_start(p)]
        cid = _pick_creature(content, level_monsters.get("group"), depth, rng)
        if free and cid:
            pos = rng.choice(free)
            spawns.append(Spawn(cid, pos, level_monsters.get("team", "hostile"), facing=_rand_facing(rng)))
            taken.add(pos)
    if boss:
        free = [p for p in rooms[0].floor if p not in taken]
        if not free:
            return None
        pos = rng.choice(free)
        spawns.append(Spawn(boss["creature"], pos, level_monsters.get("team", "hostile"), facing=_rand_facing(rng)))
        taken.add(pos)
    # some of them walk a beat between rooms
    beat_rooms = [i for i in range(len(rooms)) if i != start_room]  # nobody walks through where you start
    for s in spawns:
        if boss and s.creature == boss["creature"]:
            continue  # the boss keeps to its room
        if rng.random() < fdef.get("patrol_chance", 0.25) and beat_rooms:
            beat = rng.sample(beat_rooms, min(len(beat_rooms), rng.randint(2, 3)))
            s.patrol = [s.pos] + [rng.choice(rooms[i].floor) for i in beat]
    items: list[tuple[str, Pos2]] = []
    level_items = fdef.get("items", {})
    for pos, t, ri in item_marks:
        if pos in taken or rng.random() >= t.get("chance", 0.6):
            continue
        group = level_items.get("group") if t["item"] == "level" else t["item"]
        iid = _pick_item(content, group, depth, rng)
        if iid:
            items.append((iid, pos))
            taken.add(pos)
    for _ in range(roll_range(rng, level_items.get("extra"), 0)):
        ri = rng.randrange(len(rooms))
        iid = _pick_item(content, level_items.get("group"), depth, rng)
        free = [p for p in rooms[ri].floor if p not in taken]
        if iid and free:
            pos = rng.choice(free)
            items.append((iid, pos))
            taken.add(pos)

    plan = LevelPlan(fdef.get("name", f"depth {depth}"), depth, W, H, tiles, rooms, start, exit_pos,
                     spawns, items, fdef.get("ambient_light", 1.0), final)
    if not _reachable(plan, content):
        return None
    return plan


def _rand_facing(rng: random.Random) -> tuple[int, int]:
    return rng.choice([(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)])


def _pick_creature(content: Content, group: str | None, depth: int, rng: random.Random) -> str | None:
    if not group:
        return None
    e = pick_weighted(rng, [e for e in content.get("monster_group", group)["entries"] if in_depth(e, depth)])
    return e["creature"] if e else None


def _pick_item(content: Content, group: str | None, depth: int, rng: random.Random) -> str | None:
    if not group:
        return None
    e = pick_weighted(rng, [e for e in content.get("item_group", group)["entries"] if in_depth(e, depth)])
    return e["item"] if e else None


def _dist(a: Pos2, b: Pos2) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _spanning_edges(rooms: list[Room]) -> list[tuple[int, int]]:
    """Prim's minimum spanning tree over room centers."""
    inside, edges = {0}, []
    while len(inside) < len(rooms):
        a, b = min(((i, j) for i in inside for j in range(len(rooms)) if j not in inside),
                   key=lambda e: _dist(rooms[e[0]].center, rooms[e[1]].center))
        inside.add(b)
        edges.append((a, b))
    return edges


def _outside(room: Room, slot: Pos2) -> Pos2:
    x, y = slot
    if x == room.x:
        return x - 1, y
    if x == room.x + room.w - 1:
        return x + 1, y
    if y == room.y:
        return x, y - 1
    return x, y + 1


def _connect(a: Room, b: Room, claimed, W: int, H: int, used: set, tiles) -> list[Pos2] | None:
    """Carve a corridor between the nearest door slots of two rooms."""
    pairs = sorted(((sa, sb) for sa in a.slots for sb in b.slots),
                   key=lambda p: (_dist(p[0], p[1]), p))
    for sa, sb in pairs[:6]:
        oa, ob = _outside(a, sa), _outside(b, sb)
        path = _astar(oa, ob, claimed, W, H, tiles)
        if path is not None:
            used.update((sa, sb))
            return path
    return None


def _astar(start: Pos2, goal: Pos2, claimed, W: int, H: int, tiles) -> list[Pos2] | None:
    """4-way path through unclaimed rock; reusing corridors is cheaper."""
    def ok(p):
        x, y = p
        return 0 < x < W - 1 and 0 < y < H - 1 and not claimed[y][x]
    if not ok(start) or not ok(goal):
        return None
    frontier = [(0, start)]
    came = {start: None}
    cost = {start: 0}
    while frontier:
        _, cur = heapq.heappop(frontier)
        if cur == goal:
            path = []
            while cur is not None:
                path.append(cur)
                cur = came[cur]
            return path[::-1]
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nxt = (cur[0] + dx, cur[1] + dy)
            if not ok(nxt):
                continue
            step = 1 if tiles[nxt[1]][nxt[0]].get("fill", "air") == "air" else 3
            c = cost[cur] + step
            if c < cost.get(nxt, 1 << 30):
                cost[nxt] = c
                came[nxt] = cur
                heapq.heappush(frontier, (c + _dist(nxt, goal), nxt))
    return None


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def _room_distances(start: int, edges, rooms, links) -> dict[int, int]:
    adj: dict[int, list[int]] = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    dist, todo = {start: 0}, [(0, start)]
    while todo:  # Dijkstra: shortest walk through the corridors, room to room
        d, cur = heapq.heappop(todo)
        if d > dist[cur]:
            continue
        for n in adj.get(cur, []):
            nd = d + _dist(rooms[cur].center, rooms[n].center)
            if nd < dist.get(n, nd + 1):
                dist[n] = nd
                heapq.heappush(todo, (nd, n))
    return dist


def _reachable(plan: LevelPlan, content: Content) -> bool:
    """Every spawn, item and the exit can be walked to from the start
    (doors count as open)."""
    mats = content.all("material")

    def walkable(x, y):
        t = plan.tiles[y][x]
        fill = mats.get(t.get("fill", "air"), {})
        return (not fill.get("solid") or fill.get("door")) and bool(t.get("floor"))
    seen, todo = {plan.start}, [plan.start]
    while todo:
        x, y = todo.pop()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                n = (x + dx, y + dy)
                if n not in seen and 0 <= n[0] < plan.width and 0 <= n[1] < plan.height and walkable(*n):
                    seen.add(n)
                    todo.append(n)
    wanted = [s.pos for s in plan.spawns] + [p for _, p in plan.items] + ([plan.exit] if plan.exit else [])
    return all(p in seen for p in wanted)
