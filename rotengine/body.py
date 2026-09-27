"""Anatomy, wounds, blood and the brain.

A body is a JSON "body_plan": parts with hit weights, called-shot penalties,
wound multipliers and tags ("grasp", "stance", ...). A wound lands on a part
and feeds several separate systems:

* **HP (= ST)** measures trauma. It drives pain and consciousness, but losing
  it does not kill you by itself unless the body is physically torn apart
  (-5 x HP).
* **The part** can be crippled, fractured (by blunt force), or destroyed
  (severed, pulped, organ ruptured). Destroying the brain or the neck kills
  outright. Destroying the heart/lungs stops circulation.
* **Blood** is a percentage of normal volume. External wounds bleed and can
  clot. Internal bleeding from deep torso/organ wounds does not clot.
* **Hypoxia** is brain damage from missing oxygen: blood loss below 50% or a
  stopped heart. At 100 the brain dies.

So a leg wound never kills directly. The bleeding it causes can, a few
minutes later, if nobody puts a tourniquet on it.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# % of blood volume lost per second from an injury equal to max HP, before
# damage-type and body-part bleed multipliers.
BLEED_SCALE = 0.6


@dataclass
class PartState:
    id: str
    data: dict
    damage: float = 0.0     # raw injury taken, uncapped (drives crippling / destruction)
    counted: float = 0.0    # injury that actually came off the HP pool
    crippled: bool = False
    fractured: bool = False
    destroyed: bool = False
    note: str = ""          # what happened to it, in words ("neck snapped", "arm cut off")

    @property
    def name(self) -> str:
        return self.data.get("name", self.id)

    @property
    def tags(self) -> list[str]:
        return self.data.get("tags", [])


@dataclass
class Injury:
    part: PartState
    damage_type: str
    raw: int
    dr: int
    injury: int
    newly_crippled: bool = False
    newly_fractured: bool = False
    newly_destroyed: bool = False
    lost: list[str] = field(default_factory=list)  # child parts lost with it
    internal: bool = False                          # started internal bleeding
    spec: dict = field(default_factory=dict)        # the part's data with by_type overrides applied
    severed: object = None                          # the item left behind by a severed part

    @property
    def penetrated(self) -> bool:
        return self.injury > 0


class Body:
    def __init__(self, plan: dict, max_hp: int):
        self.plan = plan
        self.max_hp = max(1, max_hp)
        self.hp: float = float(self.max_hp)
        self.blood: float = 100.0         # % of normal volume
        self.bleed_rate: float = 0.0      # external, %/s, can clot
        self.internal_bleed: float = 0.0  # %/s, needs surgery
        self.hypoxia: float = 0.0         # brain damage, 100 = brain death
        self.parts = {p["id"]: PartState(p["id"], p) for p in plan["parts"]}

    # -- queries -----------------------------------------------------------
    def part(self, part_id: str) -> PartState:
        return self.parts[part_id]

    def targetable(self) -> list[PartState]:
        return [p for p in self.parts.values() if not p.destroyed]

    def is_functional(self, part_id: str) -> bool:
        """A part works if it and everything it hangs off are intact:
        a crippled arm means a useless hand."""
        p = self.parts[part_id]
        while p is not None:
            if p.crippled or p.destroyed:
                return False
            parent = p.data.get("parent")
            p = self.parts.get(parent) if parent else None
        return True

    def functional_with(self, tag: str) -> list[PartState]:
        return [p for p in self.parts.values() if tag in p.tags and self.is_functional(p.id)]

    def total_with(self, tag: str) -> int:
        return sum(tag in p.tags for p in self.parts.values())

    def fractures(self) -> int:
        return sum(p.fractured for p in self.parts.values())

    @property
    def total_bleed(self) -> float:
        return self.bleed_rate + self.internal_bleed

    def roll_location(self, rng: random.Random) -> str:
        parts = [p for p in self.targetable() if p.data.get("weight", 0) > 0]
        weights = [p.data["weight"] for p in parts]
        return rng.choices(parts, weights)[0].id

    def blood_penalty(self) -> int:
        """Light-headedness and shock from blood loss."""
        if self.blood >= 85:
            return 0
        if self.blood >= 70:
            return 1
        return 3

    # -- wounding ----------------------------------------------------------
    def wound_multiplier(self, part: PartState, dtype: dict) -> float:
        mults = part.data.get("wound_mult", {})
        if dtype["id"] in mults:
            return mults[dtype["id"]]
        if "*" in mults:
            return mults["*"]
        return dtype.get("wound_mult", 1.0)

    @staticmethod
    def spec(part: PartState, dtype_id: str) -> dict:
        """A part's data as seen by one damage type: "by_type" overrides
        texts and effects (a wrenched neck snaps, a cut one is decapitated)."""
        over = part.data.get("by_type", {}).get(dtype_id)
        return {**part.data, **over} if over else part.data

    def wound(self, part_id: str, raw: int, dr: int, dtype: dict) -> Injury:
        part = self.parts[part_id]
        penetrating = max(0, raw - dr)
        injury = 0
        if penetrating > 0:
            injury = max(1, math.floor(penetrating * self.wound_multiplier(part, dtype)))
        d = self.spec(part, dtype["id"])
        result = Injury(part, dtype["id"], raw, dr, injury, spec=d)
        if injury == 0:
            return result
        mh = self.max_hp

        # Injury past a limb's crippling point is not counted against HP: a
        # shotgun to the hand wrecks the hand, it does not wreck you.
        counted = injury
        cripple_at = d.get("cripple_at")
        if cripple_at is not None:
            cap = math.floor(mh * cripple_at) + 1
            counted = max(0, min(injury, cap - part.counted))
        part.damage += injury
        part.counted += counted
        self.hp -= counted

        if cripple_at is not None and not part.crippled and part.damage > mh * cripple_at:
            part.crippled = result.newly_crippled = True

        fracture_at = d.get("fracture_at")
        if (fracture_at is not None and dtype.get("fractures") and not part.fractured
                and part.damage >= mh * fracture_at):
            part.fractured = result.newly_fractured = True
            part.note = d.get("fracture_text", "broken")
            if d.get("fracture_cripples") and not part.crippled:
                part.crippled = result.newly_crippled = True

        # A "sudden" damage type (wrenching) only tears a part off in one
        # violent pull: cranking an arm over and over breaks it, it doesn't
        # remove it. Everything else accumulates.
        destroy_at = d.get("destroy_at")
        amount = injury if dtype.get("sudden") else part.damage
        if (destroy_at is not None and not part.destroyed
                and (dtype.get("dismembers") or d.get("organ"))
                and amount >= mh * destroy_at):
            part.destroyed = part.crippled = result.newly_destroyed = True
            part.note = d.get("destroy_text", "destroyed")
            result.lost = self._lose_children(part.id)
            self.bleed_rate += d.get("sever_bleed", 0.0)

        self.bleed_rate += injury / mh * dtype.get("bleed", 0.0) * d.get("bleed_mult", 1.0) * BLEED_SCALE
        internal = d.get("internal")
        if internal and injury >= mh * internal.get("at", 0):
            self.internal_bleed += injury / mh * internal["bleed"] * BLEED_SCALE
            result.internal = True
        if d.get("brain"):
            self.hypoxia += injury / mh * d.get("brain_damage", 40)
        return result

    def _lose_children(self, part_id: str) -> list[str]:
        lost = []
        for p in self.parts.values():
            if p.data.get("parent") == part_id and not p.destroyed:
                p.destroyed = p.crippled = True
                p.note = "gone"
                lost.append(p.name)
                lost.extend(self._lose_children(p.id))
        return lost

    def heal(self, amount: float) -> None:
        self.hp = min(self.max_hp, self.hp + amount)

    def summary(self) -> str:
        hurt = []
        for p in self.parts.values():
            if p.destroyed:
                hurt.append(f"{p.name}: {p.note.upper() or 'DESTROYED'}")
            elif p.crippled:
                hurt.append(f"{p.name}: {'fractured' if p.fractured else 'crippled'}")
            elif p.fractured:
                hurt.append(f"{p.name}: fractured")
            elif p.damage:
                hurt.append(f"{p.name}: {p.damage:g}")
        extra = ""
        if self.blood < 99.5:
            extra += f", blood {self.blood:.0f}%"
        if self.total_bleed > 0.01:
            extra += f" (losing {self.total_bleed:.1f}%/s"
            extra += ", internal)" if self.internal_bleed > 0.01 else ")"
        if self.hypoxia >= 1:
            extra += f", brain damage {min(100, self.hypoxia):.0f}"
        return f"HP {round(self.hp)}/{self.max_hp}{extra}" + (f" [{'; '.join(hurt)}]" if hurt else "")
