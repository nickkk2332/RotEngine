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
SKILL_DEFAULT = ("DEX", -4)  # a skill with no JSON definition defaults to DEX-4


class Item:
    def __init__(self, data: dict):
        self.data = data
        self.id: str = data["id"]
        self.name: str = data["name"]
        self.ammo: int | None = data.get("magazine")
        self.armed = False  # an explosive with its fuse burning

    @property
    def the(self) -> str:
        """ "the pistol", but "Bob's arm" (a severed part is already named)."""
        return self.name if "'s " in self.name else f"the {self.name}"

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
        self.carried = [Item(content.get("item", i)) for i in eq.get("carry", [])]  # swap with 'w' 

        self.facing: tuple[int, int] = (1, 0)
        self.shock = 0                    # one-off pain penalty on the next action
        self.aim_target: int | None = None  # uid we've spent time aiming at
        self.aim_holds = 0                # consecutive waits for a teammate to clear the line
        self.defense_until = 0            # end of the current reaction window (ms)
        self.defenses_in_window = 0
        self.dead = False
        self.death_cause: str | None = None
        self.next_time = 0
        self.target: Creature | None = None
        self.controller = "ai"  # or "player": the sim pauses for input on its turns
        self.cooldowns: dict[str, float] = {}  # power id -> world time it's ready again

        # perception and stealth (see rotengine/perception.py)
        self.awareness: dict = {}          # enemy uid -> Awareness
        self.investigate: "Pos | None" = None  # somewhere suspicious to go and check
        self.search_turns = 0             # how long to poke around once there
        self.alarmed = False              # found a body / heard the alarm: stays on guard
        self.known_bodies: set[int] = set()
        self.post: "Pos" = pos            # where a guard returns to when things calm down
        self.post_facing: tuple[int, int] | None = None
        self.patrol: list = []            # waypoints; walked in a loop when calm
        self.patrol_i = 0
        self.sneaking = False
        self.last_moved = -10_000         # world ms of the last step (movement catches the eye)
        self.noisy_until = -10_000        # attacking/shooting makes you easy to spot for a bit

        # grappling
        self.grappling: Creature | None = None
        self.grappled_by: Creature | None = None
        self.choked = 0                   # seconds spent in a chokehold
        self.rear_hold = False            # (as the holder) took them from behind

    def __repr__(self) -> str:
        return f"<{self.name}#{self.uid} {self.team} {self.pos}>"

    # -- stats -------------------------------------------------------------
    def stat(self, name: str) -> int:
        return int(self.base_stats[name] + self.stat_bonus[name] + self.trait_sum("stat_mods", name))

    def skill(self, name: str) -> int:
        if name in self.skills:
            return self.skills[name]
        if self.content.has("skill", name):
            sd = self.content.get("skill", name)
            return self.stat(sd.get("stat", "DEX")) + sd.get("default", -4)
        return self.stat(SKILL_DEFAULT[0]) + SKILL_DEFAULT[1]

    def trait_sum(self, field: str, key: str | None = None) -> float:
        total = 0.0
        for t in self.traits:
            v = t.get(field)
            if isinstance(v, dict):
                v = v.get(key, v.get("*", 0)) if key is not None else 0
            total += v or 0
        return total

    def trait_product(self, field: str, key: str | None = None) -> float:
        out = 1.0
        for t in self.traits:
            v = t.get(field)
            if isinstance(v, dict):
                v = v.get(key, v.get("*", 1.0))
            out *= v if v is not None else 1.0
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
        if self.dead:
            return
        cur = self.statuses.get(status_id, 0)
        if status_id in self.statuses and (cur is None or (until is not None and cur >= until)):
            return
        self.statuses[status_id] = until

    # -- time --------------------------------------------------------------
    @property
    def tempo(self) -> float:
        """How fast this creature's time runs. 1 = human. A tempo-10 speedster
        does ten seconds of acting, reacting and recovering per real second."""
        t = self.trait_product("tempo")
        for d in self.status_defs():
            t *= d.get("tempo", 1.0)
        return max(0.05, t)

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

    def pain(self) -> int:
        """Ongoing pain penalty from accumulated trauma and broken bones.
        WIS above 10 and pain-related traits blunt it."""
        lost = max(0.0, self.max_hp - self.hp)
        p = 4 * lost / self.max_hp + self.body.fractures()
        p *= self.trait_product("pain_mult")
        p -= max(0, self.stat("WIS") - 10) / 2
        return max(0, min(6, int(p)))

    def fatigue_level(self) -> int:
        """0 fresh, 1 winded (< 1/3 stamina), 2 exhausted (<= 0)."""
        if self.stamina <= 0:
            return 2
        return 1 if self.stamina < self.max_stamina / 3 else 0

    def exert(self, amount: float) -> None:
        """Physical effort (swinging, running). Powers pay their cost directly."""
        self.stamina -= amount * self.trait_product("exertion_mult")

    def action_penalty(self, kind: str | None = None) -> int:
        """Everything that makes this creature worse at acting right now.
        `kind` ("melee"/"ranged") adds status modifiers specific to it."""
        mods = self.status_sum("attack_mod") + (self.status_sum(f"{kind}_attack_mod") if kind else 0)
        return (self.shock + self.pain() + self.body.blood_penalty()
                + (0, 1, 3)[self.fatigue_level()] - int(mods))

    def defense_penalty(self) -> int:
        return (self.pain() // 2 + self.body.blood_penalty() + (0, 1, 3)[self.fatigue_level()]
                - int(self.status_sum("defense_mod")))

    # -- derived combat numbers --------------------------------------------
    @property
    def speed(self) -> float:
        return (self.stat("DEX") + self.stat("CON")) / 4 + self.trait_sum("speed_bonus")

    @property
    def move_per_second(self) -> float:
        """Tiles per second of this creature's own time."""
        base = max(1, math.floor(self.speed)) * self.trait_product("move_mult")
        legs = self.body.total_with("stance")
        if legs and len(self.body.functional_with("stance")) < legs:
            base = 1.0  # hopping / limping
        mult = 1.0
        for d in self.status_defs():
            mult *= d.get("move_mult", 1.0)
        mult *= (1.0, 0.5, 0.25)[self.fatigue_level()]
        if self.sneaking:
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
        """Best raw defense, before arcs, reaction windows and penalties."""
        options = [("dodges", self.dodge())]
        if incoming_kind == "melee":
            for atk, _ in self.attacks():
                p = self.parry(atk)
                if p is not None:
                    options.append(("parries", p))
        return max(options, key=lambda o: o[1])

    def dr(self, part_id: str, dtype: str) -> int:
        def lookup(table: dict) -> int:
            return table.get(dtype, table.get("*", 0))

        part = self.body.part(part_id)
        total = lookup(part.data.get("dr", {}))
        for t in self.traits:
            if "natural_dr" in t:
                total += lookup(t["natural_dr"])
        if self.content.has("damage_type", dtype) and self.content.get("damage_type", dtype).get("ignores_armor"):
            return total  # a joint lock goes around the vest, not through it
        for item in self.worn:
            if item.armor and part_id in item.armor["covers"]:
                total += lookup(item.armor["dr"])
        return total

    def can_grip(self, item: Item) -> bool:
        hands = self.body.functional_with("grasp")
        if item.data.get("two_handed"):
            return len(hands) >= 2
        return any(p.data.get("primary") for p in hands)

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
