"""Holds: joint locks, neck snaps, tearing limbs off and swinging them."""
from rotengine import actions, combat
from rotengine.creature import Item

from test_stealth import yard


def held(content, attacker="operative", victim="sentry", seed=0, st=None, out=False):
    sim = yard(content, seed=seed)
    t = sim.spawn(victim, "t", (10, 4, 0))
    t.facing = (1, 0)  # back to the attacker
    a = sim.spawn(attacker, "a", (9, 4, 0))
    if st is not None:
        a.base_stats["ST"] = st
    assert actions.grab(sim, a, t) is not None
    for _ in range(20):
        if a.grappling is t:
            break
        actions.grab(sim, a, t)
    assert a.grappling is t
    if out:
        combat.knock_out(sim, t)
    return sim, a, t


def test_a_normal_person_breaks_an_arm_but_cannot_tear_it_off(content):
    sim, a, t = held(content, out=True)
    for _ in range(40):
        actions.wrench(sim, a, "r_arm")
    arm = t.body.part("r_arm")
    assert arm.fractured and not arm.destroyed  # cranking breaks it; it never comes off
    assert t.wielded is None  # the gun hand's arm is broken: the pistol falls
    assert any("elbow snapped" in line for line in sim.lines)


def test_armor_does_not_stop_a_joint_lock(content):
    sim = yard(content)
    swat = sim.spawn("swat", "t", (10, 4, 0))
    assert swat.dr("torso", "pierce") > 0
    assert swat.dr("torso", "wrench") == 0
    assert swat.dr("neck", "wrench") == 5  # the neck's own strength still counts


def test_nobody_wrenches_the_hulk(content):
    sim, a, t = held(content, victim="hulk", seed=1)
    for part in actions.wrenchable(t):
        avg, tries, p_tear = actions.wrench_odds(a, t, part)
        assert avg == 0 and p_tear == 0


def test_the_hulk_tears_an_arm_off_and_keeps_it(content):
    sim, hulk, t = held(content, attacker="hulk")
    actions.wrench(sim, hulk, "r_arm")
    assert t.body.part("r_arm").destroyed and t.body.part("r_hand").destroyed
    assert t.body.bleed_rate > 1.0  # arterial: this one bleeds out without a tourniquet
    assert hulk.wielded is not None and hulk.wielded.name == f"{t.name}'s right arm"
    assert any("arm torn off" in line for line in sim.lines)


def test_the_hulk_twists_a_head_off(content):
    sim, hulk, t = held(content, attacker="hulk", seed=2)
    actions.wrench(sim, hulk, "neck")
    assert t.dead and t.death_cause == "head torn off"
    assert hulk.wielded.name == f"{t.name}'s head"


def test_a_snapped_neck_is_lethal_in_minutes(content):
    sim, a, t = held(content, st=20, out=True, seed=3)
    for _ in range(10):
        if t.body.part("neck").fractured:
            break
        actions.wrench(sim, a, "neck")
    assert t.has_status("broken_neck") and not t.dead
    actions.release(sim, a)
    for second in range(400):
        sim._tick_one(t, second)
        if t.dead:
            break
    assert t.dead and t.death_cause == "broken neck"
    assert 120 <= second <= 300


def test_a_torn_off_arm_is_a_club_and_a_missile(content):
    sim, hulk, t = held(content, attacker="hulk", seed=4)
    actions.wrench(sim, hulk, "r_arm")
    actions.release(sim, hulk)
    other = sim.spawn("sentry", "o", (8, 4, 0))
    plans = combat.attack_plans(sim, hulk, other)
    assert any(p.item is hulk.wielded and p.attack["skill"] == "clubs" for p in plans)
    far = sim.spawn("sentry", "f", (20, 4, 0))
    arm = hulk.wielded
    assert arm in actions.throwables(hulk)
    assert actions.throw(sim, hulk, arm, far.pos) is not None
    assert hulk.wielded is None
    assert any(i is arm for _, i in sim.items)


def test_a_sword_cut_leaves_the_arm_on_the_floor(content):
    sim = yard(content)
    t = sim.spawn("sentry", "t", (10, 4, 0))
    for _ in range(10):
        if t.body.part("l_arm").destroyed:
            break
        combat.deal_damage(sim, t, 12, "cut", "l_arm")
    assert t.body.part("l_arm").note == "arm cut off"
    assert any(i.name == f"{t.name}'s left arm" for _, i in sim.items)
    p = sim.spawn("thug", "p", (9, 4, 0))
    p.wielded = None
    assert actions.pick_up(sim, p) is not None and p.wielded.name == f"{t.name}'s left arm"


def test_decapitation_by_blade(content):
    sim = yard(content)
    t = sim.spawn("sentry", "t", (10, 4, 0))
    for _ in range(10):
        if t.dead:
            break
        combat.deal_damage(sim, t, 15, "cut", "neck")
    assert t.dead and t.death_cause == "decapitated"
    assert any(i.name == f"{t.name}'s head" for _, i in sim.items)


def test_takedown_pins_and_disarm_takes_the_gun(content):
    sim, a, t = held(content, seed=5)
    for _ in range(10):
        if t.has_status("prone"):
            break
        actions.takedown(sim, a)
    assert t.has_status("prone") and a.grappling is t
    for _ in range(10):
        if t.wielded is None:
            break
        actions.disarm(sim, a)
    assert t.wielded is None and any(i.id == "pistol" or "pistol" in i.name for _, i in sim.items)


def test_picking_up_a_live_grenade_comes_first(content):
    sim = yard(content)
    p = sim.spawn("operative", "p", (9, 4, 0))
    bat = Item(content.get("item", "baseball_bat"))
    nade = Item(content.get("item", "frag_grenade"))
    nade.armed = True
    sim.drop((10, 4, 0), bat)
    sim.drop((10, 4, 0), nade)
    p.wielded = None
    actions.pick_up(sim, p)
    assert nade in p.carried and nade in actions.throwables(p)


def test_brute_ai_grabs_and_tears(content):
    seen = set()
    for seed in range(6):
        sim = yard(content, seed=seed)
        sim.start_aware = True
        sim.spawn("hulk", "g", (9, 4, 0))
        for i in range(3):
            sim.spawn("sentry", "s", (12, 3 + i, 0))
        sim.run(max_ms=60_000)
        text = "\n".join(sim.lines)
        seen |= {w for w in ("torn off", "is left holding", "in a hold") if w in text}
    assert seen == {"torn off", "is left holding", "in a hold"}
