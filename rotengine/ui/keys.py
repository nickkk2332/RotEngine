"""Turn tcod events into simple key names the screens understand.

Printable characters arrive as TextInput (so '<', '?', 'F' work on any
keyboard layout); movement and control keys arrive as KeyDown. Directions
work with arrows, the numpad, vi-keys (hjklyubn, game screen only) and
Home/End/PgUp/PgDn for diagonals.
"""
from __future__ import annotations

import tcod.event as ev

DIRECTIONS = {
    "up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0),
    "upleft": (-1, -1), "upright": (1, -1), "downleft": (-1, 1), "downright": (1, 1),
}
VI_KEYS = {"h": "left", "j": "down", "k": "up", "l": "right",
           "y": "upleft", "u": "upright", "b": "downleft", "n": "downright"}

_K = ev.KeySym
_SPECIAL = {
    _K.UP: "up", _K.DOWN: "down", _K.LEFT: "left", _K.RIGHT: "right",
    _K.HOME: "upleft", _K.PAGEUP: "upright", _K.END: "downleft", _K.PAGEDOWN: "downright",
    _K.KP_8: "up", _K.KP_2: "down", _K.KP_4: "left", _K.KP_6: "right",
    _K.KP_7: "upleft", _K.KP_9: "upright", _K.KP_1: "downleft", _K.KP_3: "downright",
    _K.KP_5: "wait", _K.KP_PERIOD: "wait",
    _K.RETURN: "enter", _K.KP_ENTER: "enter", _K.ESCAPE: "esc", _K.TAB: "tab",
    _K.BACKSPACE: "backspace",
}


def translate(event: ev.Event) -> str | None:
    if isinstance(event, ev.KeyDown):
        if event.sym == _K.TAB and event.mod & ev.Modifier.SHIFT:
            return "shift-tab"
        return _SPECIAL.get(event.sym)
    if isinstance(event, ev.TextInput):
        # digits are also sent for numpad presses (handled as KeyDown above)
        return None if event.text.isdigit() else event.text
    return None
