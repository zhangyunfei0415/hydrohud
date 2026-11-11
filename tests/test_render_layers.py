"""v1.2 图表 / 仪表 / 模板文本图层的渲染测试。

全部纯软件：用假 hub 注入固定指标值，不读真实传感器、不打开设备、不启动 GUI。
运行：``C:\\Python314\\python.exe -m pytest tests/test_render_layers.py -q``。
"""

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

from __future__ import annotations

import os
import sys

import pytest
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hydrohud import render  # noqa: E402
from hydrohud.render import SCREEN_H, SCREEN_W, clear_cache, render_scene  # noqa: E402
from hydrohud.scenes import normalize_scene  # noqa: E402

BLACK = (0, 0, 0)


class FakeHub:
    """固定指标值的假 SensorHub：``values`` 可在测试中直接改。"""

    def __init__(self, values=None, history=None) -> None:
        self.values = dict(values or {})
        self.histories = dict(history or {})

    def sample(self, force: bool = False):
        return dict(self.values)

    def value(self, metric: str):
        return self.values.get(metric)

    def history(self, metric: str):
        return list(self.histories.get(metric, []))


@pytest.fixture()
def fake_hub(monkeypatch):
    """注入假 hub 并清空缓存，测试结束后恢复真实 hub 与缓存。"""
    hub = FakeHub(
        values={"cpu": 40.0, "gpu": 25.0, "ram": 75.0, "gpu_temp": 46.0, "uptime": 6.5},
        history={"cpu": [10.0, 20.0, 30.0, 40.0], "gpu": [5.0, 10.0, 15.0, 25.0]},
    )
    monkeypatch.setattr(render, "_sensor_hub", hub)
    clear_cache()
    yield hub
    clear_cache()


def _scene(layers: list[dict]) -> dict:
    return normalize_scene({"background": {"type": "solid", "color": "#000000"}, "layers": layers})


def _non_black_ratio(image: Image.Image) -> float:
    """非纯黑像素占比：用来判断某个图层是否真的画出来了。"""
    small = image.convert("RGB").resize((96, 96))
    getter = getattr(small, "get_flattened_data", None) or small.getdata
    pixels = list(getter())
    hits = sum(1 for pixel in pixels if pixel != BLACK)
    return hits / len(pixels)


# ------------------------------------------------------------------ chart
def test_chart_renders_visible_pixels(fake_hub) -> None:
    scene = _scene([{"type": "chart", "metric": "cpu", "x": 0.05, "y": 0.05, "w": 0.9, "h": 0.4}])
    image = render_scene(scene, 0.0)
    assert image.size == (SCREEN_W, SCREEN_H)
    assert image.mode == "RGB"
    assert _non_black_ratio(image) > 0.2


def test_chart_area_size_follows_w_and_h(fake_hub) -> None:
    small = render_scene(
        _scene([{"type": "chart", "metric": "cpu", "x": 0.05, "y": 0.05, "w": 0.3, "h": 0.3}]), 0.0
    )
    large = render_scene(
        _scene([{"type": "chart", "metric": "cpu", "x": 0.05, "y": 0.05, "w": 0.9, "h": 0.9}]), 0.0
    )
    assert _non_black_ratio(large) > _non_black_ratio(small)


def test_chart_label_toggle_changes_pixels(fake_hub) -> None:
    base = {"type": "chart", "metric": "gpu_temp", "x": 0.05, "y": 0.1, "w": 0.9, "h": 0.5}
    with_label = render_scene(_scene([dict(base, label=True)]), 0.0)
    without_label = render_scene(_scene([dict(base, label=False)]), 0.0)
    assert with_label.tobytes() != without_label.tobytes()


def test_chart_tracks_value_changes(fake_hub) -> None:
    scene = _scene([{"type": "chart", "metric": "cpu", "x": 0.05, "y": 0.05, "w": 0.9, "h": 0.6}])
    first = render_scene(scene, 0.0).tobytes()
    fake_hub.values["cpu"] = 95.0
    fake_hub.histories["cpu"] = [95.0, 95.0, 95.0, 95.0]
    clear_cache()
    second = render_scene(scene, 1.0).tobytes()
    assert first != second


def test_chart_without_sensors_degrades(monkeypatch) -> None:
    """sensors 模块缺失时 chart 仍能渲染（画 0 值线），不抛异常。"""
    monkeypatch.setattr(render, "_sensors", None)
    monkeypatch.setattr(render, "_sensor_hub", None)
    clear_cache()
    image = render_scene(
        _scene([{"type": "chart", "metric": "cpu", "x": 0.1, "y": 0.1, "w": 0.8, "h": 0.5}]), 0.0
    )
    assert image.size == (SCREEN_W, SCREEN_H)
    clear_cache()


# ------------------------------------------------------------------ gauge
def test_gauge_renders_visible_pixels(fake_hub) -> None:
    scene = _scene([{"type": "gauge", "metric": "gpu_temp", "x": 0.5, "y": 0.5, "diameter": 0.6}])
    image = render_scene(scene, 0.0)
    assert _non_black_ratio(image) > 0.08


def test_gauge_extremes_differ(fake_hub) -> None:
    scene = _scene([{"type": "gauge", "metric": "cpu", "x": 0.5, "y": 0.5, "diameter": 0.7}])
    fake_hub.values["cpu"] = 0.0
    clear_cache()
    empty = render_scene(scene, 0.0).tobytes()
    fake_hub.values["cpu"] = 100.0
    clear_cache()
    full = render_scene(scene, 0.0).tobytes()
    assert empty != full


def test_gauge_without_sensors_degrades(monkeypatch) -> None:
    monkeypatch.setattr(render, "_sensors", None)
    monkeypatch.setattr(render, "_sensor_hub", None)
    clear_cache()
    image = render_scene(
        _scene([{"type": "gauge", "metric": "gpu", "x": 0.5, "y": 0.5, "diameter": 0.5}]), 0.0
    )
    assert image.size == (SCREEN_W, SCREEN_H)
    clear_cache()


# ------------------------------------------------------------------ sysinfo / bars
def test_sysinfo_accepts_any_metric(fake_hub) -> None:
    scene = _scene(
        [{"type": "sysinfo", "fields": ["cpu", "gpu_temp", "ram"], "x": 0.5, "y": 0.5, "size": 20}]
    )
    image = render_scene(scene, 0.0)
    assert _non_black_ratio(image) > 0.01


def test_sysinfo_text_changes_with_values(fake_hub) -> None:
    scene = _scene([{"type": "sysinfo", "fields": ["cpu", "ram"], "x": 0.5, "y": 0.5, "size": 28}])
    first = render_scene(scene, 0.0).tobytes()
    fake_hub.values["cpu"] = 99.0
    clear_cache()
    second = render_scene(scene, 1.0).tobytes()
    assert first != second


def test_bars_use_metric_max_for_fill(fake_hub) -> None:
    """gpu_temp 的上限是 100，46 应当填到约 46%。"""
    scene = _scene(
        [
            {
                "type": "bars",
                "items": ["gpu_temp"],
                "x": 0.1,
                "y": 0.4,
                "w": 0.8,
                "h": 0.2,
                "bg": "#000000",
                "color": "#FFFFFF",
                "color2": "#FFFFFF",
                "label": False,
            }
        ]
    )
    image = render_scene(scene, 0.0)
    drawn = image.convert("RGB").getpixel((int(SCREEN_W * 0.2), int(SCREEN_H * 0.5)))
    empty = image.convert("RGB").getpixel((int(SCREEN_W * 0.9), int(SCREEN_H * 0.5)))
    assert drawn != BLACK
    assert empty == BLACK


# ------------------------------------------------------------------ 模板文本
def test_template_text_resolves_metrics(fake_hub) -> None:
    scene = _scene([{"type": "text", "text": "{cpu} {gpu_temp}", "x": 0.5, "y": 0.5, "size": 40}])
    assert render._text_needs_refresh(scene["layers"][0]) is True
    image = render_scene(scene, 0.0)
    assert _non_black_ratio(image) > 0.01


def test_template_text_placeholder_is_dynamic() -> None:
    plain = normalize_scene({"layers": [{"type": "text", "text": "hello"}]})["layers"][0]
    template = normalize_scene({"layers": [{"type": "text", "text": "{cpu}"}]})["layers"][0]
    assert render._text_needs_refresh(plain) is False
    assert render._text_needs_refresh(template) is True
    assert render._is_dynamic_layer(plain) is False
    assert render._is_dynamic_layer(template) is True


def test_template_time_and_date_work_without_sensors(monkeypatch) -> None:
    monkeypatch.setattr(render, "_sensors", None)
    monkeypatch.setattr(render, "_sensor_hub", None)
    clear_cache()
    ctx: dict = {"values": None, "metrics": None, "history": None}
    resolved = render._resolve_template("{date} {cpu}", ctx)
    assert "{cpu}" in resolved
    assert resolved != "{date} {cpu}"
    clear_cache()


def test_template_unknown_placeholder_is_kept(fake_hub) -> None:
    ctx: dict = {"values": None, "metrics": None, "history": None}
    assert render._resolve_template("{bogus}", ctx) == "{bogus}"


# ------------------------------------------------------------------ 性能
def test_chart_and_gauge_frame_budget(fake_hub) -> None:
    """一屏两个 chart + 一个 gauge 的单帧耗时仍然远低于 30 fps 预算（33 ms）。"""
    import time

    scene = _scene(
        [
            {"type": "chart", "metric": "cpu", "x": 0.05, "y": 0.05, "w": 0.9, "h": 0.25},
            {"type": "chart", "metric": "gpu", "x": 0.05, "y": 0.35, "w": 0.9, "h": 0.25},
            {"type": "gauge", "metric": "ram", "x": 0.5, "y": 0.78, "diameter": 0.3},
        ]
    )
    for _ in range(3):
        render_scene(scene, 0.0)
    started = time.perf_counter()
    count = 20
    for index in range(count):
        render_scene(scene, index * 0.1)
    average_ms = (time.perf_counter() - started) * 1000.0 / count
    assert average_ms < 33.0, f"chart+gauge 单帧 {average_ms:.1f} ms，超出 30 fps 预算"
