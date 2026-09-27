"""The fight screen: map, your body, the log, and every action you can take.

The screen owns no rules. It asks the sim to advance until it's your turn,
turns your keypress into an action from rotengine.actions (the same ones the
AI uses), and hands the cost back to the sim.
"""
from __future__ import annotations

import random
from typing import TYPE_CHECKING

import tcod.console

from .. import actions, ai, arena, combat, fov, perception, physics
from ..creature import Creature
from .keys import DIRECTIONS, VI_KEYS
from .menus import MainMenu, Screen
from .theme import (BLACK, BLUE, CONE_FRONT_BG, CONE_SIDE_BG, CURSOR_BG, CYAN, SPOTTED_BG, SUSPICIOUS_BG, DARK, DARK_RED, DIM, GREEN, GREY, LOG_H, LOG_Y,
                    MAP_H, MAP_W, MAP_X, MAP_Y, ORANGE, PANEL_BG, RED, SELECT_BG, SIDE_W, SIDE_X,
                    TARGET_BG, TITLE, WHITE, YELLOW, dim, material_fg)
from .widgets import bar, box, print_wrapped, wrap

if TYPE_CHECKING:
    from ..sim import Sim
    from ..world import Pos
    from .app import App

WAIT_MS = 500
AFTERMATH_S = 120

HELP = [
    ("move / attack", "arrows, numpad, vi-keys (hjklyubn); walk into an enemy to hit them"),
    ("f", "attack menu: pick target, attack, hit location, feint, aim"),
    ("F", "quick attack your target (steps closer if out of reach)"),
    ("Tab", "cycle target"),
    (". or numpad 5", "wait half a second (of your time)"),
    ("s", "sneak on/off: slower, quieter, harder to spot"),
    ("G", "grab someone next to you; again to choke (drag by moving)"),
    ("L", "let go"),
    ("w", "swap to your other weapon"),
    ("t", "throw something (grenade, Molotov...) at a spot"),
    ("T / P", "hurl the person you're holding / plant a charge on a wall"),
    ("B / c", "smash a wall or door / close a door (then a direction)"),
    ("v", "show where enemies are looking"),
    ("r", "reload"),
    ("m", "first aid (yourself, or a bleeding ally next to you)"),
    ("z", "drop prone / stand up"),
    ("g", "pick up a weapon"),
    ("p", "use a power"),
    ("< >", "go up / down stairs"),
    ("[ ]", "look at the level below / above"),
    ("x", "look around (inspect anyone's wounds)"),
    ("Esc", "quit to the menu"),
]


class AttackMenu:
    """Every way to attack one target, laid out so you can see the odds."""

    def __init__(self, sim: "Sim", player: Creature, target: Creature):
        self.target = target
        self.plans = combat.attack_plans(sim, player, target)
        self.attacks: list[tuple[dict, object]] = []
        for p in self.plans:
            if not any(a is p.attack for a, _ in self.attacks):
                self.attacks.append((p.attack, p.item))
        best = max(self.plans, key=lambda p: p.value, default=None)
        self.attack_i = next((i for i, (a, _) in enumerate(self.attacks) if best and a is best.attack), 0)
        self.dec = best.deceptive if best else 0
        self.aim = best.aim_first if best else False
        self.best = best
        self.row = 0
        if best:
            self.row = self.locations().index(best.location)

    @property
    def attack(self) -> dict | None:
        return self.attacks[self.attack_i][0] if self.attacks else None

    def locations(self) -> list[str | None]:
        seen: list[str | None] = []
        for p in self.plans:
            if p.attack is self.attack and p.location not in seen:
                seen.append(p.location)
        return seen

    def can_aim(self) -> bool:
        return any(p.aim_first for p in self.plans if p.attack is self.attack)

    def plan(self, location=..., dec=None) -> combat.AttackPlan | None:
        loc = self.locations()[self.row] if location is ... else location
        dec = self.dec if dec is None else dec
        aim = self.aim and self.can_aim()
        for p in self.plans:
            if p.attack is self.attack and p.location == loc and p.deceptive == dec and p.aim_first == aim:
                return p
        return None

    def cycle_attack(self, d: int) -> None:
        if self.attacks:
            self.attack_i = (self.attack_i + d) % len(self.attacks)
            self.row = min(self.row, len(self.locations()) - 1)
            self.dec = 0
            self.aim = False


class GameScreen(Screen):
    def __init__(self, app: "App", scenario: dict, content, sim: "Sim", player: Creature, seed: int):
        super().__init__(app)
        self.scenario, self.content, self.sim, self.player, self.seed = scenario, content, sim, player, seed
        self.player_index = sim.creatures.index(player)
        player.controller = "player"
        self.mode = "play"
        self.view_z = player.pos[2]
        self.visible: set = set()
        self.seen: dict = {}
        self.notice = f"You are {player.name}. Press ? for keys."
        self.cursor: "Pos | None" = None
        self.menu: AttackMenu | None = None
        self.summary: list[str] = []
        self.show_cones = False
        self.status = self.sim.advance()
        self._on_turn()

    # -- turn flow ---------------------------------------------------------
    @property
    def target(self) -> Creature | None:
        t = self.player.target
        return t if t is not None and t.active and t.team != self.player.team else None

    def _sees(self, c: Creature) -> bool:
        """Can the player make this creature out (line of sight and enough light)?"""
        if c is self.player or c.team == self.player.team:
            return c.pos in self.visible
        return c.pos in self.visible and perception.can_make_out(self.sim, self.player, c)

    def _visible_enemies(self) -> list[Creature]:
        p = self.player
        foes = [c for c in self.sim.creatures if c.team != p.team and c.active and self._sees(c)]
        return sorted(foes, key=lambda c: (self.sim.distance(p, c), c.uid))

    def _on_turn(self) -> None:
        self._update_fov()
        foes = self._visible_enemies()
        if self.target not in foes:
            self.player.target = foes[0] if foes else self.target
        if self.status == "player" and self.target in foes:
            combat.face(self.player, self.target.pos)  # same free turn-to-face the AI gets
        if self.status in ("over", "timeout"):
            self.mode = "over"

    def _update_fov(self) -> None:
        if self.player.dead:
            return
        fire = self.sim.fields.fire
        glowing = [(int(x), int(y), int(z)) for z, y, x in zip(*(fire > 1).nonzero())] if fire.any() else ()
        self.visible = fov.visible_from(self.sim.world, self.player.pos, glowing=glowing)
        for pos in self.visible:
            self.seen[pos] = self._tile(pos)
        self.view_z = self.player.pos[2]

    def _do(self, cost: int | None, fail: str = "You can't do that right now.") -> None:
        if cost is None:
            self.notice = fail
            return
        self.notice = ""
        self.sim.player_act(cost)
        self.status = self.sim.advance()
        self._on_turn()

    # -- input -------------------------------------------------------------
    def on_key(self, key: str):
        handler = getattr(self, f"_key_{self.mode}")
        return handler(key)

    def _direction(self, key: str) -> tuple[int, int] | None:
        return DIRECTIONS.get(VI_KEYS.get(key, key))

    def _key_play(self, key: str):
        p, sim = self.player, self.sim
        d = self._direction(key)
        if d is not None and p.grappled_by is not None:
            return self._do(actions.struggle(sim, p))
        if d is not None:
            x, y, z = p.pos
            dest = (x + d[0], y + d[1], z)
            other = sim.creature_at(dest)
            if other is not None and other.team != p.team:
                plan = combat.best_attack_plan(sim, p, other, allow_aim=False)
                if plan is None:
                    self.notice = f"You can't attack {other.name} from here."
                    return None
                p.target = other
                return self._do(actions.attack(sim, p, other, plan))
            if other is not None:
                self.notice = f"{other.name} is in the way."
                return None
            return self._do(actions.step(sim, p, dest), "You can't go that way.")
        if key in (".", "wait"):
            return self._do(actions.wait(sim, p, WAIT_MS))
        if key == "f":
            if not self._visible_enemies():
                self.notice = "No enemy in sight."
                return None
            self.mode = "target"
            if self.target not in self._visible_enemies():
                p.target = self._visible_enemies()[0]
            return None
        if key == "F":
            t = self.target
            if t is None or t not in self._visible_enemies():
                self.notice = "No target in sight."
                return None
            plan = combat.best_attack_plan(sim, p, t)
            if plan is None:
                w = p.wielded
                if (w is None or w.ammo != 0) and not combat.friendly_in_line(sim, p, t):
                    step = ai._step_toward(sim, p, t.pos)  # out of reach: close in, one step per press
                    if step is not None:
                        return self._do(actions.step(sim, p, step))
                self.notice = self._why_no_attack(t)
                return None
            return self._do(actions.attack(sim, p, t, plan))
        if key in ("tab", "shift-tab"):
            self._cycle_target(1 if key == "tab" else -1)
            return None
        if key == "r":
            return self._do(actions.reload(sim, p), "Nothing to reload.")
        if key == "s":
            self._do(actions.toggle_sneak(sim, p))
            self.notice = "Sneaking: slow and quiet." if p.sneaking else "Walking normally."
            return None
        if key == "G":
            held = p.grappling
            if held is not None:
                if held.dead:
                    self.notice = "They're dead. Move to drag the body; L to let go."
                    return None
                self._do(actions.choke(sim, p))
                if not held.dead and not held.conscious:
                    self.notice = "They're out cold. Squeezing longer kills them; L to let go."
                return None
            # enemies next to you, or anyone down (to drag them out of sight)
            near = [c for c in sim.creatures
                    if c is not p and c.pos[2] == p.pos[2] and sim.in_melee_reach(p.pos, c.pos)
                    and (c.team != p.team or not c.active)]
            if not near:
                self.notice = "Nobody next to you to grab."
                return None
            who = self.target if self.target in near else min(near, key=lambda c: (not c.active, c.uid))
            return self._do(actions.grab(sim, p, who), "You can't get a grip on them.")
        if key == "L":
            return self._do(actions.release(sim, p), "You're not holding anyone.")
        if key == "w":
            return self._do(actions.swap_weapon(sim, p), "You have nothing else to draw.")
        if key == "v":
            self.show_cones = not self.show_cones
            return None
        if key == "t":
            options = actions.throwables(p)
            if not options:
                self.notice = "You have nothing to throw."
                return None
            if len(options) == 1:
                return self._start_aim(options[0])
            self.mode = "throwmenu"
            return None
        if key == "T":
            if p.grappling is None:
                self.notice = "Grab someone first (G)."
                return None
            return self._ask_direction("Hurl them which way?", lambda d: actions.hurl(sim, p, d))
        if key == "B":
            return self._ask_direction("Smash which way?", lambda d: actions.smash(sim, p, self._adjacent(d)),
                                       "Nothing there you can smash.")
        if key == "c":
            return self._ask_direction("Close which door?", lambda d: actions.close_door(sim, p, self._adjacent(d)),
                                       "No open door there (or something's in the way).")
        if key == "P":
            charges = [i for i in p.carried if i.data.get("plantable")]
            if not charges:
                self.notice = "You have no charges."
                return None
            return self._ask_direction("Plant it on which wall?",
                                       lambda d: actions.plant(sim, p, charges[0], self._adjacent(d)),
                                       "No wall or door there to plant it on.")
        if key == "m":
            return self._first_aid()
        if key == "z":
            if p.has_status("prone"):
                return self._do(actions.stand_up(sim, p), "Your legs won't hold you.")
            return self._do(actions.go_prone(sim, p))
        if key == "g":
            return self._do(actions.pick_up(sim, p), "Nothing here you can pick up and use.")
        if key == "p":
            if not p.powers:
                self.notice = "You have no powers."
                return None
            self.mode = "powers"
            return None
        if key in ("<", ">"):
            return self._do(actions.climb(sim, p, 1 if key == "<" else -1),
                            "There are no usable stairs here.")
        if key in ("[", "]"):
            self.view_z = max(0, min(sim.world.depth - 1, self.view_z + (1 if key == "]" else -1)))
            return None
        if key == "x":
            self.mode, self.cursor = "look", p.pos
            return None
        if key == "?":
            self.mode = "help"
            return None
        if key == "esc":
            self.mode = "confirm"
            return None
        return None

    def _adjacent(self, d: tuple[int, int]) -> "Pos":
        x, y, z = self.player.pos
        return (x + d[0], y + d[1], z)

    def _ask_direction(self, prompt: str, act, fail: str = "You can't do that."):
        self.mode = "dir"
        self.notice = prompt + " (direction, Esc to cancel)"
        self._dir_action = (act, fail)
        return None

    def _key_dir(self, key: str):
        d = self._direction(key)
        if key == "esc":
            self.mode, self.notice = "play", ""
            return None
        if d is None:
            return None
        act, fail = self._dir_action
        self.mode = "play"
        return self._do(act(d), fail)

    def _start_aim(self, item) -> None:
        self.mode = "aim"
        self._throwing = item
        t = self.target
        self.cursor = t.pos if t is not None and self._sees(t) else self.player.pos
        self.notice = f"Throw the {item.name} where? (move the cursor, Enter to throw)"
        return None

    def _key_throwmenu(self, key: str):
        options = actions.throwables(self.player)
        if key == "esc":
            self.mode = "play"
        elif len(key) == 1 and "a" <= key <= "z" and ord(key) - ord("a") < len(options):
            return self._start_aim(options[ord(key) - ord("a")])
        return None

    def _key_aim(self, key: str):
        d = self._direction(key)
        p = self.player
        if d is not None:
            x, y, z = self.cursor
            nxt = (x + d[0], y + d[1], z)
            if self.sim.world.in_bounds(nxt):
                self.cursor = nxt
        elif key in ("tab", "shift-tab"):
            self._cycle_target(1 if key == "tab" else -1)
            if self.target is not None:
                self.cursor = self.target.pos
        elif key == "esc":
            self.mode, self.cursor, self.notice = "play", None, ""
        elif key in ("enter", "t"):
            target, item = self.cursor, self._throwing
            self.mode, self.cursor = "play", None
            rng = physics.throw_range(p, item.data.get("weight", 1))
            return self._do(actions.throw(self.sim, p, item, target),
                            f"Too far: you can throw that {rng} tiles." if self.sim.distance_pos(p.pos, target) > rng
                            else "You can't throw that there.")
        return None

    def _first_aid(self):
        p, sim = self.player, self.sim
        if p.body.bleed_rate > 0.05:
            return self._do(actions.bandage(sim, p, p), "You can't bandage anything right now.")
        near = [a for a in sim.creatures if a.team == p.team and a is not p and not a.dead
                and a.body.bleed_rate > 0.05 and sim.in_melee_reach(p.pos, a.pos)]
        if not near:
            self.notice = "Nobody here is bleeding in a way first aid can fix."
            return None
        worst = max(near, key=lambda a: a.body.bleed_rate)
        return self._do(actions.bandage(sim, p, worst))

    def _why_no_attack(self, t: Creature) -> str:
        p = self.player
        w = p.wielded
        if w is not None and w.ammo == 0:
            return f"Your {w.name} is empty: press r to reload."
        if combat.friendly_in_line(self.sim, p, t):
            return f"A friend is in your line of fire to {t.name}."
        return f"{t.name} is out of reach: get closer."

    def _cycle_target(self, d: int) -> None:
        foes = self._visible_enemies()
        if not foes:
            self.notice = "No enemy in sight."
            return
        i = foes.index(self.target) if self.target in foes else -d
        self.player.target = foes[(i + d) % len(foes)]

    def _key_target(self, key: str):
        if key in ("tab", "down", "right", "j", "l"):
            self._cycle_target(1)
        elif key in ("shift-tab", "up", "left", "k", "h"):
            self._cycle_target(-1)
        elif key in ("enter", "f"):
            if self.target is None:
                self.mode = "play"
                return None
            self.menu = AttackMenu(self.sim, self.player, self.target)
            if not self.menu.plans:
                self.notice = self._why_no_attack(self.target)
                self.mode = "play"
            else:
                self.mode = "attack"
        elif key == "esc":
            self.mode = "play"
        return None

    def _key_attack(self, key: str):
        m = self.menu
        if key == "up":
            m.row = (m.row - 1) % len(m.locations())
        elif key == "down":
            m.row = (m.row + 1) % len(m.locations())
        elif key == "left":
            m.dec = max(0, m.dec - 1)
        elif key == "right":
            if m.plan(dec=m.dec + 1) is not None:
                m.dec += 1
        elif key == " " and m.can_aim():
            m.aim = not m.aim
        elif key in ("tab", "<", ">", "shift-tab"):
            m.cycle_attack(-1 if key in ("<", "shift-tab") else 1)
        elif key == "enter":
            plan = m.plan()
            if plan is None:
                self.notice = "That combination isn't possible (too hard to pull off)."
                return None
            self.mode, self.menu = "play", None
            return self._do(actions.attack(self.sim, self.player, m.target, plan))
        elif key == "esc":
            self.mode, self.menu = "play", None
        return None

    def _key_powers(self, key: str):
        powers = self.player.powers
        if key == "esc":
            self.mode = "play"
            return None
        if len(key) == 1 and "a" <= key <= "z" and ord(key) - ord("a") < len(powers):
            power = powers[ord(key) - ord("a")]
            why = actions.power_blocked(self.sim, self.player, power)
            if why:
                self.notice = f"{power['name']}: {why}."
                return None
            self.mode = "play"
            return self._do(actions.use_power(self.sim, self.player, power, self.target))
        return None

    def _key_look(self, key: str):
        d = self._direction(key)
        if d is not None and self.cursor is not None:
            x, y, z = self.cursor
            nxt = (x + d[0], y + d[1], self.view_z)
            if self.sim.world.in_bounds(nxt):
                self.cursor = nxt
        elif key in ("[", "]"):
            self.view_z = max(0, min(self.sim.world.depth - 1, self.view_z + (1 if key == "]" else -1)))
            if self.cursor:
                self.cursor = (self.cursor[0], self.cursor[1], self.view_z)
        elif key in ("esc", "x", "enter"):
            self.mode, self.cursor = "play", None
            self.view_z = self.player.pos[2]
        return None

    def _key_help(self, key: str):
        self.mode = "play"
        return None

    def _key_confirm(self, key: str):
        if key in ("y", "Y"):
            return MainMenu(self.app)
        self.mode = "play"
        return None

    def _key_over(self, key: str):
        if key == "a":
            self._run_aftermath()
            self.mode = "summary"
        elif key == "r":
            return self._rematch()
        elif key in ("q", "esc", "enter"):
            return MainMenu(self.app)
        return None

    def _key_summary(self, key: str):
        if key == "r":
            return self._rematch()
        return MainMenu(self.app)

    def _run_aftermath(self) -> None:
        for c in self.sim.creatures:
            c.controller = "ai"
        self.sim.awaiting = None
        self.sim.run_aftermath(AFTERMATH_S)
        self.summary = []
        for c in self.sim.creatures:
            state = f"dead ({c.death_cause})" if c.dead else "unconscious" if not c.conscious else "standing"
            you = " (you)" if c is self.player else ""
            self.summary.append(f"{c.name}{you} [{c.team}]: {state}. {c.body.summary()}")

    def _rematch(self):
        seed = random.randrange(1_000_000)
        sim = arena.build(self.scenario, self.content, seed)
        return GameScreen(self.app, self.scenario, self.content, sim,
                          sim.creatures[self.player_index], seed)

    # -- drawing -----------------------------------------------------------
    def _tile(self, pos: "Pos") -> tuple[str, tuple]:
        w = self.sim.world
        fill = w.fill_mat(pos)
        if fill["id"] != "air":
            return fill["glyph"], material_fg(fill["id"])
        floor = w.floor_mat(pos)
        if floor is not None:
            return floor.get("floor_glyph", "."), dim(material_fg(floor["id"]), 0.6)
        x, y, z = pos
        if z > 0:  # open air: glimpse the level below
            below = (x, y, z - 1)
            f2 = w.fill_mat(below)
            if f2["id"] != "air":
                return f2["glyph"], dim(material_fg(f2["id"]), 0.3)
            fl2 = w.floor_mat(below)
            if fl2 is not None:
                return fl2.get("floor_glyph", "."), dim(material_fg(fl2["id"]), 0.3)
        return " ", BLACK

    def _camera(self) -> tuple[int, int]:
        """Top-left world coordinate shown in the viewport. Maps smaller than
        the viewport are centred (negative offsets); bigger ones follow the
        focus (you, or the look cursor)."""
        focus = self.cursor or self.player.pos
        w = self.sim.world

        def axis(f: int, size: int, view: int) -> int:
            if size <= view:
                return -((view - size) // 2)
            return min(max(0, f - view // 2), size - view)

        return axis(focus[0], w.width, MAP_W), axis(focus[1], w.height, MAP_H)

    def render(self, con: tcod.console.Console) -> None:
        self._render_top(con)
        self._render_map(con)
        self._render_side(con)
        self._render_log(con)
        self._render_hints(con)
        overlay = getattr(self, f"_render_{self.mode}", None)
        if overlay:
            overlay(con)

    def _render_top(self, con) -> None:
        s = self.sim
        title = self.scenario.get("name", self.scenario.get("id", ""))
        con.print(0, 0, f" {title} ", fg=TITLE)
        right = f"t={s.time / 1000:.2f}s  level {self.view_z}{' (viewing)' if self.view_z != self.player.pos[2] else ''} "
        con.print(con.width - len(right), 0, right, fg=GREY)
        if self.notice:
            con.print(len(title) + 3, 0, self.notice[:con.width - len(title) - len(right) - 4], fg=YELLOW)

    def _lit(self, pos, fg):
        """Shade a visible tile by how lit it is (night-vision goggles show
        the dark in green)."""
        level = perception.light_at(self.sim, pos)
        if level >= 0.99:
            return fg
        if self.player.has_trait("night_vision"):
            f = 0.55 + 0.45 * min(1.0, level + 0.3)
            return (int(fg[0] * f * 0.6), int(min(255, fg[1] * f * 1.1 + 25)), int(fg[2] * f * 0.6))
        return dim(fg, 0.3 + 0.7 * min(1.0, level))

    def _cone_tiles(self) -> dict:
        """Tiles each visible enemy is watching: front arc bright, sides dim."""
        tiles: dict = {}
        sim, z = self.sim, self.view_z
        for c in self.sim.creatures:
            if c.team == self.player.team or not c.active or c.pos[2] != z or not self._sees(c):
                continue
            cx, cy, _ = c.pos
            for y in range(cy - 10, cy + 11):
                for x in range(cx - 10, cx + 11):
                    pos = (x, y, z)
                    if pos not in self.visible or pos == c.pos:
                        continue
                    side = combat.arc(c, pos)
                    if side == "rear" or not sim.world.has_los(c.pos, pos):
                        continue
                    if side == "front" or tiles.get(pos) != "front":
                        tiles[pos] = side
        return tiles

    def _render_map(self, con) -> None:
        w, z = self.sim.world, self.view_z
        ox, oy = self._camera()
        for sy in range(MAP_H):
            y = oy + sy
            if not 0 <= y < w.height:
                continue
            for sx in range(MAP_W):
                x = ox + sx
                if not 0 <= x < w.width:
                    continue
                pos = (x, y, z)
                bg = None
                if pos in self.visible:
                    ch, fg = self._tile(pos)
                    fg = self._lit(pos, fg)
                    ch, fg, bg = self._hazards(pos, ch, fg)
                elif pos in self.seen:
                    ch, fg = self.seen[pos]
                    fg = dim(fg, 0.4)
                else:
                    continue
                con.print(MAP_X + sx, MAP_Y + sy, ch, fg=fg, bg=bg)
        if self.show_cones:
            for pos, side in self._cone_tiles().items():
                self._highlight(con, pos, CONE_FRONT_BG if side == "front" else CONE_SIDE_BG)
        marks: dict = {}
        for pos, item in self.sim.items:
            if pos[2] == z and pos in self.visible:
                marks[pos[:2]] = (item.data.get("glyph", "("), RED if item.armed else CYAN,
                                  (70, 0, 0) if item.armed else None)
        for layer in ("dead", "down", "up"):
            for c in self.sim.creatures:
                state = "dead" if c.dead else "up" if c.active else "down"
                if state != layer or c.pos[2] != z or not self._sees(c):
                    continue
                if state == "dead":
                    marks[c.pos[:2]] = ("%", DARK_RED, None)
                elif state == "down":
                    marks[c.pos[:2]] = ("&", RED if c.team != self.player.team else GREEN, None)
                else:
                    fg = YELLOW if c is self.player else GREEN if c.team == self.player.team else RED
                    marks[c.pos[:2]] = (c.glyph, fg, self._alert_bg(c))
        for (x, y), (ch, fg, bg) in marks.items():
            if ox <= x < ox + MAP_W and oy <= y < oy + MAP_H:
                con.print(MAP_X + x - ox, MAP_Y + y - oy, ch, fg=fg, bg=bg)
        t = self.target
        if t is not None and t.pos[2] == z and self._sees(t):
            self._highlight(con, t.pos, TARGET_BG)
        if self.cursor is not None:
            self._highlight(con, self.cursor, CURSOR_BG)

    def _hazards(self, pos, ch, fg):
        """Fire and gas drawn over a visible tile."""
        x, y, z = pos
        fields = self.sim.fields
        heat = float(fields.fire[z, y, x])
        if heat > 0:
            flame = YELLOW if heat > 6 else ORANGE if heat > 3 else RED
            return "^", flame, (60, 12, 0)
        bg = None
        smoke = fields.gas.get("smoke")
        if smoke is not None and smoke[z, y, x] > 3:
            v = int(min(90, 20 + smoke[z, y, x]))
            bg = (v, v, v)
            if smoke[z, y, x] > 30:
                ch, fg = "░", (170, 170, 170)
        tear = fields.gas.get("tear_gas")
        if tear is not None and tear[z, y, x] > 3:
            v = int(min(80, 15 + tear[z, y, x]))
            bg = (v, v, v // 4)
        return ch, fg, bg

    def _alert_bg(self, c: Creature):
        """Background on an enemy's glyph: amber = suspicious, red = has seen you."""
        if c.team == self.player.team:
            return None
        aw = c.awareness.get(self.player.uid)
        if aw is not None and aw.level >= perception.AWARE:
            return SPOTTED_BG
        if (aw is not None and aw.level >= perception.SUSPICIOUS) or perception.state(c) != "calm":
            return SUSPICIOUS_BG
        return None

    def _awareness_word(self, c: Creature) -> str:
        aw = c.awareness.get(self.player.uid)
        if aw is not None and aw.level >= perception.AWARE:
            return "has spotted you"
        if aw is not None and aw.level >= perception.SUSPICIOUS:
            return f"suspicious ({aw.level:.0f}%)"
        if perception.state(c) == "searching":
            return "searching for something"
        return "unaware of you" + (f" ({aw.level:.0f}%)" if aw is not None and aw.level >= 1 else "")

    def _highlight(self, con, pos, bg) -> None:
        ox, oy = self._camera()
        x, y = pos[0] - ox, pos[1] - oy
        if 0 <= x < MAP_W and 0 <= y < MAP_H:
            con.bg[MAP_X + x, MAP_Y + y] = bg

    def _render_side(self, con) -> None:
        p = self.player
        x, y, w = SIDE_X, 1, SIDE_W
        con.draw_rect(x - 1, 1, w + 1, con.height - 2, ord(" "), bg=PANEL_BG)
        con.print(x, y, p.name[:w], fg=YELLOW)
        y += 1
        con.print(x, y, f"{p.team} · tempo {p.tempo:g}x · move {p.move_per_second:.1f}/s"[:w], fg=GREY)
        y += 2
        hp_frac = max(0.0, p.hp / p.max_hp)
        con.print(x, y, f"HP    {round(p.hp):>3}/{p.max_hp}", fg=WHITE)
        bar(con, x + 15, y, w - 15, hp_frac, GREEN if hp_frac > 0.5 else ORANGE if hp_frac > 0 else RED)
        y += 1
        b = p.body
        con.print(x, y, f"Blood {b.blood:>4.0f}%", fg=WHITE)
        bar(con, x + 15, y, w - 15, b.blood / 100, RED)
        y += 1
        con.print(x, y, f"Stam  {p.stamina:>4.1f}/{p.max_stamina}", fg=WHITE)
        bar(con, x + 15, y, w - 15, max(0.0, p.stamina) / p.max_stamina, BLUE)
        y += 1
        if b.total_bleed > 0.01:
            internal = " (internal)" if b.internal_bleed > 0.01 else ""
            con.print(x, y, f"Bleeding {b.total_bleed:.2f}%/s{internal}"[:w], fg=RED)
            y += 1
        pen = p.action_penalty()
        if pen > 0:
            con.print(x, y, f"Penalty -{pen} (pain {p.pain()}, shock {p.shock})"[:w], fg=ORANGE)
            y += 1
        elif pen < 0:
            con.print(x, y, f"Bonus +{-pen} to attacks"[:w], fg=GREEN)
            y += 1
        if p.statuses:
            con.print(x, y, ("Status: " + ", ".join(self.content.get("status", s)["name"]
                                                     for s in p.statuses))[:w], fg=ORANGE)
            y += 1
        y += 1
        weapon = p.wielded
        if weapon is not None:
            ammo = f" {weapon.ammo}/{weapon.data['magazine']}" if weapon.ammo is not None else ""
            con.print(x, y, f"{weapon.name}{ammo}"[:w], fg=CYAN)
        else:
            con.print(x, y, "unarmed", fg=CYAN)
        y += 1
        if p.carried:
            from collections import Counter
            counts = Counter(i.name for i in p.carried)
            text = "carrying: " + ", ".join(f"{n}" if k == 1 else f"{k}x {n}" for n, k in counts.items())
            y += print_wrapped(con, x, y, w, text, fg=GREY, max_lines=3)
        if p.aim_target is not None and self.target is not None and p.aim_target == self.target.uid:
            con.print(x, y, f"aimed at {self.target.name}"[:w], fg=CYAN)
            y += 1
        y += 1
        y = self._render_stealth(con, x, y, w)
        wounds = [ln for ln in self._wound_lines(p)]
        if wounds:
            con.print(x, y, "Wounds", fg=TITLE)
            y += 1
            for line, fg in wounds[:6]:
                con.print(x + 1, y, line[:w - 1], fg=fg)
                y += 1
            y += 1
        if self.mode == "look" and self.cursor is not None:
            self._render_look_info(con, x, y, w)
            return
        t = self.target
        if t is not None and self._sees(t):
            con.print(x, y, "Target", fg=TITLE)
            y += 1
            con.print(x + 1, y, f"{t.name} · {self.sim.distance(p, t)} tiles"[:w - 1], fg=RED)
            y += 1
            y += print_wrapped(con, x + 1, y, w - 1, self._condition(t), fg=WHITE, max_lines=2)
            aware = self._awareness_word(t)
            con.print(x + 1, y, aware[:w - 1], fg=RED if "spotted" in aware else ORANGE
                      if "suspicious" in aware or "searching" in aware else GREEN)
            y += 1
            weapon = t.wielded.name if t.wielded else "unarmed"
            con.print(x + 1, y, weapon[:w - 1], fg=GREY)
            y += 1
            side = combat.arc(t, p.pos)
            note = {"front": "facing you", "side": "you're on their flank",
                    "rear": "their back is to you"}[side]
            con.print(x + 1, y, note[:w - 1], fg=GREEN if side != "front" else GREY)
            y += 1
            cover = combat.cover(self.sim, p.pos, t)[0]
            if cover:
                con.print(x + 1, y, f"in cover ({cover})", fg=ORANGE)
                y += 1
        n = len(self._visible_enemies())
        con.print(x, con.height - 2, f"{n} enem{'y' if n == 1 else 'ies'} in sight"[:w], fg=GREY)

    def _render_stealth(self, con, x: int, y: int, w: int) -> int:
        p, sim = self.player, self.sim
        mode = "prone" if p.has_status("prone") else "sneaking" if p.sneaking else "walking"
        light = perception.light_word(perception.light_at(sim, p.pos))
        con.print(x, y, f"{mode} · {light}"[:w], fg=GREEN if p.sneaking and light != "bright light" else GREY)
        y += 1
        watchers = [c for c in sim.creatures if c.team != p.team and c.active
                    and perception.aware_of(c, p)]
        if watchers:
            con.print(x, y, f"spotted by {len(watchers)}"[:w], fg=RED)
        else:
            con.print(x, y, "unseen", fg=GREEN)
        y += 1
        if p.grappling is not None:
            h = p.grappling
            state = "dead" if h.dead else "out cold" if not h.conscious else f"choked {h.choked}s"
            con.print(x, y, f"holding {h.name} ({state})"[:w], fg=CYAN)
            y += 1
        if p.grappled_by is not None:
            con.print(x, y, f"held by {p.grappled_by.name}: move to struggle"[:w], fg=RED)
            y += 1
        return y + 1

    def _wound_lines(self, c: Creature):
        for part in c.body.parts.values():
            if part.destroyed:
                yield f"{part.name}: {part.data.get('destroy_text', 'destroyed')}", RED
            elif part.fractured:
                yield f"{part.name}: broken", ORANGE
            elif part.crippled:
                yield f"{part.name}: crippled", ORANGE
            elif part.damage:
                yield f"{part.name}: {part.damage:g} injury", GREY

    def _condition(self, c: Creature) -> str:
        if c.dead:
            return f"dead ({c.death_cause})"
        if not c.conscious:
            return "down"
        frac = c.hp / c.max_hp
        word = ("unhurt" if frac >= 1 and not any(pt.damage for pt in c.body.parts.values())
                else "lightly hurt" if frac > 0.75 else "hurt" if frac > 0.4
                else "badly hurt" if frac > 0 else "barely standing")
        extras = [self.content.get("status", s)["name"] for s in c.statuses]
        if c.body.total_bleed > 0.05:
            extras.append("bleeding")
        return word + (f" ({', '.join(extras)})" if extras else "")

    def _render_look_info(self, con, x, y, w) -> None:
        pos = self.cursor
        con.print(x, y, f"Looking at {pos[0]},{pos[1]} level {pos[2]}"[:w], fg=TITLE)
        y += 1
        if pos not in self.visible and pos not in self.seen:
            con.print(x + 1, y, "unexplored", fg=GREY)
            return
        world = self.sim.world
        fill, floor = world.fill_mat(pos), world.floor_mat(pos)
        if fill["id"] != "air":
            hp = world.fill_hp[pos[2], pos[1], pos[0]]
            con.print(x + 1, y, f"{fill['name']} (HP {hp}, DR {fill.get('dr', 0)})"[:w - 1], fg=GREY)
            y += 1
        if floor is not None:
            con.print(x + 1, y, f"{floor['name']} floor"[:w - 1], fg=GREY)
            y += 1
        elif fill["id"] == "air" and pos[2] > 0:
            con.print(x + 1, y, "open air (a drop)", fg=GREY)
            y += 1
        for ipos, item in self.sim.items:
            if ipos == pos:
                live = " (LIVE)" if item.armed else ""
                con.print(x + 1, y, f"a {item.name}{live}"[:w - 1], fg=RED if item.armed else CYAN)
                y += 1
        if self.sim.fields.burning(pos) > 0:
            con.print(x + 1, y, "on fire", fg=ORANGE)
            y += 1
        for gas_id in self.sim.fields.gas:
            conc = self.sim.fields.concentration(gas_id, pos)
            if conc > 3:
                con.print(x + 1, y, f"{self.content.get('gas', gas_id)['name']} ({conc:.0f})"[:w - 1], fg=GREY)
                y += 1
        if pos not in self.visible:
            con.print(x + 1, y, "(remembered, not in sight)", fg=DARK)
            return
        for c in self.sim.creatures:
            if c.pos != pos:
                continue
            y += 1
            fg = YELLOW if c is self.player else GREEN if c.team == self.player.team else RED
            con.print(x, y, f"{c.name} [{c.team}]"[:w], fg=fg)
            y += 1
            y += print_wrapped(con, x + 1, y, w - 1, self._condition(c), fg=WHITE, max_lines=2)
            con.print(x + 1, y, (c.wielded.name if c.wielded else "unarmed")[:w - 1], fg=GREY)
            y += 1
            b = c.body
            con.print(x + 1, y, f"HP {round(c.hp)}/{c.max_hp} · blood {b.blood:.0f}%"[:w - 1], fg=GREY)
            y += 1
            for line, lfg in list(self._wound_lines(c))[:10]:
                con.print(x + 1, y, line[:w - 1], fg=lfg)
                y += 1

    def _render_log(self, con) -> None:
        lines: list[tuple[str, tuple]] = []
        name = self.player.name
        witnessed = [raw for raw, meta in zip(self.sim.lines, self.sim.line_meta) if self._witnessed(raw, meta)]
        for raw in witnessed[-40:]:
            body = raw.split("] ", 1)[-1]
            fg = GREY
            if " dies" in body or "falls unconscious" in body:
                fg = RED
            elif body.lstrip().startswith(name):
                fg = YELLOW
            elif name in body:
                fg = ORANGE
            for part in wrap(raw, MAP_W, indent="          "):
                lines.append((part, fg))
        con.draw_rect(0, LOG_Y - 1, MAP_W, 1, ord("─"), fg=DIM)
        for i, (line, fg) in enumerate(lines[-LOG_H:]):
            con.print(0, LOG_Y + i, line, fg=fg)

    def _witnessed(self, raw: str, meta) -> bool:
        """Log lines the player's character could know about: things that
        happened where they can see, sounds they heard, and anything about
        themselves."""
        positions, private_to = meta
        if private_to is not None:
            return private_to == self.player.uid
        if self.player.name in raw or not positions:
            return True
        return any(pos in self.visible for pos in positions)

    def _render_hints(self, con) -> None:
        hints = {
            "play": "move/bump · f attack · F quick · t throw · s sneak · G grab/choke · L let go · w swap · ? help",
            "target": "Tab/arrows: choose target · Enter: attack options · Esc: back",
            "attack": "↑↓ location · ←→ feint · Space aim · < > attack · Enter go · Esc back",
            "powers": "letter: use on your target · Esc: back",
            "look": "move the cursor · [ ] change level · Esc: done",
            "aim": "move the cursor · Tab: jump to a target · Enter: throw · Esc: cancel",
            "dir": "pick a direction · Esc: cancel",
            "throwmenu": "letter: choose · Esc: cancel",
        }
        con.print(0, con.height - 1, hints.get(self.mode, "")[:con.width], fg=DARK)

    # overlays
    def _render_attack(self, con) -> None:
        m, p = self.menu, self.player
        t = m.target
        x, y, w, h = 3, 3, 62, 29
        box(con, x, y, w, h, f"Attack {t.name}")
        side = combat.arc(t, p.pos)
        cover = combat.cover(self.sim, p.pos, t)[0]
        info = f"{self.sim.distance(p, t)} tiles · {side}" + (f" · cover {cover}" if cover else "")
        con.print(x + 2, y + 1, info, fg=GREY)
        attack, item = m.attacks[m.attack_i]
        weapon = item.name if item is not None else "bare hands"
        ammo = f" ({item.ammo}/{item.data['magazine']})" if item is not None and item.ammo is not None else ""
        con.print(x + 2, y + 3, f"< {attack['name']} with {weapon}{ammo} >", fg=CYAN)
        aim = ("yes" if m.aim else "no") if m.can_aim() else "n/a"
        con.print(x + 2, y + 4, f"Feint: {m.dec}   Aim first: {aim}", fg=WHITE)
        con.print(x + 2, y + 6, f"{'location':<16}{'to hit':>7}{'blocked':>9}{'lands':>7}{'injury':>8}", fg=TITLE)
        locs = m.locations()
        start = max(0, min(m.row - 9, len(locs) - 18))
        for i, loc in enumerate(locs[start:start + 18]):
            row = start + i
            plan = m.plan(loc)
            name = "anywhere" if loc is None else t.body.part(loc).name
            bg = SELECT_BG if row == m.row else None
            if plan is None:
                text = f"{name:<16}{'too hard':>31}"
                fg = DARK
            else:
                text = (f"{name:<16}{plan.p_hit:>7.0%}{plan.p_defended:>9.0%}{plan.p_land:>7.0%}"
                        f"{plan.injury:>8.1f}")
                fg = WHITE
                if m.best and plan.attack is m.best.attack and loc == m.best.location \
                        and plan.deceptive == m.best.deceptive and plan.aim_first == m.best.aim_first:
                    text += "  best"
            con.print(x + 2, y + 7 + i, text.ljust(w - 4), fg=fg, bg=bg)
        plan = m.plan()
        if plan is not None:
            con.print(x + 2, y + h - 2, f"skill {plan.skill} · takes {plan.time_ms / 1000:.1f}s of your time"
                      + (" (aim, then fire)" if plan.aim_first else ""), fg=GREY)

    def _render_throwmenu(self, con) -> None:
        options = actions.throwables(self.player)
        box(con, 6, 6, 50, 4 + len(options), "Throw what?")
        for i, it in enumerate(options):
            con.print(8, 8 + i, f"{chr(ord('a') + i)}  {it.name}", fg=WHITE)

    def _render_aim(self, con) -> None:
        p, item = self.player, self._throwing
        dist = self.sim.distance_pos(p.pos, self.cursor)
        rng = physics.throw_range(p, item.data.get("weight", 1))
        skill = p.skill("throwing") - p.action_penalty("ranged") + combat.range_penalty(dist)
        from ..dice import p_success
        text = (f" {item.name}: {dist} tiles (max {rng}) · {p_success(skill):.0%} to land on target "
                if dist <= rng else f" {item.name}: {dist} tiles, too far (max {rng}) ")
        con.print(MAP_X + 1, MAP_Y + MAP_H - 1, text, fg=WHITE if dist <= rng else RED, bg=PANEL_BG)

    def _render_target(self, con) -> None:
        t = self.target
        if t is not None:
            con.print(MAP_X + 1, MAP_Y + MAP_H - 1, f" target: {t.name} - {self._condition(t)} ",
                      fg=WHITE, bg=PANEL_BG)

    def _render_powers(self, con) -> None:
        powers = self.player.powers
        box(con, 6, 6, 56, 4 + 3 * len(powers), "Powers")
        for i, pw in enumerate(powers):
            why = actions.power_blocked(self.sim, self.player, pw)
            cost = pw.get("cost", {}).get("stamina", 0)
            con.print(8, 8 + 3 * i, f"{chr(ord('a') + i)}  {pw['name']} (stamina {cost})"
                      + (f" - {why}" if why else ""), fg=DARK if why else WHITE)
            print_wrapped(con, 11, 9 + 3 * i, 49, pw.get("description", ""), fg=GREY, max_lines=2)

    def _render_help(self, con) -> None:
        box(con, 4, 3, 60, len(HELP) + 6, "Keys")
        for i, (k, what) in enumerate(HELP):
            con.print(6, 5 + i, k, fg=YELLOW)
            con.print(22, 5 + i, what[:40], fg=WHITE)
        con.print(6, 6 + len(HELP), "any key to close", fg=DARK)

    def _render_confirm(self, con) -> None:
        box(con, 18, 14, 34, 5, "Quit")
        con.print(20, 16, "Leave this fight? (y/n)", fg=WHITE)

    def _render_over(self, con) -> None:
        winner = self.sim.winner()
        p = self.player
        if self.status == "timeout":
            headline = "Time's up: nobody could finish it."
        elif winner == p.team:
            headline = "Your side wins." if p.active else "Your side wins, but you're down."
        else:
            headline = "You lose." if not p.dead else f"You are dead ({p.death_cause})."
        box(con, 10, 10, 48, 11, "Fight over")
        con.print(12, 12, headline, fg=YELLOW)
        con.print(12, 13, f"after {self.sim.time / 1000:.1f}s. You: {self._condition(p)}"[:44], fg=WHITE)
        con.print(12, 15, "a  see what happens next (120s aftermath)", fg=WHITE)
        con.print(12, 16, "r  rematch", fg=WHITE)
        con.print(12, 17, "q  back to the menu", fg=WHITE)

    def _render_summary(self, con) -> None:
        box(con, 2, 2, 64, 40, f"{AFTERMATH_S}s later")
        y = 4
        for line in self.summary:
            y += print_wrapped(con, 4, y, 60, line, fg=WHITE, max_lines=3)
            if y > 38:
                break
        con.print(4, 40, "r  rematch · any other key: menu", fg=DARK)
