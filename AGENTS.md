# AGENTS.md — HydroHUD

This file is written **for coding agents** (Claude Code / Codex / Cursor / any LLM agent) that are asked to
extend HydroHUD, or to port it to a different USB-controlled screen. Humans wanting to use the app should
read [README.md](README.md) instead.

HydroHUD is an open-source controller for the **TCOMAS LG600** AIO liquid cooler's 480×480 USB display
(USB `1A2C:1520`). It speaks the vendor HID protocol directly — no vendor DLL, no vendor service.

---

## 1. Non-negotiables (violating these bricks the panel or the test suite)

| Rule | Why |
| --- | --- |
| **Every frame must be a 480×480 JPEG.** | Sending any other resolution makes the firmware stall and the device drop off the USB bus (empirically verified: 640×640 → `WriteFile` error 31/433, ~5 s of re-enumeration). |
| **The stream must never stop while the screen is on.** | The panel has a watchdog: a few seconds after the last packet it blanks / falls back to the vendor image. Static content is re-sent every 2 s by the app; `Screen.keepalive()` is the primitive. |
| **Frame budget is 46 packets × 1020 bytes = 46920 bytes.** | Verified on hardware. `device.encode_jpeg()` walks a quality ladder down until the JPEG fits. Never exceed it. |
| **`hydrohud/device.py` stays dependency-free.** | It uses `ctypes` against `hid.dll` / `setupapi.dll` only. No `hidapi`, no `pyusb`, no `libusb`. This is what makes the packaged exe work with zero drivers. |
| **Vendor software and HydroHUD are mutually exclusive.** | The HID interface cannot be opened twice. `device.kill_vendor_software()` handles it; never "retry in a loop" instead. |
| **`hydrohud/sensors.py` must not call into the OS at import time.** | The GUI imports it at startup; sampling must stay lazy (see `get_hub()` / `SensorHub.sample()`). |
| **No new heavyweight dependencies.** | The exe is a single ~73 MB file; that is a feature. New deps must work under PyInstaller and be justified. |

## 2. Architecture in one screen

```
        ┌─────────────────────────────────────────────────────────────┐
        │ hydrohud/app.py            Tkinter GUI + stream thread      │
        │   RenderWorker → render.render_scene(scene, t)               │
        │        ↓  PIL.Image 480×480                                  │
        │   device.encode_jpeg(img, max_bytes=MAX_FRAME_BYTES)         │
        │        ↓  JPEG bytes                                         │
        │   device.frame_reports(jpeg) → list[bytes] (1025 B each)      │
        │        ↓                                                     │
        │   device.Screen.write_report(report)  → HID MI_01            │
        └─────────────────────────────────────────────────────────────┘
   scene dict (.hud.json)        pools of presets/schema: hydrohud/scenes.py
   metrics/history:              hydrohud/sensors.py
   device control (brightness…): hydrohud/device.py::Control  → HID MI_00
```

| Module | Owns | Never touches |
| --- | --- | --- |
| `device.py` | HID enumeration, 1025-byte framing, JPEG encoding, `Screen`, `Control`, vendor-process handling | Tk, scenes, sensors |
| `render.py` | scene dict → `PIL.Image`; layer painters (text/clock/sysinfo/bars/chart/gauge/image); LRU caches | device, Tk |
| `scenes.py` | schema defaults/validation (`normalize_scene`), 11 presets, `.hud.json` load/save | device, Tk |
| `sensors.py` | 23 metrics, `SensorHub` (0.5 s throttle + 120-sample history), NVML / Win32 / LHM-shared-memory backends | Tk, device |
| `app.py` | everything UI: layout, editor, stream loop with absolute-deadline pacing, tray/hotkeys/playlist wiring | protocol details |
| `settings.py` `autostart.py` `tray.py` `hotkeys.py` `playlist.py` `diagnostics.py` | persistence, Run key, Shell_NotifyIconW, RegisterHotKey, rotation list, crash log | domain logic |

**Data flow for a new layer type:** `scenes.py` (defaults + validation) → `render.py` (`_draw_*_layer`,
`_render_layer` dispatch, `_DYNAMIC_TYPES`, cache signature) → `app.py` (`layer_label`, `_add_layer` defaults,
`_rebuild_layer_editor` controls) → `tests/`.

## 3. Protocol cheat sheet

Full reverse-engineering notes live in [docs/PROTOCOL.md](docs/PROTOCOL.md). The essentials:

**Image interface (MI_01, output report 1025 bytes)**

```
offset 0   1B  report id        0x00
offset 1   1B  command          0x08 = image stream
offset 2   1B  first packet: chunk count; later packets: 1-based chunk index
offset 3   1B  reserved         0x00
offset 4   1B  flags            first packet 0x80 (JPEG SOI), others 0x00
offset 5   1020B payload        raw JPEG bytes; pad the last packet with 0x00
```

**Control interface (MI_00, output report 441 bytes)** — same framing with `chunk_size=436`:

| cmd | payload | meaning |
| --- | --- | --- |
| `0x81` | — | query; 441-byte reply, offset 5 = rotation quadrant |
| `0x12` | 0–100 | brightness |
| `0x10` | 0=on, 1=off | panel power |
| `0x11` | 0–3 | rotation setting (stored only — **does not** rotate the stream; rotate pixels in Pillow) |
| `0x0A`/`0x0B` | — | flash upload (startup image) — **untested** |
| `0x18`/`0x1A`/`0x1E` | — | temperature display / content type / loop — **untested** |

## 4. Recipe: port HydroHUD to another USB screen

This is the part the project is designed around: the vendor protocol is the only thing that changes.

1. **Identify the device.** Windows: `tools/check_device.py` (prints VID/PID, interfaces, report lengths).
   Linux: `lsusb -v`, `dmesg | grep -i hid`. Record: VID, PID, transport (HID reports? USB bulk? CDC serial?),
   interface count, `OutputReportByteLength` per interface.
2. **Capture what the vendor software sends.** The method that worked here:
   Frida `Interceptor.attach` on `kernel32!WriteFile` / `ReadFile` / `DeviceIoControl` in the vendor GUI
   (32-bit MFC process → **never** use `Interceptor.replace`, it crashes the process). Dump payloads as JSONL,
   then reassemble (`tools` in the sibling `lg600` project did exactly this).
   Alternative when no vendor software exists: `Wireshark` + `USBPcap`.
3. **Find the pixel format.** Look for JPEG `FF D8 FF` in captures (that is this panel), or fixed-size blobs of
   `W*H*2` (RGB565) / `W*H*3` (RGB888) bytes. Identify chunk headers by diffing consecutive packets: length field,
   index field, first/last-packet flags.
4. **Write the transport.** Create `hydrohud/devices/<vendor>_<model>.py` implementing the same surface as
   `device.Screen`: `open/close`, `send_jpeg`, `keepalive`, plus a `Control` equivalent. Reuse
   `build_packets()` — it already handles "first packet carries the chunk count".
5. **Register it.** Add the VID/PID + transport class to a device table (see ROADMAP v1.4: `devices.py`),
   so `find_image_interface()` becomes `find_device()`.
6. **Verify in this order** — each step is cheap and isolates one failure mode:
   ① enumerate and print caps → ② send a single solid-colour frame → ③ send one frame and watch for 10 s
   (does it persist? if not, you have a watchdog: keep streaming) → ④ push a photo at the largest size you
   believe works, then shrink (find the real byte ceiling) → ⑤ try a wrong resolution **last** (it may drop the
   device off the bus, so you want a known-good state first).
7. **Document the new panel** in `docs/PROTOCOL.md` (a table row plus the packet layout) and in the README
   compatibility table. Reverse-engineered facts belong in the repo, not in chat history.

## 5. Recipe: add a scene layer

```python
# hydrohud/scenes.py — inside _norm_layer(), add a branch
if kind == "thermometer":
    layer["metric"] = _as_metric(layer.get("metric"), "cpu_temp")
    layer["w"] = _as_bound(layer.get("w")) or 0.1
    ...
```

Then in `hydrohud/render.py`: write `_draw_thermometer_layer(draw, layer, values)`, dispatch it in
`_render_layer()`, add `"thermometer"` to `_DYNAMIC_TYPES` if the value changes over time, and include it in
`_layer_signature()` / `_layer_overlay()` cache keys. Finally expose it in `app.py::_add_layer()` and
`_rebuild_layer_editor()`, and add a test in `tests/test_render_layers.py` using `FakeHub`.

## 6. Commands

```powershell
C:\Python314\python.exe -m pytest tests -q          # 184 tests, all pure-software
C:\Python314\python.exe -m black --line-length 100 hydrohud tests tools
C:\Python314\python.exe -m hydrohud                 # run from source
C:\Python314\python.exe -m hydrohud --project examples\性能曲线.hud.json --autostream --smoke-shot out.png
powershell -ExecutionPolicy Bypass -File build.ps1 -Clean   # → dist\HydroHUD.exe
C:\Python314\python.exe tools\check_device.py       # real hardware self-check
C:\Python314\python.exe tools\verify_v11.py         # real-device fps regression
C:\Python314\python.exe -X utf8 tools\render_previews.py    # regenerate preview/ + contact sheet
```

GUI changes are verified headlessly with `--smoke-shot <png>`: it renders for 1.2 s, saves the preview and the
whole window, then exits 0. Use it before claiming a UI change works.

## 7. Hardware facts already established (do not re-litigate)

| Question | Answer | Evidence |
| --- | --- | --- |
| Does the firmware accept more than 15 packets? | **Yes, 46 packets / 46920 B render completely.** Vendor software's 15-packet habit was a habit, not a limit. | E1 experiment: 46-packet 45.7 KB JPEG rendered fully; truncated 15300 B / 30600 B frames showed exactly the decodable prefix. |
| Can we send 640×640 to get more detail? | **No.** The device stalls and drops off the bus. | E5 experiment. |
| Is a single frame latched? | **No.** ~10 s watchdog. | Stopped-stream observation. |
| Does control `0x11` rotate the picture? | **No**, it only stores the quadrant. Rotate pixels in software. | Hardware test: no visible change; vendor `XYUI` does software rotation. |
| Does `read()` include the report id on Windows? | **No** with cython-hidapi (440-byte reply vs 441 with hidraw). Parse defensively. | liquidctl driver work on the same panel. |
| Max sustained frame rate? | ~500 fps free-running; 30 fps costs ~6 % of the bus. | Throughput benchmark. |

## 8. Style rules for contributors (human or agent)

- Chinese UI strings, English identifiers and docstrings, `black` at 100 columns, `pathlib`/`from __future__ import annotations`.
- GUI must degrade gracefully: `_import_optional()` means a missing module becomes a status-bar warning, never a crash.
- Anything model-visible or user-visible gets a test; anything protocol-related gets a line in `docs/PROTOCOL.md`.
- Never add telemetry or network calls. The app is fully offline by design.
