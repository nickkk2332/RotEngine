"""EOC-style effect scripting: powers, traits and statuses as JSON.

An effect list is a list of single-key objects, each one an op:

    [{"message": "{self} claps. The air cracks."},
     {"for_each_enemy": {"radius": 3, "do": [
         {"damage": {"amount": "3d6", "type": "crush"}},
         {"if": {"not": {"roll": {"stat": "CON", "who": "target", "mod": -3}}},
          "then": [{"add_status": {"id": "stunned", "duration_ms": 2000}}]}]}}]

Values can be numbers, dice strings ("2d6+1"), or expressions such as
{"stat": "ST", "who": "self"}, {"var": "damage"}, {"mul": [...]}.
Conditions are literals or {"roll": ...}, {"compare": [a, "<", b]},
{"has_status": ...}, {"and": [...]}, {"or": [...]}, {"not": ...}.

Python mods can add ops with @effect("name") / @condition("name") / @value("name").
"""
from __future__ import annotations

import operator
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from .dice import Dice, check

if TYPE_CHECKING:
    from .creature import Creature
    from .sim import Sim

EffectFn = Callable[[Any, "Ctx"], None]
EFFECTS: dict[str, EffectFn] = {}
CONDITIONS: dict[str, Callable[[Any, "Ctx"], bool]] = {}
VALUES: dict[str, Callable[[Any, "Ctx"], float]] = {}

# Keys whose values are nested effect lists, for validation.
_NESTED = ("then", "else", "do")


def effect(name: str):
    def deco(fn: EffectFn) -> EffectFn:
        EFFECTS[name] = fn
        return fn
    return deco


def condition(name: str):
    def deco(fn):
        CONDITIONS[name] = fn
        return fn
    return deco


def value(name: str):
    def deco(fn):
        VALUES[name] = fn
        return fn
    return deco


@dataclass
class Ctx:
    sim: "Sim"
    self: "Creature"
    target: "Creature | None" = None
    vars: dict[str, Any] = field(default_factory=dict)

    @property
    def rng(self) -> random.Random:
        return self.sim.rng

    def who(self, spec: dict | None) -> "Creature | None":
        name = (spec or {}).get("who", "target" if self.target else "self")
        return self.self if name == "self" else self.target

    def with_target(self, target: "Creature") -> "Ctx":
        return Ctx(self.sim, self.self, target, self.vars)


# -- interpreter -------------------------------------------------------------
def run(effects: list[dict], ctx: Ctx) -> None:
    for op in effects:
        if "if" in op:
            branch = op.get("then", []) if test(op["if"], ctx) else op.get("else", [])
            run(branch, ctx)
            continue
        (name, args), = op.items()
        EFFECTS[name](args, ctx)
        if ctx.vars.get("_abort"):
            return


def evaluate(v: Any, ctx: Ctx) -> float:
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        return Dice.parse(v).roll(ctx.rng)
    if isinstance(v, dict):
        (name, args), = v.items()
        return VALUES[name](args, ctx)
    raise ValueError(f"cannot evaluate {v!r}")


def test(c: Any, ctx: Ctx) -> bool:
    if isinstance(c, bool):
        return c
    (name, args), = c.items()
    return CONDITIONS[name](args, ctx)


def validate(effects: Any, path: str = "") -> list[str]:
    """Static check of an effect list, so modders get errors at load time."""
    if not isinstance(effects, list):
        return [f"{path or 'effects'} must be a list"]
    errors: list[str] = []
    for i, op in enumerate(effects):
        where = f"{path}[{i}]"
        if not isinstance(op, dict):
            errors.append(f"{where}: effect must be an object")
            continue
        if "if" in op:
            errors += validate_condition(op["if"], where + ".if")
            for k in ("then", "else"):
                if k in op:
                    errors += validate(op[k], f"{where}.{k}")
            continue
        if len(op) != 1:
            errors.append(f"{where}: effect must have exactly one op, got {sorted(op)}")
            continue
        (name, args), = op.items()
        if name not in EFFECTS:
            errors.append(f"{where}: unknown effect '{name}'")
        if isinstance(args, dict):
            for k in _NESTED:
                if k in args:
                    errors += validate(args[k], f"{where}.{name}.{k}")
    return errors


def validate_condition(c: Any, path: str = "condition") -> list[str]:
    if isinstance(c, bool):
        return []
    if not isinstance(c, dict) or len(c) != 1:
        return [f"{path}: condition must be true/false or a single-key object"]
    (name, args), = c.items()
    if name not in CONDITIONS:
        return [f"{path}: unknown condition '{name}'"]
    if name in ("and", "or"):
        return [e for i, sub in enumerate(args) for e in validate_condition(sub, f"{path}.{name}[{i}]")]
    if name == "not":
        return validate_condition(args, f"{path}.not")
    return []


# -- values --------------------------------------------------------------------
@value("stat")
def _v_stat(args, ctx):
    spec = args if isinstance(args, dict) else {"stat": args}
    return ctx.who(spec).stat(spec["stat"])


@value("var")
def _v_var(name, ctx):
    return ctx.vars.get(name, 0)


@value("hp")
def _v_hp(args, ctx):
    return ctx.who(args).hp


@value("stamina")
def _v_stamina(args, ctx):
    return ctx.who(args).stamina


@value("distance_to_target")
def _v_dist(_args, ctx):
    return ctx.sim.distance(ctx.self, ctx.target) if ctx.target else 9999


@value("count_enemies")
def _v_count(args, ctx):
    return len(ctx.sim.enemies_within(ctx.self, args.get("radius", 1)))


for _name, _fn in (("add", sum), ("min", min), ("max", max)):
    VALUES[_name] = (lambda fn: lambda args, ctx: fn(evaluate(a, ctx) for a in args))(_fn)


@value("mul")
def _v_mul(args, ctx):
    out = 1.0
    for a in args:
        out *= evaluate(a, ctx)
    return out


@value("neg")
def _v_neg(arg, ctx):
    return -evaluate(arg, ctx)


# -- conditions ------------------------------------------------------------
_OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
        "==": operator.eq, "!=": operator.ne}


@condition("compare")
def _c_compare(args, ctx):
    a, op, b = args
    return _OPS[op](evaluate(a, ctx), evaluate(b, ctx))


@condition("roll")
def _c_roll(args, ctx):
    """3d6 check vs a stat/skill (+mod), or vs an arbitrary value."""
    who = ctx.who(args)
    if "vs" in args:
        target = evaluate(args["vs"], ctx)
    elif "skill" in args:
        target = who.skill(args["skill"])
    else:
        target = who.stat(args["stat"])
    return check(ctx.rng, int(target + evaluate(args.get("mod", 0), ctx))).success


@condition("chance")
def _c_chance(p, ctx):
    return ctx.rng.random() < evaluate(p, ctx)


@condition("has_status")
def _c_has_status(args, ctx):
    spec = args if isinstance(args, dict) else {"id": args}
    return ctx.who(spec).has_status(spec["id"])


@condition("has_trait")
def _c_has_trait(args, ctx):
    spec = args if isinstance(args, dict) else {"id": args}
    return ctx.who(spec).has_trait(spec["id"])


@condition("has_target")
def _c_has_target(flag, ctx):
    return (ctx.target is not None and ctx.target.active) == bool(flag)


@condition("sees_target")
def _c_sees_target(flag, ctx):
    seen = ctx.target is not None and ctx.sim.world.has_los(ctx.self.pos, ctx.target.pos)
    return seen == bool(flag)


@condition("target_reachable")
def _c_target_reachable(flag, ctx):
    from .ai import can_reach
    return (ctx.target is not None and can_reach(ctx.sim, ctx.self, ctx.target)) == bool(flag)


@condition("and")
def _c_and(args, ctx):
    return all(test(c, ctx) for c in args)


@condition("or")
def _c_or(args, ctx):
    return any(test(c, ctx) for c in args)


@condition("not")
def _c_not(arg, ctx):
    return not test(arg, ctx)


# -- effects -------------------------------------------------------------------
@effect("message")
def _e_message(text, ctx):
    ctx.sim.log(text.format(self=ctx.self.name, target=ctx.target.name if ctx.target else "?"))


@effect("set_var")
def _e_set_var(args, ctx):
    ctx.vars[args["name"]] = evaluate(args["value"], ctx)


@effect("damage")
def _e_damage(args, ctx):
    from .combat import deal_damage
    victim = ctx.who(args)
    if victim is None or victim.dead:
        return
    amount = max(0, int(evaluate(args["amount"], ctx)))
    deal_damage(ctx.sim, victim, amount, args.get("type", "crush"), args.get("location"),
                source=ctx.self, origin=ctx.self.pos)


@effect("heal")
def _e_heal(args, ctx):
    who = ctx.who(args)
    if not who.dead:
        who.body.heal(evaluate(args["amount"], ctx))


@effect("stop_bleeding")
def _e_stop_bleeding(args, ctx):
    who = ctx.who(args)
    who.body.bleed_rate *= 1 - evaluate(args.get("fraction", 1), ctx)


@effect("modify_stat")
def _e_modify_stat(args, ctx):
    who = ctx.who(args)
    stat = args["stat"]
    new = who.stat_bonus[stat] + evaluate(args["amount"], ctx)
    if "max_bonus" in args:
        new = min(new, args["max_bonus"])
    who.stat_bonus[stat] = new


@effect("add_status")
def _e_add_status(args, ctx):
    who = ctx.who(args)
    dur = args.get("duration_ms")
    who.add_status(args["id"], None if dur is None else ctx.sim.time + evaluate(dur, ctx))


@effect("remove_status")
def _e_remove_status(args, ctx):
    ctx.who(args).statuses.pop(args["id"], None)


@effect("knockback")
def _e_knockback(args, ctx):
    from .combat import knockback
    who = ctx.who(args)
    knockback(ctx.sim, who, ctx.self.pos, int(evaluate(args["distance"], ctx)))


@effect("for_each_enemy")
def _e_for_each_enemy(args, ctx):
    for enemy in ctx.sim.enemies_within(ctx.self, evaluate(args.get("radius", 1), ctx)):
        run(args["do"], ctx.with_target(enemy))


@effect("teleport")
def _e_teleport(args, ctx):
    """Move self next to the target, on its far side when possible. Needs
    line of sight to the target, like Dishonored's blink."""
    target = ctx.target
    if (target is None or ctx.sim.distance(ctx.self, target) > evaluate(args.get("range", 99), ctx)
            or not ctx.sim.world.has_los(ctx.self.pos, target.pos)):
        ctx.vars["_abort"] = True
        return
    dest = ctx.sim.spot_near(target, behind_from=ctx.self.pos)
    if dest is None:
        ctx.vars["_abort"] = True
        return
    ctx.sim.move_creature(ctx.self, dest)


@effect("leap")
def _e_leap(args, ctx):
    """Jump to the target: up onto ledges, across gaps. No walking needed."""
    target = ctx.target
    if (target is None or ctx.sim.distance(ctx.self, target) > evaluate(args.get("range", 8), ctx)
            or target.pos[2] - ctx.self.pos[2] > evaluate(args.get("max_rise", 1), ctx)
            or not ctx.sim.world.has_los(ctx.self.pos, target.pos)):
        ctx.vars["_abort"] = True
        return
    x, y, z = ctx.self.pos
    dest = min((p for p in _spots_near(ctx.sim, target)),
               key=lambda p: ctx.sim.distance_pos(p, (x, y, z)), default=None)
    if dest is None:
        ctx.vars["_abort"] = True
        return
    ctx.sim.move_creature(ctx.self, dest)


def _spots_near(sim, target):
    tx, ty, tz = target.pos
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            p = (tx + dx, ty + dy, tz)
            if (dx or dy) and sim.world.standable(p) and sim.creature_at(p) is None:
                yield p


@effect("attack")
def _e_attack(args, ctx):
    from .combat import best_attack_plan, resolve_attack
    if ctx.target is None or not ctx.target.active:
        return
    plan = best_attack_plan(ctx.sim, ctx.self, ctx.target, surprise=args.get("surprise", False),
                            skill_bonus=args.get("skill_bonus", 0),
                            damage_bonus=args.get("damage_bonus", 0))
    if plan:
        resolve_attack(ctx.sim, ctx.self, ctx.target, plan, surprise=args.get("surprise", False))


@effect("damage_terrain")
def _e_damage_terrain(args, ctx):
    radius = int(evaluate(args.get("radius", 1), ctx))
    ctx.sim.damage_terrain(ctx.self.pos, radius, lambda: int(evaluate(args["amount"], ctx)),
                           args.get("z_offsets", [0, 1]))
