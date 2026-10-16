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
import zlib
from dataclasses import dataclass

import numpy as np
from PIL import (Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter,
                 ImageFont, ImageOps)

from .audio import BANDS, FMAX, FMIN
# The per-glyph reveal timing lives with the overlay's state machine; lyrics has
# no dependency on the renderer, so this import introduces no cycle.
from .lyrics import LYRIC_CHAR, LYRIC_GEOM_SLOW, word_u

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


# Chinese lyrics need a face the latin stack lacks; fall back to the UI font if
# none of these ship, so the overlay still draws (just in a latin face).
_CJK_NAMES = ("msyhbd.ttc", "msyh.ttc", "simhei.ttf")


def _cjk(size):
    for n in _CJK_NAMES:
        try:
            return ImageFont.truetype(f"{FONT_DIR}/{n}", size)
        except Exception:
            continue
    return F(size, "bold")


# The lyric overlay's own faces, each a fallback chain: a decorative name that
# is missing on this machine still lands on a CJK font rather than boxes.
_LYRIC_FACES = {
    "youyuan": ("simyou.ttf", "msyhbd.ttc", "msyh.ttc"),
    "xihei": ("STXIHEI.TTF", "msyhbd.ttc", "msyh.ttc"),
}
LYRIC_SIZE = 21


def _face_for(name):
    """The lyric font for a config name, walking that face's fallback chain."""
    for n in _LYRIC_FACES.get(name) or ():
        try:
            return ImageFont.truetype(f"{FONT_DIR}/{n}", LYRIC_SIZE)
        except Exception:
            continue
    return _cjk(LYRIC_SIZE)


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

# ---------------------------------------------------------------- spectrum --
# The analyser's fixed geometry. The band spans the plate width and its height is
# a whole number of dirty-rect rows (10 px each), so a bar that crosses a row
# costs only that row -- which is what keeps a live spectrum inside the USB
# budget. The decade ticks land well inside the plate, clear of the outer case's
# corner screws.
SPEC_X0, SPEC_X1 = 8, 312
SPEC_TOP, SPEC_BOT = 33, 153
SPEC_ROW, SPEC_ROWS = 10, 12
SPEC_CELL, SPEC_BAR = 5, 5
SPEC_MID = (SPEC_TOP + SPEC_BOT) // 2
SPEC_RAIL_Y = 152
SPEC_TICK_Y = 154
SPEC_TOP_STRIP = 24          # the top bar band, above the first card row
SPEC_PITCH = (SPEC_X1 - SPEC_X0) / BANDS
SPEC_TICKS = ((100.0, "100"), (1000.0, "1k"), (10000.0, "10k"))
SILENCE = [0.0] * BANDS      # a flat band, shared and never written to
PAD = 12

# The lyric overlay: one line hovering over the top of the bars. Each glyph is
# rasterised onto its own small layer (glyph + 1 px rim + optional glow), so the
# band is a little taller than the type and the padding is the glow's blur margin.
LYRIC_TOP = SPEC_TOP + 3
LYRIC_H = 24
LYRIC_PAD = 3
# the entrance rise, in px, a still-hidden glyph starts below its seat
LYRIC_RISE = 3
# The geom effect's shards keep this share of their opacity while they are still
# scattered, so the flight reads: tying alpha to the reveal alone would make the
# fragments most transparent exactly when they are furthest from their seat, and
# the scatter would vanish (measured: it moved 3 px more than the plain fade).
LYRIC_GEOM_FLOOR = 0.6
# The band is where a *settled* glyph sits; the reveal (rise + seat sway) reaches
# a little past it, so the overlay is allowed a taller window than the band --
# measured, not guessed (see build/_pool/lyric_envelope.py). The window's top
# sits 1 px above SPEC_TOP because a 21 px CJK face inks 22 px tall, which in a
# 24 px band leaves too little room for the sway above the ink. It governs
# everything that *rests*: the settled line and the fade reveal. The geom
# shatter is exempt by design -- its shards cross the whole panel before landing.
LYRIC_WIN_TOP = SPEC_TOP - 1
LYRIC_WIN_BOT = SPEC_TOP + 34

# The spectrum's styles, as (baked chrome, live bars). Chrome runs once per bake
# and draws whatever is identical every frame; bars runs every frame. Adding a
# style is two methods and one line here -- the dispatch itself never grows, and
# an unknown value falls back to the default instead of drawing nothing.
DEFAULT_STYLE = "led"
STYLES = {
    "led": ("_chrome_led", "_spec_led"),
    "peak": ("_chrome_grid", "_spec_peak"),
    "line": ("_chrome_grid", "_spec_line"),
    "area": ("_chrome_grid", "_spec_area"),
    "sym": ("_chrome_grid", "_spec_sym"),
    "grad": ("_chrome_grid", "_spec_grad"),
    "dots": ("_chrome_grid", "_spec_dots"),
    "fine": ("_chrome_grid", "_spec_fine"),
}


def spec_ramp(accent):
    """A bar's vertical colour, indexed by distance above the baseline.

    The colour at a pixel depends only on *where* it is, never on how tall the
    band happens to be this frame. That is what keeps a gradient bar cheap: when
    a band grows it only exposes new rows, and every row that was already lit is
    redrawn in exactly the colour it had before, so the dirty-rect pass reports
    the growth and nothing else. The deleted mirror style scaled its gradient to
    the bar height, so every row changed whenever the level moved.
    """
    span = SPEC_BOT - SPEC_TOP
    lo = mix(accent, (10, 14, 22), 0.55)
    hi = mix(accent, (255, 255, 255), 0.30)
    return [mix(lo, hi, j / span) for j in range(span + 1)]


def spec_x(i):
    """Left edge of band ``i``'s column, for the styles that fill a full pitch."""
    return int(SPEC_X0 + i * SPEC_PITCH)


def _alpha_lut(a):
    """Scale a layer's alpha by ``a``/255: the overlay's opacity, and during a
    transition the share of it a line carries this frame."""
    a = max(0, min(255, int(a)))
    return [v * a // 255 for v in range(256)]


def _shatter(txt, ci, bi, x, y):
    """Where shard ``bi`` of glyph ``ci`` starts, as an offset from its seat.

    The start is a hash-picked point anywhere on the panel, so a line's shards
    fly in from across the whole screen instead of nudging a few pixels in place
    -- expressed as an offset from the shard's resting place ``(x, y)``, which
    keeps every fragment beginning on screen rather than half of them clipped
    away off the edge. Deterministic on purpose: the seed is a crc32, never the
    builtin hash, which is salted per process -- the shards would then land
    somewhere different on every launch and would not line up between two runs
    of the same line.
    """
    s = zlib.crc32(f"{txt}|{ci}|{bi}".encode())
    return (s % W) - x, ((s >> 9) % H) - y


def _paste_overlay(img, lay, px, py):
    """Paste a finished lyric layer, clipped to the overlay's vertical window.

    The window is the settled band plus the room the reveal and the scatter need;
    a layer that would reach past it (an unusually tall glyph, a wide glow) is
    trimmed to it, so the overlay can never touch the chrome above or wash deep
    into the bars. Horizontal overhang is left to PIL's own edge clipping.
    """
    y0, y1 = 0, lay.height
    if py < LYRIC_WIN_TOP:
        y0, py = LYRIC_WIN_TOP - py, LYRIC_WIN_TOP
    if py + y1 > LYRIC_WIN_BOT + 1:
        y1 = LYRIC_WIN_BOT + 1 - py
    if y1 <= y0:
        return
    if y0 or y1 != lay.height:
        lay = lay.crop((0, y0, lay.width, y1))
    img.paste(lay, (px, py), lay)


def _seat(txt, k):
    """The fixed vertical offset (px) glyph ``k`` of ``txt`` sits at.

    Hash-derived on purpose so a line reads as deliberately uneven rather than
    machine-straight, while staying identical on every frame and every launch --
    a random draw per frame would make the whole sentence shimmer.
    """
    return (zlib.crc32(f"{txt}|{k}".encode()) % 7) - 3


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
        # what the analyser hands over each frame; silence until it is wired up
        self.spectrum_bands = list(SILENCE)
        self.spectrum_peaks = list(SILENCE)
        self.spec_state = ""       # capture health, shown as a hint in the bar
        self._plain = False     # True while a user backdrop replaces the chrome
        self._num_style = None  # face the numerals were built for
        self._face = None       # cached lyric face, rebuilt when the name changes
        self._face_key = None

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
        self._f_tick = F(9, "semibold")      # the spectrum's decade ticks
        # the overlay line is a CJK face, and it retracks when the panel switches
        # the lyric font: per-glyph layers, so nothing is rasterised line-wide
        self._f_lyric = self._lyric_face()

        # lyric overlay: drawing-time fields, deliberately outside _look_sig, so
        # a new line never re-bakes the chrome
        self.lyric_text = ""
        self.lyric_prev = ""
        self.lyric_t = 1.0
        self.lyric_effect = "fade"
        self.lyric_visible = False
        self.lyric_dur = 0.22                # the line's reveal length, seconds
        self._lyr_cache = {}                 # (txt, face, colour, glow) -> cells

        self._layout()
        self.apply_config(self.cfg, force=True)

    def _lyric_face(self):
        """The lyric face for the current setting, rebuilt only when it changes."""
        key = getattr(self.cfg, "lyrics_font", "youyuan")
        if self._face is None or key != self._face_key:
            self._face = _face_for(key)
            self._face_key = key
        return self._face

    # ------------------------------------------------------------------ config
    def _use_image(self):
        """Whether the chosen backdrop image replaces the gradient.

        "auto" keeps the old meaning -- a chosen image wins -- so existing
        configs are unaffected; the explicit modes let the image stay selected
        while the gradient is what is actually shown.
        """
        if self._bg_img is None:
            return False
        return (self.cfg.bg_mode or "auto").lower() != "gradient"

    def _look_sig(self):
        c = self.cfg
        return (c.mode,
                c.spectrum_style,
                c.style,
                c.accent_cpu, c.accent_gpu, c.accent_mem, c.accent_pwr,
                round(c.brightness, 3), round(c.bg_brightness, 3),
                round(c.saturation, 3), round(c.glass, 3),
                c.bg_mode,
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
        if self.cfg.mode == "spectrum":
            # no cards at all: the spectrum owns the plate, and an empty map is
            # what makes the pool, label and card passes fall away on their own
            self.big_h = 140
            self.bottom_order = []
            self._masks = {}
            return
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
        self.spectrum_bands = list(stats.get("spectrum_bands") or SILENCE)
        self.spectrum_peaks = list(stats.get("spectrum_peaks") or SILENCE)
        self.spec_state = stats.get("spec_state", "")
        self.lyric_text = stats.get("lyric_text") or ""
        self.lyric_prev = stats.get("lyric_prev") or ""
        self.lyric_t = float(stats.get("lyric_t", 1.0))
        self.lyric_effect = stats.get("lyric_effect") or "fade"
        self.lyric_visible = bool(stats.get("lyric_visible", False))
        # the reveal length travels with the line (LyricState computes it from the
        # glyph count); the non-lyric paths simply carry no key, so the default
        # stands in. The face is re-picked every frame -- cached, so it is free.
        self.lyric_dur = float(stats.get("lyric_dur", LYRIC_CHAR))
        self._f_lyric = self._lyric_face()

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
        if self._use_image():
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
        self._plain = self._use_image()
        ins = CASE_BEZEL if (c.style == "quartz" and not self._plain) else 0
        if ins != self._inset:
            self._inset = ins
            self._masks = {}
        img = ImageEnhance.Brightness(back).enhance(c.brightness)
        if c.mode == "spectrum":
            self._spectrum_chrome(img, back, c)
        elif c.style == "quartz":
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

    # ------------------------------------------------------------- spectrum --
    def _spec_tick_x(self, f):
        """Left edge of the band carrying ``f``, for the decade ticks."""
        t = (math.log10(f) - math.log10(FMIN)) / (math.log10(FMAX) - math.log10(FMIN))
        return int(SPEC_X0 + t * BANDS * SPEC_PITCH)

    def _panel_case(self, img):
        """The instrument's outer case: four rails and their corner screws.

        The quartz face fastens every card down with a case of its own; in
        spectrum mode there are no cards, so the plate carries the case instead,
        which is what keeps the machined reading when the cards step aside.
        """
        b = CASE_BEZEL
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, W - 1, H - 1), outline=METAL_LO)
        d.line((0, 0, W - 1, 0), fill=METAL_HI)
        d.line((0, 1, W - 1, 1), fill=mix(METAL_HI, METAL_LO, 0.45))
        d.rectangle((b, b, W - 1 - b, H - 1 - b), outline=(15, 17, 21))
        m = b / 2.0
        for cx, cy in ((m, m), (W - m, m), (m, H - m), (W - m, H - m)):
            self._screw(img, cx, cy, 1.9)

    def _spectrum_chrome(self, img, back, c):
        """Bake everything about the spectrum that does not move.

        The ghost cells, the rails, the decade ticks and the dB grid are the same
        pixels every frame, so they belong in the static layer: the dirty-rect
        pass then only ever sees the bars that actually moved, which is what
        keeps a live spectrum inside the link budget. In the quartz face the bars
        sit on the dial, the way they sit behind the glass of a real instrument.
        """
        if c.style == "quartz" and not self._plain:
            mask = Image.new("L", (W, H), 0)
            ImageDraw.Draw(mask).rectangle(
                (CASE_BEZEL, SPEC_TOP_STRIP, W - 1 - CASE_BEZEL, H - 1 - CASE_BEZEL),
                fill=255)
            img.paste(self._dial(back), (0, 0), mask)
            self._panel_case(img)
            self._plate(img, back, TOP_BAR, PLATE_BEZEL)

        d = ImageDraw.Draw(img, "RGBA")
        chrome = STYLES.get((c.spectrum_style or DEFAULT_STYLE).lower(),
                            STYLES[DEFAULT_STYLE])[0]
        getattr(self, chrome)(img, d)
        # the rail and the decade ticks are shared: the instrument's own scale
        d.line((SPEC_X0, SPEC_RAIL_Y, SPEC_X1, SPEC_RAIL_Y),
               fill=(255, 255, 255, 26))
        for f, label in SPEC_TICKS:
            x = self._spec_tick_x(f)
            d.line((x, SPEC_RAIL_Y + 1, x, SPEC_RAIL_Y + 4),
                   fill=(255, 255, 255, 34))
            d.text((x + 2, SPEC_TICK_Y), label, font=self._f_tick, fill=TEXT_DIM)

    def _chrome_led(self, img, d):
        """The unlit cells, which are the LED face's texture; baked, they are free."""
        for i in range(BANDS):
            x = int(SPEC_X0 + i * SPEC_PITCH + (SPEC_PITCH - SPEC_CELL) / 2)
            for r in range(SPEC_ROWS):
                y = SPEC_BOT - (r + 1) * SPEC_ROW + 1
                d.rectangle((x, y, x + SPEC_CELL - 1, y + SPEC_ROW - 2),
                            fill=(255, 255, 255, 13))

    def _chrome_grid(self, img, d):
        """Baked dB lines, so a style that draws no cells still carries a scale."""
        for frac in (0.2, 0.4, 0.6, 0.8):
            y = SPEC_BOT - int(frac * (SPEC_BOT - SPEC_TOP))
            d.line((SPEC_X0, y, SPEC_X1, y), fill=(255, 255, 255, 12))

    def _spectrum_bars(self, img, d):
        """The bars: the one part of a spectrum frame that is not baked.

        Every height is quantised to 2 px, and the LED face is already aligned to
        the 10 px row grid, so a band that does not cross a step this frame costs
        no bandwidth at all -- which is what keeps the mode inside the link.
        """
        _, bars = STYLES.get((self.cfg.spectrum_style or DEFAULT_STYLE).lower(),
                             STYLES[DEFAULT_STYLE])
        accent = hex_rgb(self.cfg.accent_cpu, ACCENT_CYAN)
        getattr(self, bars)(img, d, self.spectrum_bands, self.spectrum_peaks, accent)

    def _spec_led(self, img, d, bands, peaks, accent):
        """One square per row: solid when lit, and the top cell whitened."""
        hot = mix(accent, (255, 255, 255), 0.42)
        for i, level in enumerate(bands):
            x = int(SPEC_X0 + i * SPEC_PITCH + (SPEC_PITCH - SPEC_CELL) / 2)
            lit = max(0, min(SPEC_ROWS, int(round(level * SPEC_ROWS))))
            for r in range(lit):
                y = SPEC_BOT - (r + 1) * SPEC_ROW + 1
                d.rectangle((x, y, x + SPEC_CELL - 1, y + SPEC_ROW - 2),
                            fill=(hot if r == lit - 1 else accent) + (255,))

    def _spec_peak(self, img, d, bands, peaks, accent):
        """Flat bars with a cap on the recent maximum; amber near full scale."""
        bar = mix(accent, (12, 16, 24), 0.34)
        span = SPEC_BOT - SPEC_TOP
        for i, level in enumerate(bands):
            x = int(SPEC_X0 + i * SPEC_PITCH + (SPEC_PITCH - SPEC_BAR) / 2)
            h = (int(level * span) // 2) * 2
            if h:
                d.rectangle((x, SPEC_BOT - h, x + SPEC_BAR - 1, SPEC_BOT),
                            fill=bar + (255,))
            ph = (int(peaks[i] * span) // 2) * 2
            if ph > h + 2:
                cap = ACCENT_AMBER if peaks[i] > 0.88 else accent
                d.rectangle((x, SPEC_BOT - ph, x + SPEC_BAR - 1, SPEC_BOT - ph + 2),
                            fill=cap + (255,))

    def _spec_sym(self, img, d, bands, peaks, accent):
        """A flat bar mirrored about the centre line, capped at both outer ends.

        The plain colour is the point: keeping the same tone everywhere means a
        band that grows only paints the two rows it just exposed, so the mirrored
        shape costs little more than the one-sided peak bars.
        """
        body = mix(accent, (12, 16, 24), 0.34)
        cap = mix(accent, (255, 255, 255), 0.42)
        span = SPEC_BOT - SPEC_TOP
        for i, level in enumerate(bands):
            h = (int(level * span) // 2) * 2              # both halves together
            if not h:
                continue
            r = h // 2
            x = int(SPEC_X0 + i * SPEC_PITCH + (SPEC_PITCH - SPEC_BAR) / 2)
            d.rectangle((x, SPEC_MID - r, x + SPEC_BAR - 1, SPEC_MID - 1),
                        fill=body + (255,))
            d.rectangle((x, SPEC_MID, x + SPEC_BAR - 1, SPEC_MID + r - 1),
                        fill=body + (255,))
            d.rectangle((x, SPEC_MID - r, x + SPEC_BAR - 1, SPEC_MID - r + 1),
                        fill=cap + (255,))
            d.rectangle((x, SPEC_MID + r - 2, x + SPEC_BAR - 1, SPEC_MID + r - 1),
                        fill=cap + (255,))

    def _spec_grad(self, img, d, bands, peaks, accent):
        """A one-sided bar whose colour is a gradient measured from the baseline."""
        ramp = spec_ramp(accent)
        span = SPEC_BOT - SPEC_TOP
        for i, level in enumerate(bands):
            h = (int(level * span) // 2) * 2
            if not h:
                continue
            x = int(SPEC_X0 + i * SPEC_PITCH + (SPEC_PITCH - SPEC_BAR) / 2)
            x1 = x + SPEC_BAR - 1
            for y in range(SPEC_BOT - h + 1, SPEC_BOT + 1):
                d.line((x, y, x1, y), fill=ramp[SPEC_BOT - y] + (255,))

    def _spec_line(self, img, d, bands, peaks, accent):
        """A thin polyline through the band centres, with no fill beneath it."""
        col = mix(accent, (255, 255, 255), 0.25)
        span = SPEC_BOT - SPEC_TOP
        pts = [(int(SPEC_X0 + (i + 0.5) * SPEC_PITCH),
                max(SPEC_TOP + 1, SPEC_BOT - int(level * span)))
               for i, level in enumerate(bands)]
        d.line(pts, fill=col + (255,), width=2)

    def _spec_area(self, img, d, bands, peaks, accent):
        """The polyline silhouette, filled down to the rail with the height ramp.

        Filling each band's own column means the pixels under the line are the
        same colour two frames running; only the top edge moves, so a curve that
        shifts paints a couple of rows and not the mountain under it.
        """
        ramp = spec_ramp(accent)
        edge = mix(accent, (255, 255, 255), 0.55)
        span = SPEC_BOT - SPEC_TOP
        for i, level in enumerate(bands):
            top = SPEC_BOT - int(level * span)
            x0, x1 = spec_x(i), spec_x(i + 1) - 1
            for y in range(top, SPEC_BOT + 1):
                d.line((x0, y, x1, y), fill=ramp[SPEC_BOT - y] + (255,))
        pts = [(int(SPEC_X0 + (i + 0.5) * SPEC_PITCH),
                max(SPEC_TOP + 1, SPEC_BOT - int(level * span)))
               for i, level in enumerate(bands)]
        d.line(pts, fill=edge + (255,), width=2)

    def _spec_dots(self, img, d, bands, peaks, accent):
        """A column of round dots per band; a louder band lights more of them."""
        dot, gap = 3, 6
        span = SPEC_BOT - SPEC_TOP
        rows = span // gap
        hot = mix(accent, (255, 255, 255), 0.42)
        r = dot // 2
        for i, level in enumerate(bands):
            cx = int(SPEC_X0 + (i + 0.5) * SPEC_PITCH)
            lit = int(round(level * rows))
            for k in range(lit):
                cy = SPEC_BOT - r - k * gap
                d.ellipse((cx - r, cy - r, cx + r, cy + r),
                          fill=(hot if k == lit - 1 else accent) + (255,))

    def _spec_fine(self, img, d, bands, peaks, accent):
        """Narrow columns with wide gaps and a 1 px bright head."""
        w = 3
        body = mix(accent, (12, 16, 24), 0.30)
        head = mix(accent, (255, 255, 255), 0.50)
        span = SPEC_BOT - SPEC_TOP
        for i, level in enumerate(bands):
            h = (int(level * span) // 2) * 2
            if not h:
                continue
            x = int(SPEC_X0 + i * SPEC_PITCH + (SPEC_PITCH - w) / 2)
            x1 = x + w - 1
            d.rectangle((x, SPEC_BOT - h + 1, x1, SPEC_BOT), fill=body + (255,))
            d.rectangle((x, SPEC_BOT - h + 1, x1, SPEC_BOT - h + 1),
                        fill=head + (255,))

    # ------------------------------------------------------------------ lyric
    def _lyric(self, img, d):
        """The current line over the top of the bars, at the panel's opacity.

        Drawing-time only: nothing here reaches the bake, so a new line costs no
        re-bake. The transparency has to come from a layer of its own -- an alpha
        in a *text* fill is ignored by ImageDraw's RGBA blend (measured: the
        glyph lands opaque), so each glyph is rasterised onto its own RGBA layer
        and pasted through that layer's alpha instead.
        """
        if not self.lyric_visible or not self.lyric_text:
            return
        f = self._f_lyric
        span = SPEC_X1 - SPEC_X0
        txt = fit(self.lyric_text, f, span)
        if not txt:
            return
        base = int(round(255 * max(0.3, min(1.0, float(self.cfg.lyrics_opacity)))))
        t = max(0.0, min(1.0, float(self.lyric_t)))
        dur = max(1e-6, float(self.lyric_dur))
        eff = self.lyric_effect or "fade"
        # A cross-fade hands the outgoing line's opacity to the incoming one in
        # place; geom has no outgoing line at all, because its fragments fly in
        # from all over the panel and a whole sentence sitting behind them would
        # only muddy the shatter.
        if self.lyric_prev and t < 1.0 and eff != "geom":
            self._lyric_line(img, fit(self.lyric_prev, f, span), None,
                             int(base * (1.0 - t)), 0.0)
        self._lyric_line(img, txt, eff, base, t, dur)

    def _lyric_cells(self, txt):
        """The line broken into one small RGBA layer per glyph.

        Returns ``(line_x, line_y, cells)``: the origin a plain
        ``draw.text((line_x, line_y), txt)`` would use, and, per glyph, its layer
        plus the ``(dx, dy)`` offset of that layer's top-left from the origin.
        Every layer carries glow (optional), then its 1 px dark rim, then the
        glyph on top, and its size follows the glyph's own ink box, so no face or
        size can clip. Memoised on text + face + colour + glow -- the key must
        carry all four or a panel switch would keep showing the old look.
        """
        f = self._f_lyric
        color = hex_rgb(getattr(self.cfg, "lyrics_color", "#eef3fb"), (238, 243, 251))
        glow = bool(getattr(self.cfg, "lyrics_glow", True))
        key = (txt, self._face_key, color, glow)
        got = self._lyr_cache.get(key)
        if got is not None:
            return got
        # the pad clears the 1 px rim; the glow needs its blur tail on top of it
        m = LYRIC_PAD + (3 if glow else 0)
        dark = tuple(int(c * 0.35) for c in color)
        cells = []
        adv = 0.0
        for ch in txt:
            bbox = f.getbbox(ch)
            if ch.isspace() or not bbox or (bbox[2] - bbox[0] <= 0
                                            and bbox[3] - bbox[1] <= 0):
                # blank: a fully transparent 1x1 placeholder keeps the index
                # aligned with the glyph without rasterising an empty layer
                lay, dx, dy = Image.new("RGBA", (1, 1), (0, 0, 0, 0)), adv, 0
            else:
                w = bbox[2] - bbox[0] + 2 * m
                h = bbox[3] - bbox[1] + 2 * m
                o = (m - bbox[0], m - bbox[1])
                ink = Image.new("L", (w, h), 0)
                ImageDraw.Draw(ink).text(o, ch, font=f, fill=255)
                full = Image.new("L", (w, h), 0)
                ImageDraw.Draw(full).text(o, ch, font=f, fill=255,
                                          stroke_width=1, stroke_fill=255)
                ring = ImageChops.subtract(full, ink)
                lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                if glow:
                    g = Image.new("RGBA", (w, h), color + (0,))
                    g.putalpha(ink.filter(ImageFilter.GaussianBlur(1.2)))
                    lay = Image.alpha_composite(lay, g)
                r = Image.new("RGBA", (w, h), dark + (0,))
                r.putalpha(ring)
                lay = Image.alpha_composite(lay, r)
                face = Image.new("RGBA", (w, h), color + (0,))
                face.putalpha(ink)
                lay = Image.alpha_composite(lay, face)
                # the glyph's ink lands at (line_x + bbox[0], line_y + bbox[1])
                # were it drawn line-wide, so the layer sits m above/left of that
                dx, dy = adv + bbox[0] - m, bbox[1] - m
            cells.append((lay, dx, dy))
            adv += f.getlength(ch)
        line_x = int(SPEC_X0 + ((SPEC_X1 - SPEC_X0) - f.getlength(txt)) / 2)
        line_y = ink_mid(f, txt, LYRIC_TOP, LYRIC_TOP + LYRIC_H)
        got = (line_x, line_y, cells)
        if len(self._lyr_cache) > 16:
            self._lyr_cache.clear()
        self._lyr_cache[key] = got
        return got

    def _lyric_line(self, img, txt, eff, base, t, dur=0.0):
        """One line typed in glyph by glyph, each glyph off its own seat.

        ``eff`` None marks the outgoing line: it is already settled, so every
        glyph takes ``u = 1``. Otherwise ``u`` is the glyph's reveal progress and
        carries its opacity (``base * u``) and its 3 px rise. ``geom`` also cuts
        each still-moving glyph into three vertical shards that fly home from
        points scattered across the whole panel.
        """
        if not txt or base <= 0:
            return
        line_x, line_y, cells = self._lyric_cells(txt)
        settled = eff is None
        for k, (lay, dx, dy) in enumerate(cells):
            u = (1.0 if settled else
                 word_u(t, dur, k, LYRIC_GEOM_SLOW if eff == "geom" else 1.0))
            px = int(line_x + dx)
            py = int(line_y + dy + _seat(txt, k) + int(round((1.0 - u) * LYRIC_RISE)))
            if eff == "geom" and u < 1.0:
                # The glyph breaks into three vertical shards; each starts from its
                # own hash-picked point on the panel and eases onto its seat. The
                # alpha floor keeps the fragments legible while they are furthest
                # out, and the paste is deliberately *not* clipped to the overlay
                # window -- the flight is a full-panel move, so it is allowed to
                # cross the bars and the top bar on its way home.
                #
                # The path runs on a smoothstep rather than the reveal's own decay:
                # it leaves the gate unhurried, hangs a beat in mid-panel and then
                # glides into place, which is what makes the flight read as a slow
                # non-linear drift instead of a snap. The opacity rides the same
                # curve, so the shard blooms exactly as it slows down.
                p = u * u * (3.0 - 2.0 * u)
                a = int(base * (LYRIC_GEOM_FLOOR + (1.0 - LYRIC_GEOM_FLOOR) * p))
                if a <= 0:
                    continue
                ease = 1.0 - p
                w = lay.width
                for bi in range(3):
                    s0 = int(round(w * bi / 3.0))
                    s1 = max(s0 + 1, int(round(w * (bi + 1) / 3.0)))
                    strip = lay.crop((s0, 0, s1, lay.height))
                    strip.putalpha(strip.getchannel("A").point(_alpha_lut(a)))
                    jx, jy = _shatter(txt, k, bi, px + s0, py)
                    img.paste(strip, (px + s0 + int(round(jx * ease)),
                                      py + int(round(jy * ease))), strip)
                continue
            a = int(base * u)
            if a <= 0:
                continue
            one = lay.copy()
            one.putalpha(one.getchannel("A").point(_alpha_lut(a)))
            _paste_overlay(img, one, px, py)

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
        self._board = v.board
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

        if c.mode == "spectrum":
            # the bars are the only thing in this mode that is not baked
            self._spectrum_bars(img, d)
            # the lyric rides on top of them, and only when it is on
            if c.lyrics_enable and self.lyric_visible:
                self._lyric(img, d)

        # the pools fill next; every readout is then drawn on top of them
        for key, pct, theme in (("cpu", self.bar["cpu"], cpu_col),
                                ("gpu", self.bar["gpu"], gpu_col),
                                ("mem", self.v.mem, mem_col)):
            g = self.cards.get(key)
            if g is not None:
                self._water(img, key, g, pct, theme)

        for key, pct in (("cpu", self.v.cpu), ("gpu", self.v.gpu)):
            if pct > 80 and key in self.cards:
                pulse = 0.5 + 0.5 * math.sin(self.time * 3.0)
                col = ACCENT_RED if pct > 92 else ACCENT_AMBER
                a = int((pct - 80) / 20 * 55 * (0.6 + 0.4 * pulse))
                g, ins = self.cards[key], self._inset
                d.rounded_rectangle(
                    (g.x + ins, g.y + ins, g.x + g.w - ins, g.y + g.h - ins),
                    max(0, self._radius - ins),
                    outline=col + (min(a, 60),), width=2)

        self._labels(d)
        cpu_g, gpu_g = self.cards.get("cpu"), self.cards.get("gpu")
        if cpu_g is not None:
            self._big_card(d, img, cpu_g, self.num_cpu, self.num_fc, "GHz",
                           f"~{self.v.cpu_power:.0f} W" if c.show_watt else "",
                           TEXT_DIM, self._cpu_short)
            self._chip(d, cpu_g, "CPU", self._cores, TEXT_DIM)
        if gpu_g is not None:
            self._big_card(d, img, gpu_g, self.num_gpu, self.num_fg, "MHz",
                           f"~{self.v.gpu_power:.0f} W" if c.show_watt else "",
                           TEXT_DIM, self._gpu_short)
            self._chip(d, gpu_g, "GPU",
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
        # %I and %H are the same width, so dropping to 12-hour cannot squeeze the
        # rate readout in the middle (that one is ellipsised once it runs out of
        # room, so the clock must never grow)
        fmt = ("%H" if c.clock_24h else "%I") + (":%M:%S" if c.clock_seconds else ":%M")
        self.clock = time.strftime(fmt)
        cw = self._f_clock.getlength(self.clock)
        d.text((TOP_BAR.x + TOP_BAR.w - cw - 14,
                ink_mid(self._f_clock, self.clock, wtop, wbot)),
               self.clock, font=self._f_clock, fill=TEXT)

        # left corner carries the board model as the firmware reports it, e.g.
        # 'Z690M Snow Dream' -- the whole string, not just the chipset. Long ones
        # are ellipsised so the rate readout keeps a floor of room in the middle
        # instead of being squeezed away by the name.
        if (c.top_left or "board").lower() == "host":
            left_txt = self.v.host or "PC"
        else:
            left_txt = self._board or self.v.host or "PC"
        room = TOP_BAR.w - 24 - cw - (64 if c.show_net else 0)
        left_txt = fit(left_txt, self._f_host, room)
        d.text((TOP_BAR.x + 2, ink_mid(self._f_host, left_txt, wtop, wbot)),
               left_txt, font=self._f_host, fill=TEXT)

        if c.show_net:
            hw = self._f_host.getlength(left_txt)
            left = TOP_BAR.x + 6 + hw
            right = TOP_BAR.x + TOP_BAR.w - cw - 18
            net = fit(f"↓{rate(self.v.net_down)}/s ↑{rate(self.v.net_up)}/s",
                      self._f_net, right - left)
            d.text(((left + right - self._f_net.getlength(net)) / 2.0,
                    ink_mid(self._f_net, net, wtop, wbot)),
                   net, font=self._f_net, fill=TEXT_DIM)
        # the power lamp: green while all is well, amber when the spectrum is up
        # but the capture is not. The top bar is already full -- plate name, rate
        # readout, clock -- and the lamp is the only free indicator there, so the
        # fault rides on it instead of a second dot crowding the corner.
        spec_down = (c.mode == "spectrum"
                     and self.spec_state not in ("", "ok", "idle"))
        cx, cy = TOP_BAR.x + TOP_BAR.w - 8, TOP_BAR.y + 8
        a = int(120 + 135 * (0.5 + 0.5 * math.sin(self.time * 2.4)))
        d.ellipse((cx - 3, cy - 3, cx + 3, cy + 3),
                  fill=(ACCENT_AMBER if spec_down else ACCENT_GREEN) + (a,))

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
        # the right-hand readout is optional: an empty string means the panel
        # asked for the card without it, so nothing is drawn at all
        if rhs:
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