"""Colours and layout constants."""
from __future__ import annotations

SCREEN_W, SCREEN_H = 100, 45
MAP_X, MAP_Y, MAP_W, MAP_H = 0, 1, 68, 33
SIDE_X, SIDE_W = 69, 31
LOG_Y, LOG_H = 35, 9

WHITE = (230, 230, 230)
GREY = (150, 150, 150)
DARK = (70, 70, 70)
DIM = (45, 45, 55)
BLACK = (0, 0, 0)
YELLOW = (250, 220, 90)
RED = (230, 80, 70)
DARK_RED = (140, 40, 40)
GREEN = (110, 210, 110)
CYAN = (110, 200, 220)
BLUE = (100, 140, 230)
ORANGE = (240, 150, 60)
TITLE = (240, 200, 120)
PANEL_BG = (18, 18, 24)
TARGET_BG = (90, 30, 30)
CURSOR_BG = (50, 60, 130)
SELECT_BG = (45, 45, 80)
SUSPICIOUS_BG = (110, 85, 10)
SPOTTED_BG = (130, 20, 20)
CONE_FRONT_BG = (55, 45, 12)
CONE_SIDE_BG = (30, 26, 10)

MATERIAL_FG = {
    "concrete": (150, 150, 150), "brick": (180, 95, 65), "drywall": (205, 200, 180),
    "wood": (160, 110, 60), "steel": (130, 150, 175), "grate": (115, 115, 135),
    "glass": (130, 210, 240), "dirt": (135, 105, 65), "stairs": (225, 205, 90),
}


def material_fg(mat_id: str) -> tuple[int, int, int]:
    return MATERIAL_FG.get(mat_id, GREY)


def dim(rgb: tuple[int, int, int], f: float = 0.45) -> tuple[int, int, int]:
    return (int(rgb[0] * f), int(rgb[1] * f), int(rgb[2] * f))
