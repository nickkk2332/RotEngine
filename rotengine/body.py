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
* **Oxygen** is the air in you, 0-100%. It drains when you can't breathe
  (a choke, a crushed windpipe, a broken neck) or when blood can't reach the
  brain (a blood choke, a stopped heart), and comes back fast when you can.
  You gray out below 50%, black out at 10%.
* **Hypoxia** is brain damage from missing oxygen: once your oxygen is gone,
  or from blood loss below 50%. At 100 the brain dies.

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

# Recovery (roguelike mode, where time passes between fights). Compressed a
# long way from real life, but in the same order: bleeding has to stop
# before blood comes back, trauma fades over half an hour or so at CON 10,
# a splinted break knits in half an hour of game time, an unsplinted one
# never does, and nothing grows back.
HEAL_PER_S = 0.0006     # fraction of max HP per second at CON 10
BLOOD_PER_S = 0.02      # % of blood volume per second once bleeding has stopped
FRACTURE_HEAL_S = 1800  # seconds for a splinted break to knit


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
    deep: float = 0.0       # injury that reached deep inside (see damage type "depth")
    splinted: bool = False  # a break that's been set: less pain, and it heals
    knit: float = 0.0       # seconds a splinted break has had to heal

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
        self.oxygen: float = 100.0        # % of the air in you (see Creature.apnea_rate)
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
        """Breaks that hurt: a splinted one mostly doesn't."""
        return sum(p.fractured and not p.splinted for p in self.parts.values())

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
        # "depth": how much of a wound reaches what's deep inside. A slash
        # (depth 0.25) opens someone up without reaching the heart or brain;
        # a stab or a bullet goes all the way. Parts marked "deep" (vitals,
        # skull) are destroyed by deep injury, and internal bleeding and
        # brain damage come from it too.
        deep_injury = injury * dtype.get("depth", 1.0)
        part.deep += deep_injury
        destroy_at = d.get("destroy_at")
        if dtype.get("sudden") or (dtype.get("dismember_single") and not d.get("organ")):
            amount = injury  # it comes off in one blow, or not at all
        else:
            amount = part.deep if d.get("deep") else part.damage
        if (destroy_at is not None and not part.destroyed
                and (dtype.get("dismembers") or d.get("organ"))
                and amount >= mh * destroy_at):
            part.destroyed = part.crippled = result.newly_destroyed = True
            part.note = d.get("destroy_text", "destroyed")
            result.lost = self._lose_children(part.id)
            self.bleed_rate += d.get("sever_bleed", 0.0)

        self.bleed_rate += injury / mh * dtype.get("bleed", 0.0) * d.get("bleed_mult", 1.0) * BLEED_SCALE
        internal = d.get("internal")
        if internal and deep_injury >= mh * internal.get("at", 0):
            self.internal_bleed += deep_injury / mh * internal["bleed"] * BLEED_SCALE
            result.internal = True
        if d.get("brain"):
            self.hypoxia += deep_injury / mh * d.get("brain_damage", 40)
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

    def recover(self, seconds: float, con: int = 10) -> list[str]:
        """Time passing: trauma fades, blood comes back once the bleeding
        has stopped, splinted breaks knit. Returns what finished healing."""
        healed = []
        rate = HEAL_PER_S * max(0.5, con / 10) * seconds
        self.hp = min(self.max_hp, self.hp + self.max_hp * rate)
        if self.total_bleed < 0.01:
            self.bleed_rate = self.internal_bleed = 0.0
            if self.blood >= 50:
                self.blood = min(100.0, self.blood + BLOOD_PER_S * seconds)
        mh = self.max_hp
        for p in self.parts.values():
            if p.destroyed:
                continue
            if p.fractured and p.splinted:
                p.knit += seconds
                if p.knit >= FRACTURE_HEAL_S:
                    p.fractured = p.splinted = False
                    p.knit, p.note = 0.0, ""
                    cap = p.data.get("fracture_at", 1.0) * mh * 0.9
                    p.damage, p.counted = min(p.damage, cap), min(p.counted, cap)
                    healed.append(f"{p.name} has knit")
            if not p.fractured:
                p.damage = max(0.0, p.damage - mh * rate * 2)
                p.counted = max(0.0, p.counted - mh * rate * 2)
                cripple_at = p.data.get("cripple_at")
                if p.crippled and cripple_at is not None and p.damage <= mh * cripple_at:
                    p.crippled = False
                    healed.append(f"{p.name} works again")
        return healed

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
        if self.oxygen < 99.5:
            extra += f", air {self.oxygen:.0f}%"
        if self.hypoxia >= 1:
            extra += f", brain damage {min(100, self.hypoxia):.0f}"
        return f"HP {round(self.hp)}/{self.max_hp}{extra}" + (f" [{'; '.join(hurt)}]" if hurt else "")
