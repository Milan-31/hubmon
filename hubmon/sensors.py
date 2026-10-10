"""Sensor collection: CPU / GPU / memory / power, sampled in a background thread."""
from __future__ import annotations

import csv
import ctypes
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from ctypes import wintypes

import psutil

CREATE_NO_WINDOW = 0x08000000

# PresentMon (Intel's frame-timing tool) is optional: it only powers the
# foreground frame-rate readout. Point at it through the ``presentmon_path``
# setting, the ``HUBMON_PRESENTMON`` environment variable, or just put it on
# PATH. A copy bundled next to a frozen exe wins over all of those.
PRESENTMON_ENV = "HUBMON_PRESENTMON"
PRESENTMON_CANDIDATES = (
    r"C:\Program Files\Intel\PresentMon\PresentMon.exe",
    r"C:\Program Files (x86)\Intel\PresentMon\PresentMon.exe",
)


def _presentmon_path(explicit: str = ""):
    """Absolute path to PresentMon.exe, or None when it is not installed."""
    for p in (explicit, os.environ.get(PRESENTMON_ENV, "")):
        if p and os.path.exists(p):
            return p
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = os.path.join(base, "PresentMon.exe")
        if os.path.exists(p):
            return p
    p = shutil.which("PresentMon.exe")
    if p:
        return p
    for p in PRESENTMON_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


# CPU temperature. Windows exposes no per-core sensor to plain user code, so we
# try the hardware-monitor WMI providers first (they read the CPU's MSRs through
# a kernel driver) and fall back to the ACPI thermal zone, which is a mainboard
# reading rather than the die. Both move slowly, so poll rarely.
CPU_TEMP_TTL = 10.0
NET_PERIOD = 1.0

# LibreHardwareMonitor / OpenHardwareMonitor expose the same Sensor schema.
_TEMP_PROBE = r"""
$o = $null
foreach ($ns in 'root/LibreHardwareMonitor','root/OpenHardwareMonitor') {
  try {
    $s = Get-CimInstance -Namespace $ns -ClassName Sensor -ErrorAction Stop |
      Where-Object { $_.SensorType -eq 'Temperature' -and $_.Name -match 'CPU|Core|Package' } |
      Sort-Object Value -Descending | Select-Object -First 1
    if ($s) { $o = $ns + ':' + [math]::Round($s.Value, 1); break }
  } catch {}
}
if (-not $o) {
  $t = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty CurrentTemperature
  if ($t) { $o = 'ACPI:' + [math]::Round(($t / 10.0 - 273.15), 1) }
}
if ($o) { $o }
"""


def _read_cpu_temp():
    """(celsius, source) or (None, None) if nothing answered."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             _TEMP_PROBE],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW)
        lines = [ln.strip() for ln in out.stdout.strip().splitlines() if ln.strip()]
        if not lines:
            return None, None
        src, _, val = lines[-1].rpartition(":")
        return float(val), (src or "?")
    except Exception:
        return None, None

# i5-12400F-class defaults, overridden by the detected nominal clock where possible.
CPU_IDLE_W = 12.0
CPU_LOAD_W = 92.0     # extra watts at 100 % utilisation
BASE_SYSTEM_W = 38.0  # board + RAM + SSD + fans + hub itself (rough)


class _PdhCounter:
    """Read a single PDH counter (used for real-time CPU clock)."""

    PDH_FMT_DOUBLE = 0x00000200

    class _Val(ctypes.Structure):
        _fields_ = [("CStatus", wintypes.DWORD), ("value", ctypes.c_double)]

    def __init__(self, path):
        self.ok = False
        try:
            self.pdh = ctypes.WinDLL("pdh")
            self.query = wintypes.HANDLE()
            self.counter = wintypes.HANDLE()
            if self.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.query)) != 0:
                return
            if self.pdh.PdhAddEnglishCounterW(self.query, path, 0,
                                              ctypes.byref(self.counter)) != 0:
                return
            self.pdh.PdhCollectQueryData(self.query)
            self.ok = True
        except Exception:
            self.ok = False

    def read(self):
        if not self.ok:
            return None
        try:
            self.pdh.PdhCollectQueryData(self.query)
            v = self._Val()
            if self.pdh.PdhGetFormattedCounterValue(self.counter, self.PDH_FMT_DOUBLE,
                                                    None, ctypes.byref(v)) != 0:
                return None
            return v.value
        except Exception:
            return None


def _find_nvidia_smi():
    p = shutil.which("nvidia-smi")
    if p:
        return p
    p = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                     "nvidia-smi.exe")
    return p if os.path.exists(p) else None


def _cpu_name():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
    except Exception:
        return platform.processor() or "CPU"


def _foreground():
    """(exe name, pid) of the window that currently owns the foreground."""
    try:
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return "", 0
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return "", 0
        return psutil.Process(pid.value).name(), pid.value
    except Exception:
        return "", 0


class ForegroundFps:
    """Frame rate of the app owning the foreground window, via PresentMon.

    A PresentMon child process is kept running against the foreground exe and
    restarted only when the foreground app changes; the most recent
    ``msBetweenPresents`` samples give a responsive average FPS.
    """

    def __init__(self, exe=None, window=0.6):
        self._exe = _presentmon_path(exe or "")
        self._window = window
        self._proc = None
        self._app = ""
        self._rows = deque(maxlen=8192)      # (arrival time, msBetweenPresents)
        self._fps = 0.0
        self._lock = threading.Lock()
        self._stop = False
        self.available = bool(self._exe)

    def start(self):
        if not self.available:
            return
        self._stop = False
        threading.Thread(target=self._supervise, daemon=True,
                         name="hubmon-fgfps").start()
        threading.Thread(target=self._read, daemon=True,
                         name="hubmon-fgfps-read").start()

    def stop(self):
        self._stop = True
        self._kill()

    def snapshot(self):
        return self._app, self._fps

    # -- internals ---------------------------------------------------------
    def _kill(self):
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass

    def _spawn(self, app):
        with self._lock:
            self._rows.clear()
        self._app = app
        try:
            self._proc = subprocess.Popen(
                [self._exe, "--process_name", app, "--output_stdout",
                 "--no_console_stats", "--v1_metrics",
                 "--stop_existing_session", "--terminate_on_proc_exit"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, text=True, bufsize=1,
                creationflags=CREATE_NO_WINDOW)
        except Exception:
            self._proc = None

    def _supervise(self):
        while not self._stop:
            app, _ = _foreground()
            if app and app != self._app:
                self._kill()
                self._spawn(app)
            self._recalc()
            time.sleep(0.25)

    def _recalc(self):
        now = time.perf_counter()
        with self._lock:
            recent = [ms for t, ms in self._rows if now - t <= self._window and ms > 0]
        if len(recent) >= 4:
            avg = sum(recent) / len(recent)
            self._fps = 1000.0 / avg if avg > 0 else 0.0
        else:
            self._fps = 0.0

    def _read(self):
        while not self._stop:
            p = self._proc
            if p is None or p.stdout is None:
                time.sleep(0.1)
                continue
            try:
                line = p.stdout.readline()
            except Exception:
                time.sleep(0.1)
                continue
            if not line:
                time.sleep(0.05)
                continue
            if line.startswith("Application"):
                continue
            try:
                ms = float(next(csv.reader([line]))[9])   # msBetweenPresents
            except Exception:
                continue
            with self._lock:
                self._rows.append((time.perf_counter(), ms))


class Sensors:
    """Background sampler; read snapshots via ``.data``."""

    def __init__(self, gpu_interval=0.5, cpu_interval=0.2, presentmon_path=""):
        self.data = {}
        self._stop = False
        self._gpu_interval = gpu_interval
        self._cpu_interval = cpu_interval
        self._smi = _find_nvidia_smi()
        self._pdh = _PdhCounter(
            r"\Processor Information(_Total)\% Processor Performance")
        self._base_mhz = 2500.0
        try:
            f = psutil.cpu_freq()
            if f and f.max:
                self._base_mhz = float(f.max)
        except Exception:
            pass
        self._cpu = _cpu_name()
        self._gpu_name = ""
        self._fg = ForegroundFps(presentmon_path)
        self._net = None
        self._net_t = 0.0
        self._net_down = 0.0
        self._net_up = 0.0
        self._temp = 0.0
        self._temp_src = ""
        psutil.cpu_percent(interval=None)
        self._thread = threading.Thread(target=self._run, daemon=True, name="hubmon-sens")
        self._temp_thread = threading.Thread(target=self._temp_loop, daemon=True,
                                             name="hubmon-temp")

    def start(self):
        self._fg.start()
        self._thread.start()
        self._temp_thread.start()

    def stop(self):
        self._stop = True
        self._fg.stop()

    def snapshot(self):
        return self.data

    def _temp_loop(self):
        """Poll the CPU temperature on its own thread: the WMI probe takes a
        few hundred ms and must not stall the main sampler."""
        while not self._stop:
            t, src = _read_cpu_temp()
            if t is not None:
                self._temp = t
                self._temp_src = src
            slept = 0.0
            while slept < CPU_TEMP_TTL and not self._stop:
                time.sleep(0.1)
                slept += 0.1

    # ------------------------------------------------------------------ loop
    def _run(self):
        last_gpu = 0.0
        gpu = {"gpu_util": 0.0, "gpu_freq_mhz": 0.0, "gpu_power_w": 0.0,
               "gpu_temp_c": 0.0, "gpu_mem_used_mb": 0.0, "gpu_mem_total_mb": 0.0}
        while not self._stop:
            loop_start = time.time()
            cpu_util = psutil.cpu_percent(interval=None)
            perf = self._pdh.read()
            freq_mhz = self._base_mhz * (perf / 100.0) if perf else self._base_mhz
            vm = psutil.virtual_memory()

            cpu_power = CPU_IDLE_W + CPU_LOAD_W * (cpu_util / 100.0)
            cpu_power = max(CPU_IDLE_W, min(115.0, cpu_power))

            if self._smi and (loop_start - last_gpu) >= self._gpu_interval:
                gpu = self._query_gpu() or gpu
                last_gpu = loop_start

            total = cpu_power + gpu["gpu_power_w"] + BASE_SYSTEM_W
            fg_app, fg_fps = self._fg.snapshot()

            try:
                nc = psutil.net_io_counters()
                if self._net is not None:
                    dt = loop_start - self._net_t
                    if dt >= NET_PERIOD:
                        self._net_down = max(0.0, (nc.bytes_recv - self._net.bytes_recv) / dt)
                        self._net_up = max(0.0, (nc.bytes_sent - self._net.bytes_sent) / dt)
                        self._net = nc
                        self._net_t = loop_start
                else:
                    self._net = nc
                    self._net_t = loop_start
            except Exception:
                pass

            self.data = {
                "cpu_util": cpu_util,
                "cpu_freq_mhz": freq_mhz,
                "cpu_perf": perf or 0.0,
                "cpu_power_w": cpu_power,
                "cpu_temp_c": self._temp,
                "cpu_temp_src": self._temp_src,
                "net_down_bps": self._net_down,
                "net_up_bps": self._net_up,
                "mem_percent": vm.percent,
                "mem_used_gb": vm.used / 1e9,
                "mem_total_gb": vm.total / 1e9,
                "gpu_name": self._gpu_name,
                "total_power_w": total,
                "cpu_name": self._cpu,
                "host": self._host,
                "fg_app": fg_app,
                "fg_fps": fg_fps,
                **gpu,
            }
            self._stop = self._stop or False
            time.sleep(max(0.02, self._cpu_interval - (time.time() - loop_start)))

    def _query_gpu(self):
        q = ("name,clocks.sm,utilization.gpu,power.draw,temperature.gpu,"
             "memory.used,memory.total,fan.speed")
        try:
            out = subprocess.run([self._smi, f"--query-gpu={q}",
                                  "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=4,
                                 creationflags=CREATE_NO_WINDOW)
            line = out.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in line.split(",")]
            self._gpu_name = parts[0]

            def num(i):
                try:
                    return float(parts[i])
                except Exception:
                    return 0.0

            return {
                "gpu_util": num(2),
                "gpu_freq_mhz": num(1),
                "gpu_power_w": num(3),
                "gpu_temp_c": num(4),
                "gpu_mem_used_mb": num(5),
                "gpu_mem_total_mb": num(6),
            }
        except Exception:
            return None