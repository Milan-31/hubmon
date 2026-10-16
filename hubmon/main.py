"""hubmon - live PC status dashboard for the jadot V1PRO hub screen.

Usage:
    python -m hubmon.main                # stream to the hub screen
    python -m hubmon.main --preview out  # render PNG previews (no device needed)
    python -m hubmon.main --demo         # synthetic values (for preview)

Every visual and streaming option lives in ``hubmon.config`` and can be edited
live from the bundled web control panel (see ``hubmon.webui``).
"""
from __future__ import annotations

import argparse
import math
import os
import signal
import sys
import time

import numpy as np
import psutil

from . import audio, config, lyrics, transport, webui
from .render import Dashboard, H, W, core_label
from .sensors import Sensors

VENDOR_NAMES = ("佳点V1PRO控制工具", "usb_send_gif")

# dirty-region tile size; patches are only sent where the image actually changed
TILE_W, TILE_H = 16, 10
# if too much of the screen changed, a full frame is cheaper
FULL_THRESHOLD = 0.45
# occasional full frame to guarantee the panel cannot drift out of sync
RESYNC_EVERY = 120.0


def dirty_rects(prev, cur):
    """Rectangles covering every tile that differs between two RGB565 buffers."""
    a = np.frombuffer(prev, np.uint8).reshape(H, W, 2)
    b = np.frombuffer(cur, np.uint8).reshape(H, W, 2)
    tiles = (a != b).any(axis=2).reshape(H // TILE_H, TILE_H,
                                        W // TILE_W, TILE_W).any(axis=(1, 3))
    rects = []
    total = 0
    for ty in range(tiles.shape[0]):
        row = tiles[ty]
        tx = 0
        while tx < row.shape[0]:
            if not row[tx]:
                tx += 1
                continue
            start = tx
            while tx < row.shape[0] and row[tx]:
                tx += 1
            x, w, y = start * TILE_W, (tx - start) * TILE_W, ty * TILE_H
            rects.append((x, y, w, TILE_H))
            total += w * TILE_H * 2
    return rects, total


def vendor_running():
    for p in psutil.process_iter(["name"]):
        n = (p.info.get("name") or "")
        if any(v in n for v in VENDOR_NAMES):
            return n
    return None


class Demo:
    """Synthetic stats so the UI can be previewed without hardware."""

    def __init__(self):
        self.t = 0.0

    def snapshot(self):
        self.t += 0.125
        t = self.t
        cpu = 38 + 26 * math.sin(t * 0.7) + 8 * math.sin(t * 3.1)
        gpu = 55 + 28 * math.sin(t * 0.5 + 1) + 10 * math.sin(t * 2.3)
        # a plausible moving spectrum, so the mode can be previewed without a
        # capture device: a travelling ripple under a bass-heavy tilt
        spec = [max(0.0, min(1.0, (0.35 + 0.6 * abs(math.sin(t * 0.8 + i * 0.30)))
                    * (1.0 - i / 30.0))) for i in range(audio.BANDS)]
        # looped fake lyrics so the overlay previews without a player: a line
        # holds for a few seconds, then the next slides in over the effect
        lines = ["乔迁那日打扫废物", "家居仿似开战",
                 "无意发现当天", "穿返学夏季衬衣"]
        slot = int(t / 3.0)
        j = slot % len(lines)
        return dict(
            spectrum_bands=spec,
            spectrum_peaks=[min(1.0, v + 0.12) for v in spec],
            cpu_util=cpu, gpu_util=gpu, mem_percent=48 + 3 * math.sin(t * 0.3),
            cpu_freq_mhz=2500 + 17 * cpu, gpu_freq_mhz=1500 + 20 * gpu,
            total_power_w=70 + 0.6 * cpu + 0.9 * gpu, cpu_power_w=12 + 0.55 * cpu,
            gpu_power_w=0.9 * gpu, gpu_temp_c=48 + 0.25 * gpu,
            cpu_cores=6, cpu_threads=12,
            net_down_bps=abs(3.2e6 * math.sin(t * 0.8)) + 8e4,
            net_up_bps=abs(4.2e5 * math.sin(t * 1.3)) + 2e4,
            mem_used_gb=16.4, mem_total_gb=34.2, cpu_perf=100 + 0.9 * cpu,
            cpu_name="12th Gen Intel(R) Core(TM) i5-12400F", gpu_name="RTX 5060",
            fg_app="GameViewer.exe", fg_fps=138 + 12 * math.sin(t * 0.9),
            host="MILAN", board_name="Z690M Snow Dream",
            lyric_text=lines[j], lyric_prev=lines[j - 1],
            lyric_t=min(1.0, (t - slot * 3.0) / 0.4),
            lyric_dur=lyrics.LYRIC_CHAR + lyrics.LYRIC_STAGGER * (len(lines[j]) - 1),
            lyric_effect="geom" if slot % 2 else "fade", lyric_visible=True)


def run_preview(cfg, outdir, frames, fps):
    os.makedirs(outdir, exist_ok=True)
    src = Demo()
    dash = Dashboard(cfg)
    dt = 1.0 / max(1.0, fps)
    saved = []
    for i in range(frames):
        dash.update(src.snapshot())
        img = dash.render(dt)
        if i % max(1, frames // 6) == 0 or i == frames - 1:
            p = os.path.join(outdir, f"frame_{i:04d}.png")
            img.resize((W * 2, H * 2), 0).save(p)
            saved.append(p)
    print("preview frames:", ", ".join(saved))


def stats_provider(dash, state):
    """Build the panel's stats callback around a live dashboard and state dict.

    Kept as its own function so the shape of ``/api/stats`` -- including the
    spectrum band array the utilisation check reads -- can be asserted without
    standing up the whole streaming loop.
    """
    def provider():
        v = dash.v
        d = dict(state)
        d.update(cpu=round(v.cpu, 1), gpu=round(v.gpu, 1), mem=round(v.mem, 1),
                 freq_cpu=round(v.freq_cpu), freq_gpu=round(v.freq_gpu),
                 temp_gpu=round(v.temp_gpu), power=round(v.power),
                 cores=core_label(v.cpu_cores, v.cpu_threads),
                 net_down=round(v.net_down), net_up=round(v.net_up),
                 cpu_power=round(v.cpu_power), gpu_power=round(v.gpu_power),
                 fg_app=v.fg_app, fg_fps=round(v.fg_fps, 1), host=v.host,
                 spectrum_bands=list(dash.spectrum_bands),
                 spectrum_peaks=list(dash.spectrum_peaks))
        d["lyric_text"] = dash.lyric_text
        d["lyric_visible"] = dash.lyric_visible
        d["lyric_effect"] = dash.lyric_effect
        d["lyric_dur"] = dash.lyric_dur
        return d
    return provider


def run_stream(cfg, demo=False, log_every=5.0):
    sensor = Demo() if demo else Sensors(presentmon_path=cfg.presentmon_path)
    dash = Dashboard(cfg)

    state = {"connected": False, "out_fps": 0.0, "kb_s": 0.0,
             "patches": 0, "frames": 0, "hub": {}}

    provider = stats_provider(dash, state)

    if not demo:
        sensor.start()
    spec = audio.Spectrum(cfg.spectrum_device, cfg.spectrum_tilt, cfg.spectrum_smoothing)
    watcher = lyrics.MediaWatcher()
    track = {"song": "", "lines": []}      # fetch once per track, not per frame
    provider_lyrics = lyrics.Lyrics()
    lstate = lyrics.LyricState()
    if not demo:
        watcher.start()
    screen = transport.LiveScreen()

    try:
        screen.open()
        ack = screen.ping()
        print(f"[hubmon] device open, heartbeat={ack!r}")
    except Exception as e:
        if not demo:
            sensor.stop()
        print(f"[hubmon] cannot open device: {e}")
        print("[hubmon] make sure the hub is plugged in and the vendor tool is closed")
        return 2

    def _mark_connected():
        """Note that the panel is up, along with what the panel calls itself."""
        state["connected"] = True
        state["hub"] = dict(screen.identity)

    _mark_connected()

    stop = {"v": False}

    def _sig(signum, frame):
        stop["v"] = True

    panel = None
    try:
        panel = webui.start(cfg, provider, port=cfg.ui_port,
                            open_browser=cfg.open_browser,
                            device_list=audio.output_device_names,
                            on_quit=lambda: stop.__setitem__("v", True))
        print(f"[hubmon] control panel: http://127.0.0.1:{panel.actual_port}/")
    except Exception as e:
        print(f"[hubmon] control panel unavailable: {e}")

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    last = time.perf_counter()
    last_log = last
    last_full = 0.0
    prev = None
    sent = 0
    patches = 0
    fails = 0
    sent_at_log = 0
    kb_at_log = 0.0
    bytes_at_log = 0.0
    try:
        while not stop["v"]:
            now = time.perf_counter()
            dt = max(0.001, min(0.5, now - last))
            last = now
            dash.apply_config(cfg)         # pick up live panel edits
            if cfg.spectrum_device != spec.device:
                spec.stop()               # the panel changed the capture device
                spec = audio.Spectrum(cfg.spectrum_device, cfg.spectrum_tilt,
                                      cfg.spectrum_smoothing)
            # the analyser's two knobs are live values: retuning them costs a
            # float compare, so the panel's sliders respond without a restart
            spec.tune(cfg.spectrum_tilt, cfg.spectrum_smoothing)
            want_spec = (cfg.mode == "spectrum")
            if want_spec and not demo:
                spec.start()
            elif not want_spec:
                spec.stop()
            state["spec_state"] = spec.state
            try:
                stats = sensor.snapshot()
                if want_spec and not demo:
                    bands, peaks = spec.levels(dt)
                    stats["spectrum_bands"] = bands
                    stats["spectrum_peaks"] = peaks
                stats["spec_state"] = spec.state
                if want_spec and cfg.lyrics_enable and not demo:
                    snap = watcher.snapshot()
                    song = "%s|%s" % (snap["title"], snap["artist"])
                    if song != track["song"]:          # new track: look up once
                        track["song"] = song
                        track["lines"] = provider_lyrics.lines_for(
                            snap["title"], snap["artist"], snap["album"],
                            snap["duration_s"], snap["song_id"])
                    lst = lstate.update(cfg, snap, track["lines"], dt)
                    stats["lyric_text"] = lst["text"]
                    stats["lyric_prev"] = lst["prev"]
                    stats["lyric_t"] = lst["t"]
                    stats["lyric_effect"] = lst["effect"]
                    stats["lyric_visible"] = lst["visible"]
                    stats["lyric_dur"] = lst["dur"]
                dash.update(stats)
                img = dash.render(dt)
                cur = dash.to_rgb565_be(img)

                rects, nbytes = ((None, transport.FRAME_BYTES) if prev is None
                                 else dirty_rects(prev, cur))
                resync = (now - last_full) >= RESYNC_EVERY
                whole = (not cfg.partial or prev is None or resync
                         or nbytes > transport.FRAME_BYTES * FULL_THRESHOLD)
                if whole:
                    screen.send_frame(cur)
                    last_full = now
                    patches = 0
                else:
                    arr = np.frombuffer(cur, np.uint8).reshape(H, W, 2)
                    for (x, y, w, h) in rects:
                        screen.send_region(x, y, w, h,
                                           arr[y:y + h, x:x + w].tobytes())
                        patches += 1
                moved = transport.FRAME_BYTES if whole else nbytes
                kb_at_log += moved / 1024
                inst_fps = 1.0 / dt
                state["out_fps"] = inst_fps if not sent else (
                    state["out_fps"] * 0.8 + inst_fps * 0.2)
                state["kb_s"] = state["kb_s"] * 0.8 + (moved / 1024 / dt) * 0.2
                state["patches"] = patches
                state["frames"] = sent + 1
                prev = cur
                sent += 1
                fails = 0
            except transport.DeviceNotFound:
                prev = None
                state["connected"] = False
                if screen.ensure_connected():
                    _mark_connected()
                    continue
                time.sleep(1.0)
                continue
            except Exception as e:
                fails += 1
                prev = None
                state["connected"] = False
                screen.close()
                if fails in (1, 5):
                    print(f"[hubmon] usb error ({e}); reconnecting ...")
                if screen.ensure_connected(retries=1):
                    _mark_connected()
                    continue
                time.sleep(1.5)
                continue

            if now - last_log >= log_every:
                el = now - last_log
                rate = (sent - sent_at_log) / el
                sent_at_log = sent
                kb = (kb_at_log - bytes_at_log) / el
                bytes_at_log = kb_at_log
                print(f"[hubmon] {rate:5.1f} fps  {kb:6.1f} KiB/s  "
                      f"cpu={dash.v.cpu:5.1f}%  gpu={dash.v.gpu:5.1f}%  "
                      f"mem={dash.v.mem:4.1f}%  pwr={dash.v.power:.0f}W  "
                      f"patches={patches}  frames={sent}", flush=True)
                last_log = now

            fps = cfg.fps
            if fps and fps > 0:
                slack = 1.0 / fps - (time.perf_counter() - now)
                if slack > 0:
                    time.sleep(slack)
    finally:
        if not demo:
            sensor.stop()
        spec.stop()
        watcher.stop()
        screen.close()
        webui.stop(panel)
        print(f"[hubmon] stopped after {sent} frames, "
              f"{screen.regions_sent} patches")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hubmon")
    ap.add_argument("--fps", type=float, default=None,
                    help="override the frame-rate cap (0 = uncapped)")
    ap.add_argument("--demo", action="store_true", help="synthetic values")
    ap.add_argument("--full", action="store_true",
                    help="always send whole frames (no partial updates)")
    ap.add_argument("--preview", metavar="DIR", help="render PNGs instead of streaming")
    ap.add_argument("--frames", type=int, default=120)
    ap.add_argument("--port", type=int, default=None, help="control-panel port")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not open the control panel automatically")
    ap.add_argument("--force", action="store_true",
                    help="stream even if a vendor tool runs")
    args = ap.parse_args(argv)

    cfg = config.load()
    if args.fps is not None:
        cfg.fps = args.fps
    if args.full:
        cfg.partial = False
    if args.port is not None:
        cfg.ui_port = args.port
    if args.no_browser:
        cfg.open_browser = False

    if args.preview:
        run_preview(cfg, args.preview, args.frames, cfg.fps or 30.0)
        return 0

    v = vendor_running()
    if v and not args.force:
        print(f"[hubmon] '{v}' is running and owns the screen; close it first "
              f"(or pass --force).")
        return 3
    return run_stream(cfg, args.demo)


if __name__ == "__main__":
    sys.exit(main())