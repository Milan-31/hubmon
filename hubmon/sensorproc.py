"""Background sampler process.

psutil, the PDH counter, ``nvidia-smi`` and PresentMon all do their work off
the render process. Each ``nvidia-smi`` spawn plus its pipe reads competes for
the GIL, and that showed up as roughly one frame in twelve arriving 10-20 ms
late -- enough to keep the stream just under 60 fps. The sampler owns its own
process so the render loop can hold the cadence.
"""
from __future__ import annotations

import traceback


def run(out, halt, presentmon_path=""):
    try:
        _run(out, halt, presentmon_path)
    except BaseException:
        try:
            out.put(("error", traceback.format_exc().strip().splitlines()[-1]))
        except Exception:
            pass
        raise


def _run(out, halt, presentmon_path):
    from .sensors import Sensors

    sensor = Sensors(presentmon_path=presentmon_path)
    sensor.start()
    try:
        while not halt.is_set():
            out.put(("stats", sensor.snapshot()))
            # four samples a second is plenty: the dashboard eases values and
            # the readouts themselves refresh at 1-1.5 s
            halt.wait(0.25)
    finally:
        sensor.stop()
