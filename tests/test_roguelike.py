"""Roguelike mode: map generation, runs, healing, medicine, skills, saving."""
import copy

import pytest

from rotengine import actions, combat, mapgen, physics, roguelike, training
from rotengine.content import Content
from rotengine.creature import Item

from test_stealth import yard


# -- map generation ------------------------------------------------------------------
@pytest.mark.parametrize("depth", [1, 2, 3, 4, 5, 6])
def test_every_floor_generates_connected(content, depth):
    for seed in range(6):
        plan = mapgen.generate(content, "black_site", depth, seed)
        assert len(plan.rooms) >= 2
        assert mapgen._reachable(plan, content)  # every spawn, item and the exit can be walked to
        assert all(mapgen._dist(s.pos, plan.start) >= 8 for s in plan.spawns if s.creature != "commander")
        if depth < 6:
            assert plan.exit is not None
            assert plan.tiles[plan.exit[1]][plan.exit[0]]["floor"] == "stairwell_down"
        else:
            assert plan.final and plan.exit is None
            assert [s.creature for s in plan.spawns].count("commander") == 1
            assert plan.rooms[0].prefab == "vault_command"


def test_generation_is_deterministic(content):
    a = mapgen.generate(content, "black_site", 2, 42)
    b = mapgen.generate(content, "black_site", 2, 42)
    c = mapgen.generate(content, "black_site", 2, 43)
    assert a.ascii(content) == b.ascii(content)
    assert a.ascii(content) != c.ascii(content)


def test_prefab_validation_catches_mistakes(content):
    bad = Content()
    bad._raw = copy.deepcopy(content._raw)
    bad.add({"type": "prefab", "id": "no_doors", "tags": ["warehouse"], "palettes": ["rooms"],
             "rows": ["####", "#..#", "####"]})
    bad.add({"type": "prefab", "id": "bad_char", "tags": ["warehouse"], "palettes": ["rooms"],
             "rows": ["##d#", "#.Q#", "####"]})
    bad.add({"type": "prefab", "id": "corner_door", "tags": ["warehouse"], "palettes": ["rooms"],
             "rows": ["d###", "#..#", "####"]})
    errors = "\n".join(bad.validate())
    assert "no_doors" in errors and "door slot" in errors
    assert "bad_char" in errors and "'Q'" in errors
    assert "corner_door" in errors and "not a corner" in errors


# -- runs -----------------------------------------------------------------------------
def test_wounds_gear_and_time_carry_down(content):
    run = roguelike.Run(content, seed=5)
    p = run.player
    combat.deal_damage(run.sim, p, 6, "cut", "l_arm")
    bleed, blood, hp = p.body.bleed_rate, p.body.blood, p.hp
    knife = p.wielded
    run.sim.time += 5000
    t = run.sim.time
    p.statuses.pop("agony", None)  # (nobody takes the stairs doubled over in pain)
    p.statuses.pop("stunned", None)
    p.pos = (*run.plan.exit, 0)
    assert run.at_exit() and run.descend()
    assert run.depth == 2 and run.player is p and p in run.sim.creatures
    assert p.body.bleed_rate == bleed and p.body.blood == blood and p.hp == hp and p.wielded is knife
    assert run.sim.time == t  # the clock carries on
    assert p.pos == (*run.plan.start, 0) and p.awareness == {}


def test_cannot_descend_away_from_the_stairs(content):
    run = roguelike.Run(content, seed=6)
    assert not run.at_exit() and not run.descend() and run.depth == 1


def test_winning_means_the_commander_is_down(content):
    run = roguelike.Run(content, seed=7)
    while run.depth < run.last_depth:
        run.player.pos = (*run.plan.exit, 0)
        assert run.descend()
    assert run.state == "playing"
    combat.knock_out(run.sim, run.boss())
    assert run.state == "won" and "took down the Commander" in run.epitaph()


def test_save_and_load_round_trip_with_a_live_grenade(content, tmp_path):
    run = roguelike.Run(content, seed=8)
    nade = Item(content.get("item", "frag_grenade"))
    run.sim.drop(run.player.pos, nade)
    physics.arm(run.sim, nade, 3000, run.player)  # a pending fuse must pickle too
    path = run.save(tmp_path / "run.sav")
    assert roguelike.has_save(path)
    back = roguelike.load(path)
    assert back.depth == 1 and back.player.name == run.player.name
    assert back.plan.ascii(content) == run.plan.ascii(content)
    back.sim._loop(lambda: True, back.sim.time + 4000)  # the fuse still goes off after loading
    assert not any(i.name == "frag grenade" for _, i in back.sim.items)
    roguelike.delete_save(path)
    assert not roguelike.has_save(path)


def test_a_bad_save_is_an_error_not_a_crash(tmp_path):
    path = tmp_path / "run.sav"
    path.write_bytes(b"not a save")
    with pytest.raises(roguelike.SaveError):
        roguelike.load(path)


# -- healing and medicine -----------------------------------------------------------------
def test_bleeding_must_stop_before_blood_comes_back(content):
    sim = yard(content)
    c = sim.spawn("human", "a", (5, 4, 0))
    c.body.blood, c.body.bleed_rate = 70.0, 0.5
    c.body.recover(60)
    assert c.body.blood == 70.0
    c.body.bleed_rate = 0.0
    c.body.recover(60)
    assert c.body.blood > 70.0


def test_splinted_breaks_knit_and_unsplinted_ones_do_not(content):
    sim = yard(content)
    c = sim.spawn("human", "a", (5, 4, 0))
    for part in ("l_arm", "r_leg"):
        for _ in range(5):
            if not c.body.part(part).fractured:
                combat.deal_damage(sim, c, 5, "crush", part, knockback_ok=False)
    arm, leg = c.body.part("l_arm"), c.body.part("r_leg")
    assert arm.fractured and leg.fractured
    arm.splinted = True
    pain_before = c.pain()
    for _ in range(40):
        c.body.recover(60)
    assert not arm.fractured and not arm.crippled  # half an hour splinted: it's knit
    assert leg.fractured and leg.crippled          # never set: it stays broken
    assert c.pain() <= pain_before


def test_medicine(content):
    sim = yard(content)
    c = sim.spawn("operative", "a", (5, 4, 0))
    c.skills["first_aid"] = 18
    for iid in ("bandage", "splint", "trauma_kit", "painkillers", "blood_bag"):
        c.carried.append(Item(content.get("item", iid)))
    kit = {i.id: i for i in c.carried}
    assert actions.cannot_use(sim, c, kit["bandage"]) == "you're not bleeding"
    c.body.bleed_rate, c.body.internal_bleed, c.body.blood = 1.2, 0.3, 60.0
    assert actions.use_item(sim, c, kit["bandage"]) is not None
    assert c.body.bleed_rate < 0.7 and kit["bandage"] not in c.carried
    actions.use_item(sim, c, kit["blood_bag"])  # (operating while faint from blood loss goes badly)
    assert c.body.blood == 90.0
    for _ in range(6):
        if c.body.internal_bleed == 0:
            break
        c.carried.append(kit["trauma_kit"])
        actions.use_item(sim, c, kit["trauma_kit"])
    assert c.body.internal_bleed == 0
    combat.deal_damage(sim, c, 7, "crush", "l_leg", knockback_ok=False)
    assert c.body.part("l_leg").fractured
    before = c.pain()
    actions.use_item(sim, c, kit["splint"])
    assert c.body.part("l_leg").splinted
    actions.use_item(sim, c, kit["painkillers"])
    assert c.has_status("painkillers") and c.pain() <= before


# -- skills --------------------------------------------------------------------------------
def test_skills_grow_with_use(content):
    sim = yard(content)
    c = sim.spawn("human", "a", (5, 4, 0))
    c.controller = "player"
    start = c.skill("knife")
    for _ in range(40):
        training.practice(sim, c, "knife", 10, True)
    assert c.skill("knife") > start
    assert any("knife improves" in line for line in sim.lines)


def test_easy_and_hopeless_rolls_teach_nothing(content):
    sim = yard(content)
    c = sim.spawn("human", "a", (5, 4, 0))
    c.controller = "player"
    for _ in range(100):
        training.practice(sim, c, "guns", 17, True)
        training.practice(sim, c, "guns", 3, False)
    assert c.practice.get("guns", 0) == 0


def test_npcs_do_not_learn(content):
    sim = yard(content)
    c = sim.spawn("human", "a", (5, 4, 0))
    for _ in range(100):
        training.practice(sim, c, "knife", 10, True)
    assert c.practice == {}
