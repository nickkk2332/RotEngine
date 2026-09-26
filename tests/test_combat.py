from rotengine import combat
from rotengine.dice import Dice
from rotengine.sim import Sim
from rotengine.world import World


def open_arena(content, seed=0, w=12, h=7):
    world = World(content.all("material"), w, h, 1)
    for y in range(h):
        for x in range(w):
            world.set_floor((x, y, 0), "concrete")
    return Sim(content, world, seed)


def test_st_damage_table():
    assert combat.st_damage(10, "thrust") == Dice(1, 6, -2)
    assert combat.st_damage(10, "swing") == Dice(1, 6, 0)
    assert combat.st_damage(60, "thrust").mean > 18


def test_range_penalty():
    assert combat.range_penalty(2) == 0
    assert combat.range_penalty(10) == -4
    assert combat.range_penalty(100) == -10


def test_rifles_bounce_off_the_hulk(content):
    sim = open_arena(content)
    hulk = sim.spawn("hulk", "a", (2, 3, 0))
    soldier = sim.spawn("soldier", "b", (9, 3, 0))
    rifle = soldier.attacks()[0][0]
    exp = combat.expected_injury(combat.attack_dice(soldier, rifle), hulk.dr("torso", "pierce"), 1.0, 99)
    assert exp < 0.2
    plan = combat.best_attack_plan(sim, hulk, soldier)
    assert plan is None  # too far to punch


def test_hulk_punch_sends_soldier_through_a_wall(content):
    sim = open_arena(content, seed=3)
    for y in range(7):
        sim.world.set_fill((6, y, 0), "drywall")
    hulk = sim.spawn("hulk", "a", (4, 3, 0))
    soldier = sim.spawn("soldier", "b", (5, 3, 0))
    combat.deal_damage(sim, soldier, 40, "crush", "torso", source=hulk, origin=hulk.pos)
    assert soldier.pos[0] > 6  # went through the drywall
    assert sim.world.passable((6, 3, 0))
    assert not soldier.active


def test_experts_take_called_shots(content):
    sim = open_arena(content)
    wick = sim.spawn("wick", "a", (2, 3, 0))
    thug = sim.spawn("thug", "b", (6, 3, 0))
    plan = combat.best_attack_plan(sim, wick, thug)
    assert plan.location in ("vitals", "skull")
    novice = combat.best_attack_plan(sim, thug, wick)
    assert novice.location in (None, "torso")


def test_unaware_targets_get_no_defense(content):
    sim = open_arena(content)
    a = sim.spawn("assassin", "a", (2, 3, 0))
    g = sim.spawn("guard", "b", (3, 3, 0))
    combat.face(g, a.pos)
    aware = combat.best_attack_plan(sim, a, g)
    surprised = combat.best_attack_plan(sim, a, g, surprise=True)
    assert surprised.value > aware.value
    assert surprised.deceptive == 0  # nothing to feint against


def test_crippled_hand_drops_weapon(content):
    sim = open_arena(content)
    thug = sim.spawn("thug", "b", (3, 3, 0))
    combat.deal_damage(sim, thug, 30, "pierce", "r_hand")
    assert thug.wielded is None
