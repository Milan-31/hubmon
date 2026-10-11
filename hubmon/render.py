"""Apple-style glassmorphism dashboard renderer for a 320x170 RGB565 panel.

Design notes
------------
* The panel is physically tiny, so legibility beats density: heavy type, large
  tabular numerals and dark card bases instead of thin strokes on light glass.
* Load reads as a *pool*: each card fills with liquid up to its percentage, so
  the level is legible at a glance without reading the number.
* Static chrome (backdrop, dark glass cards) is baked once; each frame redraws
  only what moves.
* Motion is data, never decoration: what moves is the pools, the numerals and
  the load warning ring. Nothing drifts across the backdrop.
* Colour carries meaning: each card's accent shifts with its load, and the
  backdrop is a vertical gradient whose stops the user picks.
* Numbers roll into place like a slot reel when they change (the panel now runs
  at tens of frames per second, so the motion reads properly).
* The chrome comes in two faces, picked from the panel: the default rounded
  glass, and a quartz face -- square-cornered machined cases, screwed to the
  panel, with seven-bar watch numerals. Only the furniture changes; the data
  never does.
"""
from __future__ import annotations

import math
import os
import re
import time
from dataclasses import dataclass

import numpy as np
from PIL import (Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter,
                 ImageFont, ImageOps)

W, H = 320, 170

# ---------------------------------------------------------------- palette ----
BG_TOP = (10, 14, 22)
BG_MID = (13, 18, 28)
BG_BOTTOM = (16, 21, 34)
TEXT = (244, 247, 252)
# RGB565 quantisation plus the tiny panel soften thin, low-contrast type, so the
# secondary greys are kept well above the backdrop instead of fading into it.
TEXT_DIM = (190, 199, 214)
TEXT_FAINT = (150, 161, 178)
# type can end up over the liquid fill, so anything in that zone carries a 1px
# dark halo: it keeps the contrast and adds the visual weight the panel needs
TEXT_SHADOW = (8, 11, 18)

ACCENT_CYAN = (56, 189, 248)
ACCENT_VIOLET = (167, 139, 250)
ACCENT_GREEN = (52, 211, 153)
ACCENT_AMBER = (251, 191, 36)
ACCENT_RED = (248, 113, 113)

FONT_DIR = "C:/Windows/Fonts"

# Load-meter motion: the target is re-read at this cadence (a deliberately low
# update rate) and a critically damped spring carries the tip across, which
# reads as one smooth non-linear glide rather than a per-sample jitter.
BAR_PERIOD = 0.35
BAR_OMEGA = 3.4

# "Pool" load meter: a card fills with liquid up to its load percentage. The
# ripple is stepped at a low rate on purpose -- every moved pixel costs USB
# bandwidth, and a slow slosh reads as water just as convincingly as a fast one.
WATER_AMP = 1.7        # px of travelling swell
WATER_BOB = 0.8        # px the whole level rises and settles on a slow beat
WATER_TILT = 2.2       # px the surface tips by at full slosh
WATER_SLOSH = 0.12     # tilt per px/s the level moves
WATER_SETTLE = 0.45    # how quickly the tilt follows the level
WATER_LAM = 74.0       # px per swell
WATER_HZ = 9.0         # ripple steps per second
WATER_STEP = 0.30      # radians advanced per step
WATER_RISE = 1.1       # bob steps per second
CARD_RADIUS = 16

# The quartz face is a machined case, so its metal tones are fixed greys rather
# than anything the user picked: the backdrop tints the dial, not the frame.
# CASE_BEZEL doubles as the inset the pool is clipped to, so the liquid sits in
# the glass and never washes over the bezel.
METAL_HI = (148, 156, 167)
METAL_LO = (28, 32, 38)
SCREW = (170, 178, 189)
DIAL_BASE = (46, 50, 57)   # gunmetal the dial plate sits on
CASE_BEZEL = 4
PLATE_BEZEL = 2
DIAL_DIM = 0.75        # dial plate brightness relative to the backdrop


def _font(names, size):
    for n in names:
        try:
            return ImageFont.truetype(f"{FONT_DIR}/{n}", size)
        except Exception:
            continue
    return ImageFont.load_default()


def F(size, weight="regular"):
    if weight == "bold":
        return _font(["segoeuib.ttf", "seguisb.ttf", "arialbd.ttf"], size)
    if weight == "semibold":
        return _font(["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"], size)
    return _font(["segoeui.ttf", "arial.ttf"], size)


def hex_rgb(s, fallback):
    try:
        s = s.lstrip("#")
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return fallback


def lerp(a, b, t):
    return a + (b - a) * t


def mix(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(round(lerp(c1[i], c2[i], t))) for i in range(3))


def ramp(stops, v):
    """Piecewise-linear colour over [(value, colour), ...] sorted by value."""
    if v <= stops[0][0]:
        return stops[0][1]
    for (v0, c0), (v1, c1) in zip(stops, stops[1:]):
        if v <= v1:
            return mix(c0, c1, (v - v0) / max(1e-6, v1 - v0))
    return stops[-1][1]


TEMPERATURE_STOPS = [(0, ACCENT_GREEN), (55, ACCENT_CYAN), (72, ACCENT_AMBER),
                     (88, ACCENT_RED)]
POWER_STOPS = [(0, ACCENT_GREEN), (70, ACCENT_CYAN), (130, ACCENT_VIOLET),
               (200, ACCENT_AMBER), (270, ACCENT_RED)]


def fit(text, font, maxw):
    """Truncate with an ellipsis so a string never outgrows its column."""
    if font.getlength(text) <= maxw:
        return text
    while text and font.getlength(text + "…") > maxw:
        text = text[:-1]
    return text + "…"


def rate(bps):
    """Compact byte-rate label: 940B, 340K, 12.4M."""
    bps = max(0.0, float(bps))
    if bps >= 1048576.0:
        return f"{bps / 1048576.0:.1f}M"
    if bps >= 1024.0:
        return f"{bps / 1024.0:.0f}K"
    return f"{bps:.0f}B"


def core_label(cores, threads):
    """Compact core/thread label: 6C·12T, 12T, or a dash when unknown."""
    cores, threads = int(cores or 0), int(threads or 0)
    if cores and threads and threads != cores:
        return f"{cores}C·{threads}T"
    if threads:
        return f"{threads}T"
    return "—"


def ink_mid(font, text, top, bot):
    """Draw origin that centres ``text``'s ink box within the band top..bot.

    The top bar is a plate with a bevelled edge, so what matters is where the
    glyph *ink* lands, not where the em box does -- Segoe UI reserves room above
    the digits that the clock does not use, which is what pushed the readout
    onto the frame.
    """
    b = font.getbbox(text or "0")
    return top + (bot - top - max(1, b[3] - b[1])) / 2.0 - b[1]


# ------------------------------------------------------------ part naming ----
# The panel is 320 px wide, so a full marketing name ("12th Gen Intel(R) Core(TM)
# i5-12400F") cannot be shown; these pull the model out of it instead.
_CPU_PATTERNS = (
    re.compile(r"\bUltra\s+[3579]\s+\d{3}[A-Z]{0,2}\b", re.I),
    re.compile(r"\bRyzen\s+[3579]\s+\d{3,4}[A-Z]{0,3}\b", re.I),
    re.compile(r"\bi[3579][- ]?\d{3,5}[A-Z]{0,3}\b"),
    re.compile(r"\bM[1-4]\s*(?:Pro|Max|Ultra)?\b"),
    re.compile(r"\bXeon\s+[A-Z]?-?\d{4}[A-Z]{0,2}\b", re.I),
)
_GPU_NOISE = ("NVIDIA", "GeForce", "AMD", "Radeon", "Intel(R)", "Intel", "(R)",
              "(TM)", "Corporation", "Graphics", "GPU", "Arc")


def short_cpu(name: str) -> str:
    """Concise CPU model, e.g. '12th Gen ... i5-12400F' -> 'i5-12400F'."""
    if not name:
        return ""
    for rx in _CPU_PATTERNS:
        m = rx.search(name)
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip()
    s = re.sub(r"\((?:R|TM|C)\)", " ", name, flags=re.I)
    s = re.sub(r"\b(CPU|Processor)\b", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" -")


def short_gpu(name: str) -> str:
    """Concise GPU model, e.g. 'NVIDIA GeForce RTX 5060' -> 'RTX 5060'."""
    if not name:
        return ""
    s = name
    for t in _GPU_NOISE:
        s = s.replace(t, " ")
    return re.sub(r"\s+", " ", s).strip(" -")


# The panel shows the chipset rather than the whole board name ("Z690M Snow
# Dream" -> "Z690"), because the chipset is the part that means anything at a
# glance. Longer variants are listed first so the alternation matches 'B650E'
# before it can settle for 'B650'.
_CHIPSETS = (
    # AMD
    "TRX50", "TRX40", "WRX90", "WRX80",
    "X870E", "X870", "X670E", "X670", "B850", "B840", "B650E", "B650",
    "A620", "X570", "B550", "A520", "X470", "B450", "X370", "B350", "A320",
    # Intel
    "Z890", "B860", "H810", "Z790", "Z690", "W680", "H770", "B760", "H670",
    "B660", "H610", "Z590", "H570", "B560", "H510", "Z490", "B460", "H410",
    "Z390", "B365", "B360", "H370", "H310", "Z370", "X299", "Z270", "B250",
    "H270", "Z170", "B150", "H110",
)
_CHIPSET_RX = re.compile("|".join(_CHIPSETS), re.I)
_BOARD_NOISE = ("Motherboard", "Mainboard", "Corporation", "Inc.", "Co., Ltd.",
                "(R)", "(TM)")


def short_board(name: str) -> str:
    """Chipset from a board product string, e.g. 'Z690M Snow Dream' -> 'Z690'.

    Falls back to the cleaned-up board name when no known chipset is in it --
    an unfamiliar board is still more use than a blank corner.
    """
    if not name:
        return ""
    m = _CHIPSET_RX.search(name)
    if m:
        return m.group(0).upper()
    s = name
    for t in _BOARD_NOISE:
        s = s.replace(t, " ")
    return re.sub(r"\s+", " ", s).strip(" -")


@dataclass
class Geo:
    x: int
    y: int
    w: int
    h: int

    @property
    def box(self):
        return (self.x, self.y, self.x + self.w, self.y + self.h)


# ------------------------------------------------------------ value visuals ---
class RollText:
    """Slot-reel numeral: on change every *changed* glyph rolls one slot up
    while the others stay pinned, clipped to a band so nothing bleeds onto the
    meter or labels. A brief accent tint rides along with the roll.
    """

    def __init__(self, font, color, accent, speed=3.2):
        self.font = font
        self.color = color
        self.accent = accent
        self.text = ""
        self.prev = ""
        self.t = 1.0
        self.speed = speed
        self.enabled = True

    def set(self, text):
        text = str(text)
        if text == self.text:
            return
        if len(text) == len(self.text):
            self.prev = self.text
        else:                       # keep digit columns aligned from the right
            n = len(text)
            self.prev = self.text.rjust(n) if len(self.text) < n else self.text[-n:]
        self.text = text
        self.t = 0.0 if self.enabled else 1.0

    def step(self, dt):
        if self.t < 1.0:
            self.t = min(1.0, self.t + dt * self.speed)

    def width(self):
        return self.font.getlength(self.text)

    def _ink(self):
        """(ink_top, ink_bottom) of digits, relative to the draw origin.

        Measured from the font instead of guessed from the em size: for Segoe UI
        the glyph ink sits well below the em top and runs a little past the em
        bottom, so an em-based band would shear the numerals.
        """
        m = getattr(self, "_ink_m", None)
        if m is None:
            b = self.font.getbbox("0123456789%")
            m = (b[1], b[3])
            self._ink_m = m
        return m

    def draw(self, img, x, y):
        d = ImageDraw.Draw(img)
        if self.t >= 1.0 or self.prev == self.text:
            d.text((x, y), self.text, font=self.font, fill=self.color)
            return

        p = self.t * self.t * (3 - 2 * self.t)      # smoothstep
        col = mix(self.color, self.accent, 0.7 * math.sin(math.pi * self.t))
        ink_top, ink_bot = self._ink()
        band = ink_bot - ink_top     # window == one glyph's ink height ...
        step = band                  # ... and the roll travels exactly that far,
        pad = 5                      # so outgoing and incoming glyphs tile seam-free
        tile_h = band + 2 * pad
        top = int(y + ink_top - pad)
        base = pad - ink_top         # image row -> tile row for a glyph at (x, y)

        cx = float(x)
        for i, ch in enumerate(self.text):
            old = self.prev[i] if i < len(self.prev) else ch
            cw = self.font.getlength(ch)
            if old == ch or not ch.strip() or not old.strip():
                d.text((cx, y), ch, font=self.font, fill=col)
                cx += cw
                continue
            tw = int(math.ceil(max(cw, self.font.getlength(old)))) + 6
            tile = Image.new("RGBA", (tw, tile_h), (0, 0, 0, 0))
            td = ImageDraw.Draw(tile)
            td.text((3, base - p * step), old, font=self.font, fill=col)
            td.text((3, base + (1 - p) * step), ch, font=self.font, fill=col)
            m = Image.new("L", (tw, tile_h), 0)
            ImageDraw.Draw(m).rectangle((0, pad, tw, pad + band), fill=255)
            tile.putalpha(ImageChops.multiply(tile.getchannel("A"), m))
            img.paste(tile, (int(cx) - 3, top), tile)
            cx += cw


# ------------------------------------------------------- quartz numerals -----
def _seg_bars(x, y, w, h, t):
    """The seven bars of one digit, in panel pixels.

    ``w``/``h`` are the cell, ``t`` the bar thickness. Horizontal bars span the
    full cell so they mitre into the verticals; a small gap separates bars that
    meet. The end taper is capped at 22% of a bar's own length, and modestly --
    a vertical bar is only about twice its own thickness long, so a 45-degree
    taper would eat it down to a blob and the digit would stop reading.
    """
    p = t / 2.0
    gap = t * 0.12
    ca, cg, cd = y + p, y + h / 2.0, y + h - p
    xl, xr = x + p, x + w - p
    ya0, ya1 = ca + p + gap, cg - p - gap
    yb0, yb1 = cg + p + gap, cd - p - gap

    def hseg(x0, x1, cy):
        b = min(p, (x1 - x0) * 0.22)
        return ((x0 + b, cy - p), (x1 - b, cy - p), (x1, cy),
                (x1 - b, cy + p), (x0 + b, cy + p), (x0, cy))

    def vseg(cx, y0, y1):
        b = min(p, (y1 - y0) * 0.22)
        return ((cx, y0), (cx + p, y0 + b), (cx + p, y1 - b), (cx, y1),
                (cx - p, y1 - b), (cx - p, y0 + b))

    return {"a": hseg(x, x + w, ca), "g": hseg(x, x + w, cg),
            "d": hseg(x, x + w, cd),
            "f": vseg(xl, ya0, ya1), "b": vseg(xr, ya0, ya1),
            "e": vseg(xl, yb0, yb1), "c": vseg(xr, yb0, yb1)}


class SegText:
    """Quartz-watch numerals: seven lit bars per digit.

    Drop-in for RollText (same set/step/width/draw surface) so the dashboard can
    pick a face from config. A change cross-fades the outgoing digit's bars into
    the incoming ones -- the electronic-display reading of the slot roll, and it
    still touches only the glyph that changed.

    The face is rasterised at 4x and reduced: the panel quantises to RGB565, and
    a sub-pixel 45-degree bevel drawn at 1x turns into a staircase.
    """

    SEG = {"0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc",
           "5": "afgcd", "6": "afgedc", "7": "abc", "8": "abcdefg",
           "9": "abcdfg", "-": "g", "_": "d", " ": ""}
    EXTRA = (".", "%")          # drawn, not in the seven bars
    SS = 4
    GHOST = 0.13                # alpha of the bars that are not lit

    def __init__(self, font, color, accent, speed=3.2):
        self.font = font
        self.color = color
        self.accent = accent
        self.text = ""
        self.prev = ""
        self.t = 1.0
        self.speed = speed
        self.enabled = True
        self._mcache = {}           # (text, dim) -> rasterised tile (see _tile)
        self._lcache = {}           # text -> lit+ghost composite (see _lit)

    def set(self, text):
        text = str(text)
        if text == self.text:
            return
        if len(text) == len(self.text):
            self.prev = self.text
        else:                       # keep digit columns aligned from the right
            n = len(text)
            self.prev = self.text.rjust(n) if len(self.text) < n else self.text[-n:]
        self.text = text
        self.t = 0.0 if self.enabled else 1.0

    def step(self, dt):
        if self.t < 1.0:
            self.t = min(1.0, self.t + dt * self.speed)

    def _ink(self):
        """(ink_top, ink_height) of the vector face, so the bars land in exactly
        the band the rolled glyphs would have occupied."""
        m = getattr(self, "_ink_m", None)
        if m is None:
            b = self.font.getbbox("0123456789")
            m = (b[1], max(1, b[3] - b[1]))
            self._ink_m = m
        return m

    def _cell(self):
        """(height, width, thickness, advance gap) of one digit cell, in px.

        The cell is 3px taller than the vector face's ink, so the bars use the
        full height of the band the rolled glyphs would have filled; the advance
        comes out within a pixel of the vector face's, which is what keeps the
        layout from shifting when the style is switched. The bar runs at about
        an eighth of the cell -- thick enough to survive RGB565 quantisation on
        a 320px panel, where anything near two pixels greys out and blurs.
        """
        h = float(self._ink()[1]) + 3.0
        t = h * 0.127
        w = h * 0.55
        return h, w, t, t * 1.15

    def _adv(self, ch):
        _, w, _, gap = self._cell()
        if ch == ".":
            return w * 0.40
        if ch in self.SEG or ch == "%":
            return w + gap
        return self.font.getlength(ch)  # anything else: the vector face keeps it

    def _width(self, text):
        return sum(self._adv(c) for c in text)

    def width(self):
        return self._width(self.text)

    def _mask(self, text, w, h, t, tw, th, pad):
        """Coverage mask for one string, with the tile origin at (pad, pad)."""
        s = self.SS
        m = Image.new("L", (tw * s, th * s), 0)
        md = ImageDraw.Draw(m)
        cx = float(pad)
        for ch in text:
            if ch in self.SEG:
                on = self.SEG[ch]
                bars = _seg_bars(cx * s, pad * s, w * s, h * s, t * s)
                for name, poly in bars.items():
                    if name in on:
                        md.polygon(poly, fill=255)
            elif ch == ".":
                r = t * 0.45
                cy = pad + h - r
                x0 = cx + w * 0.05
                md.ellipse(((x0 - r) * s, (cy - r) * s,
                            (x0 + r) * s, (cy + r) * s), fill=255)
            elif ch == "%":
                # a real segment display has no percent bar, so it is drawn as
                # the printed symbol would be: two dots and a slash
                r, q = t * 0.42, t * 0.30
                c0x, c1x = cx + r * 1.15, cx + w - r * 1.15
                c0y, c1y = pad + r * 1.15, pad + h - r * 1.15
                for dx, dy in ((c0x, c0y), (c1x, c1y)):
                    md.ellipse(((dx - r) * s, (dy - r) * s,
                                (dx + r) * s, (dy + r) * s), fill=255)
                md.polygon(((c0x * s, c1y * s), ((c0x + 2 * q) * s, c1y * s),
                            (c1x * s, c0y * s), ((c1x - 2 * q) * s, c0y * s)),
                           fill=255)
            cx += self._adv(ch)
        return m.resize((tw, th), Image.LANCZOS)

    def _eights(self, text):
        """``text`` with every seven-bar glyph turned into a full '8'."""
        return "".join("8" if ch in self.SEG else ch for ch in text)

    def _tile(self, text, w, h, t, tw, th, pad, dim=1.0):
        """Rasterised coverage for one string, memoised.

        The mask depends only on the string (and the dimming), so the steady
        state -- a value that is not changing -- costs one dict lookup instead
        of a fresh 4x pass, and the CPU render is already half this pipeline's
        frame budget. Different dims are different keys because a ghost tile is
        the same geometry at low alpha, not a second rasterisation.
        """
        key = (text, dim)
        got = self._mcache.get(key)
        if got is None or got[1] != (tw, th):
            m = self._mask(text, w, h, t, tw, th, pad)
            if dim < 1.0:
                m = m.point([int(v * dim) for v in range(256)])
            got = (m, (tw, th))
            if len(self._mcache) > 24:
                self._mcache.clear()
            self._mcache[key] = got
        return got[0]

    def _ghost(self, text, w, h, t, tw, th, pad):
        """The unlit bars of ``text``: every segment an '8' would use, faint.

        A real quartz display always has its dark bars visible, and drawing them
        is what makes the lit ones read as a display rather than as free-floating
        strokes -- it also gives the eye the digit's full frame, so a '1' or a
        '7' is not just a couple of bright lines in the dark.
        """
        return self._tile(self._eights(text), w, h, t, tw, th, pad, self.GHOST)

    def _lit(self, text, w, h, t, tw, th, pad):
        """A settled digit: the lit bars over the ghost of the unlit ones.

        Composite memoised, because that union is what every frame of the steady
        state wants; redoing the ``lighter`` per numeral per frame costs more
        than the whole rest of the numeral draw.
        """
        got = self._lcache.get(text)
        if got is None or got[1] != (tw, th):
            got = (ImageChops.lighter(self._ghost(text, w, h, t, tw, th, pad),
                                      self._tile(text, w, h, t, tw, th, pad)),
                   (tw, th))
            if len(self._lcache) > 24:
                self._lcache.clear()
            self._lcache[text] = got
        return got[0]

    def draw(self, img, x, y):
        h, w, t, gap = self._cell()
        pad = 3
        # the cell tops out 3px above the vector ink and lands on the same
        # baseline, so swapping styles does not move the number
        left, top = int(x) - pad, int(y + self._ink()[0] - 3.0) - pad
        col = self.color

        # anything the bars cannot express stays in the vector face; such glyphs
        # never change, so they are drawn flat and never animated
        d = ImageDraw.Draw(img)
        cx = float(x)
        for ch in self.text:
            if ch not in self.SEG and ch not in self.EXTRA:
                d.text((cx, y), ch, font=self.font, fill=col)
            cx += self._adv(ch)

        tw = int(math.ceil(max(self._width(self.text),
                               self._width(self.prev)))) + pad * 2
        th = int(math.ceil(h)) + pad * 2
        if self.t >= 1.0 or self.prev == self.text:
            mask = self._lit(self.text, w, h, t, tw, th, pad)
        else:
            # roll: fade the outgoing bars into the incoming ones, then lay the
            # ghost back over both. Doing it in that order keeps the ghost out
            # of the cross-fade, so a rolling digit repaints only its bars.
            p = self.t * self.t * (3 - 2 * self.t)      # smoothstep
            col = mix(self.color, self.accent, 0.7 * math.sin(math.pi * self.t))
            lit = Image.blend(self._tile(self.prev, w, h, t, tw, th, pad),
                              self._tile(self.text, w, h, t, tw, th, pad), p)
            mask = ImageChops.lighter(
                ImageChops.lighter(self._ghost(self.prev, w, h, t, tw, th, pad),
                                   self._ghost(self.text, w, h, t, tw, th, pad)),
                lit)

        layer = Image.new("RGBA", (tw, th), col + (255,))
        layer.putalpha(mask)
        img.paste(layer, (left, top), layer)


# ------------------------------------------------------------- dashboard -----
@dataclass
class Vals:
    cpu: float = 0.0
    gpu: float = 0.0
    mem: float = 0.0
    freq_cpu: float = 0.0
    freq_gpu: float = 0.0
    power: float = 0.0
    cpu_power: float = 0.0
    gpu_power: float = 0.0
    temp_gpu: float = 0.0
    cpu_cores: int = 0
    cpu_threads: int = 0
    net_down: float = 0.0
    net_up: float = 0.0
    mem_used: float = 0.0
    mem_total: float = 0.0
    cpu_perf: float = 0.0
    fg_fps: float = 0.0
    cpu_name: str = ""
    gpu_name: str = ""
    fg_app: str = ""
    host: str = ""
    board: str = ""


# key -> (label, accent field)
BOTTOM = (("mem", "MEM", "accent_mem"),
          ("power", "PWR · EST", "accent_pwr"),
          ("fps", "FPS", None))

TOP_BAR = Geo(8, 4, 304, 16)
PAD = 12


class Dashboard:
    def __init__(self, cfg=None):
        from .config import Config
        self.cfg = cfg or Config()
        self.v = Vals()
        self.tgt = Vals()
        self.time = 0.0
        self._text_acc = self.cfg.text_period
        self._fps_acc = self.cfg.fps_period
        # last good numeric strings: a value is never replaced by a placeholder
        # once it exists, so the digits stay put and never flash empty
        self._hold_fc = "--"
        self._hold_fg = "--"
        self._hold_fps = "--"
        # eased load-meter state (position / velocity / slow target)
        self.bar = {"cpu": 0.0, "gpu": 0.0}
        self._bar_v = {"cpu": 0.0, "gpu": 0.0}
        self._bar_tgt = {"cpu": 0.0, "gpu": 0.0}
        self._bar_acc = BAR_PERIOD
        self._static = None
        self._sig = None
        self._masks = {}
        self._cpu_short = ""
        self._gpu_short = ""
        # liquid flow: last level seen per card, and the tilt it left behind
        self._lvl = {}
        self._tilt = {}
        self._tick = -1
        self._cores = "—"
        self._board = ""
        self._bg_key = None
        self._bg_img = None
        self._plain = False     # True while a user backdrop replaces the chrome
        self._num_style = None  # face the numerals were built for

        # RGB565 softens thin strokes, so nothing is drawn in a regular weight
        self._f_lbl = F(11, "bold")
        self._f_host = F(11, "semibold")
        self._f_big = F(30, "bold")
        self._f_mid = F(24, "bold")
        self._f_freq = F(17, "semibold")
        self._f_sub = F(11, "bold")
        self._f_unit = F(10, "semibold")
        self._f_app = F(10, "semibold")
        self._f_clock = F(13, "bold")
        self._f_net = F(11, "semibold")
        self._f_spec = F(11, "semibold")

        self._layout()
        self.apply_config(self.cfg, force=True)

    # ------------------------------------------------------------------ config
    def _look_sig(self):
        c = self.cfg
        return (c.style,
                c.accent_cpu, c.accent_gpu, c.accent_mem, c.accent_pwr,
                round(c.brightness, 3), round(c.bg_brightness, 3),
                round(c.saturation, 3), round(c.glass, 3),
                c.bg_color_top, c.bg_color_mid, c.bg_color_bot,
                c.bg_image, round(c.bg_image_blur, 2),
                round(c.bg_image_brightness, 2), round(c.bg_image_zoom, 3),
                round(c.bg_image_offset_x, 3), round(c.bg_image_offset_y, 3),
                c.show_mem, c.show_power, c.show_fps)

    def apply_config(self, cfg, force=False):
        self.cfg = cfg
        sig = self._look_sig()
        if force or sig != self._sig:
            self._sig = sig
            self._layout()                 # may rebuild the numeral face
            self._static = None            # chrome must be re-baked
        for r, key in self._accent_rolls.items():
            r.accent = hex_rgb(getattr(cfg, key), r.accent)
        for r in self._nums:
            r.speed = cfg.roll_speed
            r.enabled = cfg.roll

    def _build_nums(self):
        """The numeral face: rolled vector glyphs, or quartz bars."""
        cls = SegText if self.cfg.style == "quartz" else RollText
        self.num_cpu = cls(self._f_big, TEXT, ACCENT_CYAN)
        self.num_gpu = cls(self._f_big, TEXT, ACCENT_VIOLET)
        self.num_fc = cls(self._f_freq, TEXT, ACCENT_CYAN)
        self.num_fg = cls(self._f_freq, TEXT, ACCENT_VIOLET)
        self.num_mem = cls(self._f_mid, TEXT, ACCENT_CYAN)
        self.num_pwr = cls(self._f_mid, TEXT, ACCENT_AMBER)
        self.num_fps = cls(self._f_mid, TEXT, ACCENT_GREEN)
        self._nums = (self.num_cpu, self.num_gpu, self.num_fc, self.num_fg,
                      self.num_mem, self.num_pwr, self.num_fps)
        self._accent_rolls = {self.num_cpu: "accent_cpu", self.num_gpu: "accent_gpu",
                              self.num_fc: "accent_cpu", self.num_fg: "accent_gpu",
                              self.num_mem: "accent_mem", self.num_pwr: "accent_pwr"}
        self._num_style = self.cfg.style
        self._refresh_text()           # a fresh face starts blank; fill it now

    def _layout(self):
        self._radius = 0 if self.cfg.style == "quartz" else CARD_RADIUS
        # how far the live area sits inside the card outline. The glass face has
        # no frame, so its pool runs to the edge; the quartz case has a bezel.
        self._inset = CASE_BEZEL if self.cfg.style == "quartz" else 0
        if self._num_style != self.cfg.style:
            self._build_nums()
        keys = [k for k, _, _ in BOTTOM
                if getattr(self.cfg, "show_" + k, True)]
        self.cards = {}
        self.big_h = 78 if keys else 140
        self.cards["cpu"] = Geo(8, 24, 150, self.big_h)
        self.cards["gpu"] = Geo(162, 24, 150, self.big_h)
        self.bottom_order = keys
        if keys:
            n = len(keys)
            cw = (304 - 5 * (n - 1)) // n
            for i, k in enumerate(keys):
                self.cards[k] = Geo(8 + i * (cw + 5), 106, cw, 58)
        self._masks = {}          # geometry changed -> card masks must be rebuilt

    # -- target values come from the sensor layer --------------------------
    def update(self, stats):
        t = self.tgt
        t.cpu = stats.get("cpu_util", 0.0)
        t.gpu = stats.get("gpu_util", 0.0)
        t.mem = stats.get("mem_percent", 0.0)
        t.freq_cpu = stats.get("cpu_freq_mhz", 0.0)
        t.freq_gpu = stats.get("gpu_freq_mhz", 0.0)
        t.power = stats.get("total_power_w", 0.0)
        t.cpu_power = stats.get("cpu_power_w", 0.0)
        t.gpu_power = stats.get("gpu_power_w", 0.0)
        t.temp_gpu = stats.get("gpu_temp_c", 0.0)
        t.cpu_cores = stats.get("cpu_cores", 0)
        t.cpu_threads = stats.get("cpu_threads", 0)
        t.net_down = stats.get("net_down_bps", 0.0)
        t.net_up = stats.get("net_up_bps", 0.0)
        t.mem_used = stats.get("mem_used_gb", 0.0)
        t.mem_total = stats.get("mem_total_gb", 0.0)
        t.cpu_perf = stats.get("cpu_perf", 0.0)
        t.fg_fps = stats.get("fg_fps", 0.0)
        t.cpu_name = stats.get("cpu_name", "")
        t.gpu_name = stats.get("gpu_name", "")
        t.fg_app = stats.get("fg_app", "")
        t.host = stats.get("host", "")
        t.board = stats.get("board_name", "")

    # ---------------------------------------------------------------- static
    def _load_bg_image(self, path):
        """Frame a user image onto the panel and bake in blur + brightness.

        The zoom/offset pair is the panel's crop control: cover-fit first, then
        zoom about the centre and slide the 320x170 window across the result.
        """
        path = (path or "").strip().strip('"')
        if not path or not os.path.exists(path):
            return None
        try:
            src = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
            iw, ih = src.size
            zoom = min(4.0, max(1.0, float(self.cfg.bg_image_zoom)))
            scale = max(W / iw, H / ih) * zoom
            src = src.resize((max(W, round(iw * scale)),
                              max(H, round(ih * scale))), Image.LANCZOS)
            sw, sh = src.size
            ox = min(1.0, max(-1.0, float(self.cfg.bg_image_offset_x)))
            oy = min(1.0, max(-1.0, float(self.cfg.bg_image_offset_y)))
            left = max(0, min(sw - W, round((sw - W) * (1 + ox) / 2)))
            top = max(0, min(sh - H, round((sh - H) * (1 + oy) / 2)))
            out = src.crop((left, top, left + W, top + H))
            blur = max(0.0, float(self.cfg.bg_image_blur))
            if blur:
                out = out.filter(ImageFilter.GaussianBlur(blur))
            return ImageEnhance.Brightness(out).enhance(
                max(0.0, float(self.cfg.bg_image_brightness)))
        except Exception:
            return None

    def _background(self):
        key = (self.cfg.bg_image, round(self.cfg.bg_image_blur, 2),
               round(self.cfg.bg_image_brightness, 2),
               round(self.cfg.bg_image_zoom, 3),
               round(self.cfg.bg_image_offset_x, 3),
               round(self.cfg.bg_image_offset_y, 3))
        if key != self._bg_key:
            self._bg_key = key
            self._bg_img = self._load_bg_image(self.cfg.bg_image)

        yy, xx = np.mgrid[0:H, 0:W]
        r = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)
        if self._bg_img is not None:
            # a user photo replaces the procedural gradient; a light vignette is
            # kept so the cards still read against a bright image
            px = np.asarray(self._bg_img, np.float32)
            px *= np.clip(1.06 - 0.18 * r, 0, 1.2)[:, :, None]
            return Image.fromarray(np.clip(px, 0, 255).astype(np.uint8), "RGB")

        # no photo: a plain vertical ramp through the three user-picked stops.
        # The dither earns its keep -- RGB565 has 32 red / 64 green / 32 blue
        # levels, and an un-dithered dark ramp bands badly on this panel.
        stops = np.array((hex_rgb(self.cfg.bg_color_top, BG_TOP),
                          hex_rgb(self.cfg.bg_color_mid, BG_MID),
                          hex_rgb(self.cfg.bg_color_bot, BG_BOTTOM)), np.float32)
        at = np.linspace(0.0, 2.0, H, dtype=np.float32)
        lo = np.clip(np.floor(at).astype(np.intp), 0, 1)
        frac = (at - lo)[:, None]
        px = stops[lo] * (1.0 - frac) + stops[lo + 1] * frac
        px = np.repeat(px[:, None, :], W, axis=1)
        px *= np.clip(1.03 - 0.10 * r, 0, 1.2)[:, :, None]
        px += np.random.default_rng(7).normal(0.0, 2.5, (H, W, 1)).astype(np.float32)
        return Image.fromarray(np.clip(px, 0, 255).astype(np.uint8), "RGB")

    def _vgrad(self, top_a, bot_a):
        col = np.linspace(top_a, bot_a, H).astype(np.uint8)[:, None]
        return Image.fromarray(np.repeat(col, W, axis=1), "L")

    # the panel rounds nothing off -- a 1-px rounded rectangle drawn natively
    # lands on a hard staircase. Both card shapes are drawn at 4x and reduced,
    # which buys the edge a fraction-of-a-pixel falloff for free.
    MASK_SS = 4

    def _round_mask(self, g: Geo, radius, inset=0):
        s = self.MASK_SS
        m = Image.new("L", (W * s, H * s), 0)
        ImageDraw.Draw(m).rounded_rectangle(
            ((g.x + inset) * s, (g.y + inset) * s,
             (g.x + g.w - inset) * s, (g.y + g.h - inset) * s),
            max(0, radius - inset) * s, fill=255)
        return m.resize((W, H), Image.LANCZOS)

    def _edge_layer(self, g: Geo, radius, inset, top_a, bot_a):
        s = self.MASK_SS
        ring = Image.new("L", (W * s, H * s), 0)
        ImageDraw.Draw(ring).rounded_rectangle(
            ((g.x + inset) * s, (g.y + inset) * s,
             (g.x + g.w - inset) * s, (g.y + g.h - inset) * s),
            max(0, radius - inset) * s, outline=255, width=s)
        ring = ring.resize((W, H), Image.LANCZOS)
        layer = Image.new("RGBA", (W, H), (255, 255, 255, 0))
        layer.putalpha(ImageChops.multiply(ring, self._vgrad(top_a, bot_a)))
        return layer

    def _sheen(self, img, g: Geo, radius, strength):
        xs = (np.arange(W)[None, :] - g.x) / max(1, g.w)
        ys = (np.arange(H)[:, None] - g.y) / max(1, g.h)
        t = np.clip(xs * 0.45 + ys * 1.8, 0, 1)
        layer = np.zeros((H, W, 4), np.uint8)
        layer[..., 0:3] = 255
        layer[..., 3] = np.clip((1 - t) ** 3 * strength, 0, 255).astype(np.uint8)
        img.paste(Image.alpha_composite(img.convert("RGBA"),
                                        Image.fromarray(layer, "RGBA")).convert("RGB"),
                  (0, 0), self._round_mask(g, radius))

    def _build_static(self):
        c = self.cfg
        back = ImageEnhance.Brightness(self._background()).enhance(
            max(0.0, c.bg_brightness))
        # a loaded user backdrop is the frame: the cards then carry no glass, no
        # edge and no base, so the picture reads as one continuous sheet
        self._plain = self._bg_img is not None
        ins = CASE_BEZEL if (c.style == "quartz" and not self._plain) else 0
        if ins != self._inset:
            self._inset = ins
            self._masks = {}
        img = ImageEnhance.Brightness(back).enhance(c.brightness)
        if c.style == "quartz":
            self._plate(img, back, TOP_BAR, PLATE_BEZEL)
            for g in self.cards.values():
                self._case(img, back, g, self._radius)
        else:
            enh = ImageEnhance.Color(back.filter(ImageFilter.GaussianBlur(10))
                                     ).enhance(1.30 * c.saturation)
            blur = ImageEnhance.Brightness(enh).enhance(c.brightness)
            for g in self.cards.values():
                self._card(blur, img, g, self._radius)

        # card captions sit above the liquid fill, so they are drawn per frame
        # (see _labels) instead of being baked into the chrome
        self._static = img

    def _labels(self, d):
        """Card captions, redrawn each frame so the pool never tints them."""
        items = (("cpu", "CPU", "accent_cpu", ACCENT_CYAN),
                 ("gpu", "GPU", "accent_gpu", ACCENT_VIOLET)) + tuple(
                     (k, lbl, acc, TEXT_DIM) for k, lbl, acc in BOTTOM)
        for key, label, accent, fallback in items:
            g = self.cards.get(key)
            if g is None:
                continue
            col = hex_rgb(getattr(self.cfg, accent), fallback) if accent else fallback
            d.text((g.x + PAD, g.y + 5), label, font=self._f_lbl, fill=col,
                   stroke_width=1, stroke_fill=TEXT_SHADOW)

    def _card(self, blur, img, g: Geo, radius):
        if self._plain:               # borderless: the picture is the frame
            return
        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle(
            (g.x + 1, g.y + 4, g.x + g.w + 1, g.y + g.h + 4), radius, fill=(0, 0, 0, 120))
        sh = sh.filter(ImageFilter.GaussianBlur(6))
        img.paste(Image.alpha_composite(img.convert("RGBA"), sh).convert("RGB"), (0, 0))

        mask = self._round_mask(g, radius)
        img.paste(blur, (0, 0), mask)

        glass = max(0.0, self.cfg.glass)
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(ov).rounded_rectangle(g.box, radius,
                                             fill=(255, 255, 255, int(7 * glass)))
        ov.paste((255, 255, 255, 255), (0, 0),
                 ImageChops.multiply(mask, self._vgrad(int(7 * glass), 0)))
        # the card interior is pulled well down so the (light) type and the
        # coloured pools both sit on a dark base instead of fighting the bloom
        ov.paste((0, 0, 0, 255), (0, 0),
                 ImageChops.multiply(mask, self._vgrad(int(84 * glass),
                                                       int(118 * glass))))
        img.paste(Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB"), (0, 0))

        self._sheen(img, g, radius, 9 * glass)
        for inset, top_a, bot_a in ((0, 150, 34), (1, 64, 16)):
            img.paste(Image.alpha_composite(
                img.convert("RGBA"),
                self._edge_layer(g, radius, inset, top_a, bot_a)).convert("RGB"), (0, 0))

    def _dial(self, back):
        """The plate behind the glass: the backdrop smoked down and drained of
        colour, over a gunmetal base. A case holds a dial, not a hole -- and
        because the base is neutral, the dial reads as the same instrument
        whatever gradient the user picked."""
        plate = Image.blend(ImageEnhance.Color(back).enhance(0.34),
                            Image.new("RGB", (W, H), DIAL_BASE), 0.5)
        return ImageEnhance.Brightness(plate).enhance(DIAL_DIM)

    def _case_ramp(self, g: Geo):
        """Vertical chrome ramp across a card, so the bezel catches the light at
        its own top edge and falls into shadow at its own bottom."""
        t = (np.arange(g.h, dtype=np.float32) / max(1.0, g.h - 1.0)) ** 1.5
        hi, lo = np.array(METAL_HI, np.float32), np.array(METAL_LO, np.float32)
        col = hi + (lo - hi) * t[:, None]
        return Image.fromarray(
            np.clip(np.repeat(col[:, None, :], g.w, axis=1), 0, 255).astype(np.uint8),
            "RGB")

    def _screw(self, img, cx, cy, r):
        """One slot-head screw, rasterised at 4x so a 5 px head still reads as a
        screw rather than a smudge."""
        s = self.MASK_SS
        n = int(2 * r) + 3
        tile = Image.new("RGBA", (n * s, n * s), (0, 0, 0, 0))
        td = ImageDraw.Draw(tile)
        c, rr = n * s / 2.0, r * s
        td.ellipse((c - rr, c - rr, c + rr, c + rr), fill=SCREW + (255,))
        td.ellipse((c - rr, c - rr, c + rr, c + rr),
                   outline=TEXT_SHADOW + (225,), width=max(1, s // 2))
        sl = rr * 0.62
        td.line((c - sl, c + sl, c + sl, c - sl),
                fill=TEXT_SHADOW + (235,), width=max(1, int(s * 1.1)))
        td.arc((c - rr * 0.68, c - rr * 0.68, c + rr * 0.68, c + rr * 0.68),
               166, 302, fill=(255, 255, 255, 165), width=s)
        tile = tile.resize((n, n), Image.LANCZOS)
        img.paste(tile, (int(round(cx - n / 2.0)), int(round(cy - n / 2.0))), tile)

    def _plate(self, img, back, g: Geo, b):
        """The instrument's nameplate: the same machined chrome as the cards.

        The status dot that pulses in the top bar then reads as the panel's
        power LED in a recessed window, which is what a bench instrument's
        plate looks like.
        """
        outer = self._round_mask(g, 0, 0)
        inner = self._round_mask(g, 0, b)
        img.paste(self._case_ramp(g), (g.x, g.y),
                  ImageChops.subtract(outer, inner).crop(g.box))
        img.paste(self._dial(back), (0, 0), inner)
        for inset, top_a, bot_a in ((0, 190, 18), (b - 1, 10, 118)):
            img.paste(Image.alpha_composite(
                img.convert("RGBA"),
                self._edge_layer(g, 0, inset, top_a, bot_a)).convert("RGB"), (0, 0))

    def _case(self, img, back, g: Geo, radius):
        """A case for a bench instrument, not a sheet of glass.

        Everything separating this face from the glass one is structural: the
        corners are square, the depth is built from a light-catching chrome
        bezel plus a chamfer instead of a blur, and the thing is visibly
        fastened -- four screws, one at each corner of the bezel. The dial
        behind the glass is the backdrop smoked down and drained of colour, so
        the frame stays metal whatever gradient the user picks.
        """
        if self._plain:               # borderless: the picture is the frame
            return
        b = CASE_BEZEL
        outer = self._round_mask(g, radius, 0)
        inner = self._round_mask(g, radius, b)

        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rectangle(
            (g.x + 2, g.y + 4, g.x + g.w + 2, g.y + g.h + 4), fill=(0, 0, 0, 195))
        sh = sh.filter(ImageFilter.GaussianBlur(4))
        img.paste(Image.alpha_composite(img.convert("RGBA"), sh).convert("RGB"), (0, 0))

        dial = self._dial(back)
        img.paste(dial, (0, 0), inner)

        img.paste(self._case_ramp(g), (g.x, g.y),
                  ImageChops.subtract(outer, inner).crop(g.box))
        # the case edge, then the chamfer where the bezel drops into the glass;
        # both are one-sided, and that asymmetry is what reads as a machined bevel
        for inset, top_a, bot_a in ((0, 210, 14), (b - 1, 6, 155)):
            img.paste(Image.alpha_composite(
                img.convert("RGBA"),
                self._edge_layer(g, radius, inset, top_a, bot_a)).convert("RGB"), (0, 0))

        m = b / 2.0
        for cx, cy in ((g.x + m, g.y + m), (g.x + g.w - m, g.y + m),
                       (g.x + m, g.y + g.h - m), (g.x + g.w - m, g.y + g.h - m)):
            self._screw(img, cx, cy, 1.9)

    # ------------------------------------------------------------------ pool
    def _live_box(self, g: Geo):
        """The area the pool owns: the whole card for glass, the glass inside
        the bezel for quartz."""
        i = self._inset
        return (g.x + i, g.y + i, g.x + g.w - i, g.y + g.h - i)

    def _card_mask_np(self, key, g: Geo):
        """Card outline as a float 0..1 mask, cropped to the live box."""
        m = self._masks.get(key)
        if m is None:
            m = np.asarray(self._round_mask(g, self._radius, self._inset)
                           .crop(self._live_box(g)), np.float32) / 255.0
            self._masks[key] = m
        return m

    def _water(self, img, key, g: Geo, pct, theme):
        """Fill the glass of card ``g`` with liquid up to ``pct``%.

        The waterline is anti-aliased: a pixel's water coverage is the fraction
        of its square that falls below the surface, so the wave reads as a
        smooth curve instead of the staircase a hard threshold produces on a
        320 px panel. The crest is a separate soft highlight laid over the top
        of the body, which keeps the surface readable at a glance.

        Three motions say "liquid" rather than "filled rectangle": a swell that
        travels along the card, a slow beat the whole level rides, and a tilt
        that lags the level so the pool tips while it fills and settles after.

        Every term is a function of the *card* row or a stepped clock, never of
        the depth below the surface. If the shading moved with the surface, one
        level step would repaint the whole pool instead of the band at its edge.
        """
        pct = max(0.0, min(100.0, pct))
        if pct <= 0.35:
            return
        box = self._live_box(g)
        h, w = box[3] - box[1], box[2] - box[0]
        span = max(1.0, (h - 1.0) - 1.0)
        # Quantise the level: an unchanged pool must produce identical pixels,
        # otherwise every sub-pixel wobble dirties the entire liquid area and
        # the USB cost explodes.
        sy = float(round((h - 1.0) - span * pct / 100.0))
        yy = np.arange(h, dtype=np.float32)[:, None]
        xx = np.arange(w, dtype=np.float32)[None, :]
        ph = (int(self.time * WATER_HZ) % 4000) * WATER_STEP
        lam2 = WATER_LAM * 0.52
        # one swell with a shorter one riding it: both travel the same way, so
        # the surface reads as flowing, at different speeds so it does not slide
        # along as a rigid shape
        ripple = (0.72 * np.sin(xx / WATER_LAM * 6.283 + ph)
                  + 0.28 * np.sin(xx / lam2 * 6.283 + ph * 1.55))
        bob = WATER_BOB * math.sin(int(self.time * WATER_RISE) * 0.75)
        tilt = self._tilt.get(key, 0.0) * (xx / max(1.0, w - 1.0) * 2.0 - 1.0)
        surf = sy + bob + tilt + WATER_AMP * ripple
        depth = yy - surf                      # >= 0 below the waterline

        # coverage of row yy by water, ramped over one pixel -> edge AA
        cov = np.clip(depth + 0.5, 0.0, 1.0)
        grad = yy / float(h - 1)               # 0 at glass top, 1 at glass bottom
        a_body = (232.0 - 96.0 * grad) * cov / 255.0
        # the crest hugs the waterline and fades into the body, so it needs no
        # hard band of its own; brightening it over the short swell reads as
        # light catching moving water
        glint = 0.80 + 0.14 * np.sin(xx / lam2 * 6.283 + ph * 1.55 + 0.9)
        a_rim = glint * cov * np.clip(1.0 - depth / 1.7, 0.0, 1.0)

        a = a_rim + a_body * (1.0 - a_rim)
        # the body stays darker than the accent and deepens downwards: a light
        # fill would fight the white readouts that sit on top of it
        lip = np.array(mix(theme, (255, 255, 255), 0.80), np.float32)[None, None, :]
        deep = (np.array(mix(theme, (0, 0, 0), 0.46), np.float32)
                * (1.0 - grad[..., None])
                + np.array(mix(theme, (0, 0, 0), 0.66), np.float32)
                * grad[..., None])
        num = (lip * a_rim[..., None] + deep * (a_body * (1.0 - a_rim))[..., None])
        col = num / np.maximum(a, 1e-6)[..., None]
        a = (a * self._card_mask_np(key, g))[..., None]

        sub = np.asarray(img.crop(box), np.float32)
        img.paste(Image.fromarray(
            np.clip(sub * (1.0 - a) + col * a, 0, 255).astype(np.uint8), "RGB"),
            (box[0], box[1]))

    # ------------------------------------------------------------- animation
    def step(self, dt):
        c = self.cfg
        self.time += dt
        k = 1 - math.exp(-dt * 4.0)
        for f in ("cpu", "gpu", "mem", "freq_cpu", "freq_gpu", "power", "cpu_power",
                  "gpu_power", "temp_gpu", "mem_used", "mem_total", "cpu_perf"):
            setattr(self.v, f, lerp(getattr(self.v, f), getattr(self.tgt, f), k))
        # byte rates: the sampler refreshes them once a second, so copy straight
        # through -- the top bar then updates on that same 1 Hz cadence
        self.v.net_down = self.tgt.net_down
        self.v.net_up = self.tgt.net_up
        self.v.fg_fps = self.tgt.fg_fps
        self.v.cpu_name = self.tgt.cpu_name
        self.v.gpu_name = self.tgt.gpu_name
        self.v.fg_app = self.tgt.fg_app
        self.v.host = self.tgt.host
        self.v.board = self.tgt.board
        self.v.cpu_cores = self.tgt.cpu_cores
        self.v.cpu_threads = self.tgt.cpu_threads

        # load meters: re-read the target on a slow cadence, then let a
        # critically damped spring glide the tip there (critically damped =
        # smooth and non-linear, with no overshoot or bounce)
        self._bar_acc += dt
        if self._bar_acc >= BAR_PERIOD:
            self._bar_acc = 0.0
            self._bar_tgt["cpu"] = self.v.cpu
            self._bar_tgt["gpu"] = self.v.gpu
        h = min(dt, 0.1)
        for key in ("cpu", "gpu"):
            pos = self.bar[key]
            vel = self._bar_v[key]
            vel += (BAR_OMEGA ** 2 * (self._bar_tgt[key] - pos)
                    - 2 * BAR_OMEGA * vel) * h
            pos += vel * h
            self._bar_v[key] = vel
            self.bar[key] = max(0.0, min(100.0, pos))

        # water has inertia: while a level is moving the surface tips towards the
        # far side, and once it stops the tilt decays away instead of snapping.
        # Sampled on the ripple's own clock, so the pool only changes WATER_HZ
        # times a second: one moved band per step is affordable, a shift on
        # every frame would re-send a band of tiles on every frame.
        tick = int(self.time * WATER_HZ)
        if tick != self._tick:
            self._tick = tick
            for key, pct in (("cpu", self.bar["cpu"]), ("gpu", self.bar["gpu"]),
                             ("mem", self.v.mem)):
                prev = self._lvl.get(key, pct)
                self._lvl[key] = pct
                want = (pct - prev) * WATER_HZ * WATER_SLOSH
                self._tilt[key] = lerp(self._tilt.get(key, 0.0),
                                       max(-WATER_TILT, min(WATER_TILT, want)),
                                       WATER_SETTLE)

        self._text_acc += dt
        if self._text_acc >= c.text_period:
            self._text_acc = 0.0
            self._refresh_text()
        self._fps_acc += dt
        if self._fps_acc >= c.fps_period:
            self._fps_acc = 0.0
            if self.v.fg_fps >= 1:
                self._hold_fps = f"{self.v.fg_fps:.0f}"
            self.num_fps.set(self._hold_fps)
        for n in self._nums:
            n.step(dt)

    def _refresh_text(self):
        v = self.v
        self.num_cpu.set(f"{v.cpu:.0f}")
        self.num_gpu.set(f"{v.gpu:.0f}")
        if v.freq_cpu:
            self._hold_fc = f"{v.freq_cpu / 1000:.2f}"
        if v.freq_gpu:
            self._hold_fg = f"{v.freq_gpu:.0f}"
        self.num_fc.set(self._hold_fc)
        self.num_fg.set(self._hold_fg)
        self.num_mem.set(f"{v.mem:.0f}%")
        self.num_pwr.set(f"{v.power:.0f}")
        self._cpu_short = short_cpu(v.cpu_name)
        self._gpu_short = short_gpu(v.gpu_name)
        self._board = short_board(v.board)
        self._cores = core_label(v.cpu_cores, v.cpu_threads)

    # ---------------------------------------------------------------- render
    def render(self, dt):
        if self._static is None:
            self._build_static()
        self.step(dt)
        img = self._static.copy()
        c = self.cfg
        cpu_col = hex_rgb(c.accent_cpu, ACCENT_CYAN)
        gpu_col = hex_rgb(c.accent_gpu, ACCENT_VIOLET)
        mem_col = hex_rgb(c.accent_mem, ACCENT_CYAN)
        pwr_col = ramp(POWER_STOPS, self.v.power)

        d = ImageDraw.Draw(img, "RGBA")

        # the pools fill next; every readout is then drawn on top of them
        for key, pct, theme in (("cpu", self.bar["cpu"], cpu_col),
                                ("gpu", self.bar["gpu"], gpu_col),
                                ("mem", self.v.mem, mem_col)):
            g = self.cards.get(key)
            if g is not None:
                self._water(img, key, g, pct, theme)

        for key, pct in (("cpu", self.v.cpu), ("gpu", self.v.gpu)):
            if pct > 80:
                pulse = 0.5 + 0.5 * math.sin(self.time * 3.0)
                col = ACCENT_RED if pct > 92 else ACCENT_AMBER
                a = int((pct - 80) / 20 * 55 * (0.6 + 0.4 * pulse))
                g, ins = self.cards[key], self._inset
                d.rounded_rectangle(
                    (g.x + ins, g.y + ins, g.x + g.w - ins, g.y + g.h - ins),
                    max(0, self._radius - ins),
                    outline=col + (min(a, 60),), width=2)

        self._labels(d)
        self._big_card(d, img, self.cards["cpu"], self.num_cpu, self.num_fc,
                       "GHz", f"~{self.v.cpu_power:.0f} W", TEXT_DIM, self._cpu_short)
        self._chip(d, self.cards["cpu"], "CPU", self._cores, TEXT_DIM)
        self._big_card(d, img, self.cards["gpu"], self.num_gpu, self.num_fg,
                       "MHz", f"~{self.v.gpu_power:.0f} W", TEXT_DIM, self._gpu_short)
        self._chip(d, self.cards["gpu"], "GPU",
                   f"{self.v.temp_gpu:.0f}°C", ramp(TEMPERATURE_STOPS, self.v.temp_gpu))

        if "mem" in self.cards:
            g = self.cards["mem"]
            self.num_mem.draw(img, g.x + PAD, g.y + 17)
            sub = f"{self.v.mem_used:.0f}/{self.v.mem_total:.0f}G"
            d.text((g.x + g.w - PAD - self._f_unit.getlength(sub), g.y + 6), sub,
                   font=self._f_unit, fill=TEXT_DIM,
                   stroke_width=1, stroke_fill=TEXT_SHADOW)

        if "power" in self.cards:
            g = self.cards["power"]
            self.num_pwr.draw(img, g.x + PAD, g.y + 17)
            d.text((g.x + PAD + self.num_pwr.width() + 3, g.y + 32), "W",
                   font=self._f_unit, fill=pwr_col,
                   stroke_width=1, stroke_fill=TEXT_SHADOW)

        if "fps" in self.cards:
            g = self.cards["fps"]
            self.num_fps.draw(img, g.x + PAD, g.y + 17)
            app = fit(self.v.fg_app or "—", self._f_app, g.w - PAD * 2)
            d.text((g.x + PAD, g.y + 41), app, font=self._f_app, fill=TEXT_DIM,
                   stroke_width=1, stroke_fill=TEXT_SHADOW)

        # the top bar is a plate in the quartz face and bare backdrop in glass,
        # so the band the text must stay inside is inset by the plate's bezel
        bo = PLATE_BEZEL if c.style == "quartz" else 0
        wtop, wbot = TOP_BAR.y + bo, TOP_BAR.y + TOP_BAR.h - bo
        # left corner carries the chipset -- the one part of the board name that
        # means anything at a glance -- falling back to the machine name
        left_txt = self._board or self.v.host or "PC"
        d.text((TOP_BAR.x + 2, ink_mid(self._f_host, left_txt, wtop, wbot)),
               left_txt, font=self._f_host, fill=TEXT)

        self.clock = time.strftime("%H:%M:%S")
        cw = self._f_clock.getlength(self.clock)
        d.text((TOP_BAR.x + TOP_BAR.w - cw - 14,
                ink_mid(self._f_clock, self.clock, wtop, wbot)),
               self.clock, font=self._f_clock, fill=TEXT)

        hw = self._f_host.getlength(left_txt)
        left = TOP_BAR.x + 6 + hw
        right = TOP_BAR.x + TOP_BAR.w - cw - 18
        net = fit(f"↓{rate(self.v.net_down)}/s ↑{rate(self.v.net_up)}/s",
                  self._f_net, right - left)
        d.text(((left + right - self._f_net.getlength(net)) / 2.0,
                ink_mid(self._f_net, net, wtop, wbot)),
               net, font=self._f_net, fill=TEXT_DIM)
        cx, cy = TOP_BAR.x + TOP_BAR.w - 8, TOP_BAR.y + 8
        a = int(120 + 135 * (0.5 + 0.5 * math.sin(self.time * 2.4)))
        d.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), fill=ACCENT_GREEN + (a,))

        return img

    def _chip(self, d, g: Geo, label, text, col):
        """Small readout sitting just right of the card's label."""
        x = g.x + PAD + self._f_lbl.getlength(label) + 5
        d.text((x, g.y + 5), text, font=self._f_lbl, fill=col,
               stroke_width=1, stroke_fill=TEXT_SHADOW)

    def _big_card(self, d, img, g: Geo, num_pct, freq_num, unit, rhs, rhs_col, spec):
        uw = self._f_unit.getlength(unit)
        vw = freq_num.width()
        fx = g.x + g.w - PAD - (vw + 3 + uw)
        freq_num.draw(img, fx, g.y + 4)
        d.text((fx + vw + 3, g.y + 9), unit, font=self._f_unit, fill=TEXT_FAINT,
               stroke_width=1, stroke_fill=TEXT_SHADOW)

        num_pct.draw(img, g.x + PAD, g.y + 18)
        d.text((g.x + PAD + num_pct.width() + 3, g.y + 34), "%", font=self._f_sub,
               fill=TEXT_DIM, stroke_width=1, stroke_fill=TEXT_SHADOW)
        d.text((g.x + g.w - PAD - self._f_sub.getlength(rhs), g.y + 34), rhs,
               font=self._f_sub, fill=rhs_col,
               stroke_width=1, stroke_fill=TEXT_SHADOW)
        # part name, tucked in directly under the readout and above the pool
        d.text((g.x + PAD, g.y + 50),
               fit(spec or "—", self._f_spec, g.w - PAD * 2),
               font=self._f_spec, fill=TEXT_DIM,
               stroke_width=1, stroke_fill=TEXT_SHADOW)

    # ------------------------------------------------------------------ util
    def to_rgb565_be(self, img):
        arr = np.asarray(img, dtype=np.uint16)
        r = (arr[:, :, 0] >> 3) & 0x1F
        g = (arr[:, :, 1] >> 2) & 0x3F
        b = (arr[:, :, 2] >> 3) & 0x1F
        v = (r << 11) | (g << 5) | b
        return v.astype(">u2").tobytes()