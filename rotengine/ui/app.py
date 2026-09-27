"""The window and main loop. Everything testable lives in the screens; this
file only wires them to a real tcod window."""
from __future__ import annotations

from pathlib import Path

import tcod.console
import tcod.context
import tcod.event
import tcod.tileset

from .. import arena
from ..content import Content, load_content
from .keys import translate
from .menus import QUIT, MainMenu, Screen, character_menu
from .theme import SCREEN_H, SCREEN_W

FONTS = Path(__file__).parent / "fonts"


class App:
    def __init__(self, mods: list[str] | None = None):
        self.mods = list(mods or [])
        self._content: dict[tuple[str, ...], Content] = {}
        self.screen: Screen = MainMenu(self)
        self.running = True

    def content_for(self, scenario: dict) -> Content:
        key = tuple(sorted(set(self.mods) | set(scenario.get("mods", []))))
        if key not in self._content:
            self._content[key] = load_content(list(key))
        return self._content[key]

    def handle_key(self, key: str) -> None:
        nxt = self.screen.on_key(key)
        if nxt is QUIT:
            self.running = False
        elif nxt is not None:
            self.screen = nxt

    def render(self, con: tcod.console.Console) -> None:
        con.clear()
        self.screen.render(con)

    def start(self, scenario_id: str, play_as: str | None = None, seed: int | None = None) -> None:
        """Jump straight into a scenario (optionally as a named creature)."""
        scenario = arena.load_scenario(scenario_id)
        menu = character_menu(self, scenario, seed)
        self.screen = menu
        if play_as is not None:
            names = [label.split("  [")[0].lower() for label, _ in menu.items]
            match = [i for i, n in enumerate(names) if n == play_as.lower() or play_as.lower() in n]
            if not match:
                raise arena.ScenarioError(f"nobody called '{play_as}' in {scenario_id}; "
                                          f"try one of: {', '.join(names)}")
            self.screen = menu.on_pick(match[0])


FONT_CHOICES = ("mono", "mono-large", "square12", "square16")


def load_font(name: str = "mono") -> tcod.tileset.Tileset:
    """'mono' / 'mono-large': DejaVu Sans Mono in terminal-shaped cells
    (10x20 / 12x24), easy on text. 'square12' / 'square16': classic square
    roguelike tiles, better proportioned maps but spaced-out text."""
    if name in ("mono", "mono-large"):
        w, h = (10, 20) if name == "mono" else (12, 24)
        return tcod.tileset.load_tilesheet(FONTS / f"mono{w}x{h}.png", 16, 16, tcod.tileset.CHARMAP_CP437)
    if name in ("square12", "square16"):
        size = name[len("square"):]
        return tcod.tileset.load_tilesheet(FONTS / f"dejavu{size}x{size}_gs_tc.png", 32, 8,
                                           tcod.tileset.CHARMAP_TCOD)
    raise SystemExit(f"unknown font '{name}'; choose from {', '.join(FONT_CHOICES)}")


def main(scenario: str | None = None, play_as: str | None = None, seed: int | None = None,
         font: str = "mono", mods: list[str] | None = None) -> None:
    app = App(mods)
    if scenario:
        app.start(scenario, play_as, seed)
    console = tcod.console.Console(SCREEN_W, SCREEN_H, order="F")
    with tcod.context.new(columns=SCREEN_W, rows=SCREEN_H, tileset=load_font(font),
                          title="RotEngine", vsync=True,
                          sdl_window_flags=tcod.context.SDL_WINDOW_RESIZABLE) as context:
        while app.running:
            app.render(console)
            context.present(console, keep_aspect=True, integer_scaling=True)
            for event in tcod.event.wait():
                if isinstance(event, tcod.event.Quit):
                    app.running = False
                    break
                key = translate(event)
                if key:
                    app.handle_key(key)
