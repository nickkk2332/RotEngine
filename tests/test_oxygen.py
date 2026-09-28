"""Oxygen: breath-holding by CON, chokes as a contest, fighting back."""
from statistics import mean

from rotengine import combat, grapple, perception

from test_stealth import yard


def test_breath_holding_scales_with_con(content):
    sim = yard(content)
    weak = sim.spawn("human", "a", (3, 4, 0))
    tough = sim.spawn("human", "b", (6, 4, 0))
    weak.base_stats["CON"], tough.base_stats["CON"] = 8, 14
    assert weak.breath_seconds < tough.breath_seconds
    assert tough.blood_choke_rate < weak.blood_choke_rate  # a blood choke is fast for anyone, but not equally


def test_not_breathing_means_blackout_then_brain_damage(content):
    sim = yard(content)
    c = sim.spawn("human", "a", (5, 4, 0))
    c.add_status("crushed_windpipe", None)
    seconds_to_blackout = None
    for s in range(300):
        sim._tick_one(c, s)
        if seconds_to_blackout is None and not c.conscious:
            seconds_to_blackout = s
            assert c.body.hypoxia == 0  # the brain's fine until the air's actually gone
        if c.dead:
            break
    assert 25 <= seconds_to_blackout <= 45   # about as long as you can hold your breath
    assert c.dead and c.death_cause == "crushed windpipe"


def test_a_stopped_heart_gives_seconds_of_consciousness(content):
    sim = yard(content)
    c = sim.spawn("human", "a", (5, 4, 0))
    c.add_status("cardiac_arrest", None)
    for s in range(30):
        sim._tick_one(c, s)
        if not c.conscious:
            break
    assert 7 <= s <= 11


def test_air_comes_back_when_you_can_breathe(content):
    sim = yard(content)
    c = sim.spawn("human", "a", (5, 4, 0))
    c.body.oxygen = 20
    assert c.action_penalty() >= 4  # graying out
    for s in range(4):
        sim._tick_one(c, s)
    assert c.body.oxygen == 100 and c.air_penalty() == 0


def choke_out(content, victim, seed, guard=False):
    """Seconds of choking to put `victim` out, holding from behind."""
    sim = yard(content, seed=seed)
    t = sim.spawn(victim, "t", (10, 4, 0))
    t.facing = (1, 0)
    a = sim.spawn("operative", "a", (9, 4, 0))
    while a.grappling is None:
        grapple.grab(sim, a, t, "neck")
    for second in range(1, 60):
        if guard:
            t.neck_guard_until = sim.time + 10_000
        sim.time += 1000
        grapple.choke(sim, a)
        if not t.conscious:
            return second
    return 60


def test_a_locked_choke_takes_seconds_not_minutes(content):
    times = [choke_out(content, "human", s) for s in range(30)]
    assert 6 <= mean(times) <= 12


def test_hand_fighting_a_choke_slows_it_and_training_helps(content):
    free = mean(choke_out(content, "soldier", s) for s in range(30))
    fought = mean(choke_out(content, "soldier", s, guard=True) for s in range(30))
    assert fought > free + 2
    sim = yard(content)
    novice, soldier = sim.spawn("human", "a", (3, 4, 0)), sim.spawn("soldier", "b", (6, 4, 0))
    assert grapple.guard_bonus(soldier) > grapple.guard_bonus(novice)


def test_grabbed_unaware_means_a_moment_of_shock(content):
    sim = yard(content, seed=0)
    t = sim.spawn("sentry", "t", (10, 4, 0))
    t.facing = (1, 0)
    a = sim.spawn("operative", "a", (9, 4, 0))
    grapple.grab(sim, a, t, "neck")
    assert a.grappling is t and t.has_status("startled") and not t.can_act


def test_a_choked_soldier_fights_back(content):
    """He can't swing his rifle round, so he goes for his knife and hand-fights."""
    moves = set()
    for seed in range(10):
        sim = yard(content, seed=seed)
        t = sim.spawn("soldier", "t", (10, 4, 0))
        t.facing = (1, 0)
        a = sim.spawn("operative", "a", (9, 4, 0))
        a.controller = "player"
        perception.make_all_aware(sim)
        sim.advance()
        for _ in range(10):
            if a.grappling is t or sim.awaiting is not a:
                break
            sim.player_act(grapple.grab(sim, a, t, "neck") or 1000)
            sim.advance()
        for _ in range(15):
            if sim.advance() != "player" or a.grappling is None or not t.conscious:
                break
            sim.player_act(grapple.choke(sim, a))
        text = "\n".join(sim.lines)
        moves |= {m for m in ("claws at", "draws the combat knife", "struggles") if m in text}
    assert moves == {"claws at", "draws the combat knife", "struggles"}


def test_no_rifle_against_whoever_is_holding_you(content):
    sim = yard(content)
    t = sim.spawn("soldier", "t", (10, 4, 0))
    t.facing = (1, 0)
    a = sim.spawn("operative", "a", (9, 4, 0))
    while a.grappling is None:
        grapple.grab(sim, a, t, "neck")
    rifle = t.wielded
    assert rifle.data.get("two_handed")
    assert not any(p.item is rifle for p in combat.attack_plans(sim, t, a))


def test_player_can_hand_fight_from_the_ui():
    from test_play import arena, new_app, screen_text
    from rotengine.ui.game import GameScreen
    scenario = {"id": "yard", "name": "Yard", "levels": [["," * 12] * 5],
                "teams": {"you": [{"creature": "operative", "at": [4, 2, 0]}],
                          "them": [{"creature": "commander", "at": [5, 2, 0]}]}}
    app = new_app()
    content = app.content_for(scenario)
    sim = arena.build(scenario, content, seed=1)
    app.screen = g = GameScreen(app, scenario, content, sim, sim.creatures[0], 1)
    p, boss = g.player, sim.creatures[1]
    grapple._hold(boss, p, "neck", rear=True)  # he has you by the throat
    assert "choking you" in screen_text(app)
    app.handle_key("G")
    assert any("claws at" in line for line in sim.lines)
    assert "Air" in screen_text(app)
