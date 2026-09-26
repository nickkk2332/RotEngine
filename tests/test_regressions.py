"""Regression tests for bugs found in review."""
from rotengine import combat
from rotengine.sim import Sim
from rotengine.world import World


def open_arena(content, seed=0, w=14, h=9, d=1):
    world = World(content.all("material"), w, h, d)
    for y in range(h):
        for x in range(w):
            world.set_floor((x, y, 0), "concrete")
    return Sim(content, world, seed)


def test_stun_on_a_speedster_ends_on_his_clock(content):
    sim = open_arena(content)
    fast = sim.spawn("speedster", "a", (2, 4, 0))
    sim.spawn("soldier", "b", (12, 4, 0))
    sim.apply_status(fast, "stunned", 2000)          # 250 ms of world time at tempo 8
    until = fast.statuses["stunned"]
    assert until == sim.time + 250
    cost = sim._act(fast)                            # still stunned: waits exactly until it ends
    assert int(cost / fast.tempo) == 250
    sim.time = until
    sim.expire_statuses(fast)
    assert fast.can_act


def test_fizzled_power_backs_off(content):
    sim = open_arena(content)
    a = sim.spawn("assassin", "a", (1, 4, 0))
    g = sim.spawn("guard", "b", (8, 4, 0))
    for dx, dy in [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]:
        sim.world.set_fill((8 + dx, 4 + dy, 0), "concrete")  # nowhere to land next to him
    sim.world.set_fill((7, 4, 0), "glass")  # ...but still visible through a window
    blink = next(p for p in a.powers if p["id"] == "blink")
    combat.use_power(sim, a, blink, g)
    assert any("fizzles" in line for line in sim.lines)
    assert a.cooldowns["blink"] > sim.time


def test_prone_is_melee_only_and_harder_to_shoot(content):
    sim = open_arena(content)
    shooter = sim.spawn("soldier", "a", (1, 4, 0))
    target = sim.spawn("thug", "b", (10, 4, 0))
    rifle, item = shooter.attacks()[0]
    standing = combat.base_skill(sim, shooter, target, rifle, item)
    shooter.add_status("prone", None)
    assert combat.base_skill(sim, shooter, target, rifle, item) == standing
    target.add_status("prone", None)
    assert combat.base_skill(sim, shooter, target, rifle, item) == standing - 2


def test_stray_rounds_stop_at_floors(content):
    """Shooting up through a hole at someone on z1: misses keep climbing,
    and the concrete ceiling (z2's floor) has to stop them."""
    from rotengine.world import World as W
    assert (4, 4, 3) in W.line((1, 4, 0), (6, 4, 5))  # the bystander is on the round's path
    sim = open_arena(content, d=4)
    for y in range(9):
        for x in range(14):
            sim.world.set_floor((x, y, 2), "concrete")
    shooter = sim.spawn("thug", "a", (1, 4, 0))
    target = sim.spawn("thug", "b", (2, 4, 1))
    above = sim.spawn("street_tough", "c", (4, 4, 3))
    combat._stray_rounds(sim, shooter, target, combat.Dice(2, 6, 2), 30)
    assert above.hp == above.max_hp  # DR 20 concrete stops every pistol round
    sim.world.set_floor((3, 4, 2), None)  # knock a hole in the ceiling: now they get through
    combat._stray_rounds(sim, shooter, target, combat.Dice(2, 6, 2), 30)
    assert above.hp < above.max_hp


def test_blink_attack_never_stops_to_aim(content):
    sim = open_arena(content)
    a = sim.spawn("soldier", "a", (1, 4, 0))
    t = sim.spawn("thug", "b", (8, 4, 0))
    plan = combat.best_attack_plan(sim, a, t, allow_aim=False)
    assert plan is not None and not plan.aim_first
