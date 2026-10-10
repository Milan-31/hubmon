"""USB transport: stream 320x170 RGB565 frames to the jadot V1PRO panel.

Protocol (reverse engineered): bulk OUT 0x01, ACK on bulk IN 0x81.

    A5 5A 0B | u32 payload_len | flags | payload

``flags & 0x0F`` selects the mode: 0 = full frame, 1 = region patch (the device
does not answer those). For a region patch the payload is an 8-byte rectangle
(u16 x, y, w, h, little-endian) followed by ``w * h * 2`` RGB565 big-endian
pixels, and ``payload_len`` is the *pixel* byte count. Patches are composited
onto whatever the panel already shows, so only changed areas need to be sent.

Full frames are pushed in 16 KiB chunks; that chunk size measured fastest.
"""
from __future__ import annotations

import os
import struct
import time

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

# Override the libusb DLL with HUBMON_LIBUSB if the bundled one is not usable.
LIBUSB_ENV = "HUBMON_LIBUSB"


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


class LiveScreen:
    def __init__(self, ack_timeout_ms=250):
        self._dev = None
        self._ack_timeout = ack_timeout_ms
        self._backend = _backend()
        self.frames_sent = 0
        self.regions_sent = 0

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
        self._drain_ack()

    def send_region(self, x, y, w, h, rgb565: bytes):
        """Composite a rectangle onto the panel. The device sends no reply, so
        this is a pure OUT transfer -- far cheaper than a full frame."""
        if len(rgb565) != w * h * 2:
            raise ValueError(f"region needs {w * h * 2} bytes, got {len(rgb565)}")
        hdr = (bytes((0xA5, 0x5A, 0x0B)) + struct.pack("<I", len(rgb565))
               + bytes((0x01,)) + struct.pack("<4H", x, y, w, h))
        pkt = hdr + rgb565
        dev = self._dev
        for off in range(0, len(pkt), CHUNK):
            dev.write(EP_OUT, pkt[off:off + CHUNK], timeout=4000)
        self.regions_sent += 1

    def _drain_ack(self):
        try:
            self._dev.read(EP_IN, 64, timeout=self._ack_timeout)
        except usb.core.USBError:
            pass
        except Exception:
            pass

    # ------------------------------------------------------------- heartbeat
    def ping(self, timeout_ms=500):
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