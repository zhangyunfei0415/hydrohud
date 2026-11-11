"""真机验证 v1.1：高画质推流帧率 + 托盘/热键/轮播/日志的运行时接线。

用真实设备推流约 6 秒，统计写入帧数与单帧字节数；不截图、不交互，跑完自动退出。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hydrohud.app as appmod  # noqa: E402
import hydrohud.device as device  # noqa: E402

SECONDS = 6.0
START_DELAY = 0.5


def build_project() -> Path:
    """构造一个带时钟图层的工程：动态场景才会走 30 fps 逐帧推流。"""
    photo = ROOT / "tools" / "_assets" / "demo_photo.png"
    project = {
        "name": "verify-v11",
        "background": {"type": "image", "path": str(photo), "fit": "crop"},
        "layers": [{"type": "clock", "x": 0.5, "y": 0.5, "size": 96, "color": "#E2E8F0"}],
    }
    target = ROOT / "_verify-photo.hud.json"
    target.write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
    return target


def main() -> int:
    print(f"vendor running: {device.vendor_software_running()}")
    killed = device.kill_vendor_software(timeout=3.0)
    if killed:
        print(f"killed: {killed}")

    project_path = build_project()
    frames = 0
    sizes: list[int] = []
    original = device.Screen.send_jpeg

    def counted(self, jpeg: bytes) -> None:
        nonlocal frames
        frames += 1
        if len(sizes) < 8:
            sizes.append(len(jpeg))
        original(self, jpeg)

    device.Screen.send_jpeg = counted  # type: ignore[method-assign]

    app = appmod.App(smoke_shot=None)
    started = time.monotonic()

    def report() -> None:
        elapsed = time.monotonic() - started - START_DELAY
        print(f"frames={frames} in {elapsed:.2f}s -> {frames / elapsed:.1f} fps")
        print(f"first jpeg sizes: {sizes}")
        print(f"app._stream_frames={app._stream_frames}")
        print(f"tray={app._tray is not None} hotkeys_ok={app._hotkey_ok}")
        print(f"playlist items={len(app._playlist.items) if app._playlist else 0}")
        log_path = appmod.diagnostics_mod.log_path() if appmod.diagnostics_mod else None
        print(f"diagnostics log={log_path}")
        app.on_close()

    app.load_project(str(project_path), notify=False)
    app.after(int(START_DELAY * 1000), app._on_start_stream)
    app.after(int((SECONDS + START_DELAY) * 1000), report)
    app.mainloop()

    device.Screen.send_jpeg = original  # type: ignore[method-assign]
    fps = frames / SECONDS
    ok = fps >= 27.0 and bool(sizes) and max(sizes) > 20000
    print("RESULT:", "OK" if ok else "FAIL", f"(fps={fps:.1f})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
