"""Roguelike mode: a run down through a dungeon's floors.

A Run owns the player creature and the level it's on. Each floor is its own
Sim built from a generated LevelPlan (see mapgen.py), and the player carries
everything down with them: wounds, blood, broken bones, gear, and whatever
skill they've picked up. The clock runs on from floor to floor, so a
bleed you didn't stop upstairs is still bleeding downstairs.

Floors are stealth-first: nobody knows you're there until they see or hear
you, guards stand posts or walk beats, and bodies get found. Time heals
(slowly) and medicine helps (see body.Body.recover and the "use" items).

The run is saved with pickle, whole, and deleted when the player dies.
"""
from __future__ import annotations

import pickle
from collections import Counter
from pathlib import Path

from . import mapgen
from .content import Content
from .creature import Creature, Item
from .sim import Sim
from .world import World

SAVE_VERSION = 2  # bump whenever what gets pickled changes shape: old saves are refused, not crashed on
SAVE_DIR = Path.home() / ".rotengine"
PLAYER_TEAM = "you"


class SaveError(Exception):
    pass


class Run:
    def __init__(self, content: Content, dungeon_id: str = "black_site", character: str = "operative",
                 seed: int = 0, name: str | None = None):
        self.content = content
        self.dungeon_id = dungeon_id
        self.character = character
        self.seed = seed
        self.name = name
        self.depth = 0
        self.plan: mapgen.LevelPlan | None = None
        self.sim: Sim | None = None
        self.player: Creature | None = None
        self.history: list[str] = []   # one line per floor: what happened there
        self.kills = 0
        self.enter(1)

    # -- content ---------------------------------------------------------------
    @property
    def dungeon(self) -> dict:
        return self.content.get("dungeon", self.dungeon_id)

    @property
    def last_depth(self) -> int:
        return max(f["depth"][1] for f in self.dungeon["floors"])

    # -- floors ------------------------------------------------------------------
    def enter(self, depth: int) -> None:
        """Generate floor `depth` and put the player at its start."""
        plan = mapgen.generate(self.content, self.dungeon_id, depth, self.seed * 7919 + depth)
        world = World(self.content.all("material"), plan.width, plan.height, 1)
        for y, row in enumerate(plan.tiles):
            for x, t in enumerate(row):
                if "fill" in t:
                    world.set_fill((x, y, 0), t["fill"])
                if "floor" in t:
                    world.set_floor((x, y, 0), t["floor"])
        prev = self.sim
        sim = Sim(self.content, world, seed=self.seed * 31 + depth,
                  start_time=prev.time if prev is not None else 0)
        sim.start_aware = False
        sim.ambient_light = plan.ambient_light
        sim.healing = sim.endless = True
        start = (*plan.start, 0)
        if self.player is None:
            self.player = sim.spawn(self.character, PLAYER_TEAM, start, self.name)
            self.player.controller = "player"
        else:
            if prev is not None:
                self._tally(prev)
            sim.adopt(self.player, start)
        names = Counter(self.content.get("creature", s.creature)["name"] for s in plan.spawns)
        numbered: Counter = Counter()
        for s in plan.spawns:
            base = self.content.get("creature", s.creature)["name"]
            numbered[base] += 1
            c = sim.spawn(s.creature, s.team, (*s.pos, 0),
                          f"{base} {numbered[base]}" if names[base] > 1 else base)
            c.facing = c.post_facing = s.facing
            if s.patrol:
                c.patrol = [(*p, 0) for p in s.patrol]
        for iid, pos in plan.items:
            sim.drop((*pos, 0), Item(self.content.get("item", iid)))
        self.depth, self.plan, self.sim = depth, plan, sim
        sim.log(f"Floor {depth} of {self.last_depth}: {plan.name}.", private_to=self.player.uid)

    def _tally(self, sim: Sim) -> None:
        dead = [c for c in sim.creatures if c.dead and c.team != PLAYER_TEAM]
        down = [c for c in sim.creatures if not c.dead and not c.conscious and c.team != PLAYER_TEAM]
        self.kills += len(dead)
        self.history.append(f"Floor {self.depth} ({self.plan.name}): {len(dead)} dead, {len(down)} left "
                            f"unconscious, {sum(c.team != PLAYER_TEAM for c in sim.creatures)} there.")

    def at_exit(self) -> bool:
        p = self.player
        return (p.pos[2] == 0 and self.sim.world.floor_mat(p.pos) is not None
                and self.sim.world.floor_mat(p.pos).get("exit") == "down")

    def descend(self) -> bool:
        """Take the stairs down, if you're standing on them. Whoever you were
        holding stays behind."""
        p = self.player
        if not self.at_exit() or not p.can_act or self.depth >= self.last_depth:
            return False
        from .grapple import release
        if p.grappled_by is not None:
            return False
        release(self.sim, p)
        self.sim.awaiting = None
        self.enter(self.depth + 1)
        return True

    # -- how it's going ------------------------------------------------------------
    def boss(self) -> Creature | None:
        boss = mapgen.floor_for(self.dungeon, self.depth).get("boss")
        if not boss:
            return None
        return next((c for c in self.sim.creatures
                     if c.template["id"] == boss["creature"] and c is not self.player), None)

    @property
    def state(self) -> str:
        """'playing', 'dead' or 'won' (the final floor's boss is down)."""
        if self.player.dead:
            return "dead"
        boss = self.boss()
        if self.depth >= self.last_depth and boss is not None and not boss.active:
            return "won"
        return "playing"

    def epitaph(self) -> str:
        p = self.player
        where = f"on floor {self.depth} ({self.plan.name})"
        if self.state == "won":
            return f"{p.name} took down {self.boss().name} {where}, {self.minutes():.0f} minutes in."
        if p.dead:
            return f"{p.name} died ({p.death_cause}) {where}, {self.minutes():.0f} minutes in."
        return f"{p.name} is {where}."

    def dead_so_far(self) -> int:
        return self.kills + sum(c.dead and c.team != PLAYER_TEAM for c in self.sim.creatures)

    def minutes(self) -> float:
        return self.sim.time / 60_000

    # -- saving ------------------------------------------------------------------
    def save(self, path: Path | None = None) -> Path:
        path = path or save_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "wb") as f:
                pickle.dump({"version": SAVE_VERSION, "run": self}, f, protocol=pickle.HIGHEST_PROTOCOL)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(path)  # never leave half a save behind
        return path


def save_path() -> Path:
    return SAVE_DIR / "run.sav"


def has_save(path: Path | None = None) -> bool:
    return (path or save_path()).exists()


def load(path: Path | None = None) -> Run:
    path = path or save_path()
    try:
        with open(path, "rb") as f:
            data = pickle.load(f)
    except FileNotFoundError as e:
        raise SaveError("There's no saved run.") from e
    except Exception as e:  # truncated, corrupted, or not a save at all
        raise SaveError(f"can't read the save at {path}: {e}") from e
    if not isinstance(data, dict) or data.get("version") != SAVE_VERSION:
        raise SaveError(f"the save at {path} is from a different version of the game")
    if not isinstance(data.get("run"), Run):
        raise SaveError(f"the save at {path} is damaged")
    return data["run"]


def delete_save(path: Path | None = None) -> None:
    """Permadeath."""
    (path or save_path()).unlink(missing_ok=True)
