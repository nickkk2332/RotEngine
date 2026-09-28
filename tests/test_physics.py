"""Doors, breaching, throwing, explosions, fire and gas."""
from rotengine import actions, ai, combat, flight, perception, physics
from rotengine.creature import Item
from rotengine.sim import Sim
from rotengine.world import World


def room(content, seed=0, w=20, h=9, d=1, floor="concrete", light=1.0):
    world = World(content.all("material"), w, h, d)
    for y in range(h):
        for x in range(w):
            world.set_floor((x, y, 0), floor)
    sim = Sim(content, world, seed)
    sim.ambient_light = light
    return sim


def item(content, item_id):
    return Item(content.get("item", item_id))


def tick(sim, seconds):
    for _ in range(seconds):
        sim.time += 1000
        sim._tick()


# -- doors and breaching ---------------------------------------------------------------
def test_doors_open_close_and_block_sight(content):
    sim = room(content)
    sim.world.set_fill((5, 4, 0), "door")
    a = sim.spawn("thug", "a", (4, 4, 0))
    assert not sim.world.has_los((3, 4, 0), (7, 4, 0))
    assert actions.step(sim, a, (5, 4, 0)) is not None  # walking into it opens it
    assert sim.world.has_los((3, 4, 0), (7, 4, 0)) and a.pos == (4, 4, 0)
    assert actions.close_door(sim, a, (5, 4, 0)) is not None
    assert not sim.world.passable((5, 4, 0))


def test_ai_walks_through_doors(content):
    sim = room(content)
    for y in range(9):
        sim.world.set_fill((10, y, 0), "brick")
    sim.world.set_fill((10, 4, 0), "door")
    a = sim.spawn("thug", "a", (5, 4, 0))
    ai_step = ai._step_toward(sim, a, (15, 4, 0))
    assert ai_step is not None


def test_locked_doors_get_breached(content):
    sim = room(content, seed=3)
    for y in range(9):
        sim.world.set_fill((10, y, 0), "brick")
    sim.world.set_fill((10, 4, 0), "locked_door")
    brute = sim.spawn("hulk", "a", (9, 4, 0))
    for _ in range(10):
        if sim.world.passable((10, 4, 0)):
            break
        actions.smash(sim, brute, (10, 4, 0))
    assert sim.world.passable((10, 4, 0))


def test_breaching_charge_blows_a_hole_in_brick(content):
    sim = room(content, seed=1)
    for y in range(9):
        sim.world.set_fill((10, y, 0), "brick")
    swat = sim.spawn("swat", "a", (9, 4, 0))
    charge = next(i for i in swat.carried if i.id == "breaching_charge")
    assert actions.plant(sim, swat, charge, (10, 4, 0)) is not None
    swat.pos = (2, 4, 0)  # step well back
    sim.run_aftermath(6)
    assert sim.world.passable((10, 4, 0))
    assert any("goes off" in line for line in sim.lines)


# -- explosions ---------------------------------------------------------------------------
def test_frag_grenade_and_walls(content):
    sim = room(content, seed=2)
    for y in range(9):
        sim.world.set_fill((10, y, 0), "concrete")
    close = sim.spawn("thug", "a", (5, 4, 0))
    shielded = sim.spawn("thug", "a", (11, 4, 0))
    physics.explode(sim, (6, 4, 0), content.get("item", "frag_grenade")["explosive"])
    assert close.hp < close.max_hp
    assert shielded.hp == shielded.max_hp


def test_live_grenade_goes_off_wherever_it_is(content):
    sim = room(content, seed=4)
    a = sim.spawn("swat", "a", (2, 4, 0))
    b = sim.spawn("thug", "b", (14, 4, 0))
    g = next(i for i in a.carried if i.id == "frag_grenade")
    actions.throw(sim, a, g, (8, 4, 0))
    flight.finish(sim)
    assert g.armed and any(i is g for _, i in sim.items)
    # b picks it up and throws it back before it goes off
    pos = next(p for p, i in sim.items if i is g)
    sim.items.remove((pos, g))
    b.carried.append(g)
    actions.throw(sim, b, g, (3, 4, 0))
    flight.finish(sim)
    land = next(p for p, i in sim.items if i is g)
    assert land[0] < 8  # it went back the way it came
    near = [c for c in (a, b) if sim.distance_pos(c.pos, land) <= 2]
    sim.run_aftermath(4)
    assert not any(i is g for _, i in sim.items)  # and it went off where it landed
    assert all(c.hp < c.max_hp for c in near)


def test_flashbang_blinds_those_looking(content):
    sim = room(content, seed=5)
    facing = sim.spawn("thug", "a", (5, 4, 0))
    facing.facing = (1, 0)
    away = sim.spawn("thug", "a", (5, 6, 0))
    away.facing = (-1, 0)
    physics.explode(sim, (8, 4, 0), content.get("item", "flashbang")["explosive"])
    assert facing.has_status("dazzled")
    assert not away.has_status("dazzled")


def test_ai_runs_from_a_live_grenade(content):
    sim = room(content, seed=1)
    a = sim.spawn("thug", "a", (8, 4, 0))
    g = item(content, "frag_grenade")
    sim.drop((9, 4, 0), g)
    physics.arm(sim, g, 3000, None)
    start = sim.distance_pos(a.pos, (9, 4, 0))
    ai.take_turn(sim, a)
    assert sim.distance_pos(a.pos, (9, 4, 0)) > start or a.has_status("prone")


# -- throwing ------------------------------------------------------------------------------
def test_throwing_through_a_window(content):
    sim = room(content, seed=1)
    for y in range(9):
        sim.world.set_fill((10, y, 0), "brick")
    sim.world.set_fill((10, 4, 0), "glass")
    a = sim.spawn("swat", "a", (6, 4, 0))
    smoke = next(i for i in a.carried if i.id == "smoke_grenade")
    actions.throw(sim, a, smoke, (14, 4, 0))
    flight.finish(sim)
    land = next(p for p, i in sim.items if i is smoke)
    assert sim.world.passable((10, 4, 0))  # the window broke
    assert land[0] > 10 or any("bad throw" in line for line in sim.lines)


def test_hulk_hurls_a_soldier_into_another(content):
    sim = room(content, seed=2, w=30)
    hulk = sim.spawn("hulk", "a", (5, 4, 0))
    thrown = sim.spawn("soldier", "b", (6, 4, 0))
    other = sim.spawn("soldier", "b", (12, 4, 0))
    power = next(p for p in hulk.powers if p["id"] == "hulk_hurl")
    cost = combat.use_power(sim, hulk, power, thrown)
    sim._schedule(hulk, sim.time + cost)  # (the throw takes him a second, as in a real turn)
    sim._schedule(other, sim.time + 5000)  # (and the other one doesn't get out of the way)
    assert thrown.has_status("airborne")
    flight.finish(sim)
    assert thrown.pos[0] > 6
    assert other.hp < other.max_hp or other.has_status("prone")


def test_throwing_a_held_person(content):
    sim = room(content, seed=3)
    a = sim.spawn("swat", "a", (5, 4, 0))
    b = sim.spawn("thug", "b", (6, 4, 0))
    actions.grab(sim, a, b)
    if a.grappling is b:
        actions.hurl(sim, a, (1, 0))
        flight.finish(sim)
        assert b.pos[0] >= 7 and b.has_status("prone") and a.grappling is None


# -- fire and gas ----------------------------------------------------------------------------
def test_molotov_sets_a_wooden_room_ablaze(content):
    sim = room(content, seed=1, floor="wood")
    a = sim.spawn("firebug", "a", (2, 4, 0))
    victim = sim.spawn("thug", "b", (9, 4, 0))
    m = next(i for i in a.carried if i.id == "molotov")
    actions.throw(sim, a, m, (9, 4, 0))
    assert not sim.fields.fire.any()  # still in the air
    flight.finish(sim)
    assert sim.fields.fire.any()
    tick(sim, 40)
    assert int((sim.fields.fire > 0).sum()) > 9  # it spread across the wooden floor
    assert victim.hp < victim.max_hp or victim.dead or victim.pos != (9, 4, 0)
    burning = next((int(x), int(y), int(z)) for z, y, x in zip(*(sim.fields.fire > 3).nonzero()))
    assert perception.light_at(sim, burning) > 0.6  # and it lights the place up


def test_fire_burns_out_a_floor(content):
    sim = room(content, seed=1, d=2)
    for y in range(9):
        for x in range(20):
            sim.world.set_floor((x, y, 1), "wood")
    for x, y in ((0, 0), (19, 0), (0, 8), (19, 8)):
        sim.world.set_fill((x, y, 0), "concrete")
    up = sim.spawn("thug", "b", (10, 4, 1))
    for x in range(8, 13):
        sim.fields.ignite((x, 4, 1), 8)
    tick(sim, 30)
    sim._settle()
    assert up.pos[2] == 0 or up.dead


def test_smoke_blocks_sight_then_clears(content):
    sim = room(content, seed=1)
    assert sim.world.has_los((2, 4, 0), (17, 4, 0))
    sim.fields.add_gas("smoke", (10, 4, 0), 150, radius=2)
    sim.fields._update_fog()
    assert not sim.world.has_los((2, 4, 0), (17, 4, 0))
    tick(sim, 90)
    assert sim.world.has_los((2, 4, 0), (17, 4, 0))


def test_tear_gas_and_masks(content):
    sim = room(content, seed=1)
    thug = sim.spawn("thug", "a", (10, 4, 0))
    swat = sim.spawn("swat", "b", (11, 4, 0))
    sim.fields.add_gas("tear_gas", (10, 4, 0), 100, radius=2)
    tick(sim, 1)
    assert thug.has_status("choking")
    assert not swat.has_status("choking")


# -- things in flight --------------------------------------------------------------------------
def test_a_hurled_body_takes_time_to_land(content):
    sim = room(content, seed=2, w=30)
    hulk = sim.spawn("hulk", "a", (5, 4, 0))
    thrown = sim.spawn("soldier", "b", (6, 4, 0))
    power = next(p for p in hulk.powers if p["id"] == "hulk_hurl")
    t0 = sim.time
    cost = combat.use_power(sim, hulk, power, thrown)
    sim._schedule(hulk, sim.time + cost)
    assert thrown.pos == (6, 4, 0) and thrown.has_status("airborne") and not thrown.can_act
    flight.finish(sim)
    assert thrown.pos[0] > 10 and not thrown.has_status("airborne")
    assert 100 < sim.time - t0 < 800  # a fraction of a second, not instant


def test_a_speedster_can_act_while_a_body_is_in_the_air(content):
    """Hit him, send him flying, get there first."""
    sim = room(content, seed=2, w=30)
    hulk = sim.spawn("hulk", "a", (5, 4, 0))
    fast = sim.spawn("speedster", "a", (5, 6, 0))
    thrown = sim.spawn("soldier", "b", (6, 4, 0))
    fast.controller = "player"
    power = next(p for p in hulk.powers if p["id"] == "hulk_hurl")
    cost = combat.use_power(sim, hulk, power, thrown)
    sim._schedule(hulk, sim.time + cost)
    turns_in_flight = 0
    for _ in range(10):
        if sim.advance() != "player" or not thrown.has_status("airborne"):
            break
        turns_in_flight += 1
        sim.player_act(actions.step(sim, fast, (fast.pos[0] + 1, fast.pos[1], 0)) or 100)
    assert turns_in_flight >= 2


def test_a_thrown_grenade_can_go_off_in_the_air(content):
    sim = room(content, seed=1, w=30)
    a = sim.spawn("swat", "a", (2, 4, 0))
    g = next(i for i in a.carried if i.id == "frag_grenade")
    physics.arm(sim, g, 200, a)  # cooked too long
    assert actions.throw(sim, a, g, (14, 4, 0)) is not None
    assert flight.flying(sim, g) is not None and not any(i is g for _, i in sim.items)
    sim._loop(lambda: True, sim.time + 1000)
    assert flight.flying(sim, g) is None and not any(i is g for _, i in sim.items)
    assert any("frag grenade" in line and ("explod" in line or "blast" in line or "goes off" in line)
               for line in sim.lines) or a.hp < a.max_hp


def test_something_in_the_air_survives_a_save(content):
    import pickle
    sim = room(content, seed=1, w=30)
    a = sim.spawn("swat", "a", (2, 4, 0))
    smoke = next(i for i in a.carried if i.id == "smoke_grenade")
    assert actions.throw(sim, a, smoke, (14, 4, 0)) is not None
    back = pickle.loads(pickle.dumps(sim))
    flight.finish(back)
    assert any(i.id == "smoke_grenade" for _, i in back.items)
