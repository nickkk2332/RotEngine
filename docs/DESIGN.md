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
| **CON** | Stamina (= CON), knockdown / consciousness / clotting rolls, speed |
| **DEX** | Speed, most combat skill defaults, dodge |
| **INT** | First aid (default INT−4). Planned: skill learning, tactics AI tier, tech/crafting |
| **WIS** | Resisting agony; every point over 10 takes 0.5 off the pain penalty. Planned: perception, morale |

HP = ST and STAM = CON (confirmed). Templates can buy extra of either with
`hp_bonus` / `stamina_bonus`.

* Speed = (DEX + CON) / 4. Move = floor(Speed) tiles/sec. Dodge = floor(Speed) + 3.
* Parry = skill/2 + 3 + weapon modifier.
* Muscle damage: thrust mean ≈ 0.35·ST − 2 and swing mean ≈ 0.55·ST − 2, converted to
  d6s (`combat.st_damage`). ST 10 punches for 1d−2; ST 60 punches for 5d+2.
* Skill defaults come from `skill` JSON objects (`{"stat": "INT", "default": -4}`), or
  DEX−4 for skills with no definition.

## 3. Time and tempo

Time is continuous, in milliseconds of world time. Each creature acts, pays the
action's cost **in its own time**, and acts again when that has elapsed. Own time
converts to world time by dividing by the creature's **tempo** (1 for humans; traits
and statuses multiply it).

| Action (own time) | Cost |
|---|---|
| Attack | weapon `time_ms` (1 s default; `gun_fu` ×0.6 for shooting) |
| Aim | 1 s: adds the weapon's accuracy to the next shot at that target |
| Move one tile | 1 s ÷ Move (1 tile/s limping, 2 s crawling) |
| Get up | 1 s |
| Reload | 2 s pistol, 3 s rifle |
| First aid | 5 s |

A tempo-8 speedster therefore gets 8 actions for every human action. Tempo also
reaches into every other time-based rule:
* **Reaction window:** a defense uses up 1 s of the defender's own time (125 ms for the
  speedster). Each further defense inside the window is at −2.
* **Seeing it coming:** defense is +2 per doubling of the defender's tempo over the
  attacker's, and −2 per halving. That's ±6 between a human and a tempo-8 speedster.
  With a 4× edge a rear attack counts as a side attack; with 16× it counts as frontal.
* **Subjective statuses** (stun, agony) wear off on the creature's own clock.
* **Momentum:** traits with `momentum_exponent` scale muscle damage by tempo^exp.
  Super speed uses 0.5, so ST 11 hits like ST 31.
* Stamina recovery runs on own time too.

A 1-second world tick handles physiology for everyone: bleeding, clotting,
hypoxia, fainting and waking, stamina recovery, statuses running out, `on_second` hooks.

## 4. Resolution

**Checks:** roll 3d6 ≤ target. 3–4 always succeed and 17–18 always fail. Crits on 3–4
(5 at 15+, 6 at 16+). Margin = target − roll.

**Attacks** (`combat.py`). Effective skill = skill
+ weapon accuracy (only if the shooter aimed at this target last action)
+ range penalty (−2 at 5 tiles, −4 at 10, −8 at 50)
+ cover (−1 per blocked corner of the target's tile, −4 when mostly hidden)
+ hit location (vitals −3, face/neck −5, skull −7)
− 2 per level of deceptive attack
− shock − pain − blood loss − fatigue − status modifiers (prone: −4 in melee, and
shooting at a prone target is −2).

**Hit:** if the roll succeeds. Rapid fire gives one extra hit per `recoil` points of margin.

**Defense** is the best of dodge, or parry against melee, then modified:

| Situation | Modifier |
|---|---|
| Attack from the rear 3/8 of the circle | no defense |
| Attack from the side | −2 |
| Each earlier defense still inside the reaction window | −2 |
| Relative tempo | ±2 per doubling |
| Stunned or in agony | −4 |
| Prone | −3 |
| Pain | half the pain penalty |
| Blood loss / fatigue | the same penalty as for attacks |
| Unconscious, taken by surprise (blink), or a critical hit | no defense |

A successful defense stops one hit, plus one more per point of margin.

**Rounds are physical.** A missed shot that fails by no more than the cover penalty
hits the cover, and punches through into the target if it beats the cover's DR. Every
round that misses, gets dodged or isn't part of the hit count keeps flying. It can hit
anyone in its path (3d6 ≤ 9), downed people included, then damages and possibly
penetrates walls. Shooters won't fire with a teammate in the line of fire.

**Damage:** roll, subtract DR (part + natural + armor covering that part, per damage
type), multiply by the wound multiplier (damage type × location). Skull ×4; vitals ×3
for piercing weapons; neck ×2 for cuts; cut ×1.5, impale ×2.

**The attack planner** (`best_attack_plan`) computes expected injury per second of
the attacker's time for every attack × location × feint level × aim-or-fire-now option,
using exact dice distributions. It ignores overkill. Wick aims for the vitals; a
skill-11 thug takes a second to aim and shoots center mass.

## 5. Bodies, wounds and death (`body.py`, `data/core/bodies.json`)

The design rule: **incapacitation is common, and death has a physical cause.**
Losing HP knocks people out. It kills directly only when the body is literally torn
apart (−5×HP). Otherwise death comes from one of three things:

| Cause | How |
|---|---|
| **Brain or neck destroyed** | Skull or neck damage past `destroy_at` (1.5× / 2× HP, any damage type). Instant. |
| **Bled out** | Blood below 50% starts brain hypoxia (faster the lower it goes). Hypoxia 100 = death. |
| **Brain death** | Vitals destroyed means cardiac arrest: seconds of consciousness, then hypoxia at 0.5/s (about 3.5 minutes). |

**The layers of a wound:**
* **HP (trauma):** limbs only count up to their crippling point, so a leg can't take
  you below that no matter what hits it.
* **Crippling:** an arm or leg past HP/2, a hand or foot past HP/3. A crippled arm
  disables its hand (parts check their parents), so the weapon drops to the floor. A
  crippled leg puts you down; with no working legs you crawl.
* **Fractures:** blunt damage (types with `fractures`) past a part's `fracture_at`
  breaks bones: ribs, skull, jaw, pelvis, limbs. Each fracture adds pain; a broken limb
  is crippled.
* **Destruction:** cutting or crushing takes off limbs at `destroy_at`, along with their
  hands or feet, and severed limbs bleed arterially. Parts marked `organ`
  (skull/brain, neck, vitals) can be destroyed by any damage type.
* **Bleeding:** external bleeding is `injury/HP × type bleed × part bleed_mult`, in % of
  blood per second. Cuts and stabs bleed most, blunt force barely at all. Each 10 s a CON
  roll halves it (at −4 for arterial bleeds over 1%/s). First aid takes 5 s and reduces
  it to a quarter, or stops it on a good roll; small bleeds are easier to treat and
  arteries harder.
* **Internal bleeding:** deep torso, abdomen and vitals wounds (`internal.at`) bleed
  inside and never clot. First aid can't touch it, so it needs surgery or a healing
  power (`stop_bleeding` with `"internal": true`). A gut wound is a slow death sentence
  without help.

**Pain and consciousness:**
* **Shock:** −1 per HP/10 of injury (max −4), on the next action only.
* **Pain:** an ongoing penalty of 4 × (fraction of HP lost) + 1 per fracture, capped at
  −6. It's halved by high pain threshold and reduced by 0.5 per point of WIS over 10.
  It applies fully to attacks and half to defense.
* **Agony:** a wound of HP/3 or more, a fracture, or a lost part means a WIS roll; fail
  and you're doubled over and helpless for 2 s (4 s if you fail badly).
* **Knockdown:** a wound over HP/2 means a CON roll (skull −10, face and vitals −5).
  Fail and you're stunned and prone; fail by 5 and you're out cold.
* **Unconsciousness:** dropping to ≤ 0 HP (or past another −HP) means a CON roll
  or pass out, and every turn at ≤ 0 HP needs another roll. Blood under 70% means a
  CON roll every 10 s; under 60% you're out. You wake (a CON roll every 5 s) only
  with HP > 0, blood ≥ 60% and a beating heart.
* **Fatigue:** melee swings (0.2–0.3), shots (0.05) and running (0.05/tile) cost
  stamina. Recovery is 0.1/s in a fight and 0.5/s resting. Below 1/3 stamina you're
  winded (−1, half move); at 0 exhausted (−3, quarter move); at −½ you collapse.

**After the fight** the arena keeps the clock running (`aftermath_s`, default 120).
The wounded bleed, pass out, wake up or die, and the winners bandage each other. The
report lists who is dead and why.

## 6. Scaling: why the Hulk, John Wick and the speedster work

The balance numbers are checked by `tests/test_arena.py` and `--runs`:

* **Hulk:** DR 25 against a 5d6 rifle means only the far tail (≥ 26, about 3%)
  penetrates, and then only for a point or two. HP 60, regeneration and `tireless`
  absorb the rest. Going the other way, 5d+2 crushing against a DR 4 vest breaks ribs
  and knocks soldiers out, and the knockback (damage ÷ (ST−2) tiles) throws bodies
  through drywall and off the mezzanine. Result: 100% wins. Most soldiers survive the
  fight unconscious; the dead mostly bled out afterwards, and about 1 in 6 were torn
  apart outright.
* **Wick:** guns 20 at 6 tiles is 16 after range; −3 for the vitals leaves 13. A 9 mm
  round to the heart and lungs usually stops the heart. `gun_fu` makes his shots take
  0.6 s. Result: about 82% against 8 thugs, who mostly die in the aftermath.
* **Speedster** (tempo 8, ST 11, a knife): he acts 8× as often, defends at +6 against
  them, and hits like ST 31. Six riflemen at −6 to defend and swamped in their reaction
  windows go down in about two seconds. Result: about 52%. When he loses, it's to two
  bursts landing in the same instant: he's still a human body. The tempo number is
  the knob for how "high powered" a speedster is.
* **Desperate scraps:** two average knife fighters have skill 11 against a dodge of 8 or a
  parry of 7. The loser ends up unconscious, and sometimes bleeds out later. About 50/50.

The general rule for designing powerful beings: **give them thresholds (DR, huge ST,
tempo, no-defense surprise) rather than multipliers.** Thresholds turn ordinary
threats into non-threats, which is what "obviously devastating" means.

## 7. The world (`world.py`)

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

## 7½. Perception and stealth (`perception.py`)

Scenarios with `"start_alert": false` start with nobody knowing anybody is there.
Arena scenarios, and any sim built in code, start with everyone aware.

**Awareness.** Every creature keeps a 0–100 meter per enemy. At the start of its turn
it looks at each enemy in view (not behind it): a 3d6 **Perception** roll (WIS; −1
calm, +1 searching, +2 in combat) against the enemy's **Stealth** (skill; −4 unless
sneaking, −6 for 2 s after firing). Winning adds 25 + 20 per point of margin; a
narrow loss is a glimpse (+10). At 30 the observer is suspicious and goes to look.
At 100 it has spotted you: it knows where you are, shouts, and fights. Out of sight,
the meter drains, and after 20 s unseen a spotted enemy drops back to "searching" at
its last known position. The AI never uses positions it hasn't perceived.

**Sight modifiers:**

| | |
|---|---|
| distance | half the ranged penalty, +3 within 2 tiles, +1 within 5 |
| light | bright +2, dim −2, dark −6, pitch black −10 (night vision adds 0.4 light) |
| angle | side arc −4, rear arc can't see |
| target | prone −2, moved in the last second +2, cover as for shooting |

**Light.** Scenario `ambient_light` (0–1) plus materials with `"light": radius`
(lamps), which light what they can see. Lamps are destructible: shoot them out. The
light map is recomputed when terrain changes.

**Noise.** Actions emit sounds with a range in tiles:

| Sound | Range |
|---|---|
| sneaking / crawling | 1 |
| footsteps | 4 |
| melee | 5 |
| a struggle | 4 |
| suppressed shot | 7 |
| scream | 12 |
| crash (through a wall) | 12 |
| breaking glass | 12 |
| alarm shout | 16 |
| collapse | 30 |
| gunshot | 40 |

Out of line of sight a sound carries half as far. Listeners roll Perception to hear
it. Enemy noises raise suspicion and send them to look; an ally's shout of alarm
tells them where the intruder is. The player gets "You hear gunfire to the
north-east." Weapons set `"noise"`, powers set `"noise"`.

**Bodies.** A guard who spots a downed or dead ally (a Perception roll, like spotting
you) raises the alarm and goes to the body. So drag bodies into the dark.

**Surprise.** You can't defend against an attacker you haven't noticed: an NPC needs
awareness 100, a player needs to be able to make the attacker out. A knife from the
dark gets no dodge or parry. This replaced blink's special case; it's now general.

**Grappling.**

| Action | Rules |
|---|---|
| Grab (`G`) | Wrestling roll; the target defends if they noticed you. A downed or dead body is simply taken hold of. |
| While held | Can't move; −2 to attack, −3 to defend. |
| Choke (`G` again) | Each second: CON roll at −2 per second choked, or go limp for 20–60 s (a timed knockout, no injury). The victim can't shout. Keep squeezing and it becomes hypoxia, then death ("strangled"). |
| Struggle | Your max(ST, Wrestling) against their max(ST, Wrestling) + 2. −3 if taken from behind, −1 per second choked. |
| Drag | The holder walks and the held body follows, at double the move cost. |
| Let go (`L`) | |

**The log** in play only shows what your character witnessed: lines are tagged with
where they happened (`Sim.focus`), and sounds you heard arrive as private lines.

## 7¾. Doors, throwing, explosives, fire and gas (`physics.py`)

**Doors** are materials with `"door": <the other state>`. A closed door is solid and
opaque; walking into it opens it (0.5 s, a little noise); `c` closes it. A `"locked"`
door won't open and has to be breached. Pathfinding goes through closed doors.
Materials marked `"breachable"` (doors, wooden walls, drywall, glass) count as
passable-at-a-cost when an AI can find no other way in: it plants a charge if it
carries one (then runs), or kicks and smashes. **Smashing** (`B`) hits terrain with
your best melee attack. Crushing force counts double against structures, so the
Hulk goes through brick.

**Throwing** (`t`): a Throwing roll (DEX−3) with the usual range penalty.

| Rule | |
|---|---|
| Range | ST × 1.5 ÷ weight tiles |
| A miss | Scatters up to 4 tiles |
| Obstacles | Walls stop a throw; windows shatter and let it through |

People can be thrown too:
- Grab someone (`G`), then hurl them (`T`) in a direction for
  max(1, (ST − 2 × their ST) ÷ 4) tiles. A human manages a judo throw; the Hulk
  throws soldiers across the room.
- The flight is knockback: into walls (which may break), other people, through
  windows and off ledges, with landing damage on top.
- The Hulk's JSON `hurl` power aims the thrown soldier at another soldier.

**Explosives** are items with an `"explosive"` spec and either `"throwable":
{fuse_ms}` or `"plantable"`. The fuse follows the item wherever it goes: it goes off
where it lies, in the hand of whoever picked it up, or in their pocket. So you can
throw one back.

| Part | Effect |
|---|---|
| `damage` / `radius` | Blast: crushing damage ÷ (1 + distance), with the knockback that brings |
| `fragments` | `count` pieces, each hitting with odds that fall with distance². Halved if prone; walls stop them |
| `flash` | Anyone facing it rolls CON or is stunned; everyone is dazzled (−6 Perception, −4 attack) |
| `gas` / `fire` | Fill or ignite the area |
| `terrain` | Damage to walls and floors around it (breaching) |
| `noise` | An explosion carries 60 tiles |

The AI:
- throws grenades at enemies in cover, out of sight, or bunched up, when it has a clear
  line (a window counts) and no friend is near the landing spot;
- runs from any live explosive it can see (or goes prone when it can't get clear);
- walks out of fire.

**Fire** is a 0–10 intensity field.

| | |
|---|---|
| Fuel | `"flammable"` materials, walls and floors (`"floor_flammable"` for finished floors) |
| Consumes | The wall's or floor's HP (a burned-out floor drops whoever stands on it) |
| Spreads | To neighbours at (heat ÷ 80) × flammability per second |
| Other effects | Smoke, and light over 4 tiles |
| Standing in it | 1d burn per 3 intensity, and a roll to catch fire (1d a second until put out; going prone may smother it) |
| Pace | A Molotov in a wooden house: a room fire in about a minute, the whole house in 2–3 |

**Gas** is a JSON `"gas"` type with a concentration field.

| Key | Effect |
|---|---|
| `spread` | Diffusion into open neighbours |
| `rises` | Moves up through holes in floors |
| `decay` | Thins out over time |
| `opacity` | Blocks sight: smoke summed along a sight line hides what's behind it, for players and NPCs alike |
| `status` at `status_at` | Tear gas makes you choke: −3 attack, −2 defense, −3 Perception |
| `immune_trait` | A gas mask |

## 8. Modding reference

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
| `heal`, `restore_blood`, `stop_bleeding {fraction, internal}` | `has_status`, `has_trait` | `{hp: {who}}`, `{stamina: {who}}` |
| `modify_stat {stat, amount, max_bonus}` | `has_target`, `sees_target` | `{distance_to_target: {}}` |
| `add_status {id, duration_ms}` / `remove_status` | `target_reachable` | `{count_enemies: {radius}}` |
| `knockback {distance}` | `and`, `or`, `not` | `add`, `mul`, `min`, `max`, `neg` |
| `for_each_enemy {radius, do}` | | |
| `teleport {range}`, `leap {range, max_rise}` | | |
| `attack {surprise, skill_bonus, damage_bonus}` | | |
| `damage_terrain {radius, amount, z_offsets}`, `set_var` | | |

**Hooks** on traits and statuses: `on_damaged` (var `damage`), `on_second`, `on_kill`.
**Powers** have `cost.stamina`, `time_ms`, optional `cooldown_ms`, `effects`, and an
`ai_condition` that NPCs use to decide when to fire them. Modded powers get used sensibly without new AI code.

**Python plugins** extend the language:
```python
from rotengine.effects import effect
@effect("ignite")
def ignite(args, ctx): ...
```
(Loading plugin `.py` files from mod folders is on the roadmap. The registry is already there.)

## 9. Roadmap

1. **Playable arena (done).** `rotengine/ui/` (python-tcod). The player is a creature
   with `controller = "player"`: `Sim.advance()` runs the world until that creature's
   turn and returns, the UI turns a keypress into a function from
   `rotengine/actions.py` (the same verbs the AI uses) and hands its cost to
   `Sim.player_act()`. The attack menu lists `combat.attack_plans()`, the same options
   the AI planner scores. Field of view (`rotengine/fov.py`) uses combat's line of
   sight. UI screens are plain objects (`render(console)`, `on_key(key)`), so the tests
   drive them headlessly. The log only shows what your character witnessed or heard.
   Next for it: a map editor, and saving arena setups.
2. **Stealth and perception (done).** See section 7½ above. Next for it: carrying
   bodies over the shoulder, distraction (throwing things to make noise), light
   switches, doors, disguises, and AI that sneaks and flanks.
3. **More physics (done).** See section 7¾ above. Next for it: fluids (water, fuel,
   blood pools), heat and burns through walls, carrying items in the world (crates,
   barrels), vehicles.
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

## 10. Known gaps / tuning notes

* The assassin scenario is hard (~6%), and that is intended: teleporting doesn't
  help against bullets you can't react to. Stealth is how she should win.
* The AI is still basic: best expected-value attack or aim, reload, close in, seek line
  of sight, avoid friendly fire, bandage when safe, deal with armed enemies before
  crippled ones. It has no seeking cover, retreat, suppression or morale yet
  (WIS will drive morale).
* Floors can span any distance from a support. Span limits are needed for realistic collapse.
* Dropped weapons lie on the ground (`sim.items`) but nobody picks them up yet.
* Surgery (the fix for internal bleeding) is only possible through effects so far.

**Occupancy rules:** one living body per tile. Downed bodies block movement (walk
around the fallen); corpses don't. Someone who wakes up, lands or is knocked onto an
occupied tile is shifted to the nearest free one. A fall onto someone splits the
damage and knocks them down. Scenario spawns must be free, standable tiles.

**AI notes:** no aiming when just hurt or with an enemy within 2 tiles. A shooter whose
line is blocked by a teammate holds aim briefly instead of repositioning. Failed path
searches are remembered for 2 s (or until the terrain changes), and creatures with
nothing reachable hold position.

**Trait and status fields the engine reads:** `natural_dr`, `stat_mods`, `speed_bonus`,
`dodge_bonus`, `parry_bonus`, `move_mult`, `tempo`, `momentum_exponent`,
`action_time_mult`, `exertion_mult`, `pain_mult`, `pain_resist`, `knockdown_bonus`,
`hooks`. Statuses also: `prevents_action`, `attack_mod`, `melee_attack_mod`,
`ranged_attack_mod`, `ranged_target_mod`, `defense_mod`, `move_mult`, `subjective`.
Timed statuses expire at their exact time, not on the next world tick. Powers also
take `cooldown_ms`. A power whose effects abort (nowhere to land, out of range)
"fizzles" and can't be retried for 2 s.
