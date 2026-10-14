"""Build a single-file ``hubmon.exe`` with PyInstaller.

    pip install pyinstaller
    python build.py

Output: ``dist/hubmon.exe`` (one file, no console window).

PresentMon.exe is bundled only when it can be found locally; without it the
app still runs, the foreground frame-rate readout just stays blank.
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SPEC_DIR = os.path.join(ROOT, "build")


def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[build] PyInstaller is required: pip install pyinstaller")
        return 1

    from hubmon import config
    from hubmon.sensors import _presentmon_path

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile", "--noconsole",
        "--name", "hubmon",
        "--specpath", SPEC_DIR,
        "--workpath", SPEC_DIR,
        "--hidden-import", "usb.backend.libusb1",
        "--collect-all", "libusb_package",
        # soundcard reaches the WASAPI loopback through cffi, so its data files
        # and the cffi backend have to travel with the bundle
        "--collect-all", "soundcard",
        "--hidden-import", "cffi",
    ]

    # the app's own icon instead of PyInstaller's stock one; regenerate it with
    # `python assets/make_icon.py` if assets/hubmon.ico is ever missing
    icon = os.path.join(ROOT, "assets", "hubmon.ico")
    if os.path.exists(icon):
        args += ["--icon", icon]
        print(f"[build] icon: {icon}")
    else:
        print("[build] assets/hubmon.ico missing; the exe keeps the default icon")

    # resolve exactly like the app does, so a configured path is bundled too
    presentmon = _presentmon_path(config.load().presentmon_path)
    if presentmon:
        args += ["--add-binary", f"{presentmon}{os.pathsep}."]
        print(f"[build] bundling PresentMon: {presentmon}")
    else:
        print("[build] PresentMon not found; the frame-rate readout will be blank")

    args.append(os.path.join(ROOT, "run.py"))
    print("[build] running PyInstaller ...")
    return subprocess.call(args, cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())