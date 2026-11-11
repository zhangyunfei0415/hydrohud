"""Entry point for the PyInstaller build and for `python launcher.py`.

The import is deliberately static (no ``runpy.run_module``): PyInstaller only
bundles the modules it sees imported, so a string-based module lookup would
produce an executable that cannot find the ``hydrohud`` package.
"""

from __future__ import annotations

from hydrohud.app import main

if __name__ == "__main__":
    raise SystemExit(main())
