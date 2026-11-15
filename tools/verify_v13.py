"""真机验证 v1.3：两轮推流。

第一轮 ``static``：静态图片背景 + 7 类新图层 + 1 个插件图层，预期跑满 30 fps。
第二轮 ``capture``：屏幕捕获背景（每帧抓屏，成本更高），只要求 ≥ 10 fps 且画面不中断。

两轮各在一个子进程里跑，避免同一进程内先后销毁两个 Tk 根窗口后残留的
``after`` 回调触发 ``invalid command name`` 噪音。

用法：
    python tools\\verify_v13.py            # 跑两轮
    python tools\\verify_v13.py static     # 只跑一轮
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hydrohud.capture as capture  # noqa: E402
import hydrohud.device as device  # noqa: E402
import hydrohud.plugins as plugins  # noqa: E402

SECONDS = 6.0
START_DELAY = 0.5
MIN_FPS = {"static": 27.0, "capture": 10.0}

CLOCK = {"type": "clock", "x": 0.5, "y": 0.12, "size": 84, "color": "#FFFFFF"}
NEW_LAYERS = [
    {"type": "thermometer", "x": 0.08, "y": 0.55, "metric": "cpu_temp"},
    {"type": "countdown", "x": 0.5, "y": 0.42, "target": "2027-01-01 00:00:00"},
    {
        "type": "ticker",
        "x": 0.5,
        "y": 0.9,
        "text": "HydroHUD v1.3 真机验证 · 滚动文字 · ",
        "speed": 80.0,
    },
    {"type": "qrcode", "x": 0.86, "y": 0.24, "size": 0.2},
    {"type": "weather", "x": 0.5, "y": 0.62, "fields": ["temp", "desc"], "size": 24},
    {"type": "nowplaying", "x": 0.5, "y": 0.76, "fields": ["title", "artist"], "size": 22},
]
PLUGIN_LAYER = {"type": "sparkline", "x": 0.05, "y": 0.55, "w": 0.9, "h": 0.3, "thickness": 3}

BACKGROUNDS = {
    "static": {"type": "image", "path": "", "fit": "crop"},
    "capture": {"type": "capture", "region": [0.0, 0.0, 1.0, 1.0], "fps": 4.0},
}


def build_project(name: str) -> Path:
    """把工程写到仓库根，供 ``App.load_project`` 读取。"""
    background = dict(BACKGROUNDS[name])
    if background["type"] == "image":
        background["path"] = str(ROOT / "tools" / "_assets" / "demo_photo.png")
    project = {
        "name": f"verify-v13-{name}",
        "background": background,
        "layers": [CLOCK, *NEW_LAYERS, PLUGIN_LAYER],
    }
    target = ROOT / f"_verify-v13-{name}.hud.json"
    target.write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
    return target


def run_round(name: str) -> bool:
    """在一个子进程里用真实设备推流一轮。"""
    import hydrohud.app as appmod  # noqa: PLC0415 - 只有子进程需要 Tk

    project_path = build_project(name)
    frames = 0
    sizes: list[int] = []
    original = device.Screen.send_jpeg

    def counted(self, jpeg: bytes) -> None:
        nonlocal frames
        frames += 1
        if len(sizes) < 4:
            sizes.append(len(jpeg))
        original(self, jpeg)

    device.Screen.send_jpeg = counted  # type: ignore[method-assign]

    app = appmod.App(smoke_shot=None)
    started = time.monotonic()
    verdict: list[bool] = []

    def report() -> None:
        elapsed = time.monotonic() - started - START_DELAY
        fps = frames / elapsed if elapsed > 0 else 0.0
        ok = fps >= MIN_FPS[name] and bool(sizes) and max(sizes) > 20000
        print(f"[{name}] frames={frames} in {elapsed:.2f}s -> {fps:.1f} fps")
        print(f"[{name}] jpeg sizes: {sizes}")
        print(f"[{name}] layer count={len(app.scene['layers'])}")
        print(f"[{name}] RESULT:", "OK" if ok else "FAIL")
        verdict.append(ok)
        app.on_close()

    app.load_project(str(project_path), notify=False)
    app.after(int(START_DELAY * 1000), app._on_start_stream)
    app.after(int((SECONDS + START_DELAY) * 1000), report)
    app.mainloop()

    device.Screen.send_jpeg = original  # type: ignore[method-assign]
    return bool(verdict and verdict[0])


def run_child(name: str) -> int:
    """子进程入口。"""
    return 0 if run_round(name) else 1


def main(argv: list[str]) -> int:
    names = argv or list(BACKGROUNDS)
    for name in names:
        if name not in BACKGROUNDS:
            print(f"未知轮次：{name}（可选 {', '.join(BACKGROUNDS)}）")
            return 2

    print(f"vendor running: {device.vendor_software_running()}")
    killed = device.kill_vendor_software(timeout=3.0)
    if killed:
        print(f"killed: {killed}")
    plugins.discover()
    print(f"plugins: {sorted(plugins.registry().specs)}")
    print(f"capture bounds: {capture.virtual_bounds()}")

    python = sys.executable or "python"
    results: dict[str, bool] = {}
    for name in names:
        completed = subprocess.run(
            [python, "-X", "utf8", str(Path(__file__).resolve()), name],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for line in completed.stdout.splitlines():
            if line.startswith(f"[{name}]"):
                print(line)
        for line in completed.stderr.splitlines():
            if "invalid command name" in line:
                continue
            print(line, file=sys.stderr)
        results[name] = completed.returncode == 0

    overall = all(results.values())
    detail = " ".join(f"{name}={ok}" for name, ok in results.items())
    print("RESULT:", "OK" if overall else "FAIL", f"({detail})")
    return 0 if overall else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in BACKGROUNDS:
        raise SystemExit(run_child(sys.argv[1]))
    raise SystemExit(main(sys.argv[1:]))
