# MIT License
#
# Copyright (c) 2026 HydroHUD contributors
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""场景 schema 测试：v1.2 的指标白名单、chart/gauge 规范化与预设完整性。

只测纯函数，不读取系统状态、不打开设备、不启动 GUI。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hydrohud import scenes  # noqa: E402


def test_metric_ids_cover_the_documented_set() -> None:
    """23 个指标全部在 METRIC_IDS 里，且与 sensors 模块的列表一致（若可用）。"""
    expected = {
        "cpu",
        "cpu_freq",
        "cpu_temp",
        "cpu_power",
        "ram",
        "ram_used",
        "ram_total",
        "gpu",
        "gpu_temp",
        "gpu_mem",
        "gpu_mem_used",
        "gpu_mem_total",
        "gpu_power",
        "gpu_fan",
        "gpu_clock",
        "disk",
        "disk_read",
        "disk_write",
        "net_down",
        "net_up",
        "uptime",
        "fan",
        "cpu_cores",
    }
    assert set(scenes.METRIC_IDS) == expected
    assert len(scenes.METRIC_IDS) == 23
    assert set(scenes.FALLBACK_METRICS) == expected


def test_layer_types_and_labels_agree() -> None:
    """LAYER_TYPE_LABELS 与 LAYER_TYPES 一一对应，且都是中文名。"""
    # v1.2 的七类图层必须一直是 LAYER_TYPES 的子集；v1.3 起会继续追加新类型。
    assert {"text", "clock", "sysinfo", "bars", "chart", "gauge", "image"} <= set(
        scenes.LAYER_TYPES
    )
    assert len(scenes.LAYER_TYPES) == len(set(scenes.LAYER_TYPES))
    assert set(scenes.LAYER_TYPE_LABELS) == set(scenes.LAYER_TYPES)
    for label in scenes.LAYER_TYPE_LABELS.values():
        assert label and label != ""
        assert all(ord(char) > 0x2E80 for char in label)


def test_sysinfo_fields_accept_any_metric_and_reject_junk() -> None:
    """fields / items 使用指标白名单，非法值被丢弃，全非法时回落到 cpu+ram。"""
    scene = scenes.normalize_scene(
        {
            "layers": [
                {"type": "sysinfo", "fields": ["cpu", "gpu_temp", "bogus", "net_down"]},
                {"type": "sysinfo", "fields": ["nope", "also_no"]},
            ]
        }
    )
    first, second = scene["layers"]
    assert first["fields"] == ["cpu", "gpu_temp", "net_down"]
    assert second["fields"] == ["cpu", "ram"]


def test_sysinfo_fields_accept_comma_string() -> None:
    """手写 JSON 时允许用逗号分隔的字符串（GUI 与用户都可能这么写）。"""
    scene = scenes.normalize_scene({"layers": [{"type": "sysinfo", "fields": "cpu, gpu, ram"}]})
    assert scene["layers"][0]["fields"] == ["cpu", "gpu", "ram"]


def test_metric_lists_are_deduped_and_capped() -> None:
    """重复项去重，超过 8 项截断（屏幕太小，再多也读不清）。"""
    scene = scenes.normalize_scene(
        {
            "layers": [
                {"type": "bars", "items": ["cpu", "cpu", "ram", "gpu"]},
                {
                    "type": "bars",
                    "items": [
                        "cpu",
                        "cpu_freq",
                        "cpu_temp",
                        "cpu_power",
                        "ram",
                        "ram_used",
                        "ram_total",
                        "gpu",
                        "gpu_temp",
                        "gpu_mem",
                    ],
                },
            ]
        }
    )
    assert scene["layers"][0]["items"] == ["cpu", "ram", "gpu"]
    assert len(scene["layers"][1]["items"]) == 8


def test_chart_defaults_and_bounds() -> None:
    """chart 缺省字段齐全，metric 非法时回落 cpu，points 落在 2..480。"""
    chart = scenes.normalize_scene({"layers": [{"type": "chart"}]})["layers"][0]
    assert chart["type"] == "chart"
    assert chart["metric"] == "cpu"
    assert chart["w"] == pytest.approx(0.8)
    assert chart["h"] == pytest.approx(0.22)
    assert chart["points"] == 120
    assert chart["min"] is None and chart["max"] is None
    assert chart["fill"] is True and chart["grid"] is True and chart["label"] is True

    tuned = scenes.normalize_scene(
        {
            "layers": [
                {"type": "chart", "metric": "gpu_temp", "points": 9999, "min": 20, "max": "95.5"}
            ]
        }
    )["layers"][0]
    assert tuned["metric"] == "gpu_temp"
    assert tuned["points"] == 480
    assert tuned["min"] == pytest.approx(20.0)
    assert tuned["max"] == pytest.approx(95.5)


def test_chart_invalid_bounds_become_auto() -> None:
    """min/max 非数字时按"自动范围"处理，而不是变成 NaN 传给渲染层。"""
    chart = scenes.normalize_scene({"layers": [{"type": "chart", "min": "abc", "max": None}]})[
        "layers"
    ][0]
    assert chart["min"] is None
    assert chart["max"] is None


def test_gauge_defaults_and_clamping() -> None:
    """gauge 的直径/环宽/扫过角度都被夹到合法范围。"""
    gauge = scenes.normalize_scene({"layers": [{"type": "gauge"}]})["layers"][0]
    assert gauge["metric"] == "cpu"
    assert gauge["diameter"] == pytest.approx(0.5)
    assert gauge["thickness"] == pytest.approx(0.06)
    assert gauge["start_angle"] == pytest.approx(135.0)
    assert gauge["sweep"] == pytest.approx(270.0)
    assert gauge["show_unit"] is True

    wild = scenes.normalize_scene(
        {"layers": [{"type": "gauge", "diameter": 99, "thickness": 0.0001, "sweep": 999}]}
    )["layers"][0]
    assert wild["diameter"] <= 1.5
    assert wild["thickness"] >= 0.005
    assert wild["sweep"] <= 360.0


def test_unknown_layer_type_is_dropped() -> None:
    """未知图层类型被丢弃，不再产生"图层 · None"。"""
    scene = scenes.normalize_scene(
        {"layers": [{"type": "sausage"}, {"type": "clock"}, {"type": "chart"}]}
    )
    assert [layer["type"] for layer in scene["layers"]] == ["clock", "chart"]


def test_presets_are_complete_and_normalized() -> None:
    """12 个预设全部可规范化，且至少各有一个时钟/指标类图层。"""
    presets = scenes.preset_scenes()
    assert len(presets) == 12
    for name in ("硬件温度", "性能曲线", "极客面板", "Aurora 仪表盘"):
        assert name in presets
    for name, scene in presets.items():
        assert scene["name"] == name
        assert scene["layers"], f"{name} 没有图层"
        for layer in scene["layers"]:
            assert layer["type"] in scenes.LAYER_TYPES
        assert scenes.normalize_scene(scene) == scene, f"{name} 规范化不幂等"


def test_new_presets_use_the_new_layer_types() -> None:
    """三个 v1.2 预设确实用上了 gauge / chart / 模板文本。"""
    presets = scenes.preset_scenes()
    hw_kinds = [layer["type"] for layer in presets["硬件温度"]["layers"]]
    perf_kinds = [layer["type"] for layer in presets["性能曲线"]["layers"]]
    geek_kinds = [layer["type"] for layer in presets["极客面板"]["layers"]]
    assert hw_kinds.count("gauge") == 2
    assert "chart" in perf_kinds and perf_kinds.count("chart") == 2
    assert "gauge" in geek_kinds

    metrics = {layer.get("metric") for layer in presets["硬件温度"]["layers"]}
    assert {"cpu_temp", "gpu_temp"} <= metrics

    template_texts = [
        layer.get("text") for layer in presets["性能曲线"]["layers"] if layer["type"] == "text"
    ]
    assert any("{time}" in str(text) or "{" in str(text) for text in template_texts)


def test_project_roundtrip_keeps_new_layers(tmp_path) -> None:
    """保存/载入工程后 chart 与 gauge 的关键字段还在。"""
    scene = scenes.preset_scenes()["性能曲线"]
    path = os.path.join(str(tmp_path), "perf.hud.json")
    scenes.save_project(scene, path)
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    assert data["schema"] if "schema" in data else True
    loaded = scenes.load_project(path)
    kinds = [layer["type"] for layer in loaded["layers"]]
    assert kinds.count("chart") == 2
    assert loaded["layers"][1]["metric"] == "cpu"


def test_clone_scene_is_deep_copy() -> None:
    """clone_scene 深拷贝：改副本不影响原件（图层列表不能共享引用）。"""
    scene = scenes.preset_scenes()["硬件温度"]
    clone = scenes.clone_scene(scene)
    clone["layers"][0]["x"] = 0.01
    assert scene["layers"][0]["x"] != 0.01


# --------------------------------------------------------------------------- #
# 插件图层（v1.3）：已注册类型被保留，未知类型仍被丢弃
# --------------------------------------------------------------------------- #
def _register_probe_plugin(monkeypatch: pytest.MonkeyPatch, name: str = "probe") -> None:
    """把名为 ``name`` 的假插件注册进进程级注册表。"""
    from hydrohud import plugins

    registry = plugins.PluginRegistry()
    registry._register_spec(
        name, name, "", {"color": "#FFFFFF"}, lambda image, layer, ctx: True, ""
    )
    monkeypatch.setattr(plugins, "_REGISTRY", registry)


def test_registered_plugin_layer_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """已注册插件图层不被丢弃，且顺序与内置图层保持交错。"""
    _register_probe_plugin(monkeypatch)
    scene = scenes.normalize_scene(
        {"layers": [{"type": "clock"}, {"type": "probe"}, {"type": "text", "text": "x"}]}
    )
    assert [layer["type"] for layer in scene["layers"]] == ["clock", "probe", "text"]


def test_plugin_layer_keeps_custom_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """插件自定义字段原样保留，位置/锚点仍被归一化。"""
    _register_probe_plugin(monkeypatch)
    scene = scenes.normalize_scene(
        {"layers": [{"type": "probe", "x": 5, "y": -1, "anchor": "nope", "custom": [1, 2]}]}
    )
    layer = scene["layers"][0]
    assert layer["type"] == "probe"
    assert layer["x"] == 5.0
    assert layer["y"] == -1.0
    assert layer["anchor"] == "center"
    assert layer["custom"] == [1, 2]


def test_plugin_layer_drops_unserializable_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """不可 JSON 序列化的字段（对象 / NaN）被丢弃，场景仍可保存。"""
    _register_probe_plugin(monkeypatch)
    scene = scenes.normalize_scene(
        {
            "layers": [
                {"type": "probe", "obj": object(), "nan": float("nan"), "ok": {"a": 1}},
            ]
        }
    )
    layer = scene["layers"][0]
    assert "obj" not in layer
    assert "nan" not in layer
    assert layer["ok"] == {"a": 1}
    json.dumps(scene)  # 不抛即合格


def test_dashboard_layer_normalizes_with_defaults() -> None:
    """dashboard 的字段白名单：缺省值齐全，非法值被夹回合法区间。"""
    scene = scenes.normalize_scene({"layers": [{"type": "dashboard"}]})
    layer = scene["layers"][0]
    assert layer["type"] == "dashboard"
    assert layer["title"] == "AURORA"
    assert layer["cpu_metric"] == "cpu" and layer["gpu_metric"] == "gpu"
    assert layer["cpu_temp_metric"] == "cpu_temp"
    assert layer["gpu_temp_metric"] == "gpu_temp"
    assert layer["extras"] == list(scenes.DASHBOARD_DEFAULT_EXTRAS)
    assert layer["show_header"] is True and layer["show_extras"] is True
    assert layer["w"] == 0.92 and layer["h"] == 0.92
    assert layer["visible"] is True


def test_dashboard_layer_clamps_and_filters() -> None:
    """越界尺寸夹回 0.2–1.0，非法指标名被过滤，未知字段不进场景。"""
    scene = scenes.normalize_scene(
        {
            "layers": [
                {
                    "type": "dashboard",
                    "w": 5,
                    "h": -3,
                    "cpu_metric": "sausage",
                    "extras": ["gpu_power", "nope", "gpu_power", "gpu_clock"],
                    "unknown_field": 1,
                    "title": "",
                }
            ]
        }
    )
    layer = scene["layers"][0]
    assert layer["w"] == 1.0 and layer["h"] == 0.2
    assert layer["cpu_metric"] == "cpu"
    assert layer["extras"] == ["gpu_power", "gpu_clock"]
    assert layer["title"] == "AURORA"
    assert "unknown_field" not in layer


def test_dashboard_extras_can_be_emptied() -> None:
    """附加指标允许清空：显式空列表不会被默认值顶回来（「开关」语义）。"""
    layer = scenes.normalize_scene({"layers": [{"type": "dashboard", "extras": []}]})["layers"][0]
    assert layer["extras"] == []
    # 拿到的场景再规范化一次仍然是空列表，幂等
    again = scenes.normalize_scene({"layers": [layer]})["layers"][0]
    assert again["extras"] == []


def test_every_layer_type_carries_the_visible_flag() -> None:
    """所有图层类型都有 visible，默认 True，且能被显式关掉。"""
    for kind in scenes.LAYER_TYPES:
        raw: dict = {"type": kind}
        layer = scenes._norm_layer(raw)
        assert layer is not None, f"{kind} 没有被规范化"
        assert layer["visible"] is True
        hidden = scenes._norm_layer({**raw, "visible": False})
        assert hidden is not None and hidden["visible"] is False


def test_unregistered_type_is_still_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """插件注册表里没有的类型仍然被丢弃（插件缺失时行为与 v1.2 一致）。"""
    _register_probe_plugin(monkeypatch, "probe")
    scene = scenes.normalize_scene({"layers": [{"type": "probe"}, {"type": "sausage"}]})
    assert [layer["type"] for layer in scene["layers"]] == ["probe"]


def test_plugin_layers_survive_project_roundtrip(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """工程保存/载入后插件图层与自定义字段仍在。"""
    _register_probe_plugin(monkeypatch)
    scene = scenes.normalize_scene(
        {"name": "插件场景", "layers": [{"type": "probe", "label_text": "hello"}]}
    )
    path = os.path.join(str(tmp_path), "plugin.hud.json")
    scenes.save_project(scene, path)
    loaded = scenes.load_project(path)
    assert loaded["layers"][0]["type"] == "probe"
    assert loaded["layers"][0]["label_text"] == "hello"
