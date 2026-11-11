"""HydroHUD - open-source controller for the TCOMAS LG600 AIO LCD panel.

The package is split into layers:

- :mod:`hydrohud.device`      USB HID transport and frame encoding (no GUI)
- :mod:`hydrohud.render`      turns a scene dict into a 480x480 PIL image
- :mod:`hydrohud.scenes`      scene schema, presets, and project files
- :mod:`hydrohud.sensors`     system metric sampling (CPU/GPU/RAM/disk/network/history)
- :mod:`hydrohud.sysmon`      CPU/memory sampling for the system monitor scene
- :mod:`hydrohud.settings`    persisted UI preferences
- :mod:`hydrohud.autostart`   HKCU Run registration for launch at login
- :mod:`hydrohud.tray`        system tray icon and menu
- :mod:`hydrohud.hotkeys`     global hotkeys via RegisterHotKey
- :mod:`hydrohud.playlist`    rotating project playlist
- :mod:`hydrohud.diagnostics` crash and error log
- :mod:`hydrohud.app`         the Tkinter desktop application

Run it with ``python -m hydrohud`` or the packaged ``HydroHUD.exe``.
"""

from __future__ import annotations

__version__ = "1.2.0"
__all__ = ["__version__"]
