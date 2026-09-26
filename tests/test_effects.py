from rotengine import effects
from rotengine.sim import Sim
from rotengine.world import World


def arena(content):
    world = World(content.all("material"), 10, 5, 1)
    for y in range(5):
        for x in range(10):
            world.set_floor((x, y, 0), "concrete")
    return Sim(content, world, seed=1)


def test_program_runs(content):
    sim = arena(content)
    me = sim.spawn("street_tough", "a", (2, 2, 0))
    foe = sim.spawn("street_tough", "b", (3, 2, 0))
    far = sim.spawn("street_tough", "b", (9, 2, 0))
    prog = [
        {"set_var": {"name": "n", "value": {"count_enemies": {"radius": 2}}}},
        {"if": {"compare": [{"var": "n"}, "==", 1]},
         "then": [{"for_each_enemy": {"radius": 2, "do": [
             {"add_status": {"id": "stunned", "duration_ms": 1000}}]}}],
         "else": [{"message": "wrong branch"}]},
        {"modify_stat": {"stat": "ST", "amount": 5, "max_bonus": 3, "who": "self"}},
    ]
    assert effects.validate(prog) == []
    effects.run(prog, effects.Ctx(sim, me))
    assert foe.has_status("stunned") and not far.has_status("stunned")
    assert me.stat("ST") == 13
    assert not any("wrong branch" in line for line in sim.lines)


def test_values(content):
    sim = arena(content)
    me = sim.spawn("hulk", "a", (2, 2, 0))
    ctx = effects.Ctx(sim, me)
    assert effects.evaluate({"stat": "ST"}, ctx) == 60
    assert effects.evaluate({"mul": [2, {"add": [1, 2]}]}, ctx) == 6
    assert 3 <= effects.evaluate("3d6", ctx) <= 18


def test_validation_messages():
    errs = effects.validate([{"if": {"bogus": 1}, "then": [{"nope": {}}]}])
    assert any("unknown condition 'bogus'" in e for e in errs)
    assert any("unknown effect 'nope'" in e for e in errs)


def test_python_mods_can_register_ops(content):
    sim = arena(content)
    me = sim.spawn("street_tough", "a", (2, 2, 0))

    @effects.effect("test_shout")
    def _shout(args, ctx):
        ctx.sim.log(f"{ctx.self.name} shouts {args}")

    try:
        effects.run([{"test_shout": "hey"}], effects.Ctx(sim, me))
        assert sim.lines[-1].endswith("shouts hey")
    finally:
        del effects.EFFECTS["test_shout"]
