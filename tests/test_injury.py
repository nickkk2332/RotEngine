"""Death and incapacitation: people pass out far more often than they die
outright, and a leg wound only kills through blood loss."""
from rotengine import ai, combat
from rotengine.sim import Sim
from rotengine.world import World


def open_arena(content, seed=0, w=12, h=7):
    world = World(content.all("material"), w, h, 1)
    for y in range(h):
        for x in range(w):
            world.set_floor((x, y, 0), "concrete")
    return Sim(content, world, seed)


def test_leg_wound_cannot_kill_directly(content):
    sim = open_arena(content)
    c = sim.spawn("street_tough", "a", (3, 3, 0))
    for _ in range(3):
        combat.deal_damage(sim, c, 40, "pierce", "l_leg")
    assert not c.dead
    assert c.hp > 0  # limb damage past crippling doesn't count against HP
    assert c.has_status("prone")


def test_severed_leg_bleeds_out_without_aid(content):
    sim = open_arena(content, seed=4)
    c = sim.spawn("street_tough", "a", (3, 3, 0))
    combat.deal_damage(sim, c, 20, "cut", "l_leg")
    assert c.body.part("l_leg").destroyed and not c.dead
    combat.knock_out(sim, c)  # nobody to tie a tourniquet, not even himself
    sim.run_aftermath(600)
    assert c.dead and c.death_cause == "bled out"


def test_tourniquet_saves_a_life(content):
    sim = open_arena(content, seed=4)
    medic = sim.spawn("soldier", "a", (4, 3, 0))
    medic.skills["first_aid"] = 18
    c = sim.spawn("street_tough", "a", (3, 3, 0))
    combat.deal_damage(sim, c, 20, "cut", "l_leg")
    sim.run_aftermath(600)
    assert not c.dead
    assert c.body.bleed_rate == 0


def test_heart_shot_is_minutes_not_instant(content):
    sim = open_arena(content, seed=1)
    c = sim.spawn("street_tough", "a", (3, 3, 0))
    combat.deal_damage(sim, c, 8, "pierce", "vitals")
    assert c.has_status("cardiac_arrest")
    assert not c.dead
    sim.run_aftermath(30)
    assert not c.conscious and not c.dead
    sim.run_aftermath(300)
    assert c.dead and c.death_cause == "brain death"


def test_blunt_beating_knocks_out_long_before_it_kills(content):
    downed = dead = 0
    for seed in range(30):
        sim = open_arena(content, seed=seed)
        c = sim.spawn("street_tough", "a", (3, 3, 0))
        for _ in range(4):  # a bad beating: four hard blows to the body
            combat.deal_damage(sim, c, 7, "crush", "torso", knockback_ok=False)
        downed += not c.conscious
        dead += c.dead
    assert dead == 0
    assert downed >= 20


def test_brain_destruction_is_instant(content):
    sim = open_arena(content)
    c = sim.spawn("street_tough", "a", (3, 3, 0))
    combat.deal_damage(sim, c, 10, "pierce", "skull")
    assert c.dead and c.death_cause == "brain destroyed"


def test_crippled_arm_drops_the_weapon(content):
    sim = open_arena(content)
    thug = sim.spawn("thug", "b", (3, 3, 0))
    combat.deal_damage(sim, thug, 8, "pierce", "r_arm")
    assert thug.wielded is None
    assert sim.items and sim.items[0][1].id == "pistol"


def test_exhaustion_collapses(content):
    sim = open_arena(content)
    c = sim.spawn("street_tough", "a", (3, 3, 0))
    sim.spawn("street_tough", "b", (9, 3, 0))
    c.stamina = -c.max_stamina
    assert c.fatigue_level() == 2 and c.move_per_second < 2
    sim._act(c)
    assert not c.conscious


def test_first_aid_needs_skill_for_arteries(content):
    sim = open_arena(content, seed=2)
    medic = sim.spawn("soldier", "a", (4, 3, 0))
    c = sim.spawn("street_tough", "a", (3, 3, 0))
    c.body.bleed_rate = 0.3
    ai._first_aid(sim, medic, allies=True)
    assert c.body.bleed_rate < 0.3 or any("fumbles" in line for line in sim.lines)
