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
        "dashboard",
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
    check("插件参数表单", lambda: _add_param_plugin_layer(root_app))
    check("社区预设库", lambda: _open_preset_library(root_app))

    # 9) 本地 API token（不起服务）
    check("生成 API token", lambda: _token_text())

    # 10) 仪表盘的「开关显示」：附加指标勾选 + 图层可见性
    check("仪表盘开关指标", lambda: _check_dashboard_toggles(root_app))

    # 11) 推流按钮只保留底栏一份，左侧图标栏不再重复
    check("推流按钮不重复", lambda: _check_rail_has_no_stream_buttons(root_app))

    # 12) 待机图写入：编码 → 上传 → 进度 → 完成（全程桩设备，不碰真机）
    check("待机图写入流程", lambda: _check_flash_standby_write(root_app))

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


def _open_preset_library(app) -> None:
    """打开社区预设库窗口（离线时会回落缓存），确认列表控件与安装按钮存在。"""
    import time
    from unittest import mock

    from hydrohud import presetslib

    scene = {
        "name": "自检社区预设",
        "layers": [{"type": "text", "text": "自检", "x": 0.5, "y": 0.5}],
    }
    entries = [
        {"name": "自检社区预设", "author": "gui_check", "scene": scene, "version": "1.0"},
    ]
    with mock.patch.object(presetslib, "index_or_cache", lambda *a, **k: (entries, False)):
        app._on_open_preset_library()
        dialog = app._preset_library
        assert dialog is not None, "预设库窗口没有创建"
        for _ in range(200):
            app.update()
            if _find_listbox_count(dialog) > 0:
                break
            time.sleep(0.01)
        assert _find_listbox_count(dialog) == 1, "预设库列表没有填充条目"
        dialog.destroy()
        app._preset_library = None


def _widget_text(widget) -> str:
    """递归收集控件树里所有 Label 的文本，用于断言界面确实渲染了某段提示。"""
    try:
        own = str(widget.cget("text"))
    except Exception:  # 不是带 text 选项的控件
        own = ""
    parts = [own]
    for child in widget.winfo_children():
        parts.append(_widget_text(child))
    return " ".join(part for part in parts if part)


def _find_listbox_count(widget) -> int:
    """递归找对话框里的 Listbox，返回第一个的条目数。"""
    import tkinter as tk

    for child in widget.winfo_children():
        if isinstance(child, tk.Listbox):
            return int(child.size())
        found = _find_listbox_count(child)
        if found:
            return found
    return 0


def _add_param_plugin_layer(app) -> None:
    """写一个带 params 声明的临时插件，添加图层并确认编辑器生成了控件。"""
    import tempfile

    from hydrohud import plugins

    spec_text = (
        "PLUGIN = {\n"
        '    "name": "gaugex",\n'
        '    "label": "自检参数插件",\n'
        '    "params": [\n'
        '        {"key": "thickness", "label": "线宽", "type": "int", "default": 3,'
        ' "min": 1, "max": 12},\n'
        '        {"key": "ratio", "label": "比例", "type": "float", "default": 0.4},\n'
        '        {"key": "accent", "label": "高亮色", "type": "color",'
        ' "default": "#38BDF8"},\n'
        '        {"key": "mode", "label": "模式", "type": "choice", "default": "sine",\n'
        '         "options": [["sine", "正弦"], ["square", "方波"]]},\n'
        '        {"key": "enabled", "label": "启用", "type": "bool", "default": True},\n'
        '        {"key": "note", "label": "备注", "type": "text", "default": "hello"},\n'
        "    ],\n"
        '    "permissions": ["network"],\n'
        "}\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "gaugex.py").write_text(spec_text, encoding="utf-8")
        registry = plugins.registry()
        original_dir = registry.plugin_dir
        registry.plugin_dir = Path(tmp)
        try:
            assert registry.discover() >= 1, "临时插件没有注册成功"
            app._refresh_plugin_buttons()
            app._add_plugin_layer("gaugex")
            assert app.scene["layers"][-1]["type"] == "gaugex"
            app._layer_index = len(app.scene["layers"]) - 1
            app._rebuild_layer_editor()
            assert app._layer_editor.winfo_children(), "插件图层编辑器为空"
            layer = app._selected_layer()
            assert layer is not None and layer.get("thickness") == 3
            assert "网络访问" in _widget_text(app._layer_editor), "插件权限声明没有显示"
            app._show_preview_image(_render_current(app))
        finally:
            registry.plugin_dir = original_dir
            registry.discover()
            app._refresh_plugin_buttons()


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


def _check_dashboard_toggles(app) -> None:
    """仪表盘的「换指标 / 关附加行 / 隐藏图层」三条路径都要真的影响渲染。"""
    from unittest import mock

    from hydrohud import render

    app._load_preset("Aurora 仪表盘")
    layers = app.scene.get("layers") or []
    assert layers and layers[0].get("type") == "dashboard", "Aurora 预设里没有仪表盘图层"
    app._layer_index = 0
    app._rebuild_layer_editor()

    # 附加指标是开关列表：清空后渲染结果必须真的变化（少掉底部行）
    before = render.render_scene(app._snapshot_scene(), 0.0).tobytes()
    with mock.patch.object(app, "_ask_metrics", lambda *a, **kw: []):
        app._apply_layer_field("extras", [])
    app._rebuild_layer_editor()
    assert app._selected_layer().get("extras") == [], "附加指标没有清空"
    after = render.render_scene(app._snapshot_scene(), 0.0).tobytes()
    assert before != after, "清空附加指标后画面没有变化"

    # 勾上 GPU 功耗 / 频率
    for metric in ("gpu_power", "gpu_clock", "gpu_temp"):
        app._apply_layer_field("extras", list(app._selected_layer().get("extras") or []) + [metric])
    assert set(app._selected_layer().get("extras") or []) >= {"gpu_power", "gpu_clock", "gpu_temp"}

    # 图层可见性开关：隐藏后画面必须回到只剩背景
    app._rebuild_layer_editor()
    assert "可见" in _widget_text(app._layer_editor), "编辑器里没有可见性开关"
    app._apply_layer_field("visible", False)
    app._refresh_layer_list()
    assert "已隐藏" in str(app._layer_list.get(0)), "图层列表没有标记隐藏状态"
    hidden = render.render_scene(app._snapshot_scene(), 0.0).tobytes()
    assert hidden != after, "隐藏图层后画面没有变化"
    app._apply_layer_field("visible", True)
    app._refresh_layer_list()
    assert "已隐藏" not in str(app._layer_list.get(0))
    app._show_preview_image(render.render_scene(app._snapshot_scene(), 0.0))


def _check_rail_has_no_stream_buttons(app) -> None:
    """推流/停止只允许出现在底栏一份；左侧图标栏只做导航，避免重复入口。"""
    rail_text = _widget_text(app._rail)
    for word in ("推送", "停止"):
        assert word not in rail_text, f"左侧图标栏里仍有「{word}」按钮，与底栏重复"
    assert app._btn_start is not None and app._btn_stop is not None, "底栏推流按钮缺失"
    assert "开始推送到屏幕" in str(app._btn_start.cget("text"))
    assert "停止" in str(app._btn_stop.cget("text"))


def _check_flash_standby_write(app) -> None:
    """待机图写入的完整路径：选图 → 确认 → 编码上传 → 进度 → 完成提示。

    全程替换掉 `device.Control`（设备打开会真的占住 HID 接口），只用桩记录调用。
    """
    import tempfile
    import time
    from unittest import mock

    from PIL import Image

    from hydrohud import device

    calls: dict[str, object] = {}

    class StubControl:
        def open(self) -> StubControl:
            calls["opened"] = True
            return self

        def upload_file(self, data, target=0, progress=None) -> None:
            calls["data"] = data
            calls["target"] = target
            if progress is not None:
                progress(0.5)
                progress(1.0)

        def set_power(self, on: bool) -> None:
            calls.setdefault("power", []).append(on)  # type: ignore[union-attr]

        def close(self) -> None:
            calls["closed"] = True

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "standby.png"
        Image.new("RGB", (640, 480), (12, 34, 56)).save(path)
        with (
            mock.patch.object(device, "Control", StubControl),
            mock.patch("tkinter.filedialog.askopenfilename", lambda *a, **k: str(path)),
            mock.patch("tkinter.messagebox.askyesno", lambda *a, **k: True),
        ):
            app._on_write_standby_image()
            for _ in range(300):
                app.update()
                if not app._flash_busy:
                    break
                time.sleep(0.01)
            assert not app._flash_busy, "待机图写入没有结束"

    assert calls.get("uploaded") is not None or calls.get("data"), "没有真正上传数据"
    payload = calls["data"]
    assert isinstance(payload, bytes) and payload[:2] == b"\xff\xd8", "上传的不是 JPEG"
    assert len(payload) <= device.FLASH_IMAGE_MAX_BYTES
    assert calls["target"] == device.FLASH_TARGET_STANDBY, "写错了 flash 槽位"
    assert calls.get("closed") is True, "写完没有关掉控制口"
    assert calls.get("power") == [False, True], "写完没有关屏再开屏"
    assert "关屏再开屏" in _widget_text(app._rail.master), "界面没有提示需要关屏再开屏"


def _token_text() -> None:
    from hydrohud import api

    token = api.ensure_token()
    assert token and len(token) >= 16


if __name__ == "__main__":
    sys.exit(main())
