"""Pre-render DejaVu Sans Mono into CP437 tilesheets for the UI.

Run once (needs Pillow): python tools/make_fonts.py
Produces rotengine/ui/fonts/mono{W}x{H}.png, loaded with tcod's CP437 charmap.
"""
from pathlib import Path

import tcod.tileset
from PIL import Image, ImageDraw, ImageFont

TTF = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
OUT = Path(__file__).resolve().parent.parent / "rotengine" / "ui" / "fonts"
BOX = set(range(0x2500, 0x2580)) | {0x2580, 0x2584, 0x2588, 0x258C, 0x2590, 0x2591, 0x2592, 0x2593}


def make(size: int, w: int, h: int) -> None:
    font = ImageFont.truetype(TTF, size)
    ascent, descent = font.getmetrics()
    sheet = Image.new("L", (16 * w, 16 * h), 0)
    for i, cp in enumerate(tcod.tileset.CHARMAP_CP437):
        ch = chr(cp)
        cell = Image.new("L", (w, h), 0)
        d = ImageDraw.Draw(cell)
        if cp in BOX:
            # stretch box-drawing / block glyphs to the full cell so lines join up
            big_font = ImageFont.truetype(TTF, size * 4 // 3)
            line_h = sum(big_font.getmetrics())
            big = Image.new("L", (w * 4, line_h + 8), 0)
            ImageDraw.Draw(big).text((0, 0), ch, font=big_font, fill=255)
            glyph = big.crop((0, 0, round(big_font.getlength(ch)), line_h)).resize((w, h), Image.LANCZOS)
            cell.paste(glyph)
        else:
            top = (h - (ascent + descent)) // 2
            d.text(((w - font.getlength(ch)) / 2, top), ch, font=font, fill=255)
        sheet.paste(cell, ((i % 16) * w, (i // 16) * h))
    path = OUT / f"mono{w}x{h}.png"
    sheet.save(path)
    print("wrote", path)


if __name__ == "__main__":
    make(17, 10, 20)
    make(20, 12, 24)
