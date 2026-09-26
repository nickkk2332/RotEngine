# RotEngine

A simulation-first ASCII roguelike combat engine. It takes its freedom of approach from
Dishonored and Deus Ex, its brutal and unfair bodies from GURPS, Dwarf Fortress and
RimWorld, and its modding from CDDA's JSON and EOCs.

This is the foundation: the rules core, the world model and the modding layer, run
headless through an **arena** that plays out scenarios and batch-runs them for balance.
There's no interactive UI yet. See [docs/DESIGN.md](docs/DESIGN.md) for the architecture,
the rules, and the roadmap to a playable game.

## Quick start

```bash
pip install -e ".[dev]"          # numpy + pytest; add [ui] later for python-tcod
python -m rotengine list          # scenarios
python -m rotengine arena hulk_vs_squad --seed 3 --map    # watch one fight
python -m rotengine arena wick_vs_thugs --runs 200       # balance statistics
python -m rotengine validate --mod example_mod           # check content + mods
python -m pytest
```

## What's in the box

| | |
|---|---|
| **3d6 everything** | Roll-under checks, crits, margin of success. Skill buys called shots and feints, and the AI spends it by expected value. |
| **Bodies** | Hit locations, wound multipliers, crippling, severing, bleeding, shock, knockdown, unconsciousness and death checks. HP = ST, stamina = CON. |
| **Threshold damage** | DR subtracts, so a rifle mostly bounces off a DR 25 hide, and a ST 60 punch ignores a vest. |
| **Voxel world** | z-levels, per-voxel walls and floors with HP/DR, bullets through glass and drywall, knockback through walls, falling, and structural collapse. |
| **JSON content** | Materials, bodies, items, creatures, traits, statuses and powers, with `copy-from`/`relative`/`extend`/`delete` inheritance and mods with dependencies. |
| **Effect scripting** | EOC-style effect lists with conditions, values and hooks (`on_damaged`, `on_second`, `on_kill`). Python plugins can register new ops. |
| **Arena** | ASCII scenario maps, seeded and deterministic runs, batch win rates. |

## Current balance (100 seeded runs each)

| Scenario | Result |
|---|---|
| Street fight: two average people with knives | ~50/50, usually ends in someone bleeding out |
| The Hulk (ST 60, DR 25) vs 10 armoured riflemen | Hulk wins ~98%, soldiers never win, the rest are timeouts |
| John Wick (guns 20) vs 8 armed thugs | Wick wins ~83% |
| Blink assassin vs 6 guards, 2 with pistols | Assassin wins ~10%, usually after 3–5 kills. Deliberately hard; needs stealth |

## Layout

```
rotengine/   dice, content, body, creature, world, effects, combat, sim, ai, arena, render
data/core/   base game content (JSON)
data/mods/   mods: a folder with modinfo.json + JSON files
data/scenarios/  arena maps
docs/DESIGN.md   architecture, rules, modding reference, roadmap
```
