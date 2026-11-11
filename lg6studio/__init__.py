"""LG600 Studio - open-source controller for the TCOMAS LG600 AIO LCD panel.

The package is split into four layers:

- :mod:`lg6studio.device`   USB HID transport and frame encoding (no GUI)
- :mod:`lg6studio.render`   turns a scene dict into a 480x480 PIL image
- :mod:`lg6studio.scenes`   scene schema, presets, and project files
- :mod:`lg6studio.app`      the Tkinter desktop application

Run it with ``python -m lg6studio`` or the packaged ``LG600Studio.exe``.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
