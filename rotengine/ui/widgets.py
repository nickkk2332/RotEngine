"""Small drawing helpers on top of tcod consoles."""
from __future__ import annotations

import textwrap

import tcod.console

from .theme import DARK, GREY, PANEL_BG, TITLE, WHITE


def wrap(text: str, width: int, indent: str = "") -> list[str]:
    return textwrap.wrap(text, width=width, subsequent_indent=indent) or [""]


def print_wrapped(con: tcod.console.Console, x: int, y: int, width: int, text: str,
                  fg=WHITE, max_lines: int | None = None) -> int:
    lines = wrap(text, width)
    if max_lines is not None:
        lines = lines[:max_lines]
    for i, line in enumerate(lines):
        con.print(x, y + i, line, fg=fg)
    return len(lines)


def box(con: tcod.console.Console, x: int, y: int, w: int, h: int, title: str = "") -> None:
    con.draw_frame(x, y, w, h, clear=True, fg=GREY, bg=PANEL_BG)
    if title:
        con.print(x + 2, y, f" {title} ", fg=TITLE, bg=PANEL_BG)


def bar(con: tcod.console.Console, x: int, y: int, w: int, frac: float, fg, empty=DARK) -> None:
    frac = max(0.0, min(1.0, frac))
    filled = round(w * frac)
    con.print(x, y, "█" * filled, fg=fg)
    con.print(x + filled, y, "░" * (w - filled), fg=empty)


def centered(con: tcod.console.Console, y: int, text: str, fg=WHITE) -> None:
    con.print((con.width - len(text)) // 2, y, text, fg=fg)
