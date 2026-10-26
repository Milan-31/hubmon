"""Entry point for the hubmon monitor.

Kept separate from ``hubmon/main.py`` because that module uses
package-relative imports and so cannot be frozen as a top-level script
directly. Use this for both ``python run.py`` and PyInstaller.
"""
import multiprocessing
import sys

if __name__ == "__main__":
    # the USB sender runs in a child process; frozen builds must route the
    # bootstrap arguments before anything else imports the app
    multiprocessing.freeze_support()
    from hubmon.main import main

    sys.exit(main())