"""Draw the hubmon app icon and write ``assets/hubmon.ico``.

The same design lives in ``assets/hubmon.svg`` for the web panel's favicon; this
script reproduces it with Pillow so the Windows executable carries a real icon
instead of PyInstaller's stock one. Coordinates below are in a 64-unit design
box, exactly as the SVG, scaled up to a supersampled master and then downsampled
into every size Windows asks for.

    python assets/make_icon.py
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
ICO = os.path.join(OUT_DIR, "hubmon.ico")
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

UNIT = 64          # the design box the SVG is drawn in
MASTER = 512       # supersampled master, downsampled into each icon size

TILE_TOP, TILE_BOT = (0x1A, 0x23, 0x31), (0x0A, 0x0E, 0x16)
BAR_BOT, BAR_TOP = (0x38, 0xBD, 0xF8), (0xA7, 0x8B, 0xFA)
# (x, top, height) in design units -- the same five bars as the SVG
BARS = ((9, 33, 10), (19, 26, 17), (29, 36, 7), (39, 29, 14), (49, 31, 12))
BAR_W = 7
BAR_R = 1.8
CAP_H = 1.6
RAIL = (12, 42.2, 40, 1.6)                    # x, y, w, h
PLATE = (7, 18, 50, 28, 5.5)                  # x, y, w, h, radius (fill only)
BORDER = 1.5
TILE_R = 15


def vgrad(w, h, top, bot):
    """A vertical gradient strip as an RGB image."""
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px_row = tuple(round(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        for x in range(w):
            px[x, y] = px_row
    return im


def rounded_mask(size, box, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle(box, radius=radius, fill=255)
    return m


def build():
    """Draw on an RGB base and clip to the squircle at the end.

    Pillow's ImageDraw does not blend semi-transparent ink onto an RGBA canvas --
    it writes the ink (and its alpha) straight in, which turns a 3 % glass plate
    into an opaque white slab. Drawing on an RGB base with mode "RGBA" composites
    properly, so the whole figure is built there and the rounded alpha mask is
    applied last.
    """
    k = MASTER / UNIT
    size = (MASTER, MASTER)

    base = vgrad(MASTER, MASTER, TILE_TOP, TILE_BOT)

    # the soft top sheen: white pasted through a fading alpha mask
    sheen = Image.new("L", size, 0)
    sheen.putdata([round(41 * max(0.0, 1.0 - (y / MASTER) / 0.55))
                   for y in range(MASTER) for _ in range(MASTER)])
    base.paste(Image.new("RGB", size, (255, 255, 255)), (0, 0), sheen)

    d = ImageDraw.Draw(base, "RGBA")

    # the machined rim
    inset = BORDER / 2 * k
    d.rounded_rectangle((inset, inset, MASTER - 1 - inset, MASTER - 1 - inset),
                        radius=(TILE_R * k) - inset, outline=(255, 255, 255, 36),
                        width=max(1, round(BORDER * k)))

    # the glass plate the bars sit on -- fill only, so it stays a tint rather than
    # a second outline that turns to noise at 16 px
    px, py, pw, ph, pr = PLATE
    d.rounded_rectangle((px * k, py * k, (px + pw) * k, (py + ph) * k),
                        radius=pr * k, fill=(255, 255, 255, 10))

    # the rail the bars rest on
    rx, ry, rw, rh = RAIL
    d.rounded_rectangle((rx * k, ry * k, (rx + rw) * k, (ry + rh) * k),
                        radius=rh * k / 2, fill=(255, 255, 255, 56))

    # the bars: one shared gradient pasted through a rounded mask apiece
    bar_grad = vgrad(MASTER, MASTER, BAR_TOP, BAR_BOT)
    for x, top, h in BARS:
        box = (x * k, top * k, (x + BAR_W) * k, (top + h) * k)
        base.paste(bar_grad, (0, 0), rounded_mask(size, box, BAR_R * k))
    for x, top, h in BARS:
        d.rounded_rectangle((x * k, top * k, (x + BAR_W) * k, (top + CAP_H) * k),
                            radius=(CAP_H * k) / 2, fill=(255, 255, 255, 242))

    out = base.convert("RGBA")
    out.putalpha(rounded_mask(size, (0, 0, MASTER - 1, MASTER - 1), TILE_R * k))
    return out


def main():
    master = build()
    master.save(ICO, format="ICO", sizes=SIZES)
    print("wrote", ICO, os.path.getsize(ICO), "bytes; sizes", len(SIZES))

    # a preview so the design can be eyeballed before it ships; over a mid grey so
    # both the dark tile and its rim are visible. It lands in build/ (git-ignored)
    # so assets/ holds only the things that actually ship.
    out = os.path.join(OUT_DIR, "..", "build", "_pool")
    os.makedirs(out, exist_ok=True)
    bg = Image.new("RGBA", (256, 256), (0x6B, 0x72, 0x80, 255))
    preview = Image.alpha_composite(bg, master.resize((256, 256), Image.LANCZOS))
    preview.convert("RGB").save(os.path.join(out, "icon_preview.png"))
    print("wrote preview to build/_pool/icon_preview.png")


if __name__ == "__main__":
    main()
