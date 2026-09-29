"""Regression tests for bugs found in review."""
import pickle

from rotengine import actions, combat, grapple, perception
from rotengine.sim import Sim
from rotengine.world import World

from test_stealth import yard


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


# -- found by fuzzing ----------------------------------------------------------
def test_waking_under_someone_shifts_over(content):
    sim = open_arena(content)
    a = sim.spawn("street_tough", "a", (3, 4, 0))
    b = sim.spawn("street_tough", "a", (4, 4, 0))
    combat.knock_out(sim, a)
    b.pos = a.pos  # e.g. knocked back onto the body
    del a.statuses["unconscious"]
    sim.make_room(a)
    assert a.pos != b.pos and sim.world.standable(a.pos)


def test_ai_does_not_path_onto_bodies(content):
    from rotengine import ai
    sim = open_arena(content, w=5, h=1)
    walker = sim.spawn("street_tough", "a", (0, 0, 0))
    body = sim.spawn("street_tough", "b", (2, 0, 0))
    goal = sim.spawn("street_tough", "b", (4, 0, 0))
    combat.knock_out(sim, body)
    assert ai._step_toward(sim, walker, goal.pos) is None  # a 1-wide corridor blocked by a body


def test_falling_onto_someone(content):
    sim = open_arena(content, d=2)
    sim.world.set_floor((5, 4, 1), None)
    below = sim.spawn("soldier", "a", (5, 4, 0))
    faller = sim.spawn("soldier", "b", (5, 4, 1))
    sim.check_fall(faller)
    assert any("lands on soldier" in line for line in sim.lines)
    assert faller.pos != below.pos and faller.pos[2] == 0
    assert below.has_status("prone")


def test_corpses_take_no_statuses(content):
    sim = open_arena(content)
    c = sim.spawn("street_tough", "a", (3, 4, 0))
    combat.kill(sim, c, "test")
    sim.apply_status(c, "stunned", 2000)
    c.add_status("prone", None)
    assert c.statuses == {}


def test_shooting_down_doesnt_hit_your_own_floor(content):
    world = World(content.all("material"), 6, 3, 2)
    for x in range(6):
        for y in range(3):
            world.set_floor((x, y, 0), "concrete")
    world.set_floor((1, 1, 1), "glass")  # the shooter stands on a glass ledge
    assert world.crossing((1, 1, 1), (2, 1, 0)) == (2, 1, 1)
    assert world.obstacles((1, 1, 1), (3, 2, 0)) == []


def test_bad_content_is_reported_not_raised():
    from rotengine.content import Content
    c = Content()
    c.add({"type": "damage_type", "id": "crush"})
    c.add({"type": "status", "id": "stunned"})
    c.add({"type": "power", "id": "a", "name": "a"})                                   # no effects
    c.add({"type": "power", "id": "b", "name": "b", "effects": [], "ai_condition": {"and": 5}})
    c.add({"type": "trait", "id": "c", "hooks": [1, 2]})
    c.add({"type": "trait", "id": "d", "hooks": {"on_second": [{"add_status": {"id": "nope"}}]}})
    c.add({"type": "power", "id": "e", "name": "e",
           "effects": [{"damage": {"amount": "2x6", "type": "fire"}}]})
    c.add({"type": "trait", "id": "base", "tags": ["x"]})
    c.add({"type": "trait", "id": "f", "copy-from": "base", "relative": {"tags": 1}})
    errors = "\n".join(c.validate())
    for expected in ("missing required field 'effects'", "must be a list of conditions",
                     "'hooks' must be an object", "unknown status 'nope'", "bad dice expression",
                     "unknown damage_type 'fire'", "only numbers can be relative"):
        assert expected in errors, expected


def test_bad_spawn_positions_are_rejected(content):
    import pytest
    from rotengine import arena
    base = {"id": "t", "levels": [["#....", "....."]], "teams": {"b": [{"creature": "thug", "at": [4, 1, 0]}]}}
    for group in ({"creature": "thug", "at": [0, 0, 0]},                # inside a wall
                  {"creature": "thug", "at": [2, 0, 0], "count": 2},    # stacked
                  {"creature": "thug", "at": [9, 9, 0]}):               # out of bounds
        with pytest.raises(arena.ScenarioError):
            arena.build({**base, "teams": {**base["teams"], "a": [group]}}, content, 0)


# -- second review ------------------------------------------------------------
def test_a_shot_from_nowhere_still_cant_be_dodged(content):
    """The bang arrives with the bullet: hearing it doesn't let you dodge it."""
    dodged = 0
    for seed in range(60):
        sim = yard(content, seed=seed)
        shooter = sim.spawn("soldier", "a", (2, 4, 0))
        t = sim.spawn("human", "b", (12, 4, 0))
        t.facing = (-1, 0)  # looking right at him, but hasn't noticed him
        plan = next(p for p in combat.attack_plans(sim, shooter, t, allow_aim=False)
                    if p.attack["kind"] == "ranged" and p.attack.get("rof", 1) == 1)
        combat.resolve_attack(sim, shooter, t, plan)
        sim._loop(lambda: True, sim.time + 200)
        dodged += any("dodges" in line for line in sim.lines)
    assert dodged == 0


def test_a_knockout_timer_doesnt_wake_someone_in_no_state_to(content):
    sim = yard(content)
    c = sim.spawn("human", "a", (5, 4, 0))
    combat.knock_out(sim, c, "", 1000)
    c.body.blood = 40
    sim.time += 2000
    sim.expire_statuses(c)
    assert not c.conscious and c.statuses["unconscious"] is None


def test_the_dead_let_go(content):
    sim = yard(content)
    a = sim.spawn("operative", "a", (5, 4, 0))
    b = sim.spawn("human", "b", (6, 4, 0))
    grapple._hold(a, b, "neck", rear=True)
    combat.kill(sim, a, "test")
    assert a.grappling is None and b.grappled_by is None


def test_no_drawing_a_knife_into_a_held_arm(content):
    sim = yard(content)
    s = sim.spawn("soldier", "s", (10, 4, 0))
    op = sim.spawn("operative", "o", (9, 4, 0))
    grapple._hold(op, s, "r_arm", rear=False)
    knife = next(i for i in s.carried if i.attacks and not i.data.get("two_handed"))
    assert actions.wield(sim, s, knife) is None


def test_a_critical_burst_always_hits_something(content):
    sim = yard(content)
    a = sim.spawn("thug", "a", (4, 4, 0))
    b = sim.spawn("human", "b", (5, 4, 0))
    perception.make_all_aware(sim)
    sim.timed_shots = False
    plan = next(p for p in combat.attack_plans(sim, a, b, allow_aim=False) if p.attack.get("rof", 1) > 1)
    plan.skill = 3
    sim.rng.randint = lambda lo, hi: 1  # every die a 1: a roll of 3, a critical
    combat.resolve_attack(sim, a, b, plan)
    assert b.hp < b.max_hp


def test_saves_dont_depend_on_pickling_iterators(content):
    sim = yard(content)
    sim.spawn("human", "a", (5, 4, 0))
    back = pickle.loads(pickle.dumps(sim))
    back.schedule_event(back.time + 10, lambda: None)
    assert isinstance(back._seq_n, int) and isinstance(back._event_n, int)


def test_flashes_arent_replayed_when_the_list_is_trimmed(content):
    sim = yard(content)
    for _ in range(450):
        sim.cue((1, 1, 0), "hit")
    assert sim.fx_seq == 450 and len(sim.fx) <= 200


# -- round 4: reactions, force, organs ---------------------------------------------
def test_stepping_into_reach_gives_you_the_first_blow(content):
    """An NPC needs a moment to answer someone stepping in: the player's
    next action comes first."""
    sim = yard(content, seed=1)
    p = sim.spawn("street_tough", "p", (4, 4, 0))
    e = sim.spawn("street_tough", "e", (7, 4, 0))
    p.controller = "player"
    perception.make_all_aware(sim)
    e.facing = (-1, 0)
    sim._schedule(e, sim.time + 5)  # ready to go
    from rotengine import actions as acts
    sim.awaiting = p
    sim.player_act(acts.step(sim, p, (5, 4, 0)) or 1)
    sim.advance()
    sim.player_act(acts.step(sim, p, (6, 4, 0)) or 1)  # now in reach
    assert sim.advance() == "player"
    assert not any("e punches" in line or "e kicks" in line for line in sim.lines)


def test_a_missed_ambush_leaves_the_victim_a_beat_behind(content):
    sim = yard(content, seed=2)
    a = sim.spawn("operative", "a", (5, 4, 0))
    t = sim.spawn("sentry", "t", (6, 4, 0))
    t.facing = (1, 0)  # back to him
    sim._schedule(t, sim.time + 1)
    plan = combat.best_attack_plan(sim, a, t, allow_aim=False)
    combat.resolve_attack(sim, a, t, plan)
    assert t.dead or not t.conscious or t.next_time >= sim.time + 500


def test_the_hulk_sends_people_flying(content):
    sim = yard(content)
    hulk = sim.spawn("hulk", "h", (3, 4, 0))
    s = sim.spawn("soldier", "s", (4, 4, 0))
    human = sim.spawn("human", "a", (10, 4, 0))
    assert combat.knockback_tiles(18, s, hulk) >= 4
    assert combat.knockback_tiles(6, human, sim.spawn("human", "b", (12, 4, 0))) == 0


def test_parrying_the_hulk_does_not_work(content):
    sim = yard(content, seed=3)
    hulk = sim.spawn("hulk", "h", (3, 4, 0))
    g = sim.spawn("guard", "g", (4, 4, 0))
    plan = combat.best_attack_plan(sim, hulk, g, allow_aim=False)
    assert not combat._parry_holds(sim, hulk, g, plan)


def test_throwing_someone_over_your_shoulder(content):
    from rotengine import flight, physics
    sim = yard(content, seed=1)
    c = sim.spawn("hulk", "h", (10, 4, 0))
    t = sim.spawn("human", "t", (11, 4, 0))
    grapple._hold(c, t, "torso", rear=False)
    physics.fling(sim, c, t, (-1, 0))  # behind you
    flight.finish(sim)
    assert t.pos[0] < 10 and not any("crashes into h" in line for line in sim.lines)


def test_a_bullet_in_the_chest_says_what_it_hit(content):
    hits = set()
    for seed in range(40):
        sim = yard(content, seed=seed)
        c = sim.spawn("human", "a", (5, 4, 0))
        combat.deal_damage(sim, c, 10, "pierce", "torso")
        hits |= set(c.body.organs)
    assert len(hits) >= 3 and hits <= {"left lung", "right lung", "liver", "spleen", "stomach", "spine"}


def test_a_blunt_blow_breaks_a_limb_long_before_it_pulps_it(content):
    sim = yard(content)
    c = sim.spawn("human", "a", (5, 4, 0))
    combat.deal_damage(sim, c, 12, "crush", "l_arm", knockback_ok=False)
    arm = c.body.part("l_arm")
    assert arm.fractured and not arm.destroyed
    combat.deal_damage(sim, c, 25, "crush", "r_leg", knockback_ok=False)
    leg = c.body.part("r_leg")
    assert leg.destroyed and "pulp" in leg.note
    assert c.body.bleed_rate >= 2.0 and c.pain() >= 4


def test_dislocations_happen_and_can_be_put_back(content):
    from rotengine import actions as acts
    sim = yard(content, seed=4)
    c = sim.spawn("operative", "a", (5, 4, 0))
    for _ in range(20):
        combat.deal_damage(sim, c, 6, "wrench", "r_arm", knockback_ok=False)
        if c.body.part("r_arm").dislocated:
            break
        c.body.part("r_arm").damage = c.body.part("r_arm").counted = 0
    arm = c.body.part("r_arm")
    assert arm.dislocated and not c.body.is_functional("r_hand")
    for _ in range(10):
        if not arm.dislocated:
            break
        acts.reset_joint(sim, c, c)
    assert not arm.dislocated


def test_a_rib_can_be_driven_into_a_lung(content):
    punctured = 0
    for seed in range(60):
        sim = yard(content, seed=seed)
        c = sim.spawn("human", "a", (5, 4, 0))
        c.body.part("torso").fractured = True
        combat.deal_damage(sim, c, 6, "crush", "torso", knockback_ok=False)
        punctured += any("rib is driven" in line for line in sim.lines)
    assert 5 <= punctured <= 35


def test_a_small_miss_on_a_knockdown_roll_just_staggers(content):
    staggered = knocked = 0
    for seed in range(80):
        sim = yard(content, seed=seed)
        c = sim.spawn("human", "a", (5, 4, 0))
        combat.deal_damage(sim, c, 8, "crush", "torso", knockback_ok=False)
        text = "\n".join(sim.lines)
        staggered += "staggers" in text
        knocked += "knocked down" in text
    assert staggered and knocked
