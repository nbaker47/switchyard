#!/usr/bin/env python3
"""Regenerate docs/images/dash-*.png from the real ANSI output of `cr dash --demo --once`.

    python3 scripts/render_dash_images.py

Needs Pillow (pip install --user pillow) and macOS fonts (Menlo, Arial Unicode for arrows).
"""
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
BG, FG = (22, 24, 29), (212, 216, 224)
COLORS = {31: (240, 98, 98), 32: (110, 205, 120), 33: (232, 190, 90), 34: (100, 150, 240),
          35: (200, 130, 235), 36: (90, 205, 215), 37: FG}
SCALE, PAD, SIZE = 2, 14, 15  # 2x for crisp output
FONTS = ["/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
         "/System/Library/Fonts/Apple Symbols.ttf"]
SGR = re.compile(r"\x1b\[([0-9;]*)m")
OTHER = re.compile(r"\x1b\[[0-9;]*[A-Za-ln-z]")


def mix(c, d, t):
    return tuple(round(a * (1 - t) + b * t) for a, b in zip(c, d))


def parse(text):
    """ANSI text -> rows of (char, rgb, bold)."""
    rows, fg, bold, dim = [], FG, False, False
    for line in OTHER.sub("", text).split("\n"):
        row, i = [], 0
        while i < len(line):
            m = SGR.match(line, i)
            if m:
                for code in [int(x or 0) for x in m.group(1).split(";")]:
                    if code == 0:
                        fg, bold, dim = FG, False, False
                    elif code == 1:
                        bold = True
                    elif code == 2:
                        dim = True
                    elif code in COLORS:
                        fg = COLORS[code]
                    elif code == 39:
                        fg = FG
                i = m.end()
                continue
            row.append((line[i], mix(fg, BG, 0.5) if dim else fg, bold))
            i += 1
        rows.append(row)
    return rows


def render(text, path):
    s = SCALE
    fonts = [ImageFont.truetype(FONTS[0], SIZE * s, index=0), ImageFont.truetype(FONTS[0], SIZE * s, index=1)]
    fallbacks = [ImageFont.truetype(f, SIZE * s) for f in FONTS[1:]]
    cw = round(fonts[0].getlength("M"))
    asc, desc = fonts[0].getmetrics()
    ch = asc + desc + 2 * s
    rows = parse(text.rstrip("\n"))
    cols = max(len(r) for r in rows)
    w, h = cols * cw + 2 * PAD * s, len(rows) * ch + 2 * PAD * s
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    for y, row in enumerate(rows):
        top = PAD * s + y * ch
        for x, (c, col, bold) in enumerate(row):
            left = PAD * s + x * cw
            if c == " ":
                continue
            if c in "█░":  # drawn as rectangles so bars are gap-free
                d.rectangle([left, top + 3 * s, left + cw - 1, top + ch - 3 * s - 1],
                            fill=col if c == "█" else mix(BG, col, 0.5))
                continue
            if c == "─":
                d.line([left, top + ch // 2, left + cw, top + ch // 2], fill=col, width=s)
                continue
            if c == "│":
                d.line([left + cw // 2, top, left + cw // 2, top + ch], fill=col, width=s)
                continue
            font = fonts[bold]
            if font.getmask(c).getbbox() is None or c in "⇄●✓":  # Menlo lacks or mis-sizes these
                font = next((f for f in fallbacks if f.getmask(c).getbbox()), font)
            d.text((left + cw // 2, top + ch // 2), c, font=font, fill=col, anchor="mm")
    # rounded corners
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=10 * s, fill=255)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(img, mask=mask)
    out.save(path)
    print(f"wrote {path.relative_to(ROOT)} ({cols}x{len(rows)} cells, {w}x{h}px)")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, cols, rows in (("dash-sidebar", 50, 28), ("dash-wide", 200, 22)):
        ansi = subprocess.run([sys.executable, str(ROOT / "lib" / "dash.py"), "--demo", "--once",
                               "--width", str(cols), "--height", str(rows)],
                              capture_output=True, text=True, check=True).stdout
        render(ansi, OUT / f"{name}.png")


if __name__ == "__main__":
    main()
