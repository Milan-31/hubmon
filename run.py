"""Entry point for the hubmon monitor.

Kept separate from ``hubmon/main.py`` because that module uses
package-relative imports and so cannot be frozen as a top-level script
directly. Use this for both ``python run.py`` and PyInstaller.
"""
import sys

from hubmon.main import main

if __name__ == "__main__":
    sys.exit(main())