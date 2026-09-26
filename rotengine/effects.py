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


VALUE_KEYS = ("amount", "distance", "radius", "range", "duration_ms", "value", "fraction",
              "mod", "vs", "max_rise", "skill_bonus", "damage_bonus")
STATS = ("ST", "CON", "DEX", "INT", "WIS")


def validate(effects: Any, path: str = "", content: Any = None) -> list[str]:
    """Static check of an effect list, so modders get errors at load time
    rather than a crash mid-fight. With `content`, references to damage
    types and statuses are checked too."""
    if not isinstance(effects, list):
        return [f"{path or 'effects'} must be a list"]
    errors: list[str] = []
    for i, op in enumerate(effects):
        where = f"{path}[{i}]"
        if not isinstance(op, dict):
            errors.append(f"{where}: effect must be an object")
            continue
        if "if" in op:
            extra = set(op) - {"if", "then", "else"}
            if extra:
                errors.append(f"{where}: unexpected keys next to 'if': {sorted(extra)}")
            errors += validate_condition(op["if"], where + ".if")
            for k in ("then", "else"):
                if k in op:
                    errors += validate(op[k], f"{where}.{k}", content)
            continue
        if len(op) != 1:
            errors.append(f"{where}: effect must have exactly one op, got {sorted(op)}")
            continue
        (name, args), = op.items()
        if name not in EFFECTS:
            errors.append(f"{where}: unknown effect '{name}'")
            continue
        errors += _validate_args(name, args, f"{where}.{name}", content)
    return errors


def _validate_args(name: str, args: Any, where: str, content: Any) -> list[str]:
    errors: list[str] = []
    if name == "message":
        return [] if isinstance(args, str) else [f"{where}: must be a string"]
    if not isinstance(args, dict):
        return [f"{where}: arguments must be an object"]
    for k in VALUE_KEYS:
        if k in args:
            errors += validate_value(args[k], f"{where}.{k}")
    for k in _NESTED:
        if k in args:
            errors += validate(args[k], f"{where}.{k}", content)
    if args.get("who", "self") not in ("self", "target"):
        errors.append(f"{where}.who: must be 'self' or 'target'")
    if name == "modify_stat" and args.get("stat") not in STATS:
        errors.append(f"{where}.stat: must be one of {', '.join(STATS)}")
    if name in ("damage", "set_var", "add_status", "remove_status"):
        need = {"damage": "amount", "set_var": "name", "add_status": "id", "remove_status": "id"}[name]
        if need not in args:
            errors.append(f"{where}: missing '{need}'")
    if content is not None:
        if name == "damage" and not content.has("damage_type", args.get("type", "crush")):
            errors.append(f"{where}.type: unknown damage_type '{args.get('type')}'")
        if name in ("add_status", "remove_status") and "id" in args and not content.has("status", args["id"]):
            errors.append(f"{where}.id: unknown status '{args['id']}'")
    return errors


def validate_value(v: Any, path: str) -> list[str]:
    if isinstance(v, bool):
        return [f"{path}: expected a number, dice or expression, got {v!r}"]
    if isinstance(v, (int, float)):
        return []
    if isinstance(v, str):
        try:
            Dice.parse(v)
            return []
        except ValueError:
            return [f"{path}: bad dice expression {v!r}"]
    if not isinstance(v, dict) or len(v) != 1:
        return [f"{path}: expected a number, dice string or single-key expression"]
    (name, args), = v.items()
    if name not in VALUES:
        return [f"{path}: unknown value '{name}'"]
    if name in ("add", "min", "max", "mul"):
        if not isinstance(args, list):
            return [f"{path}.{name}: must be a list"]
        return [e for i, a in enumerate(args) for e in validate_value(a, f"{path}.{name}[{i}]")]
    if name == "neg":
        return validate_value(args, f"{path}.neg")
    if name == "stat":
        stat = args.get("stat") if isinstance(args, dict) else args
        if stat not in STATS:
            return [f"{path}.stat: must be one of {', '.join(STATS)}"]
    return []


def validate_condition(c: Any, path: str = "condition") -> list[str]:
    if isinstance(c, bool):
        return []
    if not isinstance(c, dict) or len(c) != 1:
        return [f"{path}: condition must be true/false or a single-key object"]
    (name, args), = c.items()
    if name not in CONDITIONS:
        return [f"{path}: unknown condition '{name}'"]
    if name in ("and", "or"):
        if not isinstance(args, list):
            return [f"{path}.{name}: must be a list of conditions"]
        return [e for i, sub in enumerate(args) for e in validate_condition(sub, f"{path}.{name}[{i}]")]
    if name == "not":
        return validate_condition(args, f"{path}.not")
    if name == "compare":
        if not isinstance(args, list) or len(args) != 3:
            return [f"{path}.compare: must be [value, operator, value]"]
        errors = [] if args[1] in _OPS else [f"{path}.compare: unknown operator {args[1]!r}"]
        return errors + validate_value(args[0], f"{path}.compare[0]") + validate_value(args[2], f"{path}.compare[2]")
    if name == "roll":
        if not isinstance(args, dict) or not ({"stat", "skill", "vs"} & set(args)):
            return [f"{path}.roll: needs 'stat', 'skill' or 'vs'"]
        errors = validate_value(args["vs"], f"{path}.roll.vs") if "vs" in args else []
        if "stat" in args and args["stat"] not in STATS:
            errors.append(f"{path}.roll.stat: must be one of {', '.join(STATS)}")
        if "mod" in args:
            errors += validate_value(args["mod"], f"{path}.roll.mod")
        return errors
    if name == "chance":
        return validate_value(args, f"{path}.chance")
    return []


# -- values --------------------------------------------------------------------
@value("stat")
def _v_stat(args, ctx):
    spec = args if isinstance(args, dict) else {"stat": args}
    who = ctx.who(spec)
    return who.stat(spec["stat"]) if who else 0


@value("var")
def _v_var(name, ctx):
    return ctx.vars.get(name, 0)


@value("hp")
def _v_hp(args, ctx):
    who = ctx.who(args)
    return who.hp if who else 0


@value("stamina")
def _v_stamina(args, ctx):
    who = ctx.who(args)
    return who.stamina if who else 0


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
    if who is None:
        return False
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
    who = ctx.who(spec)
    return who is not None and who.has_status(spec["id"])


@condition("has_trait")
def _c_has_trait(args, ctx):
    spec = args if isinstance(args, dict) else {"id": args}
    who = ctx.who(spec)
    return who is not None and who.has_trait(spec["id"])


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
    if who is None or who.dead:
        return
    if not who.dead:
        who.body.heal(evaluate(args["amount"], ctx))


@effect("stop_bleeding")
def _e_stop_bleeding(args, ctx):
    """Reduce external bleeding; "internal": true also closes internal bleeds
    (surgery, healing factors, magic)."""
    who = ctx.who(args)
    if who is None or who.dead:
        return
    keep = 1 - min(1.0, max(0.0, evaluate(args.get("fraction", 1), ctx)))
    who.body.bleed_rate *= keep
    if args.get("internal"):
        who.body.internal_bleed *= keep


@effect("restore_blood")
def _e_restore_blood(args, ctx):
    who = ctx.who(args)
    if who is None or who.dead:
        return
    if not who.dead:
        who.body.blood = min(100.0, who.body.blood + evaluate(args["amount"], ctx))


@effect("modify_stat")
def _e_modify_stat(args, ctx):
    who = ctx.who(args)
    if who is None or who.dead:
        return
    stat = args["stat"]
    new = who.stat_bonus[stat] + evaluate(args["amount"], ctx)
    if "max_bonus" in args:
        new = min(new, args["max_bonus"])
    who.stat_bonus[stat] = new


@effect("add_status")
def _e_add_status(args, ctx):
    who = ctx.who(args)
    if who is None or who.dead:
        return
    dur = args.get("duration_ms")
    if dur is None:
        who.add_status(args["id"], None)
    else:
        ctx.sim.apply_status(who, args["id"], evaluate(dur, ctx))


@effect("remove_status")
def _e_remove_status(args, ctx):
    who = ctx.who(args)
    if who is not None:
        who.statuses.pop(args["id"], None)


@effect("knockback")
def _e_knockback(args, ctx):
    from .combat import knockback
    who = ctx.who(args)
    if who is not None and who is not ctx.self:
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
            if (dx or dy) and sim.is_free(p):
                yield p


@effect("attack")
def _e_attack(args, ctx):
    from .combat import best_attack_plan, resolve_attack
    if ctx.target is None or not ctx.target.active:
        return
    plan = best_attack_plan(ctx.sim, ctx.self, ctx.target, surprise=args.get("surprise", False),
                            skill_bonus=args.get("skill_bonus", 0),
                            damage_bonus=args.get("damage_bonus", 0), allow_aim=False)
    if plan:
        resolve_attack(ctx.sim, ctx.self, ctx.target, plan, surprise=args.get("surprise", False))


@effect("damage_terrain")
def _e_damage_terrain(args, ctx):
    radius = int(evaluate(args.get("radius", 1), ctx))
    ctx.sim.damage_terrain(ctx.self.pos, radius, lambda: int(evaluate(args["amount"], ctx)),
                           args.get("z_offsets", [0, 1]))
