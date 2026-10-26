"""USB transport: stream 320x170 RGB565 frames to the jadot V1PRO panel.

Protocol (reverse engineered): bulk OUT 0x01, ACK on bulk IN 0x81.

    A5 5A 0B | u32 payload_len | flags | payload

``flags & 0x0F`` selects the mode: 0 = full frame, 1 = region patch. For a region
patch the payload is an 8-byte rectangle (u16 x, y, w, h, little-endian) followed
by ``w * h * 2`` RGB565 big-endian pixels, and ``payload_len`` is the *pixel*
byte count. Patches are composited onto whatever the panel already shows, so only
changed areas need to be sent.

Every command queues one text reply on the IN endpoint (``LIVE,ok`` or
``LIVE,err,...``). Those replies must be read back: the firmware blocks its next
transfer once the queue is full, which showed up as a 20 ms stall per patch and
about 2 fps. Reading one ACK after each patch keeps the queue empty and measured
5x faster on the same frame sequence.

Mode 2 packs up to six rectangles into one command with a single ACK:

    A5 5A 0B | u32 total_pixel_bytes | 02 | u8 count | count*(u16 x,y,w,h)
    | pixels concatenated

That removes the per-rectangle command overhead, which is what makes fine-grained
dirty tiles (2x5) affordable and the stream reach 60 fps.

Full frames are pushed in 16 KiB chunks; that chunk size measured fastest.
"""
from __future__ import annotations

import os
import struct
import time

import numpy as np
import usb.core
import usb.backend.libusb1
import usb.util

VID = 0x0483
PID = 0x66AA
EP_OUT = 0x01
EP_IN = 0x81
CHUNK = 16384
W, H = 320, 170
FRAME_BYTES = W * H * 2
# the panel rejects a batch with more than six rectangles
BATCH_MAX = 6

# Dirty-region tile size. 1x5 tiles cut a typical frame from 21 KB to 7 KB; a
# batch carries six of them per command, so the finer grid stays affordable.
TILE_W, TILE_H = 1, 5
# If nearly the whole screen changed, a full frame is simpler: with 1x5 tiles a
# full-screen patch set costs about the same as a full frame (108 KB, ~140 ms),
# so switching earlier only adds latency spikes; medium changes stay patches.
FULL_THRESHOLD = 0.95
# Occasional full frame so the panel cannot drift out of sync.
RESYNC_EVERY = 120.0

# Override the libusb DLL with HUBMON_LIBUSB if the bundled one is not usable.
LIBUSB_ENV = "HUBMON_LIBUSB"


def dirty_rects(prev, cur):
    """Rectangles covering every tile that differs between two RGB565 buffers.

    Row runs are merged vertically whenever consecutive rows share the exact
    same x span, which cuts the command count without adding a byte.
    """
    # compare as 16-bit words: one bool array instead of two per channel, and
    # no byte-level reduction pass (byte order is irrelevant to equality)
    a = np.frombuffer(prev, np.uint16).reshape(H, W)
    b = np.frombuffer(cur, np.uint16).reshape(H, W)
    tiles = (a != b).reshape(H // TILE_H, TILE_H,
                             W // TILE_W, TILE_W).any(axis=(1, 3))
    # Tile-row runs, found from the edges of each row instead of scanning every
    # tile in Python: a zero-padded row turns each run into a +1/-1 pair, and
    # the transitions come out of one diff. Rows never interleave, so the k-th
    # start pairs with the k-th end.
    rows, cols = tiles.shape
    pad = np.zeros((rows, cols + 2), np.int8)
    pad[:, 1:-1] = tiles
    edges = np.diff(pad, axis=1)
    starts = np.argwhere(edges == 1)
    ends = np.argwhere(edges == -1)
    runs = [(int(s[1]) * TILE_W, int(s[0]) * TILE_H,
             int(e[1] - s[1]) * TILE_W, TILE_H)
            for s, e in zip(starts, ends)]
    spans = {}
    for (x, y, w, h) in runs:
        spans.setdefault((x, w), []).append((y, h))
    rects = []
    for (x, w), rows_ in spans.items():
        rows_.sort()
        y0, h = rows_[0]
        for y, hh in rows_[1:]:
            if y == y0 + h:
                h += hh
            else:
                rects.append((x, y0, w, h))
                y0, h = y, hh
        rects.append((x, y0, w, h))
    total = sum(w * h * 2 for (_, _, w, h) in rects)
    return rects, total


def to_rgb565_be(arr, buf=None, tmp=None):
    """RGBA pixels as big-endian RGB565 bytes, with reusable scratch buffers.

    The panel wants rrrrrggg gggbbbbb, so the two bytes are built separately:
    writing the high byte first avoids a 16-bit assembly pass plus a byte swap,
    which measured ~30% slower. Returns ``(bytes, buf, tmp)`` so the caller can
    hand the buffers back next frame.
    """
    h, w = arr.shape[:2]
    if buf is None or buf.shape != (h, w, 2):
        buf = np.empty((h, w, 2), np.uint8)
        tmp = np.empty((h, w), np.uint8)
    t = tmp
    hi, lo = buf[:, :, 0], buf[:, :, 1]
    np.bitwise_and(arr[:, :, 0], 0xF8, out=hi)          # rrrrr<<3
    np.right_shift(arr[:, :, 1], 5, out=t)
    np.bitwise_or(hi, t, out=hi)                        # ... | ggg
    np.bitwise_and(arr[:, :, 1], 0x1C, out=t)
    np.left_shift(t, 3, out=t)                          # ggg<<3
    np.right_shift(arr[:, :, 2], 3, out=lo)             # bbbbb
    np.bitwise_or(lo, t, out=lo)
    return buf.tobytes(), buf, tmp


def _backend():
    try:
        import libusb_package
        b = libusb_package.get_libusb1_backend()
        if b:
            return b
    except Exception:
        pass
    dll = os.environ.get(LIBUSB_ENV)
    if dll:
        try:
            b = usb.backend.libusb1.get_backend(find_library=lambda _: dll)
            if b:
                return b
        except Exception:
            pass
    return usb.backend.libusb1.get_backend()


class DeviceNotFound(RuntimeError):
    pass


def bcd_str(v):
    """USB binary-coded-decimal to a dotted version: 0x0105 -> '1.05'.

    Each nibble is one decimal digit, so 0x0231 is 2.31 -- not 2.49, which is
    what a plain ``%02d`` of the low byte would print.
    """
    v = int(v)
    return "%d.%02d" % ((v >> 8) & 0xFF, ((v >> 4) & 0x0F) * 10 + (v & 0x0F))


def read_identity(dev):
    """The panel's own report of itself, for the control panel's header.

    Everything here comes off the device descriptors, so it costs one round of
    control transfers at open time and nothing afterwards. String reads are
    best-effort: a descriptor the firmware left empty, or one Windows refuses,
    simply drops out of the dict rather than failing the connection.
    """
    out = {}
    for key, attr in (("vendor", "manufacturer"),
                      ("product", "product"),
                      ("serial", "serial_number")):
        try:
            val = getattr(dev, attr)
            if val:
                out[key] = str(val).strip()
        except Exception:
            pass
    try:
        out["version"] = bcd_str(dev.bcdDevice)
    except Exception:
        pass
    return out


def batch_packet(rects, arr):
    """Build the mode-2 command for ``rects`` from an ``(H, W, 2)`` frame."""
    pix = b"".join(arr[y:y + h, x:x + w].tobytes() for (x, y, w, h) in rects)
    body = (bytes((len(rects),))
            + b"".join(struct.pack("<4H", *r) for r in rects)
            + pix)
    return bytes((0xA5, 0x5A, 0x0B)) + struct.pack("<I", len(pix)) + bytes((0x02,)) + body


class LiveScreen:
    def __init__(self, ack_timeout_ms=250):
        self._dev = None
        self._ack_timeout = ack_timeout_ms
        self._backend = _backend()
        self.frames_sent = 0
        self.regions_sent = 0
        self.identity = {}            # vendor / product / version, read at open

    # ------------------------------------------------------------------ open
    def open(self):
        if self._dev is not None:
            return
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=self._backend)
        if dev is None:
            raise DeviceNotFound("V1PRO hub screen not found")
        dev.set_configuration()
        try:
            dev.clear_halt(EP_IN)
        except Exception:
            pass
        self._dev = dev
        self.identity = read_identity(dev)
        # a previous session (or the vendor tool) may have left replies queued;
        # starting with a full IN queue caps every later write at ~20 ms
        self._drain_pending()

    def close(self):
        if self._dev is not None:
            try:
                usb.util.dispose_resources(self._dev)
            except Exception:
                pass
            self._dev = None

    @property
    def connected(self):
        return self._dev is not None

    # ------------------------------------------------------------------ send
    def send_frame(self, rgb565: bytes):
        if len(rgb565) != FRAME_BYTES:
            raise ValueError(f"frame must be {FRAME_BYTES} bytes, got {len(rgb565)}")
        hdr = bytes((0xA5, 0x5A, 0x0B, 0x00)) + struct.pack("<I", len(rgb565) // 256)
        pkt = hdr + rgb565
        dev = self._dev
        for off in range(0, len(pkt), CHUNK):
            dev.write(EP_OUT, pkt[off:off + CHUNK], timeout=4000)
        self.frames_sent += 1
        ack = self._read_ack()        # full frames take ~90 ms to acknowledge
        self._drain_pending()
        return ack

    def send_region(self, x, y, w, h, rgb565: bytes):
        """Composite a rectangle onto the panel and consume its ACK.

        The panel answers every patch with ``LIVE,ok``. Leaving those replies
        unread fills the IN queue, after which each write stalls for ~20 ms, so
        the ACK is read eagerly rather than once per frame.
        """
        if len(rgb565) != w * h * 2:
            raise ValueError(f"region needs {w * h * 2} bytes, got {len(rgb565)}")
        hdr = (bytes((0xA5, 0x5A, 0x0B)) + struct.pack("<I", len(rgb565))
               + bytes((0x01,)) + struct.pack("<4H", x, y, w, h))
        pkt = hdr + rgb565
        dev = self._dev
        for off in range(0, len(pkt), CHUNK):
            dev.write(EP_OUT, pkt[off:off + CHUNK], timeout=4000)
        self.regions_sent += 1
        return self._read_ack(timeout_ms=120)

    def write_packet(self, pkt, ack_timeout=30):
        """Send one prepared LIVE command and consume its ACK."""
        dev = self._dev
        for off in range(0, len(pkt), CHUNK):
            dev.write(EP_OUT, pkt[off:off + CHUNK], timeout=4000)
        return self._read_ack(timeout_ms=ack_timeout)

    def send_batch(self, rects, arr):
        """Composite up to ``BATCH_MAX`` rectangles in one command and one ACK.

        ``arr`` is the frame as ``(H, W, 2)`` uint8; only the listed rectangles
        are copied out, so this stays cheap even at 2-pixel tile widths.
        """
        if not rects:
            return b""
        self.regions_sent += len(rects)
        return self.write_packet(batch_packet(rects, arr))

    def _drain_pending(self, max_reads=32):
        """Read every queued reply, then return the last one seen.

        Arriving frames are not the only source of replies: the vendor tool, a
        killed session, or a rejected probe can leave a backlog behind. One
        blocking read is not enough to recover from that, so this empties the
        endpoint until it answers nothing.
        """
        last = b""
        for _ in range(max_reads):
            got = self._read_ack(timeout_ms=1)
            if not got:
                break
            last = got
        return last

    def _read_ack(self, timeout_ms=None):
        """One queued device reply, or ``b""`` if it did not answer in time."""
        try:
            return bytes(self._dev.read(EP_IN, 64,
                                        timeout=timeout_ms or self._ack_timeout))
        except usb.core.USBError:
            return b""
        except Exception:
            return b""

    # ------------------------------------------------------------- heartbeat
    def ping(self, timeout_ms=500):
        self._drain_pending()
        self._dev.write(EP_OUT, bytes((0xA5, 0x5A, 0x09)), timeout=1000)
        try:
            return bytes(bytearray(self._dev.read(EP_IN, 64, timeout=timeout_ms)))
        except usb.core.USBError:
            return b""

    def ensure_connected(self, retries=3, delay=1.0):
        for attempt in range(retries):
            try:
                if not self.connected:
                    self.open()
                return True
            except Exception:
                self.close()
                time.sleep(delay)
        return False