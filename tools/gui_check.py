"""无人值守的 GUI 集成自检：逐项点一遍 v1.3 新增功能，输出 PASS/FAIL。

用法::

    C:\\Python314\\python.exe -X utf8 tools\\gui_check.py [--shots 目录]

脚本会真实创建 Tk 窗口（隐藏标题栏不影响），逐项调用界面动作后立即撤回，
不连接设备、不推流、不发起网络请求。任何一项抛异常都记为 FAIL 并以非零码退出。
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gui_check")
    parser.add_argument("--shots", metavar="DIR", default=None, help="把每步截图写到该目录")
    args = parser.parse_args(argv)

    import tkinter as tk

    from hydrohud import app as app_mod
    from hydrohud import scenes

    shots = Path(args.shots) if args.shots else None
    if shots is not None:
        shots.mkdir(parents=True, exist_ok=True)

    root_app = app_mod.App()
    root = root_app
    root.withdraw()
    results: list[tuple[str, str]] = []

    def check(name: str, action) -> None:
        try:
            action()
            root.update()
            results.append((name, "PASS"))
        except Exception as exc:  # 自检脚本：把失败收集进报告
            results.append((name, f"FAIL {type(exc).__name__}: {exc}"))
            traceback.print_exc()

    def shot(name: str) -> None:
        if shots is None:
            return
        from PIL import ImageGrab

        root.deiconify()
        root.update()
        try:
            x, y = root.winfo_rootx(), root.winfo_rooty()
            w, h = root.winfo_width(), root.winfo_height()
            ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(shots / f"{name}.png")
        finally:
            root.withdraw()
            root.update()

    # 1) 每个新图层类型都能添加并渲染
    for kind in (
        "thermometer",
        "countdown",
        "ticker",
        "sequence",
        "qrcode",
        "weather",
        "nowplaying",
    ):
        check(f"添加图层 {kind}", lambda k=kind: _add_layer_silently(root_app, k))

    # 2) 每个新图层都能在编辑器面板里打开
    count = len(root_app.scene.get("layers") or [])
    for index in range(count):
        check(
            f"编辑器渲染图层 #{index}",
            lambda i=index: _select_layer(root_app, i),
        )

    # 3) 预设里全部 11 个都能加载
    for preset_name in scenes.preset_scenes():
        check(
            f"加载预设 {preset_name}",
            lambda n=preset_name: _load_preset(root_app, n),
        )

    # 4) 背景切成屏幕捕获并改区域
    check("背景切屏幕捕获", lambda: _set_capture_background(root_app))

    # 5) 撤销 / 重做
    check("撤销", root_app._undo)
    check("重做", root_app._redo)

    # 6) 画布编辑模式开关
    check("进入画布编辑", root_app._toggle_canvas_edit)
    check("退出画布编辑", root_app._toggle_canvas_edit)

    # 7) 主题包导出 + 重新导入
    check("导出主题包", lambda: _export_theme(root_app, shots))
    check("导入主题包", lambda: _import_theme(root_app, shots))

    # 8) 插件重载（本机可能没有任何插件）
    check("重载插件", root_app._on_reload_plugins)

    # 9) 本地 API token（不起服务）
    check("生成 API token", lambda: _token_text())

    shot("final")

    root_app.on_close()  # 内含 shutdown + destroy，之后不能再操作窗口

    width = max(len(name) for name, _ in results) + 2
    failures = 0
    for name, outcome in results:
        if outcome != "PASS":
            failures += 1
        print(f"{name:<{width}} {outcome}")
    print(f"\n{len(results) - failures} passed, {failures} failed of {len(results)} checks")
    return 1 if failures else 0


def _add_layer_silently(app, kind: str) -> None:
    """调用 `_add_layer`，但把交互式对话框替换成固定答案。"""
    from unittest import mock

    answers = {
        "countdown": {"target": "2030-01-01 00:00", "title": "自检倒计时"},
        "ticker": {"text": "自检滚动文字 · HydroHUD"},
        "qrcode": {"data": "https://github.com/zhangyunfei0415/hydrohud"},
        "weather": {"place": "北京", "lat": 39.9042, "lon": 116.4074},
    }

    def fake_askstring(*_a, **_kw):
        return answers.get(kind, {}).get("target") or answers.get(kind, {}).get("text") or "自检"

    def fake_askfloat(*_a, **_kw):
        return 39.9042

    def fake_askopenfilenames(*_a, **_kw):
        sample = (ROOT / "assets" / "screenshot.png").as_posix()
        return (sample,)

    patches = [
        mock.patch("tkinter.simpledialog.askstring", fake_askstring),
        mock.patch("tkinter.simpledialog.askfloat", fake_askfloat),
        mock.patch("tkinter.filedialog.askopenfilenames", fake_askopenfilenames),
        mock.patch("tkinter.filedialog.askopenfilename", lambda *a, **k: ""),
    ]
    for patch in patches:
        patch.start()
    try:
        app._add_layer(kind)
    finally:
        for patch in reversed(patches):
            patch.stop()
    layers = app.scene.get("layers") or []
    assert layers, f"添加 {kind} 后场景里没有图层"
    assert layers[-1].get("type") == kind, f"最后一个是 {layers[-1].get('type')}，期望 {kind}"
    app._show_preview_image(_render_current(app))


def _render_current(app):
    from hydrohud import render

    return render.render_scene(app._snapshot_scene(), 0.0)


def _select_layer(app, index: int) -> None:
    app._layer_index = index
    app._rebuild_layer_editor()
    app._show_preview_image(_render_current(app))


def _load_preset(app, name: str) -> None:
    app._load_preset(name)
    app._show_preview_image(_render_current(app))


def _set_capture_background(app) -> None:
    app._set_background(type="capture", region=[0.25, 0.25, 0.5, 0.5], fps=5.0, monitor=0)
    assert app.scene["background"]["type"] == "capture"
    app._show_preview_image(_render_current(app))


def _export_theme(app, shots: Path | None) -> None:
    from unittest import mock

    target = (shots or Path.cwd()) / "_check.hudpack"
    with mock.patch("tkinter.filedialog.asksaveasfilename", lambda *a, **k: str(target)):
        app._on_export_theme()
    assert target.exists(), "主题包没有生成"


def _import_theme(app, shots: Path | None) -> None:
    from unittest import mock

    target = (shots or Path.cwd()) / "_check.hudpack"
    with mock.patch("tkinter.filedialog.askopenfilename", lambda *a, **k: str(target)):
        app._on_import_theme()
    assert app.scene.get("layers"), "导入主题包后场景为空"


def _token_text() -> None:
    from hydrohud import api

    token = api.ensure_token()
    assert token and len(token) >= 16


if __name__ == "__main__":
    sys.exit(main())
