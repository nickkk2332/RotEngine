"""Creatures and items built from JSON templates."""
from __future__ import annotations

import math
from collections import defaultdict
from typing import TYPE_CHECKING

from .body import Body

if TYPE_CHECKING:
    from .content import Content
    from .world import Pos

STATS = ("ST", "CON", "DEX", "INT", "WIS")
SKILL_DEFAULT_PENALTY = -4  # untrained skills default to DEX-4


class Item:
    def __init__(self, data: dict):
        self.data = data
        self.id: str = data["id"]
        self.name: str = data["name"]
        self.ammo: int | None = data.get("magazine")

    @property
    def attacks(self) -> list[dict]:
        return self.data.get("attacks", [])

    @property
    def armor(self) -> dict | None:
        return self.data.get("armor")


class Creature:
    def __init__(self, uid: int, template: dict, content: "Content", team: str, pos: "Pos",
                 name: str | None = None):
        self.uid = uid
        self.template = template
        self.content = content
        self.team = team
        self.pos = pos
        self.name = name or template["name"]
        self.glyph: str = template.get("glyph", "@")
        self.base_stats = {s: template["stats"].get(s, 10) for s in STATS}
        self.stat_bonus: dict[str, float] = defaultdict(float)
        self.skills: dict[str, int] = dict(template.get("skills", {}))
        self.traits = [content.get("trait", t) for t in template.get("traits", [])]
        self.powers = [content.get("power", p) for p in template.get("powers", [])]
        self.statuses: dict[str, float | None] = {}  # status id -> expiry time (ms) or None

        plan = content.get("body_plan", template["body"])
        # HP = ST, stamina = CON. Templates can buy extra of either.
        self.body = Body(plan, max_hp=self.base_stats["ST"] + template.get("hp_bonus", 0))
        self.max_stamina = self.base_stats["CON"] + template.get("stamina_bonus", 0)
        self.stamina = float(self.max_stamina)
        self.natural_attacks: list[dict] = plan.get("natural_attacks", []) + template.get("natural_attacks", [])

        eq = template.get("equipment", {})
        self.wielded = Item(content.get("item", eq["wield"])) if eq.get("wield") else None
        self.worn = [Item(content.get("item", i)) for i in eq.get("wear", [])]

        self.shock = 0          # pain penalty applied to the next action
        self.dead = False
        self.next_time = 0
        self.death_checks = 0   # multiples of -HP already survived
        self.target: Creature | None = None

    def __repr__(self) -> str:
        return f"<{self.name}#{self.uid} {self.team} {self.pos}>"

    # -- stats -------------------------------------------------------------
    def stat(self, name: str) -> int:
        return int(self.base_stats[name] + self.stat_bonus[name] + self.trait_sum("stat_mods", name))

    def skill(self, name: str) -> int:
        if name in self.skills:
            return self.skills[name]
        return self.stat("DEX") + SKILL_DEFAULT_PENALTY

    def trait_sum(self, field: str, key: str | None = None) -> float:
        total = 0.0
        for t in self.traits:
            v = t.get(field)
            if isinstance(v, dict):
                v = v.get(key, v.get("*", 0)) if key is not None else 0
            total += v or 0
        return total

    def trait_product(self, field: str, key: str) -> float:
        out = 1.0
        for t in self.traits:
            v = t.get(field, {})
            out *= v.get(key, v.get("*", 1.0))
        return out

    def has_trait(self, trait_id: str) -> bool:
        return any(t["id"] == trait_id for t in self.traits)

    # -- statuses ----------------------------------------------------------
    def status_defs(self) -> list[dict]:
        return [self.content.get("status", s) for s in self.statuses]

    def has_status(self, status_id: str) -> bool:
        return status_id in self.statuses

    def status_sum(self, field: str) -> float:
        return sum(d.get(field, 0) for d in self.status_defs())

    def add_status(self, status_id: str, until: float | None) -> None:
        cur = self.statuses.get(status_id, 0)
        if status_id in self.statuses and (cur is None or (until is not None and cur >= until)):
            return
        self.statuses[status_id] = until

    # -- condition ---------------------------------------------------------
    @property
    def hp(self) -> float:
        return self.body.hp

    @property
    def max_hp(self) -> int:
        return self.body.max_hp

    @property
    def conscious(self) -> bool:
        return not self.dead and "unconscious" not in self.statuses

    @property
    def active(self) -> bool:
        """Still in the fight."""
        return self.conscious

    @property
    def can_act(self) -> bool:
        return self.conscious and not any(d.get("prevents_action") for d in self.status_defs())

    # -- derived combat numbers --------------------------------------------
    @property
    def speed(self) -> float:
        return (self.stat("DEX") + self.stat("CON")) / 4 + self.trait_sum("speed_bonus")

    @property
    def move_per_second(self) -> float:
        base = max(1, math.floor(self.speed)) * self.trait_product("move_mult", "*")
        legs = self.body.total_with("stance")
        if legs and len(self.body.functional_with("stance")) < legs:
            base = 1.0  # crawling / hopping
        mult = 1.0
        for d in self.status_defs():
            mult *= d.get("move_mult", 1.0)
        if self.stamina <= 0:
            mult *= 0.5
        return max(0.25, base * mult)

    def dodge(self) -> int:
        return math.floor(self.speed) + 3 + int(self.trait_sum("dodge_bonus"))

    def parry(self, attack: dict) -> int | None:
        if not attack or attack.get("kind") != "melee" or "parry" not in attack:
            return None
        return (self.skill(attack["skill"]) // 2 + 3 + attack["parry"]
                + int(self.trait_sum("parry_bonus")))

    def best_defense(self, incoming_kind: str) -> tuple[str, int]:
        options = [("dodges", self.dodge())]
        if incoming_kind == "melee":
            for atk, _ in self.attacks():
                p = self.parry(atk)
                if p is not None:
                    options.append(("parries", p))
        name, value = max(options, key=lambda o: o[1])
        return name, value + int(self.status_sum("defense_mod"))

    def dr(self, part_id: str, dtype: str) -> int:
        def lookup(table: dict) -> int:
            return table.get(dtype, table.get("*", 0))

        part = self.body.part(part_id)
        total = lookup(part.data.get("dr", {}))
        for t in self.traits:
            if "natural_dr" in t:
                total += lookup(t["natural_dr"])
        for item in self.worn:
            if item.armor and part_id in item.armor["covers"]:
                total += lookup(item.armor["dr"])
        return total

    def attacks(self) -> list[tuple[dict, Item | None]]:
        """Every attack available right now: the wielded weapon's plus natural ones."""
        out: list[tuple[dict, Item | None]] = []
        if self.wielded:
            out += [(a, self.wielded) for a in self.wielded.attacks]
        grasping = len(self.body.functional_with("grasp"))
        kicking = len(self.body.functional_with("stance"))
        for a in self.natural_attacks:
            needs = a.get("uses")
            if needs == "grasp" and not grasping or needs == "stance" and not kicking:
                continue
            out.append((a, None))
        return out
