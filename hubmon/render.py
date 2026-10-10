"""Apple-style glassmorphism dashboard renderer for a 320x170 RGB565 panel.

Design notes
------------
* The panel is physically tiny, so legibility beats density: large tabular
  numerals, horizontal meters instead of rings (thin arcs smear after RGB565
  quantisation).
* Static chrome (backdrop, glass cards, labels, meter tracks) is baked once;
  each frame redraws only what moves.
* The backdrop is *animated* (drifting bokeh motes + a soft bloom), but every
  element touches only its own small area, so the transport keeps sending cheap
  partial updates instead of whole frames.
* Colour carries meaning: each card's accent shifts with its load, and the
  whole-panel bloom + power meter follow a dedicated power ramp.
* Numbers roll into place like a slot reel when they change (the panel now runs
  at tens of frames per second, so the motion reads properly).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

W, H = 320, 170

# ---------------------------------------------------------------- palette ----
BG_TOP = (10, 14, 22)
BG_BOTTOM = (16, 21, 34)
TEXT = (244, 247, 252)
TEXT_DIM = (150, 160, 176)
TEXT_FAINT = (104, 114, 130)

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


# ------------------------------------------------------------ motion layer ---
def _radial_mask(r):
    d = np.arange(-r, r + 1, dtype=np.float32)
    yy, xx = np.meshgrid(d, d, indexing="ij")
    a = np.clip(1.0 - np.sqrt(xx * xx + yy * yy) / max(1.0, r), 0.0, 1.0) ** 2
    return a


_MASKS: dict = {}


def _mask(r):
    m = _MASKS.get(r)
    if m is None:
        m = _radial_mask(r)
        _MASKS[r] = m
    return m


def glow(img, x, y, r, color, strength):
    """Paste a soft radial halo of ``color`` centred on (x, y)."""
    if r <= 0 or strength <= 1:
        return
    m = Image.fromarray((_mask(r) * (strength / 255.0)).astype(np.uint8), "L")
    blob = Image.new("RGBA", (2 * r + 1, 2 * r + 1), tuple(color) + (255,))
    img.paste(blob, (int(x) - r, int(y) - r), m)


class Motion:
    """Drifting bokeh motes plus a soft bloom, composited over the panel.

    Seeded so the pattern is deterministic; each mote is only a few pixels wide
    which keeps the per-frame dirty area (and therefore the USB traffic) small.
    """

    def __init__(self, cfg):
        rng = np.random.default_rng(11)
        n = max(0, int(cfg.particles))
        self.motes = []
        for i in range(n):
            self.motes.append(dict(
                x=float(rng.uniform(0, W)), y=float(rng.uniform(0, H)),
                r=int(rng.integers(3, 8)),
                vx=float(rng.uniform(-7, 7)), vy=float(rng.uniform(-10, -3)),
                phase=float(rng.uniform(0, 6.28)),
                freq=float(rng.uniform(0.3, 0.9)),
                cat=i % 3,          # 0 = CPU hue, 1 = GPU hue, 2 = neutral
                glow=float(rng.uniform(22, 46))))
        self.blooms = []
        for i in range(max(0, int(cfg.blooms))):
            self.blooms.append(dict(
                x=float(rng.uniform(40, W - 40)), y=float(rng.uniform(30, H - 30)),
                r=int(rng.integers(15, 21)),
                vx=float(rng.uniform(-6, 6)), vy=float(rng.uniform(-4, 4)),
                glow=float(rng.uniform(30, 44))))
        self.t = 0.0

    def _blob(self, img, x, y, r, color, strength):
        glow(img, x, y, r, color, strength)

    def step(self, dt, speed):
        self.t += dt
        s = max(0.0, speed)
        for m in self.motes:
            m["x"] += m["vx"] * dt * s
            m["y"] += m["vy"] * dt * s
            if m["y"] < -12:
                m["y"] = H + 12
            if m["x"] < -14:
                m["x"] = W + 14
            elif m["x"] > W + 14:
                m["x"] = -14
        for b in self.blooms:
            b["x"] += b["vx"] * dt * s
            b["y"] += b["vy"] * dt * s
            if b["x"] < 20 or b["x"] > W - 20:
                b["vx"] = -b["vx"]
            if b["y"] < 20 or b["y"] > H - 20:
                b["vy"] = -b["vy"]

    def draw(self, img, cpu_col, gpu_col, power_col, dim=1.0):
        for b in self.blooms:
            self._blob(img, b["x"], b["y"], b["r"], power_col, b["glow"] * dim)
        for m in self.motes:
            a = m["glow"] * (0.55 + 0.45 * math.sin(self.t * m["freq"] + m["phase"]))
            base = (232, 238, 248) if m["cat"] == 2 else (cpu_col if m["cat"] == 0
                                                          else gpu_col)
            self._blob(img, m["x"], m["y"], m["r"],
                       mix(base, (255, 255, 255), 0.25), a * dim)


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
    temp_cpu: float = 0.0
    cpu_temp_src: str = ""
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
        self.motion = Motion(self.cfg)

        self._f_lbl = F(11, "semibold")
        self._f_host = F(11, "semibold")
        self._f_big = F(30, "bold")
        self._f_mid = F(24, "bold")
        self._f_freq = F(17, "semibold")
        self._f_sub = F(11, "semibold")
        self._f_unit = F(10, "regular")
        self._f_app = F(10, "semibold")
        self._f_clock = F(13, "semibold")

        self.num_cpu = RollText(self._f_big, TEXT, ACCENT_CYAN)
        self.num_gpu = RollText(self._f_big, TEXT, ACCENT_VIOLET)
        self.num_fc = RollText(self._f_freq, TEXT, ACCENT_CYAN)
        self.num_fg = RollText(self._f_freq, TEXT, ACCENT_VIOLET)
        self.num_mem = RollText(self._f_mid, TEXT, ACCENT_CYAN)
        self.num_pwr = RollText(self._f_mid, TEXT, ACCENT_AMBER)
        self.num_fps = RollText(self._f_mid, TEXT, ACCENT_GREEN)
        self._nums = (self.num_cpu, self.num_gpu, self.num_fc, self.num_fg,
                      self.num_mem, self.num_pwr, self.num_fps)
        self._accent_rolls = {self.num_cpu: "accent_cpu", self.num_gpu: "accent_gpu",
                              self.num_fc: "accent_cpu", self.num_fg: "accent_gpu",
                              self.num_mem: "accent_mem", self.num_pwr: "accent_pwr"}
        self._layout()
        self.apply_config(self.cfg, force=True)

    # ------------------------------------------------------------------ config
    def _look_sig(self):
        c = self.cfg
        return (c.accent_cpu, c.accent_gpu, c.accent_mem, c.accent_pwr,
                round(c.brightness, 3), round(c.bg_brightness, 3),
                round(c.saturation, 3), round(c.glass, 3),
                c.show_mem, c.show_power, c.show_fps)

    def apply_config(self, cfg, force=False):
        self.cfg = cfg
        for r, key in self._accent_rolls.items():
            r.accent = hex_rgb(getattr(cfg, key), r.accent)
            r.speed = cfg.roll_speed
            r.enabled = cfg.roll
        for r in self._nums:
            r.speed = cfg.roll_speed
            r.enabled = cfg.roll
        sig = self._look_sig()
        if force or sig != self._sig:
            self._sig = sig
            self._layout()
            self._static = None            # chrome must be re-baked
        self.motion = Motion(cfg) if (force or len(self.motion.motes) != int(cfg.particles)
                                      or len(self.motion.blooms) != int(cfg.blooms)) else self.motion

    def _layout(self):
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
        self.bar_top = 57 if self.big_h <= 90 else 112

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
        t.temp_cpu = stats.get("cpu_temp_c", 0.0)
        t.cpu_temp_src = stats.get("cpu_temp_src", "")
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

    # ---------------------------------------------------------------- static
    def _background(self):
        ys = np.linspace(0, 1, H)[:, None, None]
        px = np.empty((H, W, 3), np.float32)
        px[:] = (np.array(BG_TOP, np.float32) * (1 - ys)
                 + np.array(BG_BOTTOM, np.float32) * ys)
        yy, xx = np.mgrid[0:H, 0:W]
        for (gx, gy, gr, col, amt) in (
            (26, 26, 78, ACCENT_CYAN, 0.62),
            (296, 26, 78, ACCENT_VIOLET, 0.58),
            (48, 150, 66, ACCENT_VIOLET, 0.52),
            (272, 152, 66, ACCENT_CYAN, 0.54),
            (160, 106, 96, ACCENT_GREEN, 0.22),
        ):
            d = np.sqrt((xx - gx) ** 2 + (yy - gy) ** 2)
            px += (np.clip(1 - d / gr, 0, 1) ** 2 * amt)[:, :, None] * np.array(col, np.float32)
        r = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)
        px *= np.clip(1.04 - 0.10 * r, 0, 1.2)[:, :, None]
        px += np.random.default_rng(7).normal(0.0, 3.0, (H, W, 1)).astype(np.float32)
        px += (((xx % 16 == 0) | (yy % 16 == 0)).astype(np.float32) * 4.0)[:, :, None]
        return Image.fromarray(np.clip(px, 0, 255).astype(np.uint8), "RGB")

    def _vgrad(self, top_a, bot_a):
        col = np.linspace(top_a, bot_a, H).astype(np.uint8)[:, None]
        return Image.fromarray(np.repeat(col, W, axis=1), "L")

    def _round_mask(self, g: Geo, radius, inset=0):
        m = Image.new("L", (W, H), 0)
        ImageDraw.Draw(m).rounded_rectangle(
            (g.x + inset, g.y + inset, g.x + g.w - inset, g.y + g.h - inset),
            max(1, radius - inset), fill=255)
        return m

    def _edge_layer(self, g: Geo, radius, inset, top_a, bot_a):
        ring = Image.new("L", (W, H), 0)
        ImageDraw.Draw(ring).rounded_rectangle(
            (g.x + inset, g.y + inset, g.x + g.w - inset, g.y + g.h - inset),
            max(1, radius - inset), outline=255, width=1)
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
        back = ImageEnhance.Brightness(self._background()).enhance(
            max(0.0, self.cfg.bg_brightness))
        enh = ImageEnhance.Color(back.filter(ImageFilter.GaussianBlur(10))
                                 ).enhance(1.30 * self.cfg.saturation)
        blur = ImageEnhance.Brightness(enh).enhance(self.cfg.brightness)
        img = ImageEnhance.Brightness(back).enhance(self.cfg.brightness)
        for g in self.cards.values():
            self._card(blur, img, g, 16)

        d = ImageDraw.Draw(img, "RGBA")
        for key in ("cpu", "gpu"):
            x0, x1, my = self._meter(self.cards[key])
            d.rounded_rectangle((x0, my, x1, my + 1), 1, fill=(255, 255, 255, 28))

        a = self.cards["cpu"]
        b = self.cards["gpu"]
        d.text((a.x + PAD, a.y + 5), "CPU", font=self._f_lbl,
               fill=hex_rgb(self.cfg.accent_cpu, ACCENT_CYAN))
        d.text((b.x + PAD, b.y + 5), "GPU", font=self._f_lbl,
               fill=hex_rgb(self.cfg.accent_gpu, ACCENT_VIOLET))
        for key, label, accent in BOTTOM:
            if key not in self.cards:
                continue
            col = hex_rgb(getattr(self.cfg, accent), TEXT_DIM) if accent else TEXT_DIM
            d.text((self.cards[key].x + PAD, self.cards[key].y + 5), label,
                   font=self._f_lbl, fill=col)
        self._static = img

    def _card(self, blur, img, g: Geo, radius):
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
                                             fill=(255, 255, 255, int(8 * glass)))
        ov.paste((255, 255, 255, 255), (0, 0),
                 ImageChops.multiply(mask, self._vgrad(int(10 * glass), 0)))
        ov.paste((0, 0, 0, 255), (0, 0),
                 ImageChops.multiply(mask, self._vgrad(0, 48)))
        img.paste(Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB"), (0, 0))

        self._sheen(img, g, radius, 16 * glass)
        for inset, top_a, bot_a in ((0, 150, 34), (1, 64, 16)):
            img.paste(Image.alpha_composite(
                img.convert("RGBA"),
                self._edge_layer(g, radius, inset, top_a, bot_a)).convert("RGB"), (0, 0))

    # ------------------------------------------------------------- animation
    def step(self, dt):
        c = self.cfg
        self.time += dt
        self.motion.step(dt, c.bg_speed if c.bg_motion else 0.0)
        k = 1 - math.exp(-dt * 4.0)
        for f in ("cpu", "gpu", "mem", "freq_cpu", "freq_gpu", "power", "cpu_power",
                  "gpu_power", "temp_gpu", "temp_cpu", "mem_used", "mem_total",
                  "cpu_perf"):
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
        self.v.cpu_temp_src = self.tgt.cpu_temp_src

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

    # ---------------------------------------------------------------- render
    def render(self, dt):
        if self._static is None:
            self._build_static()
        self.step(dt)
        img = self._static.copy()
        c = self.cfg
        cpu_col = hex_rgb(c.accent_cpu, ACCENT_CYAN)
        gpu_col = hex_rgb(c.accent_gpu, ACCENT_VIOLET)
        pwr_col = ramp(POWER_STOPS, self.v.power)
        cpu_load = ramp([(0, cpu_col), (80, cpu_col), (92, ACCENT_AMBER),
                         (100, ACCENT_RED)], self.v.cpu)
        gpu_load = ramp([(0, gpu_col), (80, gpu_col), (92, ACCENT_AMBER),
                         (100, ACCENT_RED)], self.v.gpu)

        if c.bg_motion:
            self.motion.draw(img, cpu_load, gpu_load, pwr_col,
                             dim=max(0.0, c.bg_brightness))

        d = ImageDraw.Draw(img, "RGBA")

        for key, pct in (("cpu", self.v.cpu), ("gpu", self.v.gpu)):
            if pct > 80:
                pulse = 0.5 + 0.5 * math.sin(self.time * 3.0)
                col = ACCENT_RED if pct > 92 else ACCENT_AMBER
                a = int((pct - 80) / 20 * 55 * (0.6 + 0.4 * pulse))
                d.rounded_rectangle(self.cards[key].box, 16,
                                    outline=col + (min(a, 60),), width=2)

        self._big_card(d, img, self.cards["cpu"], self.num_cpu, self.num_fc,
                       "GHz", f"~{self.v.cpu_power:.0f} W", TEXT_DIM)
        self._meter_line(d, img, self.cards["cpu"], self.bar["cpu"], cpu_col)
        self._temp_chip(d, self.cards["cpu"], "CPU", self.v.temp_cpu)
        self._big_card(d, img, self.cards["gpu"], self.num_gpu, self.num_fg,
                       "MHz", f"~{self.v.gpu_power:.0f} W", TEXT_DIM)
        self._meter_line(d, img, self.cards["gpu"], self.bar["gpu"], gpu_col)
        self._temp_chip(d, self.cards["gpu"], "GPU", self.v.temp_gpu)

        if "mem" in self.cards:
            g = self.cards["mem"]
            self.num_mem.draw(img, g.x + PAD, g.y + 22)
            sub = f"{self.v.mem_used:.0f}/{self.v.mem_total:.0f}G"
            d.text((g.x + g.w - PAD - self._f_unit.getlength(sub), g.y + 6), sub,
                   font=self._f_unit, fill=TEXT_FAINT)

        if "power" in self.cards:
            g = self.cards["power"]
            self.num_pwr.draw(img, g.x + PAD, g.y + 22)
            d.text((g.x + PAD + self.num_pwr.width() + 3, g.y + 27), "W",
                   font=self._f_unit, fill=pwr_col)

        if "fps" in self.cards:
            g = self.cards["fps"]
            self.num_fps.draw(img, g.x + PAD, g.y + 22)
            app = fit(self.v.fg_app or "—", self._f_app, g.w - PAD * 2)
            d.text((g.x + PAD, g.y + 44), app, font=self._f_app, fill=TEXT_FAINT)

        d.text((TOP_BAR.x + 2, TOP_BAR.y + 2), self.v.host or "PC", font=self._f_host,
               fill=TEXT)
        self.clock = time.strftime("%H:%M:%S")
        cw = self._f_clock.getlength(self.clock)
        d.text((TOP_BAR.x + TOP_BAR.w - cw - 14, TOP_BAR.y + 1), self.clock,
               font=self._f_clock, fill=TEXT)

        hw = self._f_host.getlength(self.v.host or "PC")
        left = TOP_BAR.x + 6 + hw
        right = TOP_BAR.x + TOP_BAR.w - cw - 18
        net = fit(f"↓{rate(self.v.net_down)}/s ↑{rate(self.v.net_up)}/s",
                  self._f_lbl, right - left)
        d.text(((left + right - self._f_lbl.getlength(net)) / 2.0, TOP_BAR.y + 2),
               net, font=self._f_lbl, fill=TEXT_DIM)
        cx, cy = TOP_BAR.x + TOP_BAR.w - 8, TOP_BAR.y + 8
        a = int(120 + 135 * (0.5 + 0.5 * math.sin(self.time * 2.4)))
        d.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), fill=ACCENT_GREEN + (a,))

        sw = 60
        sx = int((self.time * 55) % (TOP_BAR.w + sw * 2)) - sw
        shine = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(shine).polygon(
            [(sx, TOP_BAR.y + 2), (sx + 18, TOP_BAR.y + 2),
             (sx - 8, TOP_BAR.y + TOP_BAR.h - 2), (sx - 26, TOP_BAR.y + TOP_BAR.h - 2)],
            fill=(255, 255, 255, 18))
        shine = shine.filter(ImageFilter.GaussianBlur(4))
        top_mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(top_mask).rectangle(TOP_BAR.box, fill=255)
        img.paste(Image.alpha_composite(img.convert("RGBA"), shine).convert("RGB"),
                  (0, 0), top_mask)

        return img

    def _meter(self, g: Geo):
        """(x0, x1, y) of the hairline load meter inside card ``g``."""
        return (g.x + PAD, g.x + g.w - PAD, g.y + self.bar_top + 4)

    def _meter_line(self, d, img, g: Geo, pct, theme):
        """Minimal hairline meter capped by a single white pixel."""
        x0, x1, my = self._meter(g)
        pct = max(0.0, min(100.0, pct))
        tx = x0 + (x1 - x0) * pct / 100.0
        if pct > 0.5:
            d.rounded_rectangle((x0, my, tx, my + 1), 1, fill=theme + (255,))
        d.point((int(tx), my), fill=(255, 255, 255, 255))

    def _temp_chip(self, d, g: Geo, label, temp):
        """Instant temperature, sitting just right of the card's label."""
        x = g.x + PAD + self._f_lbl.getlength(label) + 5
        d.text((x, g.y + 5), f"{temp:.0f}°C", font=self._f_lbl,
               fill=ramp(TEMPERATURE_STOPS, temp))

    def _big_card(self, d, img, g: Geo, num_pct, freq_num, unit, rhs, rhs_col):
        uw = self._f_unit.getlength(unit)
        vw = freq_num.width()
        fx = g.x + g.w - PAD - (vw + 3 + uw)
        freq_num.draw(img, fx, g.y + 4)
        d.text((fx + vw + 3, g.y + 9), unit, font=self._f_unit, fill=TEXT_FAINT)

        num_pct.draw(img, g.x + PAD, g.y + 18)
        d.text((g.x + PAD + num_pct.width() + 3, g.y + 34), "%", font=self._f_sub,
               fill=TEXT_FAINT)
        d.text((g.x + g.w - PAD - self._f_sub.getlength(rhs), g.y + 34), rhs,
               font=self._f_sub, fill=rhs_col)

    # ------------------------------------------------------------------ util
    def to_rgb565_be(self, img):
        arr = np.asarray(img, dtype=np.uint16)
        r = (arr[:, :, 0] >> 3) & 0x1F
        g = (arr[:, :, 1] >> 2) & 0x3F
        b = (arr[:, :, 2] >> 3) & 0x1F
        v = (r << 11) | (g << 5) | b
        return v.astype(">u2").tobytes()