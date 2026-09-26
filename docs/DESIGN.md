# RotEngine design

## 1. The pillars, and what each one forces technically

| Pillar | Technical consequence |
|---|---|
| Freedom (Dishonored / Deus Ex) | Systems, not scripts. Powers, terrain and AI all run through the same generic rules, so unplanned combinations still work. |
| Brutal bodily realism (GURPS / DF / RimWorld) | Damage goes to *body parts*, not just an HP bar. Wounds have lasting effects: crippling, bleeding, shock, unconsciousness. |
| Low-level fights feel desperate | 3d6 bell curve and low HP (= ST). Two average people have a real chance to maim each other in seconds. |
| Powers are obvious and devastating | Damage is subtracted by DR (a threshold) rather than reduced by a percentage, and ST damage grows superlinearly. Big numbers win completely rather than slightly. |
| Skill ceiling (John Wick) | Margin of success is a resource: called shots, feints (deceptive attack), rapid-fire extra hits. Experts get more *options*, not just +% to hit. |
| Deep modding (CDDA) | Almost all content is JSON, and behaviour is an effect language interpreted by the engine. Python plugins cover the rest. |
| Physical world | A voxel world with material properties, knockback, penetration, falling and structural collapse. |

The main architectural rule: **the simulation is headless and deterministic.**
`Sim` never draws anything. The arena CLI, the batch balancer, the tests and the
future tcod UI all drive the same object. With a seed, a fight replays exactly. That
gives you balance tests, reproducible bug reports, and later replays or netplay.

```
 data/*.json ──► Content (load, inherit, validate) ──┐
                                                      ▼
            World (voxels) ◄── Sim (time queue, tick, hooks) ──► log / events
                                  │   ▲
                         ai.take_turn │ effects.run (powers, traits, statuses)
                                  ▼   │
                     combat (3d6, DR, wounds, knockback)
 frontends: arena CLI · batch runner · tests · (next) tcod UI · (later) roguelike mode
```

## 2. Stats and derived values

| Stat | Drives |
|---|---|
| **ST** | HP (= ST), muscle damage (thrust / swing), knockback resistance |
| **CON** | Stamina (= CON), knockdown / consciousness / death / bleeding rolls, speed |
| **DEX** | Speed, default skill level (untrained = DEX-4), dodge |
| **INT** | (planned) skill learning, tactics AI tier, tech/crafting checks |
| **WIS** | (planned) perception, willpower, fear and pain resistance |

* Speed = (DEX + CON) / 4. Move = floor(Speed) tiles/sec. Dodge = floor(Speed) + 3.
* Parry = skill/2 + 3 + weapon modifier.
* Muscle damage: thrust mean ≈ 0.35·ST − 2 and swing mean ≈ 0.55·ST − 2, converted to
  d6s (`combat.st_damage`). ST 10 punches for 1d-2; ST 60 punches for 5d+2.

HP = ST and STAM = CON (confirmed). Templates can buy extra of either with
`hp_bonus` / `stamina_bonus`. INT (learning, tactics, tech) and WIS (perception,
willpower, morale, pain) are confirmed as described above and not wired in yet.

## 3. Resolution

**Checks:** roll 3d6 ≤ target. 3–4 always succeed and 17–18 always fail. Crits on 3–4
(5 at 15+, 6 at 16+). Margin = target − roll.

**Attacks** (`combat.py`):
1. Effective skill = skill + accuracy + range penalty (−2 at 5 tiles, −4 at 10, −8 at 50)
   − shock + status modifiers + hit-location penalty − 2 per level of deceptive attack.
2. Hit if the roll succeeds. Rapid fire: 1 extra hit per `recoil` points of margin.
3. The defender rolls their best defense (dodge, or parry against melee), minus 1 per
   deceptive level. Each point of defense margin stops another hit. Unaware or stunned
   targets and critical hits get no defense.
4. Each hit: roll damage, subtract DR (armor + natural + part), multiply by the wound
   multiplier (damage type × location), then apply to the body.

**The attack planner** (`best_attack_plan`) computes expected injury per second for
every attack × location × deceptive-level combination, using exact dice distributions.
Nobody hard-codes "Wick aims for the head". A skill-20 shooter works out that the
vitals are worth −3, and a skill-11 thug works out that he isn't good enough. Any
modded creature gets this for free.

## 4. Bodies and wounds (`body.py`, `data/core/bodies.json`)

A body plan is a list of parts: hit `weight`, called-shot `hit_penalty`, `wound_mult`
per damage type, `tags` (`grasp`, `stance`), `cripple_at` / `destroy_at` as fractions of
max HP, `parent` (lose the arm and you lose the hand), `fatal_if_destroyed`,
`knockdown_mod`, `bleed_mult`.

* **HP pool:** every wound comes off HP. Limbs only count up to their crippling
  threshold: a shotgun to the hand wrecks the hand, not your life.
* **Crippling:** a crippled `grasp` part drops the weapon, and a crippled `stance` part
  puts you on the ground.
* **Severing / pulping:** `dismembers` damage types (cut, crush) destroy parts that
  take ≥ `destroy_at`. The skull caves in; the neck decapitates.
* **Shock:** −1 per HP/10 of injury (max −4) on your next action. High pain threshold ignores it.
* **Major wound** (> HP/2 in one hit): CON roll, with a skull −10 / face and vitals −5
  modifier, or be knocked down and stunned (fail by 5+: out cold).
* **HP ≤ 0:** CON roll every turn to stay conscious. At each −HP multiple, a CON roll
  or die. At −5×HP you're dead.
* **Bleeding:** cut/impale/pierce wounds bleed per second. Every 10s a CON roll halves
  it (critical: stops it). Fights end with people bleeding out after the shooting stops.

## 5. Scaling: why the Hulk and John Wick work

The balance numbers are checked by `tests/test_arena.py` and `--runs`:

* **Hulk:** DR 25 against a 5d6 rifle means only the far tail (≥ 26, about 3%)
  penetrates, and then only for a point or two. HP 60 plus regeneration absorbs even
  that. Going the other way, 5d+2 crushing against a DR 4 vest takes out a soldier per
  punch, and the knockback (damage ÷ (ST−2) tiles) throws bodies through drywall and
  off the mezzanine. Result: about 98% wins and never a loss; the remainder are
  timeouts in odd collapsed geometry. Rage (+1 ST per wound, capped) is a 10-line JSON trait.
* **Wick:** guns 20 at 6 tiles is 16 after range; −3 for the vitals leaves 13. A pistol's
  ×3 vitals multiplier means one hit usually drops a man. `gun_fu` makes his shots take
  0.6 s, so he acts faster than they can. Result: about 83% against 8 thugs.
* **Desperate scraps:** two average knife fighters have skill 11 against a dodge of 8 or a
  parry of 7. The loser usually ends up unconscious and bleeding. About 50/50.

The general rule for designing powerful beings: **give them thresholds (DR, huge ST,
speed, no-defense surprise) rather than multipliers.** Thresholds turn ordinary
threats into non-threats, which is what "obviously devastating" means.

## 6. The world (`world.py`)

Voxels in the Dwarf Fortress style. Each (x, y, z) has a **fill** (air, wall, glass,
stairs) and a **floor** (the slab you stand on, which is also the ceiling of z−1). Both
have per-voxel HP and DR from their material. Arrays are numpy `[z, y, x]`.

* **Line of sight / fire:** a 3D line. Opaque fills and floors block it; transparent
  solids (glass, grates) let it through but add DR and get damaged (windows shatter).
* **Penetration:** missed shots continue and chew through thin walls.
* **Knockback:** a body travels tile by tile. Hitting a wall damages both, and if the
  wall breaks the body keeps going. If it leaves a floor edge, it falls.
* **Falling:** 2d6 crushing per storey, landing prone.
* **Structural collapse:** `settle()` flood-fills support from z = 0 through walls and
  floors. Anything disconnected falls, and debris hits whoever is below. Break the
  mezzanine's pillars and the soldiers come down.
* **Movement:** 8-way plus climbable fills (stairs). Melee works across a staircase.

**Next for the world:** span limits (a floor can hang only N tiles from support), doors,
items on the ground, thrown objects and bodies, fire, smoke, gas and liquids as numpy
cellular automata, explosions with overpressure and fragments, light and noise maps
for stealth.

## 7. Modding reference

### Content types
`material`, `damage_type`, `body_plan`, `item`, `trait`, `status`, `power`, `creature`,
`tile_legend`. Each object needs `type` and `id`. A later mod replaces objects by id.

### Inheritance
```json
{"type": "creature", "id": "super_soldier", "copy-from": "soldier",
 "relative": {"stats": {"ST": 6}}, "extend": {"traits": ["high_pain_threshold"]},
 "delete": {"traits": ["combat_reflexes"]}}
```
`"abstract": true` marks a template that can't be spawned.

### Mods
`data/mods/<id>/modinfo.json` holds `{"id", "name", "dependencies": ["core", ...]}` and
any number of JSON files. Load with `--mod <id>` or a scenario's `"mods": [...]`.
`python -m rotengine validate` reports unknown references, unknown effect ops and
missing fields, with file names.

### Effects
An effect list is a list of single-op objects:

| Effects | Conditions | Values |
|---|---|---|
| `if` / `then` / `else` | `roll` `{stat\|skill\|vs, who, mod}` | number, `"2d6+1"` |
| `message` `"{self} hits {target}"` | `compare [a, op, b]` | `{stat: {stat, who}}` |
| `damage {amount, type, location, who}` | `chance p` | `{var: name}` |
| `heal`, `stop_bleeding {fraction}` | `has_status`, `has_trait` | `{hp: {who}}`, `{stamina: {who}}` |
| `modify_stat {stat, amount, max_bonus}` | `has_target`, `sees_target` | `{distance_to_target: {}}` |
| `add_status {id, duration_ms}` / `remove_status` | `target_reachable` | `{count_enemies: {radius}}` |
| `knockback {distance}` | `and`, `or`, `not` | `add`, `mul`, `min`, `max`, `neg` |
| `for_each_enemy {radius, do}` | | |
| `teleport {range}`, `leap {range, max_rise}` | | |
| `attack {surprise, skill_bonus, damage_bonus}` | | |
| `damage_terrain {radius, amount, z_offsets}`, `set_var` | | |

**Hooks** on traits and statuses: `on_damaged` (var `damage`), `on_second`, `on_kill`.
**Powers** have `cost.stamina`, `time_ms`, `effects`, and an `ai_condition` that NPCs use
to decide when to fire them. Modded powers get used sensibly without new AI code.

**Python plugins** extend the language:
```python
from rotengine.effects import effect
@effect("ignite")
def ignite(args, ctx): ...
```
(Loading plugin `.py` files from mod folders is on the roadmap. The registry is already there.)

## 8. Roadmap

1. **Playable arena (next).** A python-tcod frontend: map with z-level switching (`<` `>`),
   message log, look/inspect (body status per part), and a player-controlled creature
   with an action menu: attack option → location → feint levels, showing hit odds from
   `p_success`. An arena setup screen to pick creatures and teams and place them on a
   map. The player is just a creature whose `take_turn` waits for input.
2. **Stealth and perception.** Facing, awareness states (unaware → suspicious → alert),
   light and noise propagation over the voxel grid, and takedowns (the `surprise` path
   already exists). Grappling, chokes and non-lethal options.
3. **More physics.** Items on the ground, throwing (including creatures: a Hulk throwing a
   soldier is `knockback` with a chosen direction), doors and breaching, fire/gas/fluids,
   explosives.
4. **Roguelike mode.** Map generation from JSON prefabs (CDDA-style mapgen with
   palettes), levels and biomes, save/load, persistent injuries and medicine, skill
   growth through use.
5. **Breadth.** More body plans (quadrupeds, robots with component "organs", swarms),
   a library of powers (telekinesis, shields, time dilation, possession), energy
   weapons, and ammo types with armor divisors.

**Performance plan:** Python is fine for turn logic. Keep bulk work in numpy (FOV,
fields, collapse), keep an "active bubble" of simulated space in the roguelike mode,
profile before optimising, and move any proven hot loop to numba/Cython (or Rust via
PyO3) behind the same API.

## 9. Known gaps / tuning notes

* The assassin scenario is hard (~10%), and that is intended: teleporting doesn't
  help against bullets you can't react to. Stealth is how she should win.
* The AI is deliberately basic: best expected-value attack, reload, close distance,
  seek line of sight. It has no cover use, retreat or morale yet (WIS will drive morale).
* Floors can span any distance from a support. Span limits are needed for realistic collapse.
