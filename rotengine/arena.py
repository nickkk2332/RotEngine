"""Arena mode: build a fight from a scenario file and run it, once or many times.

Batch runs are the balance tool. "Does the Hulk actually shred a rifle squad?"
becomes a win rate over a few hundred seeded runs, not a guess.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .content import DATA_DIR, Content, load_content
from .render import render_all
from .sim import Sim
from .world import World


def load_scenario(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        p = DATA_DIR / "scenarios" / (str(path) + ("" if str(path).endswith(".json") else ".json"))
    scenario = json.loads(p.read_text())
    scenario.setdefault("id", p.stem)
    return scenario


def build_world(scenario: dict, content: Content) -> World:
    legend = dict(content.get("tile_legend", "default")["tiles"])
    legend.update(scenario.get("legend", {}))
    levels = scenario["levels"]
    height = max(len(level) for level in levels)
    width = max(len(row) for level in levels for row in level)
    world = World(content.all("material"), width, height, len(levels))
    for z, level in enumerate(levels):
        for y, row in enumerate(level):
            for x, ch in enumerate(row.ljust(width)):
                if ch not in legend:
                    raise ValueError(f"scenario {scenario['id']}: no legend entry for {ch!r} at {(x, y, z)}")
                tile = legend[ch]
                if "fill" in tile:
                    world.set_fill((x, y, z), tile["fill"])
                if "floor" in tile:
                    world.set_floor((x, y, z), tile["floor"])
    return world


def build(scenario: dict, content: Content, seed: int | None = None,
          echo: Callable[[str], None] | None = None) -> Sim:
    sim = Sim(content, build_world(scenario, content), seed, echo)
    groups = [(team, g) for team, gs in scenario["teams"].items() for g in gs]
    base_names = [g.get("name") or content.get("creature", g["creature"])["name"] for _, g in groups]
    totals = Counter(n for (_, g), n in zip(groups, base_names) for _ in range(g.get("count", 1)))
    numbered: Counter = Counter()
    for (team, g), base in zip(groups, base_names):
        for _ in range(g.get("count", 1)):
            pos = tuple(g["at"]) if "at" in g else _free_spot(sim, g["area"])
            numbered[base] += 1
            name = f"{base} {numbered[base]}" if totals[base] > 1 else base
            sim.spawn(g["creature"], team, pos, name)
    return sim


def _free_spot(sim: Sim, area: list[int]) -> tuple[int, int, int]:
    x0, y0, x1, y1, z = area
    spots = [(x, y, z) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)
             if sim.world.standable((x, y, z)) and sim.creature_at((x, y, z)) is None]
    if not spots:
        raise ValueError(f"no free standable tile in area {area}")
    return sim.rng.choice(spots)


@dataclass
class Outcome:
    winner: str | None
    seconds: float
    dead: Counter
    down: Counter


def run_once(scenario: dict, content: Content, seed: int | None = None,
             echo: Callable[[str], None] | None = None, show_map: bool = False) -> Outcome:
    sim = build(scenario, content, seed, echo)
    if show_map and echo:
        echo(render_all(sim))
    winner = sim.run(scenario.get("time_limit_s", 180) * 1000)
    if echo:
        if show_map:
            echo(render_all(sim))
        echo(f"\n=== {'Winner: ' + winner if winner else 'No winner'} after {sim.time / 1000:.1f}s ===")
        for c in sim.creatures:
            state = "dead" if c.dead else "unconscious" if not c.conscious else "standing"
            echo(f"  {c.name:<16} [{c.team}] {state:<11} {c.body.summary()}")
    dead = Counter(c.team for c in sim.creatures if c.dead)
    down = Counter(c.team for c in sim.creatures if not c.dead and not c.conscious)
    return Outcome(winner, sim.time / 1000, dead, down)


def run_batch(scenario: dict, content: Content, runs: int, seed: int = 0) -> str:
    outcomes = [run_once(scenario, content, seed + i) for i in range(runs)]
    teams = list(scenario["teams"])
    wins = Counter(o.winner for o in outcomes)
    lines = [f"{scenario.get('name', scenario['id'])}: {runs} runs"]
    for t in teams + [None]:
        if wins[t] or t is not None:
            lines.append(f"  {t or 'draw':<10} wins {wins[t]:>4}  ({100 * wins[t] / runs:5.1f}%)")
    lines.append(f"  fight length: median {statistics.median(o.seconds for o in outcomes):.1f}s")
    for t in teams:
        size = sum(g.get("count", 1) for g in scenario["teams"][t])
        d = statistics.mean(o.dead[t] for o in outcomes)
        k = statistics.mean(o.down[t] for o in outcomes)
        lines.append(f"  {t:<10} avg dead {d:4.1f} / down {k:4.1f} of {size}")
    return "\n".join(lines)


def content_for(scenario: dict, extra_mods: list[str] = ()) -> Content:
    return load_content(list(scenario.get("mods", [])) + list(extra_mods))
