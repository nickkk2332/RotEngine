"""Turn tcod events into simple key names the screens understand.

Printable characters arrive as TextInput (so '<', '?', 'F' work on any
keyboard layout); movement and control keys arrive as KeyDown. Directions
work with arrows, the numpad, vi-keys (hjklyubn, game screen only) and
Home/End/PgUp/PgDn for diagonals.

SDL3 (tcod 19+) only sends TextInput after the window asks for it (see
app.main). In case a platform still doesn't, KeyReader falls back to reading
letters and the usual symbols off KeyDown (US layout) until the first real
TextInput shows up, without letting that first key count twice.
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


# shifted symbols on a US keyboard, for the KeyDown fallback
_SHIFTED = {"/": "?", ",": "<", ".": ">", "2": "@", "1": "!", "3": "#", "-": "_", "=": "+",
            ";": ":", "'": '"', "[": "{", "]": "}"}


def _char(event: ev.KeyDown) -> str | None:
    """The character a KeyDown would type (US layout), or None."""
    try:
        ch = chr(int(event.sym))
    except (ValueError, OverflowError):
        return None
    if len(ch) != 1 or not ch.isprintable():
        return None
    shift = bool(event.mod & ev.Modifier.SHIFT)
    if ch.isalpha() and ch.isascii():
        return ch.upper() if shift else ch
    if shift and ch in _SHIFTED:
        return _SHIFTED[ch]
    if ch.isdigit():
        return None  # numbers come from the numpad as directions, or not at all
    return ch if ch in "/,.;'[]-=`" else None


class KeyReader:
    """translate(), plus the KeyDown fallback for systems that send no
    TextInput. Once any TextInput arrives, it's trusted from then on."""

    def __init__(self) -> None:
        self.text_ok = False
        self._pending: str | None = None

    def read(self, event: ev.Event) -> str | None:
        if isinstance(event, ev.TextInput):
            if not self.text_ok:
                self.text_ok = True
                if self._pending == event.text:  # already handled from its KeyDown
                    self._pending = None
                    return None
            return None if event.text.isdigit() else event.text
        if isinstance(event, ev.KeyDown):
            key = translate(event)
            if key is not None or self.text_ok:
                return key
            ch = _char(event)
            self._pending = ch
            return ch
        return None
