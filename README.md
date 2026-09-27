# RotEngine

A simulation-first ASCII roguelike combat engine. It takes its freedom of approach from
Dishonored and Deus Ex, its brutal and unfair bodies from GURPS, Dwarf Fortress and
RimWorld, and its modding from CDDA's JSON and EOCs.

Right now it's an **arena**: pick a scenario (or build a custom fight), pick who you
play, and fight it out in an ASCII window. The same engine also runs headless to
batch-simulate fights for balance. See [docs/DESIGN.md](docs/DESIGN.md) for the
architecture, the rules, and the roadmap.

## Playing

| Key | |
|---|---|
| arrows / numpad / `hjklyubn` | move; walk into an enemy to hit them |
| `f` | attack menu: target, attack, hit location, feint, aim, with the real odds |
| `F` | quick attack with the best option (steps closer if out of reach) |
| `Tab` | cycle target |
| `.` / numpad 5 | wait half a second (of your own time) |
| `r` / `m` / `z` / `g` | reload / first aid / drop prone or stand / pick up a weapon |
| `p` | powers |
| `<` `>` | stairs up / down |
| `[` `]` | view the level below / above |
| `x` | look: inspect terrain and anyone's wounds |
| `?` / `Esc` | help / quit to menu |

Time only moves when you act, and it moves by the cost of what you do in *your*
time: play the speedster and everyone else is in slow motion. When the fight ends
you can watch the next two minutes play out: who bleeds out, who comes to.
`--font mono-large` for bigger text, `--font square16` for classic square tiles.

## Quick start

```bash
pip install -e ".[ui,dev]"        # numpy, python-tcod, pytest
python -m rotengine play          # the game: pick a scenario and who to play
python -m rotengine play wick_vs_thugs --as "John Wick"   # straight into a fight
python -m rotengine list          # scenarios
python -m rotengine arena hulk_vs_squad --map            # watch one fight (+120s aftermath);
                                                          # prints its seed, replay with --seed N
python -m rotengine arena speedster_vs_squad --summary    # just the outcome and injuries
python -m rotengine arena wick_vs_thugs --runs 200       # balance statistics
python -m rotengine validate --mod example_mod           # check content + mods
python -m pytest
```

## What's in the box

| | |
|---|---|
| **3d6 everything** | Roll-under checks, crits, margin of success. Skill buys called shots, feints and aimed shots, and the AI spends it by expected value. |
| **Bodies** | Hit locations, crippling, fractures, severing, organ destruction, blood volume, external and internal bleeding, pain, agony, knockdown and unconsciousness. You die from blood loss, a stopped heart or a destroyed brain, not from an HP bar. HP = ST, stamina = CON. |
| **Tempo** | Continuous time where each creature runs on its own clock. A tempo-8 speedster acts, reacts and recovers 8× as fast and hits with the momentum of it. |
| **Tactics** | Facing (no defense from behind), a reaction window that punishes being mobbed, cover, stray rounds that hit bystanders, fatigue, first aid. |
| **Threshold damage** | DR subtracts, so a rifle mostly bounces off a DR 25 hide, and a ST 60 punch ignores a vest. |
| **Voxel world** | z-levels, per-voxel walls and floors with HP/DR, bullets through glass and drywall, knockback through walls, falling, and structural collapse. |
| **JSON content** | Materials, bodies, items, creatures, traits, statuses and powers, with `copy-from`/`relative`/`extend`/`delete` inheritance and mods with dependencies. |
| **Effect scripting** | EOC-style effect lists with conditions, values and hooks (`on_damaged`, `on_second`, `on_kill`). Python plugins can register new ops. |
| **Arena** | ASCII scenario maps, seeded and deterministic runs, batch win rates. |

## Current balance (100 seeded runs each)

| Scenario | Result |
|---|---|
| Street fight: two average people with knives | ~50/50; the loser is usually unconscious, occasionally bleeds out later |
| The Hulk (ST 60, DR 25) vs 10 armoured riflemen | Hulk wins 100%; soldiers mostly end up unconscious with broken ribs |
| John Wick (guns 20) vs 8 armed thugs | Wick wins ~82%; most thugs die of blood loss after the fight |
| Speedster (tempo 8, knife) vs 6 armoured riflemen | Speedster wins ~52%; loses to simultaneous bursts |
| Blink assassin vs 6 guards, 2 with pistols | Assassin wins ~9%, usually after several kills. Deliberately hard; needs stealth |

Map glyphs: creatures by their template glyph, `&` someone down (unconscious), `%` a
corpse, dropped weapons by their item glyph. Terrain: `#` concrete/brick, `|` drywall,
`=` glass, `O` wooden pillar, `X` stairs, `.`/`_`/`,` floors, blank = open air.

## Layout

```
rotengine/   dice, content, body, creature, world, effects, combat, sim, ai, arena, render
data/core/   base game content (JSON)
data/mods/   mods: a folder with modinfo.json + JSON files
data/scenarios/  arena maps
docs/DESIGN.md   architecture, rules, modding reference, roadmap
```
