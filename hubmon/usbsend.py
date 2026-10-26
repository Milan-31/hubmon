"""USB transport worker process.

The renderer holds the GIL for several milliseconds per frame while PIL draws,
so a sender *thread* loses that time between USB transfers and the stream tops
out near 55 fps even though the link itself can do ~84 fps. Owning the device in
a separate process removes the contention entirely.

Protocol with the parent (``multiprocessing.Queue`` pairs):

    tasks  : ("open",) | ("frame", bytes, moved, patches)
             | ("batches", [(packet, rects)], moved, patches) | None
    events : ("open", identity, ack) | ("stats", frames, bytes, patches, fps,
             kb_s) | ("error", text)
"""
from __future__ import annotations

import queue
import time
import traceback


def run(tasks, events):
    try:
        _run(tasks, events)
    except BaseException:
        try:
            events.put(("fatal", traceback.format_exc()))
        except Exception:
            pass
        raise


def _run(tasks, events):
    from . import transport

    screen = None
    frames = nbytes = patches = 0
    busy = 0.0
    busy0 = 0.0
    empties = 0
    t0 = time.perf_counter()
    n0 = b0 = 0

    def beat(now):
        """Publish counters, and do it even while idle.

        The parent treats the message itself as a heartbeat, so a quiet window
        must still report (otherwise a healthy worker looks dead and the stream
        reconnects for nothing).
        """
        nonlocal t0, n0, b0, busy0
        el = now - t0
        df = max(1, frames - n0)
        events.put(("stats", frames, nbytes, patches,
                    (frames - n0) / el, (nbytes - b0) / 1024 / el,
                    (busy - busy0) / df * 1000, empties))
        t0, n0, b0, busy0 = now, frames, nbytes, busy

    try:
        while True:
            try:
                item = tasks.get(timeout=0.2)
            except queue.Empty:
                now = time.perf_counter()
                if now - t0 >= 0.5:
                    beat(now)
                continue
            except (EOFError, OSError):
                break               # the parent went away; release the device
            if item is None:
                break
            kind = item[0]
            if kind == "open":
                # The parent may restart this worker the instant the previous
                # one let go of the device; the OS can still be releasing that
                # handle, so opening again fails for a beat. Retry here instead
                # of reporting a failure the parent would answer by spawning
                # yet another worker -- that feedback loop turned one hiccup
                # into a reconnect every few hundred milliseconds.
                err = None
                for attempt in range(6):
                    try:
                        screen = transport.LiveScreen()
                        screen.open()
                        ack = screen.ping()
                        events.put(("open", dict(screen.identity), ack))
                        err = None
                        break
                    except Exception as e:
                        screen = None
                        err = str(e) or type(e).__name__
                        time.sleep(0.1 * (attempt + 1))
                if err is not None:
                    events.put(("error", err))
                continue
            if kind == "close":
                if screen is not None:
                    screen.close()
                    screen = None
                events.put(("closed",))
                break

            _, payload, moved, npatches = item
            started = time.perf_counter()
            try:
                if kind == "frame":
                    screen.send_frame(payload)
                else:
                    for pkt, nrect in payload:
                        if not screen.write_packet(pkt):
                            empties += 1
                        screen.regions_sent += nrect
            except Exception as e:
                # a dead device cannot be recovered from inside this process:
                # report it and exit so the parent opens a fresh handle instead
                events.put(("error", str(e) or type(e).__name__))
                break

            frames += 1
            nbytes += moved
            patches = npatches
            busy += time.perf_counter() - started
            now = time.perf_counter()
            if now - t0 >= 0.5:
                beat(now)
    finally:
        try:
            if screen is not None:
                screen.close()
        except Exception:
            pass
