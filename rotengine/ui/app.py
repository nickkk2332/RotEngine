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
from .keys import KeyReader
from .menus import QUIT, MainMenu, Screen, character_menu
from .theme import SCREEN_H, SCREEN_W

FONTS = Path(__file__).parent / "fonts"
FRAME_S = 1 / 60


class App:
    def __init__(self, mods: list[str] | None = None):
        self.mods = list(mods or [])
        self._content: dict[tuple[str, ...], Content] = {}
        from ..roguelike import save_path
        self.save_path = save_path()  # tests point this somewhere harmless
        self.screen: Screen = MainMenu(self)
        self.running = True
        self.animate = False  # the real window plays flights out frame by frame (tests don't)
        self.lying_glyphs = False  # the font has tipped-over letters for people lying down

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

    def close(self) -> None:
        """The window is closing: a run in progress is saved, not lost."""
        run = getattr(self.screen, "run", None)
        if run is not None and run.state == "playing":
            run.save(self.save_path)

    def render(self, con: tcod.console.Console) -> None:
        con.clear()
        self.screen.render(con)

    def start_run(self, character: str, seed: int | None = None, dungeon_id: str = "black_site") -> None:
        """Jump straight into a new roguelike run as that creature."""
        import random

        from .. import roguelike
        from .game import GameScreen
        content = self.content_for({})
        if not content.has("creature", character):
            raise arena.ScenarioError(f"no creature '{character}'")
        run = roguelike.Run(content, dungeon_id, character, random.randrange(1_000_000) if seed is None else seed)
        run.save(self.save_path)
        self.screen = GameScreen(self, {"id": "run", "name": run.dungeon["name"]}, content, run.sim,
                                 run.player, run.seed, run=run)

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


LYING_BASE = 0xE000  # private-use codepoints: the same glyphs, lying on their side


def add_lying_glyphs(ts: tcod.tileset.Tileset) -> None:
    """For every letter (and @), a copy tipped over onto its side, for anyone
    lying down (see GameScreen: prone, out cold, dead). Trimmed to the ink,
    turned 90 degrees, scaled to fit and set on the bottom of the cell."""
    import numpy as np
    w, h = ts.tile_width, ts.tile_height
    for ch in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ@&":
        tile = ts.get_tile(ord(ch))
        ink = np.argwhere(tile[..., 3] > 40)
        if not len(ink):
            continue
        (y0, x0), (y1, x1) = ink.min(0), ink.max(0) + 1
        glyph = np.rot90(tile[y0:y1, x0:x1], k=-1)  # clockwise: the head goes to the right
        gh, gw = glyph.shape[:2]
        scale = min(1.0, w / gw, (h * 0.6) / gh)
        nw, nh = max(1, round(gw * scale)), max(1, round(gh * scale))
        ys = (np.arange(nh) / scale).astype(int).clip(0, gh - 1)
        xs = (np.arange(nw) / scale).astype(int).clip(0, gw - 1)
        small = glyph[ys][:, xs]
        out = np.zeros_like(tile)
        top, left = h - nh - max(1, h // 10), (w - nw) // 2
        out[top:top + nh, left:left + nw] = small
        out[..., :3] = 255
        ts.set_tile(LYING_BASE + ord(ch), out)


def load_font(name: str = "mono") -> tcod.tileset.Tileset:
    """'mono' / 'mono-large': DejaVu Sans Mono in terminal-shaped cells
    (10x20 / 12x24), easy on text. 'square12' / 'square16': classic square
    roguelike tiles, better proportioned maps but spaced-out text."""
    if name in ("mono", "mono-large"):
        w, h = (10, 20) if name == "mono" else (12, 24)
        ts = tcod.tileset.load_tilesheet(FONTS / f"mono{w}x{h}.png", 16, 16, tcod.tileset.CHARMAP_CP437)
        add_lying_glyphs(ts)
        return ts
    if name in ("square12", "square16"):
        size = name[len("square"):]
        ts = tcod.tileset.load_tilesheet(FONTS / f"dejavu{size}x{size}_gs_tc.png", 32, 8,
                                         tcod.tileset.CHARMAP_TCOD)
        add_lying_glyphs(ts)
        return ts
    raise SystemExit(f"unknown font '{name}'; choose from {', '.join(FONT_CHOICES)}")


def main(scenario: str | None = None, play_as: str | None = None, seed: int | None = None,
         font: str = "mono", mods: list[str] | None = None, run: str | None = None) -> None:
    app = App(mods)
    if scenario:
        app.start(scenario, play_as, seed)
    elif run == "continue":
        from .menus import continue_run
        app.screen = continue_run(app.screen) or app.screen
    elif run:
        app.start_run(run, seed)
    console = tcod.console.Console(SCREEN_W, SCREEN_H, order="F")
    with tcod.context.new(columns=SCREEN_W, rows=SCREEN_H, tileset=load_font(font),
                          title="RotEngine", vsync=True,
                          sdl_window_flags=tcod.context.SDL_WINDOW_RESIZABLE) as context:
        # SDL3 (tcod 19+) sends no TextInput, i.e. no letter keys, until asked
        window = context.sdl_window
        if window is not None and hasattr(window, "start_text_input"):
            window.start_text_input(autocorrect=False)
        keys = KeyReader()
        app.animate = True
        app.lying_glyphs = True
        while app.running:
            app.render(console)
            context.present(console, keep_aspect=True, integer_scaling=True)
            animating = getattr(app.screen, "animating", False)
            busy = animating or (hasattr(app.screen, "needs_frames") and app.screen.needs_frames())
            for event in tcod.event.wait(timeout=FRAME_S if busy else None):
                if isinstance(event, tcod.event.Quit):
                    app.running = False
                    break
                key = keys.read(event)
                animating = getattr(app.screen, "animating", False)  # (a key may have just started one)
                if key and not animating:  # (keys pressed mid-flight are dropped)
                    app.handle_key(key)
            if animating:
                app.screen.tick()
    app.close()
