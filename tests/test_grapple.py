"""Holds: grab a thing first, then work on it. Joint locks, neck snaps,
strangling, crushing, tearing limbs off and swinging them, and grabbing
weapons."""
from rotengine import actions, combat, flight, grapple, perception
from rotengine.creature import Item

from test_stealth import yard


def held(content, grip, attacker="operative", victim="sentry", seed=0, st=None, out=False):
    sim = yard(content, seed=seed)
    t = sim.spawn(victim, "t", (10, 4, 0))
    t.facing = (1, 0)  # back to the attacker
    a = sim.spawn(attacker, "a", (9, 4, 0))
    if st is not None:
        a.base_stats["ST"] = st
    if out:
        combat.knock_out(sim, t)
    for _ in range(30):
        if a.grappling is t:
            break
        grapple.grab(sim, a, t, grip)
    assert a.grappling is t and a.hold == grip
    return sim, a, t


def test_you_can_only_work_on_what_you_hold(content):
    sim, a, t = held(content, "r_arm")
    assert "wrench" in grapple.moves(a) and "choke" not in grapple.moves(a)
    assert grapple.choke(sim, a) is None           # not holding the neck
    assert grapple.wrench(sim, a, "l_arm") is None  # holding the other arm
    sim, a, t = held(content, "neck", seed=1)
    assert {"choke", "strangle", "wrench"} <= set(grapple.moves(a))
    assert "squeeze" not in grapple.moves(a)        # squeezing a neck is strangling
    sim, a, t = held(content, "torso", seed=2)
    assert "squeeze" in grapple.moves(a) and "wrench" not in grapple.moves(a)


def test_holding_the_gun_arm_stops_the_gun(content):
    sim, a, t = held(content, "r_arm")
    assert t.wielded is not None
    assert not any(item is t.wielded for _, item in t.attacks())
    assert any(atk["id"] == "punch" for atk, _ in t.attacks())  # the other hand still works
    grapple.release(sim, a)
    assert any(item is t.wielded for _, item in t.attacks())


def test_changing_grip_is_a_new_grab(content):
    sim, a, t = held(content, "torso", seed=3)
    for _ in range(20):
        if a.hold == "neck":
            break
        grapple.grab(sim, a, t, "neck")
        assert a.grappling is t  # a miss keeps the old grip
    assert a.hold == "neck"


def test_a_throat_grip_silences_and_an_arm_grip_does_not(content):
    sim, a, t = held(content, "neck")
    assert grapple.silenced(t)
    sim, a, t = held(content, "l_arm", seed=1)
    assert not grapple.silenced(t)


def test_a_normal_person_breaks_an_arm_but_cannot_tear_it_off(content):
    sim, a, t = held(content, "r_arm", out=True)
    for _ in range(40):
        grapple.wrench(sim, a)
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
    sim = yard(content)
    hulk = sim.spawn("hulk", "t", (10, 4, 0))
    a = sim.spawn("operative", "a", (9, 4, 0))
    for part in grapple.grab_targets(hulk):
        if part != grapple.WEAPON:
            for kind in ("wrench", "squeeze"):
                avg, tries, p_tear = grapple.odds(a, hulk, hulk.body.part(part), kind)
                assert avg == 0 and p_tear == 0


def test_the_hulk_tears_off_the_arm_he_holds_and_keeps_it(content):
    sim, hulk, t = held(content, "r_arm", attacker="hulk")
    grapple.wrench(sim, hulk)
    assert t.body.part("r_arm").destroyed and t.body.part("r_hand").destroyed
    assert t.body.bleed_rate > 1.0  # arterial: this one bleeds out without a tourniquet
    assert hulk.grappling is None  # he's holding the arm now, not the man
    assert hulk.wielded is not None and hulk.wielded.name == f"{t.name}'s right arm"
    assert any("arm torn off" in line for line in sim.lines)


def test_the_hulk_twists_a_head_off(content):
    sim, hulk, t = held(content, "neck", attacker="hulk", seed=2)
    grapple.wrench(sim, hulk)
    assert t.dead and t.death_cause == "head torn off"
    assert hulk.wielded.name == f"{t.name}'s head"


def test_a_snapped_neck_is_lethal_in_minutes(content):
    sim, a, t = held(content, "neck", st=20, out=True, seed=3)
    for _ in range(10):
        if t.body.part("neck").fractured:
            break
        grapple.wrench(sim, a)
    assert t.has_status("broken_neck") and not t.dead
    grapple.release(sim, a)
    for second in range(400):
        sim._tick_one(t, second)
        if t.dead:
            break
    assert t.dead and t.death_cause == "broken neck"
    assert 120 <= second <= 300


def test_strangling_goes_round_by_round(content):
    sim, a, t = held(content, "neck", seed=4)
    rounds = 0
    while t.conscious and rounds < 30:
        grapple.strangle(sim, a)
        rounds += 1
    assert not t.conscious and rounds >= 2  # not instant
    assert t.body.part("neck").damage < t.max_hp  # a normal grip barely hurts the throat


def test_a_strong_grip_crushes_the_windpipe(content):
    sim, a, t = held(content, "neck", st=22, seed=5)
    for _ in range(10):
        if t.has_status("crushed_windpipe") or t.dead:
            break
        grapple.strangle(sim, a)
    assert t.has_status("crushed_windpipe")
    grapple.release(sim, a)
    for second in range(200):
        sim._tick_one(t, second)
        if t.dead:
            break
    assert t.dead and t.death_cause == "crushed windpipe"


def test_bear_hug_breaks_ribs(content):
    sim, hulk, t = held(content, "torso", attacker="hulk", seed=6)
    grapple.squeeze(sim, hulk)
    assert t.body.part("torso").fractured and t.body.internal_bleed > 0


def test_a_torn_off_arm_is_a_club_and_a_missile(content):
    sim, hulk, t = held(content, "r_arm", attacker="hulk", seed=4)
    grapple.wrench(sim, hulk)
    other = sim.spawn("sentry", "o", (8, 4, 0))
    plans = combat.attack_plans(sim, hulk, other)
    assert any(p.item is hulk.wielded and p.attack["skill"] == "clubs" for p in plans)
    far = sim.spawn("sentry", "f", (20, 4, 0))
    arm = hulk.wielded
    assert arm in actions.throwables(hulk)
    assert actions.throw(sim, hulk, arm, far.pos) is not None
    assert hulk.wielded is None
    flight.finish(sim)
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


def test_takedown_from_a_leg_and_disarm_from_the_arm(content):
    sim, a, t = held(content, "l_leg", seed=5)
    for _ in range(10):
        if t.has_status("prone"):
            break
        grapple.takedown(sim, a)
    assert t.has_status("prone") and a.grappling is t
    assert "disarm" not in grapple.moves(a)  # a leg grip can't reach the gun
    sim, a, t = held(content, "r_arm", seed=6)
    for _ in range(10):
        if t.wielded is None:
            break
        grapple.disarm(sim, a)
    assert t.wielded is None and any("pistol" in i.name for _, i in sim.items)


def test_grabbing_a_weapon_and_wresting_it_away(content):
    sim, a, t = held(content, grapple.WEAPON, seed=7)
    gun = t.wielded
    assert not any(item is gun for _, item in t.attacks())  # can't shoot what you're both holding
    a.wielded = None
    for _ in range(20):
        if a.grappling is None:
            break
        grapple.wrest(sim, a)
    assert a.wielded is gun and t.wielded is None


def facing_off(content, seed, victim):
    sim = yard(content, seed=seed)
    t = sim.spawn(victim, "t", (10, 4, 0))
    t.facing = (-1, 0)  # facing you, and he knows you're there
    a = sim.spawn("human", "a", (9, 4, 0))
    a.facing = (1, 0)
    perception.make_all_aware(sim)
    return sim, a, t


def test_grabbing_for_a_blade_can_cost_you_your_fingers(content):
    hurt = 0
    for seed in range(30):
        sim, a, t = facing_off(content, seed, "street_tough")
        grapple.grab(sim, a, t, grapple.WEAPON)
        if a.grappling is None and any(p.damage for p in a.body.parts.values()):
            hurt += 1
            assert any("edge of" in line for line in sim.lines)
    assert hurt >= 10  # a knife in a fighter's hand is not a thing to grab at


def test_grabbing_for_a_gun_can_get_you_shot(content):
    shot = 0
    for seed in range(30):
        sim, a, t = facing_off(content, seed, "thug")
        grapple.grab(sim, a, t, grapple.WEAPON)
        shot += any("fires as" in line for line in sim.lines)
    assert shot >= 10


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
    for seed in range(10):
        sim = yard(content, seed=seed)
        sim.start_aware = True
        hulk = sim.spawn("hulk", "g", (9, 4, 0))
        hulk.powers = []  # no thunderclap or hurl: hands only
        for pos in ((12, 2, 0), (18, 6, 0), (24, 2, 0)):
            sim.spawn("sentry", "s", pos)
        sim.run(max_ms=60_000)
        text = "\n".join(sim.lines)
        seen |= {w for w in ("torn off", "is left holding", "grabs", "bear hug", "head around") if w in text}
    assert {"torn off", "is left holding", "grabs"} <= seen and seen & {"bear hug", "head around"}


def test_dragging_someone_who_fights_it(content):
    """You can haul a conscious person straight away, but each step is a
    contest: sometimes they dig in and you get nowhere."""
    moved = stuck = 0
    for seed in range(20):
        sim, a, t = held(content, "torso", seed=seed)
        for _ in range(4):
            before = a.pos
            actions.step(sim, a, (a.pos[0] - 1, a.pos[1], 0))
            if a.pos != before:
                moved += 1
                assert t.pos == before  # they come along
            else:
                stuck += 1
    assert moved > 0 and stuck > 0


def test_drag_straight_from_the_hold_menu():
    from test_play import arena, new_app
    from rotengine.ui.game import GameScreen
    scenario = {"id": "yard", "name": "Yard", "start_alert": False, "levels": [["," * 12] * 5],
                "teams": {"you": [{"creature": "hulk", "at": [5, 2, 0]}],
                          "them": [{"creature": "sentry", "at": [6, 2, 0], "facing": [1, 0]}]}}
    app = new_app()
    content = app.content_for(scenario)
    sim = arena.build(scenario, content, seed=1)
    app.screen = g = GameScreen(app, scenario, content, sim, sim.creatures[0], 1)
    p, t = g.player, sim.creatures[1]
    grapple._hold(p, t, "torso", rear=True)
    g.mode = "grapple"
    app.handle_key("left")
    assert p.pos == (4, 2, 0) and t.pos == (5, 2, 0) and g.mode == "grapple"
