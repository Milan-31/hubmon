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
import multiprocessing
import os
import queue
import signal
import sys
import time

import numpy as np
import psutil

from . import audio, config, lyrics, sensorproc, transport, usbsend, webui
from .render import Dashboard, H, W, core_label

VENDOR_NAMES = ("佳点V1PRO控制工具", "usb_send_gif")


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
        self._cover = self._make_cover()

    @staticmethod
    def _make_cover():
        """A fake album sleeve, so the vinyl scene previews without a player."""
        try:
            import io
            from PIL import Image, ImageDraw
            im = Image.new("RGB", (128, 128), (24, 34, 62))
            d = ImageDraw.Draw(im)
            for i in range(-128, 256, 16):
                d.polygon(((i, 128), (i + 34, 128), (i + 34 + 90, 0),
                           (i + 90, 0)), fill=(44, 96, 168))
            d.ellipse((34, 30, 94, 90), fill=(226, 120, 72))
            d.ellipse((52, 48, 76, 72), fill=(24, 34, 62))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=88)
            return buf.getvalue()
        except Exception:
            return b""

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
            lyric_next=lines[(j + 1) % len(lines)],
            lyric_t=min(1.0, (t - slot * 3.0) / 0.4),
            lyric_scroll=min(1.0, (t - slot * 3.0) / lyrics.LYRIC_SCROLL),
            lyric_dur=lyrics.LYRIC_CHAR + lyrics.LYRIC_STAGGER * (len(lines[j]) - 1),
            lyric_effect="geom" if slot % 2 else "fade", lyric_visible=True,
            lyric_song="demo|artist", cover=self._cover)


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


def _err_text(e):
    """Readable text for an exception that may carry no message at all.

    ``multiprocessing`` reports a broken pipe back to the parent as a bare
    ``EOFError`` whose ``str`` is empty; storing that would make the state
    tests see a failed link as a healthy one (empty is falsy).
    """
    return str(e) or type(e).__name__


class _Sampler:
    """Owns the sensor sampler in a separate process (see ``hubmon.sensorproc``).

    The sampler's ``nvidia-smi`` spawns and pipe readers cost the render loop
    a slice of GIL every half second; isolated here they cannot disturb the
    frame cadence.
    """

    def __init__(self, presentmon_path=""):
        self._path = presentmon_path
        self.data = {}
        self.error = None
        self._spawn()

    def _spawn(self):
        ctx = multiprocessing.get_context("spawn")
        self.q = ctx.Queue(maxsize=4)
        self.halt = ctx.Event()
        self.proc = ctx.Process(target=sensorproc.run,
                                args=(self.q, self.halt, self._path),
                                daemon=True)
        self.proc.start()
        self._last = time.perf_counter()

    def snapshot(self):
        """Latest sample; the loop never waits on the sampler."""
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "stats":
                    self.data = payload
                    self._last = time.perf_counter()
                else:
                    self.error = payload
        except queue.Empty:
            pass
        if not self.proc.is_alive() and time.perf_counter() - self._last > 2.0:
            # a dead sampler would freeze every readout, so take a fresh one
            self.error = self.error or "sampler stopped"
            self._spawn()
        return self.data

    def stop(self):
        self.halt.set()
        self.proc.join(timeout=3.0)
        if self.proc.is_alive():
            self.proc.terminate()
            self.proc.join(timeout=1.0)


class _Sender:
    """Owns the USB transport in a separate process (see ``hubmon.usbsend``).

    A sender thread shared the GIL with PIL and stalled for the renderer's
    several-millisecond draw calls, capping the stream near 55 fps. The child
    process has its own GIL, so the link runs at its own ~12 ms/frame while the
    render loop keeps a steady 60.
    """

    def __init__(self, base=(0, 0)):
        ctx = multiprocessing.get_context("spawn")
        # one buffered frame absorbs a single slow panel frame without pushing
        # commands at the device back-to-back, which makes its writes stall
        self.tasks = ctx.Queue(maxsize=1)
        self.events = ctx.Queue()
        # The worker hands one credit back per frame it finishes. Waiting on it
        # wakes the moment the link is free; polling the queue instead cost a
        # whole millisecond of timer granularity per frame, which capped an
        # otherwise ~2 ms pipeline near 450 fps.
        self.credits = ctx.Semaphore(1)
        self.proc = ctx.Process(target=usbsend.run,
                                args=(self.tasks, self.events, self.credits),
                                daemon=True)
        self.proc.start()
        self._base_frames, self._base_bytes = base
        self.error = None
        self.identity = {}
        self.frames = base[0]
        self.patches = 0
        self.bytes = base[1]
        self.dropped = 0
        self.fps = 0.0
        self.kb_s = 0.0
        self.busy_ms = 0.0
        self.empties = 0
        self.ack = b""
        # the worker publishes stats every 0.5 s even when idle, so the arrival
        # of any message proves the process is alive; silence is the real fault
        # signal (``Process.exitcode`` can briefly read stale/misleading values
        # and used to trigger spurious reconnects mid-stream)
        self._last_msg = time.perf_counter()
        self._open()

    def dead(self):
        """True when the worker has failed or stopped answering."""
        # ``is_alive`` is the reliable signal that the process is gone; the
        # message timer only catches a worker that is still running but wedged,
        # and it has to be generous so a busy machine cannot fake a failure
        return (self.error is not None
                or not self.proc.is_alive()
                or time.perf_counter() - self._last_msg > 8.0)

    def _open(self):
        try:
            self.tasks.put_nowait(("open",))
        except Exception as e:
            self.error = _err_text(e)
            return
        reply = self.pump(timeout=15.0)
        if reply is None:
            self.error = "sender process did not answer"
        elif reply[0] == "error":
            # never leave this empty: an empty string is falsy, so the caller
            # used to treat a failed open as a healthy link and immediately
            # spawn another worker that failed the same way
            self.error = reply[1] or "device open failed"
        else:
            self.ack = reply[1]

    def pump(self, timeout=0.0):
        """Handle worker messages; return an open/error reply when one is due."""
        end = time.perf_counter() + timeout
        while True:
            if timeout:
                remain = end - time.perf_counter()
                if remain <= 0:
                    return None
                try:
                    msg = self.events.get(timeout=remain)
                except queue.Empty:
                    return None
            else:
                try:
                    msg = self.events.get_nowait()
                except queue.Empty:
                    return None
            self._last_msg = time.perf_counter()
            if msg[0] == "stats":
                (_, f, b, self.patches, self.fps, self.kb_s,
                 self.busy_ms, self.empties) = msg
                self.frames = self._base_frames + f
                self.bytes = self._base_bytes + b
            elif msg[0] == "open":
                self.identity = msg[1]
                return ("open", msg[2])
            elif msg[0] == "error":
                self.error = msg[1] or "usb worker failed"
                return ("error", self.error)
            elif msg[0] == "fatal":
                self.error = msg[1].strip().splitlines()[-1]
                return ("error", self.error)

    def ready(self, timeout=0.02):
        """Wait for the worker to be able to take another frame."""
        try:
            return bool(self.credits.acquire(timeout=timeout))
        except Exception as e:
            self.error = _err_text(e)
            return True

    def submit(self, kind, payload, moved, patches):
        """Hand one prepared frame to the worker.

        The conversion, diff and packing are already done; the worker only
        writes the packets, so its process is never in front of the USB write
        doing CPU work of its own.
        """
        try:
            self.tasks.put_nowait((kind, payload, moved, patches))
            return True
        except queue.Full:
            # the caller already holds a credit it must hand back, or the
            # worker would wait for a frame that never arrives
            self.dropped += 1
            self._release()
            return False
        except Exception as e:
            self.error = _err_text(e)
            self._release()
            return False

    def _release(self):
        try:
            self.credits.release()
        except Exception:
            pass

    def close(self):
        for _ in range(2):
            try:
                self.tasks.put_nowait(None)
                break
            except queue.Full:
                try:
                    self.tasks.get_nowait()
                except queue.Empty:
                    pass
            except Exception:
                break
        self.proc.join(timeout=2.0)
        if self.proc.is_alive():
            self.proc.terminate()
            self.proc.join(timeout=1.0)


def run_stream(cfg, demo=False, log_every=5.0):
    # The sampler, the lyric watcher and the panel server are all threads in
    # this process. With the default 5 ms switch interval one of them can hold
    # the GIL right when the frame deadline comes due, which showed up as a
    # steady ~1 ms of lateness and a stream stuck in the high 50s. A shorter
    # slice keeps the handoff inside the frame budget.
    sys.setswitchinterval(0.0005)
    # Windows coalesces timers for background apps, so a 4 ms sleep can come
    # back after 15 ms -- exactly the kind of hitch that drops a 60 fps stream
    # into the 50s. Ask for 1 ms timer resolution while we stream.
    winmm = None
    try:
        import ctypes
        winmm = ctypes.WinDLL("winmm")
        winmm.timeBeginPeriod(1)
    except Exception:
        winmm = None
    # A busy desktop can deschedule the render loop for a whole timeslice, which
    # showed up as a handful of 20 ms frames every second. The stream is a
    # realtime-ish task, so ask Windows for a higher class and take the frame
    # budget back for the ~10 ms the loop is actually awake per frame.
    try:
        psutil.Process().nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass
    # Cyclic garbage left over from start-up would otherwise be rescanned by the
    # full collections, whose pauses showed up as the occasional 30 ms frame;
    # freezing the already-live objects keeps those collections away from them.
    try:
        import gc
        gc.freeze()
        gc.set_threshold(4000, 25, 25)
    except Exception:
        pass
    sensor = Demo() if demo else _Sampler(cfg.presentmon_path)
    dash = Dashboard(cfg)

    state = {"connected": False, "out_fps": 0.0, "kb_s": 0.0,
             "patches": 0, "frames": 0, "hub": {}}

    provider = stats_provider(dash, state)

    spec = audio.Spectrum(cfg.spectrum_device, cfg.spectrum_tilt, cfg.spectrum_smoothing)
    watcher = lyrics.MediaWatcher()
    track = {"song": "", "lines": []}      # fetch once per track, not per frame
    provider_lyrics = lyrics.Lyrics()
    lstate = lyrics.LyricState()
    if not demo:
        watcher.start()

    sender = _Sender()
    if sender.error is not None:
        if not demo:
            sensor.stop()
        print(f"[hubmon] cannot open device: {sender.error}")
        print("[hubmon] make sure the hub is plugged in and the vendor tool is closed")
        return 2
    print(f"[hubmon] device open, heartbeat={sender.ack!r}")
    state["connected"] = True
    state["hub"] = dict(sender.identity)

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
    next_frame = last
    fails = 0
    rate_at_log = 0
    kb_at_log = 0.0
    loops = 0
    loops_at_log = 0
    prev_top = None
    # the deadline is trimmed by the measured lag between wake-up and the
    # requested cadence, so sleep/handoff overhead is cancelled rather than
    # accumulated (the loop landed ~1 ms late per frame and sat at 57 fps)
    pace_err = 0.0
    last_gap = None
    last_full = 0.0        # when the panel last got a whole frame
    prev = None            # RGB565 the panel actually shows
    buf = tmp = None       # scratch buffers for the per-frame conversion
    stats = {}
    try:
        while not stop["v"]:
            now = time.perf_counter()
            if prev_top is not None:
                last_gap = now - prev_top
            prev_top = now
            if sender is not None:
                sender.pump()
            if sender is None or sender.dead():
                if sender is not None:
                    base = (sender.frames, sender.bytes)
                    if sender.error:
                        print(f"[hubmon] usb error ({sender.error}); reconnecting ...")
                    else:
                        print(f"[hubmon] sender silent (pid={sender.proc.pid}, "
                              f"alive={sender.proc.is_alive()}, code="
                              f"{sender.proc.exitcode}); reconnecting ...")
                    sender.close()
                    sender = None
                else:
                    base = (0, 0)
                state["connected"] = False
                prev = None             # the new handle's panel state is unknown
                sender = _Sender(base)
                if sender.error is not None:
                    print(f"[hubmon] cannot reopen device: {sender.error}")
                    sender.close()
                    sender = None
                    time.sleep(1.5)
                else:
                    state["connected"] = True
                    state["hub"] = dict(sender.identity)
                last_gap = None         # the reopen gap is not a pacing sample
                continue
            # Back-pressure. The worker hands the credit back the moment it
            # lifts a frame off the queue, so this waits exactly as long as the
            # link is busy: a frame built while the worker is still writing
            # would only be thrown away, and drawing it was the whole CPU cost
            # of an "unlimited" stream.
            if not sender.ready():
                state["out_fps"] = sender.fps
                state["kb_s"] = sender.kb_s
            else:
                # time since the last frame actually produced, not since the
                # last loop pass: skipped passes must not slow the animation
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
                        # one envelope step per drawn frame, with the real
                        # elapsed time: stepping it on a separate clock left
                        # the bars lagging behind the frame that drew them
                        bands, peaks = spec.levels(dt)
                        stats["spectrum_bands"] = bands
                        stats["spectrum_peaks"] = peaks
                    stats["spec_state"] = spec.state
                    if want_spec and cfg.lyrics_enable and not demo:
                        # no music app open: SMTC may still offer a browser or a
                        # video player, so the band stays empty instead
                        if not lyrics.music_app_running():
                            stats["lyric_text"] = ""
                            stats["lyric_visible"] = False
                        else:
                            snap = watcher.snapshot()
                            song = "%s|%s" % (snap["title"], snap["artist"])
                            if song != track["song"]:      # new track: look up once
                                track["song"] = song
                                track["lines"] = provider_lyrics.lines_for(
                                    snap["title"], snap["artist"], snap["album"],
                                    snap["duration_s"], snap["song_id"])
                            lst = lstate.update(cfg, snap, track["lines"], dt)
                            stats["lyric_text"] = lst["text"]
                            stats["lyric_prev"] = lst["prev"]
                            stats["lyric_next"] = lst["next"]
                            stats["lyric_t"] = lst["t"]
                            stats["lyric_scroll"] = lst["scroll"]
                            stats["lyric_effect"] = lst["effect"]
                            stats["lyric_visible"] = lst["visible"]
                            stats["lyric_dur"] = lst["dur"]
                            stats["lyric_song"] = song
                            stats["cover"] = snap.get("cover") or b""
                    dash.update(stats)
                    img = dash.render(dt)
                    # one conversion pass per frame with reused scratch buffers;
                    # the worker only writes what comes out of here
                    cur, buf, tmp = transport.to_rgb565_be(np.asarray(img), buf, tmp)

                    rects, nbytes = ((None, transport.FRAME_BYTES) if prev is None
                                     else transport.dirty_rects(prev, cur))
                    resync = (now - last_full) >= transport.RESYNC_EVERY
                    whole = (not cfg.partial or prev is None or resync
                             or nbytes > transport.FRAME_BYTES * transport.FULL_THRESHOLD)
                    if whole:
                        sent = sender.submit("frame", cur, transport.FRAME_BYTES, 0)
                        if sent:
                            last_full = now
                    else:
                        arr565 = np.frombuffer(cur, np.uint8).reshape(H, W, 2)
                        pkts = []
                        moved = 0
                        for k in range(0, len(rects), transport.BATCH_MAX):
                            batch = rects[k:k + transport.BATCH_MAX]
                            pkts.append((transport.batch_packet(batch, arr565), len(batch)))
                            moved += sum(w * h * 2 for (_, _, w, h) in batch)
                        sent = sender.submit("batches", pkts, moved, len(rects))
                    # ``prev`` mirrors what the panel actually received. A frame
                    # the worker skipped never reached it, so the next diff has
                    # to be computed against the last delivered frame -- otherwise
                    # the pixels that changed only in the skipped frame are never
                    # sent and stay visible as stale digits or leftovers.
                    if sent:
                        prev = cur
                    loops += 1
                    state["out_fps"] = sender.fps
                    state["kb_s"] = sender.kb_s
                    state["patches"] = sender.patches
                    state["frames"] = sender.frames
                    state["dropped"] = sender.dropped
                    fails = 0
                except Exception as e:
                    fails += 1
                    if fails in (1, 5):
                        print(f"[hubmon] frame error ({e})")
                    sender._release()      # this frame will never be sent
                    time.sleep(0.2)
                    continue

            if now - last_log >= log_every and sender is not None:
                el = now - last_log
                rate = (sender.frames - rate_at_log) / el
                rate_at_log = sender.frames
                kb = (sender.bytes / 1024 - kb_at_log) / el
                kb_at_log = sender.bytes / 1024
                loop_rate = (loops - loops_at_log) / el
                loops_at_log = loops
                print(f"[hubmon] {rate:5.1f} fps  {kb:6.1f} KiB/s  "
                      f"render={loop_rate:4.1f}  drop={sender.dropped}  "
                      f"link={sender.busy_ms:4.1f}ms  "
                      f"cpu={dash.v.cpu:5.1f}%  gpu={dash.v.gpu:5.1f}%  "
                      f"mem={dash.v.mem:4.1f}%  pwr={dash.v.power:.0f}W  "
                      f"patches={sender.patches}  frames={sender.frames}",
                      flush=True)
                last_log = now

            fps = cfg.fps
            if fps and fps > 0:
                period = 1.0 / fps
                # trim the deadline by the measured cadence error: waking from a
                # Windows sleep costs about a millisecond, and without the trim
                # that error is paid on every frame
                next_frame += max(period * 0.85, period - pace_err)
                slack = next_frame - time.perf_counter()
                # Sleep for the bulk of a long gap, but never within 12 ms of
                # the deadline: a coalesced timer can return 10-15 ms late, and
                # that lands as a visible hitch in the stream.
                if slack > 0.012:
                    time.sleep(slack - 0.010)
                while time.perf_counter() < next_frame:
                    pass
                after = time.perf_counter()
                # trim on the measured frame-to-frame cadence, which is what the
                # stream actually delivers, rather than on the timer alone
                if last_gap is not None:
                    pace_err += 0.03 * (last_gap - period - pace_err)
                    pace_err = max(-0.0025, min(0.0025, pace_err))
                if after - next_frame > period:
                    next_frame = after
    finally:
        stop["v"] = True
        if winmm is not None:
            try:
                winmm.timeEndPeriod(1)
            except Exception:
                pass
        if sender is not None:
            sender.close()
        if not demo:
            sensor.stop()
        spec.stop()
        watcher.stop()
        webui.stop(panel)
        print(f"[hubmon] stopped after {sender.frames if sender else 0} frames")
    return 0


def main(argv=None):
    multiprocessing.freeze_support()
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