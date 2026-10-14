"""Audio spectrum analyser for the hubmon panel.

Capture is best-effort by design: the dashboard has to run on a machine with no
capture device, a muted endpoint, or no audio library installed at all. So every
failure here degrades to a silent baseline instead of raising -- ``state`` says
what happened and the renderer shows a hint, but the stream keeps running.

The analyser itself is deliberately small: one 1024-point FFT per frame, folded
into 24 log-spaced bands, then smoothed with a fast-attack / slow-release
envelope and a peak hold. Nothing here touches Pillow or the USB link.
"""
from __future__ import annotations

import math
import os
import threading

import numpy as np

try:
    import soundcard as _sc
except Exception:                      # a missing library must not break the app
    _sc = None

BANDS = 24
SR = 48000
BLOCK = 1024
FFT_N = 1024
FMIN, FMAX = 40.0, 16000.0
FLOOR_DB = -60.0                       # everything quieter reads as zero
# Envelope time constants in seconds, at the *middle* of the panel's smoothing
# control: attack = ATTACK_BASE + ATTACK_RANGE * smoothing, and likewise for the
# release. The defaults are deliberately slower than the first cut, which read as
# flicker rather than motion.
ATTACK_BASE, ATTACK_RANGE = 0.05, 0.08
RELEASE_BASE, RELEASE_RANGE = 0.25, 0.26
DEFAULT_SMOOTHING = 0.5
PEAK_HOLD = 0.8                        # how long a peak stays latched, seconds
# Real music falls about 3 dB per octave. Against a flat dB mapping that pins the
# bass to the top of the scale and leaves the treble on the floor, so each band
# carries a tilt that cancels the slope; 1 kHz is the octave it leaves alone.
TILT_DB_PER_OCT = 3.0
TILT_REF_HZ = 1000.0

_EDGES = np.geomspace(FMIN, FMAX, BANDS + 1)
_FC = np.sqrt(_EDGES[:-1] * _EDGES[1:])          # geometric centre of each band
_BINS = np.fft.rfftfreq(FFT_N, 1.0 / SR)
_WINDOW = np.hanning(FFT_N)
_WINDOW_SUM = float(_WINDOW.sum())
# one bin range per band; the +1 keeps a band from ever coming back empty
_EDGE_BINS = np.searchsorted(_BINS, _EDGES)
_SLICES = [(int(_EDGE_BINS[i]), max(int(_EDGE_BINS[i]) + 1, int(_EDGE_BINS[i + 1])))
           for i in range(BANDS)]


def tilt_db(db_per_oct):
    """Per-band gain that cancels the ~3 dB/octave slope of real music."""
    return db_per_oct * np.log2(_FC / TILT_REF_HZ)


def output_device_names():
    """Output endpoints the spectrum can tap, for the panel's device picker.

    Only outputs are listed: a loopback rides on a render endpoint, and the
    loopback enumeration also mixes microphone inputs in with them. Enumerating
    from another thread needs that thread's own COM apartment (see
    ``_com_apartment``), so it is opened here instead of leaving that detail to
    whoever asks -- the panel's HTTP handler runs on a thread of its own.
    """
    if _sc is None:
        return []
    _com_apartment()
    try:
        names = [s.name for s in _sc.all_speakers() if s.name]
    except Exception:
        return []
    out, seen = [], set()
    for n in names:                   # a `<select>` needs distinct values
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _com_apartment():
    """Bring COM up on the calling thread; returns the HRESULT, or None.

    ``soundcard`` builds its COM library at *import* time, so the apartment it
    opens covers only the thread that did the importing. Every other thread has
    to open its own before the first ``CoCreateInstance`` -- a frozen build has
    no implicit apartment to fall back on, and the capture thread is the one
    that calls in, so it does this itself. S_OK and S_FALSE mean the apartment
    is ours now; anything else leaves COM as it was found.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        return int(ctypes.windll.ole32.CoInitializeEx(None, 0))  # MULTITHREADED
    except Exception:
        return None


class Spectrum:
    """Loopback capture plus the 24-band analyser, behind one small interface.

    Only four things outside this module matter: ``start``, ``stop``,
    ``levels(dt)`` and ``state``.
    """

    def __init__(self, device: str = "", tilt_db_per_oct: float = TILT_DB_PER_OCT,
                 smoothing: float = DEFAULT_SMOOTHING):
        self.device = device or ""
        self.state = "idle" if _sc is not None else "no-lib"
        self.tilt_db_per_oct = float(tilt_db_per_oct)
        self.smoothing = min(1.0, max(0.0, float(smoothing)))
        self._tilt = tilt_db(self.tilt_db_per_oct)
        self._latest = None            # newest block, written by the capture thread
        self._target = [0.0] * BANDS
        self._bands = [0.0] * BANDS
        self._hold = [0.0] * BANDS
        self._hold_at = [0.0] * BANDS
        self._now = 0.0
        self._thread = None
        self._stop = threading.Event()

    def tune(self, tilt_db_per_oct, smoothing):
        """The panel's two analyser knobs; cheap enough to call every frame."""
        tilt = float(tilt_db_per_oct)
        if tilt != self.tilt_db_per_oct:
            self.tilt_db_per_oct = tilt
            self._tilt = tilt_db(tilt)
        self.smoothing = min(1.0, max(0.0, float(smoothing)))

    # ------------------------------------------------------------ life cycle
    def start(self):
        """Idempotent: opens the endpoint once and then just keeps running."""
        if _sc is None or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hubmon-spectrum",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        """Idempotent, and bounded: the recorder returns within one block."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=0.5)

    def _open(self):
        if self.device:
            try:
                mic = _sc.get_microphone(id=self.device, include_loopback=True)
            except Exception:
                mic = None
            if mic is not None:
                return mic
            for m in _sc.all_microphones(include_loopback=True):
                if self.device.lower() in (m.name or "").lower():
                    return m
            return None
        return _sc.get_microphone(id=str(_sc.default_speaker().name),
                                  include_loopback=True)

    def _run(self):
        _com_apartment()               # the apartment belongs to this thread
        try:
            mic = self._open()
            if mic is None:
                self.state = "no-device"
                return
            with mic.recorder(samplerate=SR) as rec:
                self.state = "ok"
                while not self._stop.is_set():
                    self._latest = np.asarray(rec.record(numframes=BLOCK),
                                              dtype=np.float32)
        except Exception:
            self.state = "error"

    # ------------------------------------------------------------- analysing
    def _analyse(self, block):
        """24 band levels in 0..1, peak-per-band, dB-scaled to full scale."""
        x = np.asarray(block, dtype=np.float32)
        if x.ndim > 1:
            x = x.mean(axis=1)
        if x.shape[0] < FFT_N:
            x = np.pad(x, (0, FFT_N - x.shape[0]))
        elif x.shape[0] > FFT_N:
            x = x[:FFT_N]
        amp = 2.0 * np.abs(np.fft.rfft(x * _WINDOW)) / _WINDOW_SUM
        out = []
        for i, (lo, hi) in enumerate(_SLICES):
            peak = float(amp[lo:hi].max()) if hi > lo else 0.0
            db = 20.0 * math.log10(peak) if peak > 1e-7 else FLOOR_DB
            db += float(self._tilt[i])
            out.append(min(1.0, max(0.0, (db - FLOOR_DB) / -FLOOR_DB)))
        return out

    def levels(self, dt: float):
        """(bands, peaks) for the next frame, stepping the envelope by ``dt``."""
        dt = max(1e-4, float(dt))
        if self._latest is not None:
            self._target = self._analyse(self._latest)
        attack = ATTACK_BASE + ATTACK_RANGE * self.smoothing
        release = RELEASE_BASE + RELEASE_RANGE * self.smoothing
        up = 1.0 - math.exp(-dt / attack)
        down = 1.0 - math.exp(-dt / release)
        for i, want in enumerate(self._target):
            cur = self._bands[i]
            cur += (want - cur) * (up if want > cur else down)
            self._bands[i] = cur
            if cur >= self._hold[i]:        # latch the peak while it is climbing
                self._hold[i] = cur
                self._hold_at[i] = self._now
            elif self._now - self._hold_at[i] > PEAK_HOLD:
                self._hold[i] += (cur - self._hold[i]) * down
                if self._hold[i] < cur:
                    self._hold[i] = cur
        self._now += dt
        return list(self._bands), list(self._hold)
