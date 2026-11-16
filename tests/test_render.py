"""渲染引擎测试：预设、容错、项目文件、系统采样与性能。

运行：``C:\\Python314\\python.exe -m pytest tests -q``（在 HydroHUD 目录下）。
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
import time

import pytest
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hydrohud import render  # noqa: E402
from hydrohud.render import (  # noqa: E402
    SCREEN_H,
    SCREEN_W,
    clear_cache,
    load_font,
    render_scene,
)
from hydrohud.scenes import (  # noqa: E402
    load_project,
    normalize_scene,
    preset_scenes,
    save_project,
)
from hydrohud.sysmon import CpuSampler  # noqa: E402

PRESETS = preset_scenes()

TIMELINE = (0.0, 1.5, 3.0)


def _assert_screen(image: Image.Image) -> None:
    assert isinstance(image, Image.Image)
    assert image.size == (SCREEN_W, SCREEN_H)
    assert image.mode == "RGB"


def _distinct_colors(image: Image.Image) -> int:
    colors = image.resize((48, 48)).getcolors(maxcolors=48 * 48)
    return len(colors) if colors else 48 * 48


def _make_png(path, size=(200, 100), color=(220, 60, 40)) -> str:
    Image.new("RGB", size, color).save(path)
    return str(path)


# ------------------------------------------------------------------ 公开 API
def test_screen_constants() -> None:
    assert SCREEN_W == 480 and SCREEN_H == 480


def test_presets_at_least_six() -> None:
    assert len(PRESETS) >= 6
    for scene in PRESETS.values():
        assert normalize_scene(scene) == scene


@pytest.mark.parametrize("name", list(PRESETS))
def test_preset_renders_at_multiple_times(name: str) -> None:
    scene = PRESETS[name]
    for t in TIMELINE:
        _assert_screen(render_scene(scene, t))


@pytest.mark.parametrize("name", list(PRESETS))
def test_preset_is_not_blank(name: str) -> None:
    image = render_scene(PRESETS[name], 1.0)
    assert _distinct_colors(image) > 3, f"预设 {name} 渲染结果几乎是纯色"


def test_clear_cache_keeps_rendering() -> None:
    scene = PRESETS["霓虹仪表盘"]
    first = render_scene(scene, 0.0)
    clear_cache()
    second = render_scene(scene, 0.0)
    _assert_screen(second)
    assert first.size == second.size


def test_load_font_supports_chinese() -> None:
    font = load_font(32, bold=True)
    assert font is not None
    # 中文字体应当有非零尺寸（内置位图字体也会有，这里只验证不抛异常）
    assert font.getlength("中文 42% °")


def test_font_lookup_walks_into_vendor_subdirectories(tmp_path) -> None:
    """Linux 把字体放在 truetype/noto/ 这类子目录里，查找要能进一层。"""
    nested = tmp_path / "truetype" / "noto"
    nested.mkdir(parents=True)
    target = nested / "myfont.ttf"
    target.write_bytes(b"\x00")
    assert render._find_font_file(str(tmp_path), "myfont.ttf") == str(target)


def test_font_lookup_returns_none_for_missing_files_and_dirs(tmp_path) -> None:
    assert render._find_font_file(str(tmp_path), "nope.ttf") is None
    assert render._find_font_file(str(tmp_path / "missing"), "nope.ttf") is None


def test_font_directories_follow_the_platform(monkeypatch) -> None:
    monkeypatch.setattr(render.sys, "platform", "linux")
    linux_dirs = render._font_dirs()
    assert "/usr/share/fonts" in linux_dirs
    monkeypatch.setattr(render.sys, "platform", "darwin")
    assert "/System/Library/Fonts" in render._font_dirs()
    monkeypatch.setattr(render.sys, "platform", "win32")
    assert any("Fonts" in directory for directory in render._font_dirs())


# ------------------------------------------------------------------ 容错
def test_normalize_tolerates_missing_and_unknown_fields() -> None:
    scene = normalize_scene(
        {
            "unknown_top": 123,
            "background": {"type": "no-such-type", "color": "not-a-color"},
            "tint": {"alpha": 99, "color": "#abc"},
            "layers": [
                {"type": "text"},
                {"type": "clock", "format": 123, "size": "big"},
                {"type": "sysinfo", "fields": ["cpu", "gpu", "ram"]},
                {"type": "bars", "items": [], "w": 5, "h": -2},
                {"type": "image", "path": "", "scale": 0, "opacity": 9},
                {"type": "unknown-layer", "x": 1},
                "not-a-dict",
                None,
            ],
        }
    )
    assert scene["background"]["type"] == "solid"
    assert scene["tint"]["alpha"] == 1.0
    assert len(scene["layers"]) == 5
    assert [layer["type"] for layer in scene["layers"]] == [
        "text",
        "clock",
        "sysinfo",
        "bars",
        "image",
    ]
    assert scene["layers"][3]["w"] == 1.0 and scene["layers"][3]["h"] == 0.01

    _assert_screen(render_scene(scene, 0.0))
    _assert_screen(render_scene(scene, 2.0))


def test_normalize_is_idempotent_and_handles_non_dict() -> None:
    for raw in (None, {}, {"layers": "nope"}, {"name": ""}):
        normalized = normalize_scene(raw)
        assert normalize_scene(normalized) == normalized


def test_render_accepts_raw_partial_scene() -> None:
    _assert_screen(render_scene({"layers": [{"type": "text", "text": "你好"}]}, 0.0))
    _assert_screen(render_scene({}, 0.0))
    _assert_screen(render_scene(None, 0.0))  # type: ignore[arg-type]


def test_unknown_layer_types_are_dropped() -> None:
    scene = normalize_scene({"layers": [{"type": "sparkles"}, {"type": "text", "text": "保留"}]})
    assert len(scene["layers"]) == 1
    _assert_screen(render_scene(scene, 0.0))


def test_missing_background_image_shows_placeholder(tmp_path) -> None:
    scene = {
        "background": {"type": "image", "path": str(tmp_path / "nope.png"), "fit": "crop"},
        "layers": [{"type": "text", "text": "占位"}],
    }
    image = render_scene(scene, 0.0)
    _assert_screen(image)
    assert _distinct_colors(image) > 3


@pytest.mark.parametrize("bg_type", ["image", "gif", "video"])
def test_missing_background_media_does_not_raise(bg_type: str, tmp_path) -> None:
    scene = {"background": {"type": bg_type, "path": str(tmp_path / "missing.bin")}}
    _assert_screen(render_scene(scene, 0.5))


def test_missing_image_layer_shows_placeholder(tmp_path) -> None:
    scene = {
        "background": {"type": "solid", "color": "#000000"},
        "layers": [{"type": "image", "path": str(tmp_path / "nope.png"), "scale": 0.5}],
    }
    image = render_scene(scene, 0.0)
    _assert_screen(image)
    assert _distinct_colors(image) > 3


def test_image_background_fit_modes(tmp_path) -> None:
    path = _make_png(tmp_path / "wide.png")
    for fit in ("crop", "fit", "stretch"):
        scene = {"background": {"type": "image", "path": path, "fit": fit}}
        _assert_screen(render_scene(scene, 0.0))


def test_image_layer_renders_with_opacity(tmp_path) -> None:
    path = _make_png(tmp_path / "tile.png", size=(64, 64), color=(80, 200, 120))
    scene = {
        "background": {"type": "solid", "color": "#101010"},
        "layers": [
            {"type": "image", "path": path, "x": 0.5, "y": 0.5, "scale": 0.5, "opacity": 0.5}
        ],
    }
    opaque = {
        "background": {"type": "solid", "color": "#101010"},
        "layers": [
            {"type": "image", "path": path, "x": 0.5, "y": 0.5, "scale": 0.5, "opacity": 1.0}
        ],
    }
    clear_cache()
    half = render_scene(scene, 0.0)
    full = render_scene(opaque, 0.0)
    assert half.getpixel((SCREEN_W // 2, SCREEN_H // 2)) != full.getpixel(
        (SCREEN_W // 2, SCREEN_H // 2)
    )


def test_gif_background_animates(tmp_path) -> None:
    path = str(tmp_path / "anim.gif")
    frames = [
        Image.new("RGB", (120, 120), (240, 20, 20)),
        Image.new("RGB", (120, 120), (20, 40, 240)),
    ]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=300, loop=0)
    scene = {"background": {"type": "gif", "path": path, "fit": "stretch"}}
    clear_cache()
    first = render_scene(scene, 0.05).getpixel((SCREEN_W // 2, SCREEN_H // 2))
    second = render_scene(scene, 0.45).getpixel((SCREEN_W // 2, SCREEN_H // 2))
    assert first[0] > first[2]  # 第一帧偏红
    assert second[2] > second[0]  # 第二帧偏蓝
    assert render_scene(scene, 0.65).getpixel((SCREEN_W // 2, SCREEN_H // 2))[0] > 100


def test_video_background_decodes(tmp_path) -> None:
    numpy = pytest.importorskip("numpy")
    imageio = pytest.importorskip("imageio")
    pytest.importorskip("imageio_ffmpeg")
    path = str(tmp_path / "clip.mp4")
    writer = imageio.get_writer(path, fps=20, macro_block_size=1)
    try:
        for index in range(12):
            frame = numpy.zeros((64, 64, 3), dtype=numpy.uint8)
            frame[:, :] = ((15 * index) % 256, 40, 200 - 10 * index)
            writer.append_data(frame)
    finally:
        writer.close()
    scene = {"background": {"type": "video", "path": path, "fit": "stretch"}}
    clear_cache()
    _assert_screen(render_scene(scene, 0.1))
    _assert_screen(render_scene(scene, 0.35))


# ------------------------------------------------------------------ 项目文件
def test_project_roundtrip(tmp_path) -> None:
    path = str(tmp_path / "项目.hud.json")
    scene = PRESETS["极简时钟"]
    save_project(scene, path)
    loaded = load_project(path)
    assert loaded == normalize_scene(scene)
    assert loaded["name"] == scene["name"]
    clear_cache()
    _assert_screen(render_scene(loaded, 1.0))


def test_save_project_creates_parent_directory(tmp_path) -> None:
    path = str(tmp_path / "sub" / "dir" / "a.hud.json")
    save_project(PRESETS["纯色标语"], path)
    assert os.path.isfile(path)


def test_load_project_rejects_bad_json(tmp_path) -> None:
    path = tmp_path / "bad.hud.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_project(str(path))
    with pytest.raises(FileNotFoundError):
        load_project(str(tmp_path / "nope.hud.json"))


# ------------------------------------------------------------------ 系统采样
def test_sysmon_range() -> None:
    sampler = CpuSampler(interval=0.0)
    cpu, ram = sampler.sample()
    assert 0.0 <= cpu <= 1.0
    assert 0.0 <= ram <= 1.0
    time.sleep(0.3)
    cpu, ram = sampler.sample()
    assert 0.0 <= cpu <= 1.0
    assert 0.0 <= ram <= 1.0


def test_sysinfo_and_bars_update_without_error() -> None:
    scene = {
        "background": {"type": "solid", "color": "#0B1220"},
        "layers": [
            {"type": "sysinfo", "fields": ["cpu", "ram"], "x": 0.5, "y": 0.2, "size": 30},
            {
                "type": "bars",
                "items": ["cpu", "ram"],
                "x": 0.1,
                "y": 0.5,
                "w": 0.8,
                "h": 0.2,
                "label": True,
                "size": 22,
            },
            {"type": "clock", "x": 0.5, "y": 0.85, "size": 48, "show_date": True},
        ],
    }
    for t in TIMELINE:
        _assert_screen(render_scene(scene, t))


# ------------------------------------------------------------------ 仪表盘图层
def _dashboard_scene(**overrides) -> dict:
    layer = {"type": "dashboard", "x": 0.5, "y": 0.5, "w": 0.94, "h": 0.94}
    layer.update(overrides)
    return {
        "background": {"type": "solid", "color": "#050A14"},
        "layers": [layer],
    }


def test_dashboard_renders_hero_numbers_and_extras() -> None:
    """CPU / GPU 双英雄卡是常驻信息：换 t 也要每帧重绘（不能冻结）。"""
    scene = normalize_scene(_dashboard_scene())
    for t in TIMELINE:
        _assert_screen(render_scene(scene, t))
    assert _distinct_colors(render_scene(scene, 0.0)) > 8


def test_dashboard_extras_toggle_changes_the_frame() -> None:
    """附加指标勾上/取消要真的改变画面（这就是「功耗/频率可开关」）。"""
    clear_cache()
    with_extras = normalize_scene(
        _dashboard_scene(extras=["gpu_power", "gpu_clock", "ram", "gpu_mem"])
    )
    clear_cache()
    without_extras = normalize_scene(_dashboard_scene(extras=[]))
    first = render_scene(with_extras, 0.0).tobytes()
    second = render_scene(without_extras, 0.0).tobytes()
    assert first != second


def test_dashboard_title_and_header_are_optional() -> None:
    """关掉标题栏后画面顶部不该再有 AURORA 那块内容。"""
    clear_cache()
    with_header = normalize_scene(_dashboard_scene(show_header=True))
    clear_cache()
    without_header = normalize_scene(_dashboard_scene(show_header=False))
    assert render_scene(with_header, 0.0).tobytes() != render_scene(without_header, 0.0).tobytes()


def test_dashboard_tolerates_missing_hardware_metrics() -> None:
    """本机没有 CPU 温度时画「—」而不是抛异常（很多机器读不到 cpu_temp）。"""
    scene = normalize_scene(
        _dashboard_scene(
            cpu_metric="cpu_temp",
            gpu_metric="gpu_temp",
            cpu_temp_metric="cpu_power",
            gpu_temp_metric="fan",
            extras=["disk_read", "net_down"],
        )
    )
    for t in TIMELINE:
        _assert_screen(render_scene(scene, t))


def test_dashboard_renders_at_odd_geometry() -> None:
    """尺寸/位置越界或极小格子里也不抛异常。"""
    for overrides in (
        {"w": 0.2, "h": 0.2},
        {"w": 1.0, "h": 1.0},
        {"x": 0.0, "y": 0.0, "anchor": "top_left"},
        {"x": 1.0, "y": 1.0, "anchor": "bottom_right"},
    ):
        scene = normalize_scene(_dashboard_scene(**overrides))
        _assert_screen(render_scene(scene, 1.0))


def test_visible_false_layer_is_skipped() -> None:
    """visible=False 的图层完全不参与渲染：画面与「没有这个图层」一致。"""
    background = {"type": "solid", "color": "#123456"}
    hidden_scene = normalize_scene(
        {
            "background": background,
            "layers": [{"type": "text", "text": "不该出现", "visible": False, "size": 60}],
        }
    )
    empty_scene = normalize_scene({"background": background, "layers": []})
    clear_cache()
    assert render_scene(hidden_scene, 0.0).tobytes() == render_scene(empty_scene, 0.0).tobytes()


def test_visible_false_dashboard_leaves_the_background_alone() -> None:
    """隐藏仪表盘后只剩背景色，说明整层被跳过而不是画成半透明。"""
    scene = normalize_scene(_dashboard_scene(visible=False))
    image = render_scene(scene, 0.0)
    assert _distinct_colors(image) <= 3


# ------------------------------------------------------------------ 性能
def test_performance_static_scene() -> None:
    scene = PRESETS["纯色标语"]
    for _ in range(3):
        render_scene(scene, 0.0)
    started = time.perf_counter()
    count = 40
    for index in range(count):
        render_scene(scene, index * 0.1)
    average_ms = (time.perf_counter() - started) * 1000.0 / count
    assert average_ms < 15.0, f"静态场景单帧 {average_ms:.2f} ms，超过 15 ms 目标"


def test_performance_dynamic_scene() -> None:
    scene = PRESETS["霓虹仪表盘"]
    for _ in range(3):
        render_scene(scene, 0.0)
    started = time.perf_counter()
    count = 40
    for index in range(count):
        render_scene(scene, index * 0.1)
    average_ms = (time.perf_counter() - started) * 1000.0 / count
    assert average_ms < 15.0, f"动态场景单帧 {average_ms:.2f} ms，超过 15 ms 目标"


def test_static_scene_is_deterministic() -> None:
    scene = PRESETS["纯色标语"]
    first = render_scene(scene, 0.0).tobytes()
    second = render_scene(scene, 3.0).tobytes()
    assert first == second
