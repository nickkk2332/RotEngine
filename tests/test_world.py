from rotengine.world import World


def make_world(content, w=7, h=5, d=2):
    world = World(content.all("material"), w, h, d)
    for y in range(h):
        for x in range(w):
            world.set_floor((x, y, 0), "concrete")
    return world


def test_line_endpoints():
    line = World.line((0, 0, 0), (4, 2, 1))
    assert line[-1] == (4, 2, 1) and len(line) == 4


def test_walls_block_sight_glass_does_not(content):
    world = make_world(content)
    world.set_fill((3, 2, 0), "concrete")
    assert not world.has_los((1, 2, 0), (5, 2, 0))
    world.set_fill((3, 2, 0), "glass")
    assert world.has_los((1, 2, 0), (5, 2, 0))
    assert world.obstacles((1, 2, 0), (5, 2, 0)) == [("fill", (3, 2, 0))]


def test_floors_block_vertical_sight(content):
    world = make_world(content)
    world.set_floor((2, 2, 1), "wood")
    assert not world.has_los((2, 2, 0), (2, 2, 1))
    assert world.has_los((0, 0, 0), (0, 0, 1))  # open air above


def test_destruction_respects_dr(content):
    world = make_world(content)
    world.set_fill((3, 2, 0), "brick")
    assert not world.damage_fill((3, 2, 0), 10)  # DR 10 stops it
    assert world.fill_hp[0, 2, 3] == 60
    assert world.damage_fill((3, 2, 0), 80)
    assert world.passable((3, 2, 0))


def test_platform_collapses_when_pillars_go(content):
    world = make_world(content)
    for x in (2, 3, 4):
        world.set_floor((x, 2, 1), "wood")
    world.set_fill((2, 2, 0), "wood")  # one pillar under the platform
    assert world.settle() == []
    assert world.damage_fill((2, 2, 0), 100)
    collapsed = world.settle()
    assert sorted(p for kind, p in collapsed if kind == "floor") == [(2, 2, 1), (3, 2, 1), (4, 2, 1)]
    assert not world.supported((3, 2, 1))


def test_stairs(content):
    world = make_world(content)
    world.set_fill((1, 1, 0), "stairs")
    world.set_fill((1, 1, 1), "stairs")
    assert (1, 1, 1) in set(world.neighbors((1, 1, 0)))
    assert (1, 1, 0) in set(world.neighbors((1, 1, 1)))
