"""Balance smoke tests. These are the design goals as numbers, run over a few
seeded fights each: if a rules change breaks one, that's a design
conversation, not just a failing test."""
from collections import Counter

from rotengine import arena


def win_rates(name, runs):
    scenario = arena.load_scenario(name)
    content = arena.content_for(scenario)
    wins = Counter(arena.run_once(scenario, content, seed).winner for seed in range(runs))
    return {k: v / runs for k, v in wins.items()}


def test_every_scenario_builds():
    from rotengine.content import DATA_DIR
    for path in (DATA_DIR / "scenarios").glob("*.json"):
        scenario = arena.load_scenario(path)
        sim = arena.build(scenario, arena.content_for(scenario), seed=0)
        assert len(sim.active_teams()) >= 2


def test_even_fight_is_a_coin_flip():
    rates = win_rates("street_fight", 60)
    assert 0.3 <= rates.get("red", 0) <= 0.7


def test_hulk_shreds_a_rifle_squad():
    assert win_rates("hulk_vs_squad", 20).get("hulk", 0) >= 0.9


def test_wick_beats_the_odds():
    assert win_rates("wick_vs_thugs", 40).get("wick", 0) >= 0.6


def test_same_seed_same_fight():
    scenario = arena.load_scenario("wick_vs_thugs")
    content = arena.content_for(scenario)
    a = arena.build(scenario, content, seed=7)
    b = arena.build(scenario, content, seed=7)
    a.run()
    b.run()
    assert a.lines == b.lines


def test_speedster_is_dangerous():
    assert win_rates("speedster_vs_squad", 30).get("speedster", 0) >= 0.4
