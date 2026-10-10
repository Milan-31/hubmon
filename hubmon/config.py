"""User-adjustable settings, shared live between the render loop and the panel.

The monitor reads this object every frame, so the control panel can change any
option at runtime. Persisted as JSON under %APPDATA%\\hubmon (or ~/.hubmon).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields


def _config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "hubmon")


CONFIG_PATH = os.path.join(_config_dir(), "config.json")


@dataclass
class Config:
    # ---- streaming ---------------------------------------------------------
    fps: float = 60.0            # 0 = as fast as the link allows
    partial: bool = True         # send only changed regions
    text_period: float = 1.5     # seconds between value refreshes
    fps_period: float = 0.5      # seconds between frame-rate refreshes

    # ---- text motion -------------------------------------------------------
    roll: bool = True            # glyph slot-roll when a value changes
    roll_speed: float = 3.2      # rolls per second

    # ---- backdrop ----------------------------------------------------------
    bg_color_top: str = "#0a0e16"    # three-stop vertical gradient, user-picked
    bg_color_mid: str = "#0d121c"
    bg_color_bot: str = "#101522"
    bg_image: str = ""           # custom backdrop image; "" = the gradient above
    bg_image_blur: float = 8.0   # Gaussian blur radius applied to that image, px
    bg_image_brightness: float = 1.0
    bg_image_zoom: float = 1.0   # 1 = cover, up to 4 = cropped in
    bg_image_offset_x: float = 0.0   # -1..1 pan of the crop window
    bg_image_offset_y: float = 0.0

    # ---- look --------------------------------------------------------------
    brightness: float = 1.0
    bg_brightness: float = 0.6   # backdrop dimmer, independent of UI brightness
    saturation: float = 1.0
    glass: float = 1.0           # glass tint strength
    accent_cpu: str = "#38bdf8"
    accent_gpu: str = "#a78bfa"
    accent_mem: str = "#22d3ee"
    accent_pwr: str = "#fbbf24"
    show_mem: bool = True
    show_power: bool = True
    show_fps: bool = True

    # ---- panel -------------------------------------------------------------
    ui_port: int = 8756
    open_browser: bool = True

    # ---- external tools ----------------------------------------------------
    presentmon_path: str = ""    # PresentMon.exe for the FPS readout; "" = auto

    # ------------------------------------------------------------------ io
    def save(self, path: str | None = None):
        path = path or CONFIG_PATH
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, indent=2)
        except Exception:
            pass

    def update_from(self, data: dict):
        """Merge a partial dict, ignoring unknown keys and bad types."""
        types = {f.name: f.type for f in fields(self)}
        for k, v in (data or {}).items():
            if k not in types:
                continue
            cur = getattr(self, k)
            try:
                if isinstance(cur, bool):
                    v = bool(v)
                elif isinstance(cur, (int, float)):
                    v = type(cur)(v)
                elif not isinstance(v, str):
                    continue
            except (TypeError, ValueError):
                continue
            setattr(self, k, v)

    def to_dict(self):
        return asdict(self)


def load(path: str | None = None) -> Config:
    cfg = Config()
    path = path or CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg.update_from(json.load(f))
    except Exception:
        pass
    return cfg