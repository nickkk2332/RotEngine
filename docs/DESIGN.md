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
  (skull/brain, neck, vitals) can be destroyed by any damage type. Severed arms, legs,
  hands, feet and heads land on the floor as items (see Grappling in 7½).
* **Per-type overrides:** a part's `by_type` block changes its texts and effects for one
  damage type: a wrenched neck is "neck snapped" with the `broken_neck` status, and a
  cut one ends in "decapitated". A part is wrenchable in a hold if it has a `by_type`
  entry for `wrench`.
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

**Grappling (`grapple.py`).** You grab a *thing*, then work on what you've got hold of.
`G` asks where (with the odds for each); `G` again, while holding, lists only what
that grip allows.

| Grip | Grab at | Moves |
|---|---|---|
| Neck | −5 | **choke** (sleeper hold), **strangle**, **snap** (wrench); the victim can't cry out |
| Body | 0 | **bear hug** (squeeze the ribs), take down, hurl, drag |
| Arm / hand | −2 / −4 | **wrench** (break or tear off), **crush**, throw down; a grip on the weapon hand or its arm can **disarm**, and they can't use that weapon while you hold it |
| Leg / foot | −2 / −4 | wrench, crush, **trip** (take down at +2) |
| Their weapon | −3 melee, −2 pistol, −1 long gun | **wrest** it away (no leverage bonus; win and it's yours). *Risky*: fail against someone who saw you coming and a blade cuts your hand, or a gun goes off at you point-blank. A blade ripped free through your grip slices your fingers |

* **Grab roll:** Wrestling at the location's penalty, then their defense if they
  noticed you. Against someone who doesn't see it coming there's no defense and the
  penalty is halved (they aren't guarding their throat): a trained operative gets an
  arm around an unaware guard's neck ~75% of the time. A miss alerts them. Downed or
  dead bodies are simply taken hold of.
* **Changing grip** is a new grab roll; a miss keeps the grip you had.
* **While held:** can't move; −2 to attack, −3 to defend. The grip ends if the part
  comes off, the weapon is dropped, or the two of you are separated.
* **Choke:** each second a CON roll at −2 per second choked, or limp for 20–60 s (no
  injury). Keep choking and it becomes hypoxia, then death ("strangled").
* **Strangle:** the same, but their CON roll is +2 (slower to put them out) and each
  second is also `squeeze` damage to the throat. A normal grip barely marks it; ST 20+
  crushes the windpipe (`crushed_windpipe`: out at once, dead in under 2 minutes); the
  Hulk crushes the neck.
* **Squeeze / crush / bear hug:** `squeeze` damage (thrust from ST, +technique; armor
  doesn't help; the part's `dr.squeeze` does) to the part you hold. It breaks bones
  but never tears anything off.
* **Take down:** slammed prone (thrust damage from the floor) and still held; pinned,
  they struggle at −2.
* **Struggle:** your max(ST, Wrestling) against theirs + 2 (leverage). −3 held from
  behind, −2 pinned, −1 per second choked. Against a grip on your weapon: your ST or
  weapon skill against their ST or Wrestling, no leverage.
* **Hurl (`T`) / drag / let go (`L`):** hurl throws them like knockback
  (`(ST − 2 × their ST) / 4` tiles); dragging costs double move time.

**The hold contest** (take down, disarm, wrest, wrench, squeeze): your max(ST, Wrestling)
+ 2 for the leverage (+3 more from behind) against their max(ST, Wrestling), minus the
usual penalties and 1 per second they've been choked. Someone unconscious doesn't
resist.

**Wrenching, snapping and tearing.** `wrench` is a JSON damage type with three
properties: it fractures, it `ignores_armor` (a vest doesn't stop an arm lock, though
natural toughness does, so nobody wrenches the Hulk), and it's `sudden`, meaning a part
only comes off if a single pull does its whole `destroy_at` in one go. Joints resist
with their own DR (`dr.wrench`: neck 5, knee 3, elbow 2, wrist and ankle 1), and the
neck takes ×2. What that works out to:

| Who | Arm | Neck |
|---|---|---|
| Average guard (ST 11) | breaks in ~3 tries | ~13 tries on someone out cold |
| Trained operative (ST 11, Wrestling 14) | breaks in ~2 | ~6 on someone out cold: a faster silent kill than strangling |
| ST 20 | 1–2 | ~2: snapped |
| ST 26 | tears off ~60% of the time | snapped in one |
| The Hulk (ST 60) | comes right off | the head comes off |

A **snapped neck** (fracture) applies the `broken_neck` status: paralysed, not
breathing, unconscious, and dead of hypoxia in about 3½ minutes ("broken neck"). A
destroyed neck is instant. Cranking a human arm again and again breaks it; it never
tears it off. When the part you hold comes off, you're holding it, not them.

**Severed parts are items.** A part with `sever_item` leaves one behind when it comes
off, by any means: a sword takes an arm off, a blast takes a leg, a strong hold
takes a head. It is named after its owner ("soldier 3's head"). If the tearer has a
free hand they keep hold of it. An arm is a club (`clubs`, swing crush, parry −2), a
leg a two-handed one, and all of them can be thrown (`t`), with thrown damage from ST.
Anyone can pick them up (`g`); ordinary NPCs don't pick up body parts to fight with.

**Brutes.** A creature template with a `"grapple"` block (`chance` to grab someone in
reach instead of hitting them; `tear`, the chance to go for the gun arm) fights like
the Hulk: it grabs the gun arm and tears it off, beats the next soldier with it or
throws it at one out of reach; otherwise it grabs the neck (if the odds are fair) or
the body, and twists or crushes. Everyone else lets go of holds.

**Body plan fields:** `grab` (can be grabbed), `choke` (a grip there chokes, strangles
and silences), a `by_type.wrench` entry (can be wrenched), `dr.wrench` / `dr.squeeze`
(how the part resists). A new body plan gets grappling by setting these.

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

## 7⅞. Roguelike mode (`mapgen.py`, `roguelike.py`, `training.py`)

**A run** (`roguelike.Run`) is one character going down through a dungeon's floors. Each
floor is its own `Sim`, generated fresh; the player creature object is carried down
(`Sim.adopt`) with its body, gear, skills and statuses intact, and the clock carries on
(`Sim(start_time=...)`). A bleed you didn't stop upstairs is still bleeding downstairs.
Floors run with `endless` (nothing ends until the player does), `healing` (bodies
recover over time) and stealth rules (`start_aware` off: guards stand posts or walk
beats and don't know you're there). The way down is a floor material with
`"exit": "down"`: stand on it and press `>`. The run autosaves on every new floor and
on quitting (or closing the window); death deletes the save.

**The Black Site** (`data/core/dungeon/`): six floors under a harbour warehouse, from
dock crews with pistols and knives (warehouse), through site security (offices), to
contractors with rifles, grenades and steel doors (bunker), to the Commander in the
vault. Take him down (dead or out cold) to win. Playable: the operative, John Wick,
the assassin, the speedster, a soldier, a street tough, the Hulk.

**Map generation.** A floor is a grid of cells (18×12); some get a prefab room, maybe
mirrored or rotated. Rooms are joined by a minimum spanning tree plus a few loops,
with corridors carved through rock by A* (reusing corridors is cheaper), from door
slot to door slot. Used slots become doors (by `door_chance`) or open doorways; unused
ones stay wall. The player starts in one room and the exit goes in the room farthest
away by corridor. A boss floor puts the boss's prefab in first and starts you as far
from it as possible. Any level where a spawn, item or the exit can't be walked to is
thrown away and rolled again. Everything is deterministic from the seed
(`python -m rotengine mapgen --depth 4 --seed 9` prints one).

| Content type | What it is |
|---|---|
| `prefab` | A room drawn in ASCII (`rows`), with `tags` (which floors use it), `weight`, optional `depth`, `palettes` and an inline `legend`. Needs door slots on its outer wall, not in corners. `mirror` / `rotate` default true. |
| `palette` | Character → tile. Besides `fill` / `floor`: `door_slot`, `monster` (`"level"` or a group id, with `chance`), `item` (same), `exit`. The shared `rooms` palette gives `d` door slot, `M` someone, `I` loot, `E` exit spot, `c` furniture, `g` fencing. |
| `monster_group` / `item_group` | Weighted `entries` (`creature` / `item`, `weight`, optional `depth: [a, b]`). |
| `dungeon` | `name`, `description`, `characters` (who you can play), and `floors`: each with `depth: [a, b]`, `name`, `size`, `rooms`, `loops`, `prefab_tags`, `ambient_light`, `door_chance`, `door` / `doorway` / `rock` / `corridor` / `exit_floor` tiles, `monsters` and `items` (`group`, `extra` count), `patrol_chance`, and on the last floor `final` and `boss` (`creature`, `prefab_tag`). |

**Recovery** (`Body.recover`, every second, on `healing` floors). Compressed a long way
from real life but in the same order: trauma (HP) comes back at 0.06% of max HP per
second at CON 10, about half an hour from nothing; blood returns at 0.02%/s, but only
once all bleeding has stopped; a crippled (not broken) limb works again when its
damage fades; a **splinted** break knits in 30 minutes of game time, and an unsplinted
one never does; brain damage and lost parts are permanent. Enemies you knocked out
recover too, and wake up. `R` rests in 10-second steps until you're as good as you'll
get, someone comes into view or grows suspicious, you hear something, or an hour
passes.

**Medicine** is ordinary items with a `use` block: `effects` (and `fail_effects` if a
`skill` roll with `mod` fails), `time_ms`, a `needs` condition with `needs_text`, and
`keep` if it isn't used up. The Black Site has field dressings (first aid +2, stops
external bleeding), painkillers (`painkillers` status: pain ×0.4 for 20 minutes),
splints (first aid, 20 s: splints the worst break), adrenaline (+10 stamina, pain ×0.7
and +1 to attacks for 2 minutes), trauma kits (self-surgery at first aid −4 for a
minute: the only fix for internal bleeding; a botched job cuts you) and blood bags
(+30% blood). New effect ops `splint`, `restore_stamina`; new values `bleeding`,
`internal_bleeding`, `blood`, `fractures` (unsplinted); status `pain_mult`.

**Skill growth** (`training.practice`). Rolling a skill when it matters teaches you:
`2 × (1 − p)` points for a success, half that for a failure, where `p` is the chance
you had. A sure thing (95%+) or a hopeless one (under 2%) teaches nothing. A level
costs `5 × (1 + levels above 10)`, so 10→11 takes a few fights and 17→18 a career.
Hooked into attack rolls, grabs, holds and struggles, throwing, first aid, medicine
and stealth (each time someone looks your way and you stay hidden). Only the player
(and templates with `"learns": true`) keep score. `@` shows your skills and progress.

**Saving.** The whole `Run` is pickled (content included, so a save plays the same
even if the data files change) with a version number; a save from another version is
refused with a message, not a crash. Grenade fuses are scheduled as a
`functools.partial` of a module-level function so they pickle too.

**Inventory** (`i`): wield, wear (8 s) or take off armor, use, throw, drop. `a` goes
straight to medicine. `g` takes a live grenade first, and otherwise asks what to pick
up if there's a choice; weapons go to your hands if they're free, everything else to
your pack.

## 8. Modding reference

### Content types
`material`, `damage_type`, `body_plan`, `item`, `trait`, `status`, `power`, `creature`,
`skill`, `gas`, `tile_legend`, and for the roguelike `prefab`, `palette`,
`monster_group`, `item_group`, `dungeon` (see 7⅞). Each object needs `type` and `id`. A
later mod replaces objects by id.

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
4. **Roguelike mode (first milestone done).** See section 7⅞. Next for it: more
   dungeons and floor themes, multi-storey prefabs (z-levels inside a floor), going
   back up to earlier floors, companions, NPC factions that fight each other,
   carrying weight and ammunition, surgery and medicine on others, hunger and sleep
   over longer runs, a world map linking dungeons.
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
* NPCs pick weapons back up, but only brutes pick fights with body parts; nobody reacts
  to gore yet (it should hit morale when morale exists).
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
`ranged_attack_mod`, `ranged_target_mod`, `defense_mod`, `move_mult`, `subjective`,
`hypoxia` (brain damage per second: can't breathe; no waking up), `knocks_out` (when
applied by a fracture), `death_text`. Damage types: `wound_mult`, `bleed`, `knockback`,
`dismembers`, `fractures`, `sudden`, `ignores_armor`. Items: `gore`, `throwable`,
`thrown`. Creatures: `grapple` (brute AI).
Timed statuses expire at their exact time, not on the next world tick. Powers also
take `cooldown_ms`. A power whose effects abort (nowhere to land, out of range)
"fizzles" and can't be retried for 2 s.
