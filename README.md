# RotEngine

A simulation-first ASCII roguelike combat engine. It takes its freedom of approach from
Dishonored and Deus Ex, its brutal and unfair bodies from GURPS, Dwarf Fortress and
RimWorld, and its modding from CDDA's JSON and EOCs.

There are two ways to play: the **arena** (pick a scenario or build a custom fight,
pick who you play, fight it out) and **roguelike mode**: take a character down six
generated floors of a black site, carrying every wound with you, patching yourself
up with whatever medicine you find, getting better at what you practise, with one
life and one save. The same engine also runs headless to batch-simulate fights for
balance. See [docs/DESIGN.md](docs/DESIGN.md) for the
architecture, the rules, and the roadmap.

## Playing

| Key | |
|---|---|
| arrows / numpad / `hjklyubn` | move; walk into an enemy to hit them |
| `f` | attack menu: target, attack, hit location, feint, aim, with the real odds |
| `F` | quick attack with the best option (steps closer if out of reach) |
| `Tab` | cycle target |
| `.` / numpad 5 | wait half a second (of your own time) |
| `r` / `m` / `z` / `g` | reload / first aid / drop prone or stand / pick up (a live grenade first, then a weapon, then anything throwable) |
| `s` | sneak: half speed, near silent, much harder to spot |
| `G` / `L` | grab someone next to you *by something*: neck, body, an arm, a leg, or their weapon (risky), with the odds for each. `G` again: what that grip allows (choke, strangle, snap; wrench, crush, tear off; trip, take down, disarm; bear hug). Move to drag; `L` let go |
| `w` | swap to your other weapon |
| `t` | throw a grenade, flashbang, smoke, Molotov... (cursor to aim, with your odds) |
| `T` / `P` | hurl the person you're holding / plant a breaching charge (then a direction) |
| `B` / `c` | smash a wall, door or window / close a door (then a direction); walk into doors to open them |
| `v` | show whole vision cones (an arrow in front of each enemy always shows which way they face) |
| `p` | powers |
| `<` `>` | stairs up / down; `>` on a stairwell (yellow `>`) takes you to the next floor |
| `[` `]` | view the level below / above |
| `x` | look: inspect terrain and anyone's wounds |
| `M` | message log: scroll back through everything you saw and heard |
| `i` / `a` / `@` | inventory (wield, wear, use, throw, drop) / use medicine / character sheet (skills, wounds) |
| `R` | rest: heal until something happens (roguelike) |
| `?` / `Esc` | help / quit to menu (a run is saved) |

Time only moves when you act, and it moves by the cost of what you do in *your*
time: play the speedster and everyone else is in slow motion. When the fight ends
you can watch the next two minutes play out: who bleeds out, who comes to.
`--font mono-large` for bigger text, `--font square16` for classic square tiles.

## Quick start

```bash
pip install -e ".[ui,dev]"        # numpy, python-tcod, pytest
python -m rotengine play          # the game: scenarios, custom fights, roguelike runs
python -m rotengine run --as operative   # straight into a new roguelike run (--continue to resume)
python -m rotengine mapgen --depth 4     # print a generated floor
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
| **Physics** | Doors (locked ones get breached), smashing walls, throwing things and people, grenades with real fuses (throw them back), flashbangs, smoke that blocks sight, tear gas, fire that spreads through wood and burns floors out from under you. |
| **Grappling** | Grab a specific part, then work on it: choke or strangle a neck, wrench or crush an arm, trip a leg, bear-hug the body, grab the gun itself (a miss can get you shot). Holding the gun arm stops the gun. Armor doesn't stop a joint lock. A normal person breaks an arm; the Hulk tears it off and beats the next soldier with it. Severed limbs and heads are items you can swing and throw. |
| **Stealth** | Per-enemy awareness from 3d6 Perception vs Stealth, light and darkness (shootable lamps), noise that travels and is muffled by walls, alarms, guards finding bodies, patrols, no defense against the unseen, chokeholds, dragging bodies. |
| **Threshold damage** | DR subtracts, so a rifle mostly bounces off a DR 25 hide, and a ST 60 punch ignores a vest. |
| **Voxel world** | z-levels, per-voxel walls and floors with HP/DR, bullets through glass and drywall, knockback through walls, falling, and structural collapse. |
| **JSON content** | Materials, bodies, items, creatures, traits, statuses and powers, with `copy-from`/`relative`/`extend`/`delete` inheritance and mods with dependencies. |
| **Effect scripting** | EOC-style effect lists with conditions, values and hooks (`on_damaged`, `on_second`, `on_kill`). Python plugins can register new ops. |
| **Arena** | ASCII scenario maps, seeded and deterministic runs, batch win rates. |
| **Roguelike** | Six floors generated from JSON room prefabs and palettes (warehouse, offices, bunker, the vault and its Commander). Wounds, blood and broken bones carry between floors; bodies heal slowly, splinted breaks knit, unsplinted ones don't. Medicine items (dressings, splints, painkillers, adrenaline, trauma kits, blood bags). Skills improve with use. Autosave, permadeath. |

## Current balance (100 seeded runs each)

| Scenario | Result |
|---|---|
| Street fight: two average people with knives | ~50/50; the loser is usually unconscious, occasionally bleeds out later |
| The Hulk (ST 60, DR 25) vs 10 armoured riflemen | Hulk wins 100%; soldiers mostly end up unconscious with broken ribs, the rest missing arms or heads |
| John Wick (guns 20) vs 8 armed thugs | Wick wins ~82%; most thugs die of blood loss after the fight |
| Speedster (tempo 8, knife) vs 6 armoured riflemen | Speedster wins ~52%; loses to simultaneous bursts |
| Blink assassin vs 6 guards, 2 with pistols | Assassin wins ~5-9%, usually after several kills. Deliberately hard in an open fight |
| Night infiltration: the operative vs 6 guards who don't know you're there | For playing: keep to the shadows, choke them out, hide the bodies |
| Breach and clear: 4 SWAT (grenades, flashbangs, smoke, charges) vs 6 barricaded occupants (Molotovs) | SWAT wins ~95%, usually losing people to shotguns and their own grenades |

Map glyphs: creatures by their template glyph, `&` someone down (unconscious), `%` a
corpse, dropped weapons by their item glyph. Terrain: `#` concrete/brick, `|` drywall,
`=` glass, `O` wooden pillar, `X` stairs, `.`/`_`/`,` floors, blank = open air.

Enemy markings: an arrow in front of each enemy shows which way they face (red if
they know you're there). Background: amber = suspicious, red = has spotted you,
magenta = aiming at you. Your target is black on bright yellow. Enemies you can see
on the level above are drawn purple where they stand, below blue.

## Layout

```
rotengine/   dice, content, body, creature, world, effects, combat, grapple, physics,
             perception, actions, sim, ai, arena, render, fov, mapgen, roguelike,
             training; ui/ (python-tcod)
data/core/   base game content (JSON); data/core/dungeon/ the roguelike's prefabs,
             palettes, spawn groups and dungeon
data/mods/   mods: a folder with modinfo.json + JSON files
data/scenarios/  arena maps
docs/DESIGN.md   architecture, rules, modding reference, roadmap
```
