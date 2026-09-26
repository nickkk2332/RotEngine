"""Tempo, reaction windows, facing, cover and stray rounds."""
from rotengine import combat
from rotengine.sim import Sim
from rotengine.world import World


def open_arena(content, seed=0, w=14, h=9):
    world = World(content.all("material"), w, h, 1)
    for y in range(h):
        for x in range(w):
            world.set_floor((x, y, 0), "concrete")
    return Sim(content, world, seed)


def test_speedster_acts_many_times_per_second(content):
    sim = open_arena(content)
    fast = sim.spawn("speedster", "a", (1, 4, 0))
    slow = sim.spawn("street_tough", "b", (12, 4, 0))
    counts = {fast.uid: 0, slow.uid: 0}
    act = sim._act

    def counting_act(c):
        counts[c.uid] += 1
        return act(c)

    sim._act = counting_act
    sim.run(1000)
    assert fast.tempo == 8
    assert counts[fast.uid] >= 5 * max(1, counts[slow.uid])


def test_momentum_makes_fast_hits_hard(content):
    sim = open_arena(content)
    fast = sim.spawn("speedster", "a", (1, 4, 0))
    knife = fast.wielded.attacks[0]
    assert combat.attack_dice(fast, knife).mean > combat.st_damage(11, "swing").mean * 2


def test_relative_tempo_changes_defense(content):
    sim = open_arena(content)
    fast = sim.spawn("speedster", "a", (5, 4, 0))
    slow = sim.spawn("soldier", "b", (6, 4, 0))
    combat.face(fast, slow.pos)
    combat.face(slow, fast.pos)
    fast_def = combat.defense_against(sim, slow, fast, "ranged")[1]
    slow_def = combat.defense_against(sim, fast, slow, "melee")[1]
    assert fast_def == fast.dodge() + 6
    assert slow_def <= max(slow.dodge(), slow.best_defense("melee")[1]) - 6


def test_defenses_stack_within_the_reaction_window(content):
    sim = open_arena(content)
    a = sim.spawn("thug", "a", (5, 4, 0))
    d = sim.spawn("street_tough", "b", (6, 4, 0))
    combat.face(d, a.pos)
    first = combat.defense_against(sim, a, d, "melee")[1]
    combat._spend_defense(sim, d)
    combat._spend_defense(sim, d)
    assert combat.defense_against(sim, a, d, "melee")[1] == first - 4
    sim.time += 1000
    assert combat.defense_against(sim, a, d, "melee")[1] == first


def test_facing_arcs(content):
    sim = open_arena(content)
    d = sim.spawn("street_tough", "b", (6, 4, 0))
    d.facing = (1, 0)
    front = sim.spawn("thug", "a", (7, 4, 0))
    side = sim.spawn("thug", "a", (6, 5, 0))
    back = sim.spawn("thug", "a", (5, 4, 0))
    assert combat.arc(d, front.pos) == "front"
    assert combat.arc(d, side.pos) == "side"
    assert combat.defense_against(sim, back, d, "melee") is None


def test_stunned_can_still_defend(content):
    sim = open_arena(content)
    a = sim.spawn("thug", "a", (5, 4, 0))
    d = sim.spawn("street_tough", "b", (6, 4, 0))
    combat.face(d, a.pos)
    normal = combat.defense_against(sim, a, d, "melee")[1]
    sim.apply_status(d, "stunned", 2000)
    assert combat.defense_against(sim, a, d, "melee")[1] == normal - 4


def test_cover_behind_a_corner(content):
    sim = open_arena(content)
    shooter = sim.spawn("thug", "a", (1, 1, 0))
    target = sim.spawn("thug", "b", (8, 6, 0))
    assert combat.cover(sim, shooter.pos, target)[0] == 0
    sim.world.set_fill((7, 6, 0), "concrete")
    penalty, blocker = combat.cover(sim, shooter.pos, target)
    assert penalty < 0 and blocker == (7, 6, 0)
    assert sim.world.has_los(shooter.pos, target.pos)


def test_aiming_adds_accuracy(content):
    sim = open_arena(content)
    shooter = sim.spawn("soldier", "a", (1, 4, 0))
    target = sim.spawn("thug", "b", (12, 4, 0))
    rifle, item = shooter.attacks()[0]
    snap = combat.base_skill(sim, shooter, target, rifle, item)
    shooter.aim_target = target.uid
    assert combat.base_skill(sim, shooter, target, rifle, item) == snap + rifle["acc"]


def test_stray_rounds_hit_bystanders(content):
    sim = open_arena(content, seed=3)
    shooter = sim.spawn("thug", "a", (1, 4, 0))
    target = sim.spawn("thug", "b", (5, 4, 0))
    bystander = sim.spawn("street_tough", "c", (8, 4, 0))
    combat._stray_rounds(sim, shooter, target, shooter.wielded and combat.attack_dice(
        shooter, shooter.wielded.attacks[0]), 12)
    assert any("stray round hits street tough" in line for line in sim.lines)
    assert bystander.body.hp < bystander.max_hp or bystander.dead


def test_shooters_hold_fire_with_a_friend_in_the_way(content):
    sim = open_arena(content)
    shooter = sim.spawn("thug", "a", (1, 4, 0))
    sim.spawn("thug", "a", (4, 4, 0))
    target = sim.spawn("thug", "b", (8, 4, 0))
    plan = combat.best_attack_plan(sim, shooter, target)
    assert plan is None or plan.attack["kind"] == "melee"
