"""Anatomy and wounds.

A body is a JSON "body_plan": a list of parts with hit weights, called-shot
penalties, wound multipliers and tags ("grasp", "stance", ...). Damage lands on
a part, gets multiplied by how much that part hates that damage type, and then
feeds three separate systems:

* the global HP pool (HP = ST), which drives unconsciousness and death checks,
* the part itself, which can be crippled or destroyed (severed / pulped),
* bleeding, which keeps hurting after the fight has moved on.

A hit to the torso might only cost HP. The same hit to a hand cripples it, and
the pistol it was holding drops.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field


@dataclass
class PartState:
    id: str
    data: dict
    damage: float = 0.0     # raw injury taken, uncapped (drives crippling / severing)
    counted: float = 0.0    # injury that actually came off the HP pool
    crippled: bool = False
    destroyed: bool = False

    @property
    def name(self) -> str:
        return self.data.get("name", self.id)

    @property
    def tags(self) -> list[str]:
        return self.data.get("tags", [])

    @property
    def functional(self) -> bool:
        return not (self.crippled or self.destroyed)


@dataclass
class Injury:
    part: PartState
    damage_type: str
    raw: int
    dr: int
    injury: int
    newly_crippled: bool = False
    newly_destroyed: bool = False
    lost: list[str] = field(default_factory=list)  # child parts lost with it

    @property
    def penetrated(self) -> bool:
        return self.injury > 0


class Body:
    def __init__(self, plan: dict, max_hp: int):
        self.plan = plan
        self.max_hp = max_hp
        self.hp: float = float(max_hp)
        self.bleed_rate: float = 0.0  # HP lost per second
        self.parts = {p["id"]: PartState(p["id"], p) for p in plan["parts"]}

    # -- queries -----------------------------------------------------------
    def part(self, part_id: str) -> PartState:
        return self.parts[part_id]

    def targetable(self) -> list[PartState]:
        return [p for p in self.parts.values() if not p.destroyed]

    def functional_with(self, tag: str) -> list[PartState]:
        return [p for p in self.parts.values() if tag in p.tags and p.functional]

    def total_with(self, tag: str) -> int:
        return sum(tag in p.tags for p in self.parts.values())

    def roll_location(self, rng: random.Random) -> str:
        parts = [p for p in self.targetable() if p.data.get("weight", 0) > 0]
        weights = [p.data["weight"] for p in parts]
        return rng.choices(parts, weights)[0].id

    # -- wounding ----------------------------------------------------------
    def wound_multiplier(self, part: PartState, dtype: dict) -> float:
        mults = part.data.get("wound_mult", {})
        if dtype["id"] in mults:
            return mults[dtype["id"]]
        if "*" in mults:
            return mults["*"]
        return dtype.get("wound_mult", 1.0)

    def wound(self, part_id: str, raw: int, dr: int, dtype: dict) -> Injury:
        part = self.parts[part_id]
        penetrating = max(0, raw - dr)
        injury = 0
        if penetrating > 0:
            injury = max(1, math.floor(penetrating * self.wound_multiplier(part, dtype)))
        result = Injury(part, dtype["id"], raw, dr, injury)
        if injury == 0:
            return result

        # Injury past a limb's crippling point is not counted against HP: a
        # shotgun to the hand wrecks the hand, it does not kill you.
        counted = injury
        cripple_at = part.data.get("cripple_at")
        if cripple_at is not None:
            cap = math.floor(self.max_hp * cripple_at) + 1
            counted = max(0, min(injury, cap - part.counted))
        part.damage += injury
        part.counted += counted
        self.hp -= counted

        if cripple_at is not None and not part.crippled and part.damage > self.max_hp * cripple_at:
            part.crippled = result.newly_crippled = True

        destroy_at = part.data.get("destroy_at")
        if (destroy_at is not None and not part.destroyed and dtype.get("dismembers")
                and part.damage >= self.max_hp * destroy_at):
            part.destroyed = part.crippled = result.newly_destroyed = True
            result.lost = self._lose_children(part.id)
            self.bleed_rate += part.data.get("sever_bleed", 0.3)

        self.bleed_rate += injury * dtype.get("bleed", 0.0) * part.data.get("bleed_mult", 1.0) / 60
        return result

    def _lose_children(self, part_id: str) -> list[str]:
        lost = []
        for p in self.parts.values():
            if p.data.get("parent") == part_id and not p.destroyed:
                p.destroyed = p.crippled = True
                lost.append(p.name)
                lost.extend(self._lose_children(p.id))
        return lost

    def heal(self, amount: float) -> None:
        self.hp = min(self.max_hp, self.hp + amount)

    def summary(self) -> str:
        hurt = []
        for p in self.parts.values():
            if p.destroyed:
                hurt.append(f"{p.name}: DESTROYED")
            elif p.crippled:
                hurt.append(f"{p.name}: crippled")
            elif p.damage:
                hurt.append(f"{p.name}: {p.damage:g}")
        bleed = f", bleeding {self.bleed_rate:.2f}/s" if self.bleed_rate > 0.01 else ""
        return f"HP {round(self.hp)}/{self.max_hp}{bleed}" + (f" [{'; '.join(hurt)}]" if hurt else "")
