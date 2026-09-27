"""Perception, stealth, noise and takedowns."""
from rotengine import actions, arena, combat, perception
from rotengine.sim import Sim
from rotengine.world import World


def yard(content, seed=0, light=1.0, w=30, h=9):
    world = World(content.all("material"), w, h, 1)
    for y in range(h):
        for x in range(w):
            world.set_floor((x, y, 0), "concrete")
    sim = Sim(content, world, seed)
    sim.start_aware = False
    sim.ambient_light = light
    return sim


def turns_to_spot(content, light, dist, sneak, seed):
    sim = yard(content, seed, light)
    g = sim.spawn("sentry", "g", (2, 4, 0))
    g.facing = (1, 0)
    p = sim.spawn("operative", "p", (2 + dist, 4, 0))
    p.sneaking = sneak
    for turn in range(1, 31):
        sim.time += 1000
        perception.perceive(sim, g)
        if perception.aware_of(g, p):
            return turn
    return 31


def test_light_and_sneaking_matter(content):
    import statistics
    bright_walk = statistics.median(turns_to_spot(content, 1.0, 5, False, s) for s in range(30))
    dark_sneak = statistics.median(turns_to_spot(content, 0.08, 5, True, s) for s in range(30))
    assert bright_walk <= 4
    assert dark_sneak == 31


def test_nobody_sees_behind_them(content):
    sim = yard(content)
    g = sim.spawn("sentry", "g", (10, 4, 0))
    g.facing = (1, 0)
    p = sim.spawn("operative", "p", (8, 4, 0))
    for _ in range(20):
        sim.time += 1000
        perception.perceive(sim, g)
    assert g.awareness.get(p.uid) is None or g.awareness[p.uid].level == 0


def test_unnoticed_attackers_get_no_defense(content):
    sim = yard(content)
    g = sim.spawn("sentry", "g", (10, 4, 0))
    p = sim.spawn("operative", "p", (11, 4, 0))
    g.facing = (1, 0)  # facing the operative, but hasn't noticed him yet
    assert combat.defense_against(sim, p, g, "melee") is None
    perception.spotted(sim, g, p)
    assert combat.defense_against(sim, p, g, "melee") is not None


def test_gunshots_carry_and_suppressors_dont(content):
    sim = yard(content, seed=3, w=40)
    near = sim.spawn("sentry", "g", (2, 4, 0))
    far = sim.spawn("sentry", "g", (36, 4, 0))
    p = sim.spawn("operative", "p", (18, 4, 0))
    perception.emit_noise(sim, p, p.pos, "suppressed")
    assert near.investigate is None and far.investigate is None  # 16+ tiles: nobody hears it
    perception.emit_noise(sim, p, p.pos, "gunshot")
    assert near.investigate is not None and far.investigate is not None


def test_walls_muffle_noise(content):
    sim = yard(content, seed=1)
    for y in range(9):
        sim.world.set_fill((10, y, 0), "brick")
    g = sim.spawn("sentry", "g", (15, 4, 0))
    p = sim.spawn("operative", "p", (5, 4, 0))
    for _ in range(10):
        perception.emit_noise(sim, p, p.pos, "crash")  # 12 tiles in the open, 6 through a wall
    assert g.investigate is None


def test_the_alarm_spreads(content):
    sim = yard(content, seed=2)
    a = sim.spawn("sentry", "g", (5, 4, 0))
    b = sim.spawn("sentry", "g", (15, 2, 0))
    p = sim.spawn("operative", "p", (8, 4, 0))
    perception.spotted(sim, a, p)
    assert perception.aware_of(b, p)
    assert b.awareness[p.uid].last_pos == p.pos


def test_finding_a_body_raises_the_alarm(content):
    sim = yard(content, seed=1)
    g = sim.spawn("sentry", "g", (5, 4, 0))
    g.facing = (1, 0)
    friend = sim.spawn("sentry", "g", (9, 4, 0))
    combat.kill(sim, friend, "test")
    perception.perceive(sim, g)
    assert g.alarmed and g.investigate == friend.pos
    assert any("raises the alarm" in line for line in sim.lines)


def test_silent_chokeout(content):
    sim = yard(content, seed=4)
    g = sim.spawn("sentry", "g", (10, 4, 0))
    g.facing = (1, 0)
    buddy = sim.spawn("sentry", "g", (20, 4, 0))
    buddy.facing = (1, 0)  # looking the other way
    p = sim.spawn("operative", "p", (9, 4, 0))  # right behind the first guard
    assert actions.grab(sim, p, g) is not None and p.grappling is g
    for _ in range(12):
        if not g.conscious:
            break
        actions.choke(sim, p)
    assert not g.conscious and not g.dead
    assert not any("spots" in line or "alarm" in line for line in sim.lines)
    assert not perception.aware_of(buddy, p)
    # and he wakes up later if you let go
    actions.release(sim, p)
    sim.time += 70_000
    sim.expire_statuses(g)
    assert g.conscious


def test_holding_on_after_they_go_limp_kills(content):
    sim = yard(content, seed=4)
    g = sim.spawn("sentry", "g", (10, 4, 0))
    p = sim.spawn("operative", "p", (9, 4, 0))
    actions.grab(sim, p, g)
    for _ in range(80):
        if g.dead:
            break
        actions.choke(sim, p)
    assert g.dead and g.death_cause == "strangled"


def test_dragging_a_body(content):
    sim = yard(content)
    body = sim.spawn("sentry", "g", (10, 4, 0))
    combat.kill(sim, body, "test")
    p = sim.spawn("operative", "p", (9, 4, 0))
    actions.grab(sim, p, body)
    actions.step(sim, p, (8, 4, 0))
    actions.step(sim, p, (7, 4, 0))
    assert p.pos == (7, 4, 0) and body.pos == (8, 4, 0)


def test_breaking_free(content):
    sim = yard(content, seed=9)
    brute = sim.spawn("hulk", "g", (10, 4, 0))
    p = sim.spawn("operative", "p", (9, 4, 0))
    actions.grab(sim, p, brute)
    for _ in range(10):
        if brute.grappled_by is None:
            break
        actions.struggle(sim, brute)
    assert brute.grappled_by is None  # ST 60 doesn't stay held


def test_shooting_out_a_lamp_makes_it_dark(content):
    sim = yard(content, light=0.08)
    sim.world.set_fill((10, 4, 0), "lamp")
    assert perception.light_at(sim, (12, 4, 0)) > 0.6
    sim.world.damage_fill((10, 4, 0), 20)
    assert perception.light_at(sim, (12, 4, 0)) < 0.1


def test_unaware_guards_keep_to_their_patrol(content):
    sim = yard(content, seed=1, light=0.08)  # a dark yard: the operative stays unseen
    g = sim.spawn("sentry", "g", (2, 2, 0))
    g.patrol = [(2, 2, 0), (8, 2, 0)]
    sim.spawn("operative", "p", (25, 8, 0))
    sim.run(8000)
    assert all(not ln.split("] ", 1)[1].startswith("sentry shoots") for ln in sim.lines)
    assert g.pos != (2, 2, 0) or g.patrol_i > 0


def test_stealth_scenario_starts_quiet(content):
    scenario = arena.load_scenario("stealth_compound")
    sim = arena.build(scenario, content, seed=1)
    assert sim.ambient_light < 0.2
    guards = [c for c in sim.creatures if c.team == "guards"]
    assert all(not c.awareness for c in guards)
    assert any(c.patrol for c in guards)
