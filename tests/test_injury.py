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


def test_blunt_beating_puts_you_down_long_before_it_kills(content):
    downed = out = dead = 0
    for seed in range(30):
        sim = open_arena(content, seed=seed)
        c = sim.spawn("street_tough", "a", (3, 3, 0))
        for _ in range(4):  # a bad beating: four hard blows to the body
            combat.deal_damage(sim, c, 7, "crush", "torso", knockback_ok=False)
        downed += c.has_status("prone") or not c.conscious
        out += not c.conscious
        dead += c.dead
    assert dead == 0
    assert downed >= 25          # on the floor, one way or another
    assert out <= 3              # but body blows don't switch you off


def test_going_into_the_red_is_not_a_knockout(content):
    """A cut that takes you just below 0 HP hurts, and may drop you, but
    you're almost always still awake."""
    awake = 0
    for seed in range(40):
        sim = open_arena(content, seed=seed)
        c = sim.spawn("soldier", "a", (3, 3, 0))
        combat.deal_damage(sim, c, 9, "cut", "l_arm")    # hurts
        combat.deal_damage(sim, c, 9, "cut", "torso")    # and now in the red
        assert c.hp < 0
        awake += c.conscious
    assert awake >= 34


def test_head_blows_can_knock_you_out_body_blows_only_put_you_down(content):
    head = body = 0
    for seed in range(40):
        sim = open_arena(content, seed=seed)
        a = sim.spawn("street_tough", "a", (3, 3, 0))
        b = sim.spawn("street_tough", "b", (6, 3, 0))
        combat.deal_damage(sim, a, 6, "crush", "face", knockback_ok=False)
        combat.deal_damage(sim, b, 6, "crush", "torso", knockback_ok=False)
        head += not a.conscious
        body += not b.conscious
    assert head >= 5 and body == 0


def test_collapsed_but_still_shooting(content):
    sim = open_arena(content, seed=1)
    c = sim.spawn("thug", "a", (3, 3, 0))
    foe = sim.spawn("thug", "b", (9, 3, 0))
    combat.collapse(sim, c, "from the pain")
    assert c.conscious and c.has_status("prone") and c.can_act
    assert combat.best_attack_plan(sim, c, foe) is not None  # a pistol from the floor
    from rotengine import actions
    assert actions.stand_up(sim, c) is None                  # but not getting up yet


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
    assert c.conscious and c.has_status("collapsed")  # down, not out


def test_first_aid_needs_skill_for_arteries(content):
    sim = open_arena(content, seed=2)
    medic = sim.spawn("soldier", "a", (4, 3, 0))
    c = sim.spawn("street_tough", "a", (3, 3, 0))
    c.body.bleed_rate = 0.3
    ai._first_aid(sim, medic, allies=True)
    assert c.body.bleed_rate < 0.3 or any("fumbles" in line for line in sim.lines)


def test_slashes_open_you_up_but_do_not_reach_organs(content):
    sim = open_arena(content)
    slashed = sim.spawn("human", "a", (3, 3, 0))
    stabbed = sim.spawn("human", "b", (6, 3, 0))
    for _ in range(3):
        combat.deal_damage(sim, slashed, 6, "cut", "vitals")
        combat.deal_damage(sim, stabbed, 6, "impale", "vitals")
    assert slashed.body.internal_bleed == 0 and not slashed.body.part("vitals").destroyed
    assert slashed.body.bleed_rate > 0.5  # it still bleeds, outside
    assert stabbed.body.internal_bleed > 0 and stabbed.body.part("vitals").destroyed


def test_a_limb_comes_off_in_one_blow_or_not_at_all(content):
    sim = open_arena(content)
    c = sim.spawn("human", "a", (3, 3, 0))
    for _ in range(6):  # a knife nicking away at an arm cripples it, it doesn't take it off
        combat.deal_damage(sim, c, 5, "cut", "l_arm")
    assert c.body.part("l_arm").crippled and not c.body.part("l_arm").destroyed
    combat.deal_damage(sim, c, 8, "cut", "r_arm")  # one heavy blow (12 injury, HP 10) does
    assert c.body.part("r_arm").destroyed


def test_knife_modes_trade_accuracy_for_depth(content):
    sim = open_arena(content)
    c = sim.spawn("street_tough", "a", (3, 3, 0))
    t = sim.spawn("street_tough", "b", (4, 3, 0))
    plans = combat.attack_plans(sim, c, t, allow_aim=False)
    slash = next(p for p in plans if p.attack["id"] == "slash" and p.location is None and not p.deceptive)
    stab = next(p for p in plans if p.attack["id"] == "stab" and p.location is None and not p.deceptive)
    assert slash.skill == stab.skill + 2


def test_a_cut_to_the_head_does_not_knock_you_out(content):
    out = 0
    for seed in range(40):
        sim = open_arena(content, seed=seed)
        c = sim.spawn("street_tough", "a", (3, 3, 0))
        combat.deal_damage(sim, c, 5, "cut", "face")
        out += not c.conscious
    assert out == 0


def test_a_knockout_blow_wears_off(content):
    for seed in range(60):  # a club to the head, until one lands a knockout
        sim = open_arena(content, seed=seed)
        c = sim.spawn("street_tough", "a", (3, 3, 0))
        combat.deal_damage(sim, c, 4, "crush", "skull", knockback_ok=False)
        if not c.conscious and not c.dead:
            break
    assert not c.conscious and c.statuses["unconscious"] is not None  # timed, not forever
    for s in range(200):
        sim.time += 1000
        sim._tick_one(c, s)
        if c.conscious:
            break
    assert c.conscious or c.dead


def test_bleeding_out_is_a_threshold(content):
    """Faint from blood loss and you may come round, but not below 55%."""
    sim = open_arena(content, seed=2)
    c = sim.spawn("human", "a", (3, 3, 0))
    combat.knock_out(sim, c, "test")
    c.body.blood = 52
    for s in range(60):
        sim._tick_one(c, s)
    assert not c.conscious  # too little blood to wake
    c.body.blood = 80
    for s in range(60, 200):
        sim._tick_one(c, s)
        if c.conscious:
            break
    assert c.conscious


def test_helpless_and_downed_targets_are_easier_to_hit(content):
    sim = open_arena(content, seed=1)
    a = sim.spawn("street_tough", "a", (3, 3, 0))
    t = sim.spawn("street_tough", "b", (4, 3, 0))
    knife = next(x for x, _ in a.attacks() if x["id"] == "slash")
    standing = combat.base_skill(sim, a, t, knife, a.wielded)
    t.add_status("prone", None)
    prone = combat.base_skill(sim, a, t, knife, a.wielded)
    combat.knock_out(sim, t, "test")
    out = combat.base_skill(sim, a, t, knife, a.wielded)
    assert standing < prone < out
