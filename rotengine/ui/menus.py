"""Menus: main menu, scenario / character pickers, custom fight setup."""
from __future__ import annotations

import random
from typing import TYPE_CHECKING, Callable

import tcod.console

from .. import arena
from ..content import DATA_DIR
from .theme import CYAN, DARK, GREY, SELECT_BG, TITLE, WHITE, YELLOW
from .widgets import box, centered, print_wrapped

if TYPE_CHECKING:
    from .app import App

QUIT = object()

LOGO = [
    r" ____       _   _____             _            ",
    r"|  _ \ ___ | |_| ____|_ __   __ _(_)_ __   ___ ",
    r"| |_) / _ \| __|  _| | '_ \ / _` | | '_ \ / _ \ ",
    r"|  _ < (_) | |_| |___| | | | (_| | | | | |  __/",
    r"|_| \_\___/ \__|_____|_| |_|\__, |_|_| |_|\___|",
    r"                            |___/              ",
]


class Screen:
    def __init__(self, app: "App"):
        self.app = app

    def render(self, con: tcod.console.Console) -> None:
        raise NotImplementedError

    def on_key(self, key: str):
        """Return a new Screen to switch to, QUIT, or None to stay."""
        return None


class ListMenu(Screen):
    """A titled list; pick with arrows + Enter or the item's letter."""

    def __init__(self, app: "App", title: str, items: list[tuple[str, str]],
                 on_pick: Callable[[int], object], back: Callable[[], object] | None = None,
                 footer: str = ""):
        super().__init__(app)
        self.title, self.items, self.on_pick, self.back, self.footer = title, items, on_pick, back, footer
        self.index = 0

    def render(self, con):
        box(con, 4, 2, con.width - 8, con.height - 4, self.title)
        for i, (label, _) in enumerate(self.items[:24]):
            y = 4 + i
            bg = SELECT_BG if i == self.index else None
            con.print(7, y, f"{chr(ord('a') + i)}", fg=YELLOW, bg=bg)
            con.print(9, y, f" {label}".ljust(con.width - 20), fg=WHITE, bg=bg)
        if self.items:
            desc = self.items[self.index][1]
            print_wrapped(con, 8, 30, con.width - 16, desc, fg=CYAN, max_lines=6)
        con.print(8, con.height - 4, self.footer or "↑↓ choose · Enter or letter to pick · Esc back",
                  fg=DARK)

    def on_key(self, key):
        if key == "up":
            self.index = (self.index - 1) % max(1, len(self.items))
        elif key == "down":
            self.index = (self.index + 1) % max(1, len(self.items))
        elif key == "enter" and self.items:
            return self.on_pick(self.index)
        elif key == "esc":
            return self.back() if self.back else None
        elif len(key) == 1 and "a" <= key <= "z":
            i = ord(key) - ord("a")
            if i < len(self.items):
                self.index = i
                return self.on_pick(i)
        return None


class MainMenu(Screen):
    def __init__(self, app: "App"):
        super().__init__(app)
        self.error = ""
        self.confirm_new = False

    def options(self) -> list[tuple[str, str]]:
        from .. import roguelike
        opts = [("a", "Play a scenario"), ("b", "Custom fight"), ("c", "New run: the Black Site")]
        if roguelike.has_save(self.app.save_path):
            opts.append(("d", "Continue your run"))
        return opts + [("q", "Quit")]

    def render(self, con):
        for i, line in enumerate(LOGO):
            centered(con, 6 + i, line, fg=TITLE)
        centered(con, 13, "a simulation-first roguelike combat sandbox", fg=GREY)
        for i, (k, label) in enumerate(self.options()):
            con.print(38, 17 + 2 * i, k, fg=YELLOW)
            con.print(41, 17 + 2 * i, label, fg=WHITE)
        if self.error:
            centered(con, 29, self.error[:con.width - 4], fg=YELLOW)
        centered(con, 31, "In a fight, press ? for the keys.", fg=DARK)

    def on_key(self, key):
        from .. import roguelike
        if self.confirm_new:
            self.confirm_new, self.error = False, ""
            return run_menu(self.app) if key == "y" else None
        if key == "a":
            return scenario_menu(self.app)
        if key == "b":
            return CustomFight(self.app)
        if key == "c":
            if roguelike.has_save(self.app.save_path):  # permadeath means one run at a time
                self.confirm_new = True
                self.error = "A new run ends the one you have saved. Start over? (y/n)"
                return None
            return run_menu(self.app)
        if key == "d" and any(k == "d" for k, _ in self.options()):
            return continue_run(self)
        if key in ("q", "esc"):
            return QUIT
        return None


def run_menu(app: "App", dungeon_id: str = "black_site", seed: int | None = None) -> Screen:
    """Pick who goes down."""
    content = app.content_for({})
    dungeon = content.get("dungeon", dungeon_id)
    ids = dungeon.get("characters", ["operative"])
    items = [(content.get("creature", i)["name"], describe_template(content.get("creature", i), content))
             for i in ids]

    def pick(i: int):
        from .. import roguelike
        from .game import GameScreen
        run = roguelike.Run(content, dungeon_id, ids[i], random.randrange(1_000_000) if seed is None else seed)
        run.save(app.save_path)
        return GameScreen(app, {"id": "run", "name": dungeon["name"]}, content, run.sim, run.player, run.seed,
                          run=run)

    return ListMenu(app, f"{dungeon['name']}: who goes down?", items, pick, back=lambda: MainMenu(app),
                    footer=dungeon.get("description", "")[:90])


def continue_run(menu: "MainMenu") -> Screen | None:
    from .. import roguelike
    from .game import GameScreen
    app = menu.app
    try:
        run = roguelike.load(app.save_path)
    except roguelike.SaveError as e:
        menu.error = str(e)
        return None
    return GameScreen(app, {"id": "run", "name": run.dungeon["name"]}, run.content, run.sim, run.player,
                      run.seed, run=run)


def scenario_menu(app: "App") -> Screen:
    paths = sorted((DATA_DIR / "scenarios").glob("*.json"))
    scenarios = [arena.load_scenario(p) for p in paths]

    def pick(i: int):
        return character_menu(app, scenarios[i])

    return ListMenu(app, "Choose a scenario",
                    [(s.get("name", s["id"]), s.get("description", "")) for s in scenarios],
                    pick, back=lambda: MainMenu(app))


def describe_template(t: dict, content) -> str:
    bits = []
    eq = t.get("equipment", {})
    if eq.get("wield"):
        bits.append(content.get("item", eq["wield"])["name"])
    if eq.get("wear"):
        bits.append(", ".join(content.get("item", w)["name"] for w in eq["wear"]))
    st = t["stats"]
    bits.append(" ".join(f"{k} {st.get(k, 10)}" for k in ("ST", "CON", "DEX", "INT", "WIS")))
    if t.get("traits"):
        bits.append("traits: " + ", ".join(content.get("trait", x).get("name", x) for x in t["traits"]))
    if t.get("powers"):
        bits.append("powers: " + ", ".join(content.get("power", x).get("name", x) for x in t["powers"]))
    return " · ".join(bits)


def character_menu(app: "App", scenario: dict, seed: int | None = None) -> Screen:
    content = app.content_for(scenario)
    seed = random.randrange(1_000_000) if seed is None else seed
    sim = arena.build(scenario, content, seed)
    items = [(f"{c.name}  [{c.team}]", describe_template(c.template, content)) for c in sim.creatures]

    def pick(i: int):
        from .game import GameScreen
        return GameScreen(app, scenario, content, sim, sim.creatures[i], seed)

    return ListMenu(app, f"{scenario.get('name', scenario['id'])}: who do you play?", items, pick,
                    back=lambda: scenario_menu(app))


class CustomFight(Screen):
    """Pick a map, your character, the opposition and how many."""

    FIELDS = ("map", "you", "enemy", "count", "allies")

    def __init__(self, app: "App"):
        super().__init__(app)
        paths = sorted((DATA_DIR / "scenarios").glob("*.json"))
        self.maps = [arena.load_scenario(p) for p in paths]
        self.content = app.content_for({})
        self.creatures = sorted(self.content.ids("creature"))
        self.values = {"map": 0, "you": self.creatures.index("wick") if "wick" in self.creatures else 0,
                       "enemy": self.creatures.index("thug") if "thug" in self.creatures else 0,
                       "count": 6, "allies": 0}
        self.field = 0
        self.error = ""

    def _label(self, f: str) -> str:
        v = self.values[f]
        if f == "map":
            return self.maps[v].get("name", self.maps[v]["id"])
        if f in ("you", "enemy"):
            return self.content.get("creature", self.creatures[v])["name"]
        return str(v)

    def render(self, con):
        box(con, 4, 2, con.width - 8, con.height - 4, "Custom fight")
        names = {"map": "Map", "you": "You play", "enemy": "Against", "count": "How many", "allies": "Allies (same as you)"}
        for i, f in enumerate(self.FIELDS):
            bg = SELECT_BG if i == self.field else None
            con.print(8, 5 + 2 * i, names[f].ljust(22), fg=GREY, bg=bg)
            con.print(31, 5 + 2 * i, f"◄ {self._label(f)} ►".ljust(50), fg=WHITE, bg=bg)
        f = self.FIELDS[self.field]
        if f in ("you", "enemy"):
            t = self.content.get("creature", self.creatures[self.values[f]])
            print_wrapped(con, 8, 17, con.width - 16, describe_template(t, self.content), fg=CYAN)
        elif f == "map":
            print_wrapped(con, 8, 17, con.width - 16, self.maps[self.values[f]].get("description", ""), fg=CYAN)
        if self.error:
            print_wrapped(con, 8, 24, con.width - 16, self.error, fg=YELLOW)
        con.print(8, con.height - 4, "↑↓ field · ←→ change · Enter fight · Esc back", fg=DARK)

    def on_key(self, key):
        f = self.FIELDS[self.field]
        sizes = {"map": len(self.maps), "you": len(self.creatures), "enemy": len(self.creatures)}
        if key == "up":
            self.field = (self.field - 1) % len(self.FIELDS)
        elif key == "down":
            self.field = (self.field + 1) % len(self.FIELDS)
        elif key in ("left", "right"):
            d = 1 if key == "right" else -1
            if f in sizes:
                self.values[f] = (self.values[f] + d) % sizes[f]
            elif f == "count":
                self.values[f] = max(1, min(30, self.values[f] + d))
            else:
                self.values[f] = max(0, min(10, self.values[f] + d))
        elif key == "esc":
            return MainMenu(self.app)
        elif key == "enter":
            return self._start()
        return None

    def build_scenario(self) -> dict:
        base = self.maps[self.values["map"]]
        h = max(len(level) for level in base["levels"])
        w = max(len(row) for level in base["levels"] for row in level)
        you, enemy = self.creatures[self.values["you"]], self.creatures[self.values["enemy"]]
        left, right = [1, 1, max(1, w // 3), h - 2, 0], [2 * w // 3, 1, w - 2, h - 2, 0]
        team = [{"creature": you, "area": left}]
        if self.values["allies"]:
            team.append({"creature": you, "count": self.values["allies"], "area": left, "name": "ally"})
        return {"id": "custom", "name": "Custom fight", "levels": base["levels"],
                "legend": base.get("legend", {}), "mods": base.get("mods", []),
                "teams": {"you": team,
                          "enemy": [{"creature": enemy, "count": self.values["count"], "area": right}]}}

    def _start(self):
        from .game import GameScreen
        scenario = self.build_scenario()
        content = self.app.content_for(scenario)
        seed = random.randrange(1_000_000)
        try:
            sim = arena.build(scenario, content, seed)
        except arena.ScenarioError as e:
            self.error = f"Can't set that up: {e}"
            return None
        return GameScreen(self.app, scenario, content, sim, sim.creatures[0], seed)
