"""生成预览图与性能基准：把内置预设和图片/GIF/视频背景各渲染成 PNG。

用法（在 HydroHUD 目录下）::

    C:\\Python314\\python.exe tools\\render_previews.py

输出：
- ``preview/*.png``：每个预设一张，加上图片/GIF/视频背景演示图
- ``preview/contact-sheet.png``：所有预览的拼版，便于目视检查
- 控制台打印每个场景的平均帧耗时，用于核对 <15 ms 目标

需安装 Pillow；视频背景演示另需 ``pip install imageio imageio-ffmpeg``。
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

import copy
import math
import os
import sys
import time

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hydrohud.render import load_font, render_scene  # noqa: E402
from hydrohud.scenes import preset_scenes  # noqa: E402

ASSETS_DIR = os.path.join(ROOT, "tools", "_assets")
PREVIEW_DIR = os.path.join(ROOT, "preview")

SLUGS = {
    "极简时钟": "minimal-clock",
    "霓虹仪表盘": "neon-dash",
    "系统监控": "sysmon",
    "极客终端": "terminal",
    "日落暖光": "sunset",
    "冰晶蓝调": "ice-blue",
    "纯色标语": "solid-slogan",
    "图片背景": "image-bg",
    "硬件温度": "hw-temp",
    "性能曲线": "perf-chart",
    "极客面板": "geek-panel",
}


# ---------------------------------------------------------------- 素材生成
def _ensure_dirs() -> None:
    os.makedirs(ASSETS_DIR, exist_ok=True)
    os.makedirs(PREVIEW_DIR, exist_ok=True)


def _make_photo(path: str) -> None:
    """生成一张 900x600 的渐变风景图，作为图片背景演示素材。"""
    width, height = 900, 600
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / (height - 1)
        top = (36, 54, 102)
        bottom = (246, 148, 92)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color)
    draw.ellipse([640, 130, 820, 310], fill=(255, 226, 170))
    draw.ellipse([660, 150, 800, 290], fill=(255, 244, 214))
    for index, radius in enumerate((260, 200, 140)):
        shade = 30 + index * 18
        draw.polygon(
            [
                (index * 260 - 80, height),
                (index * 260 + 150, height - radius),
                (index * 260 + 420, height),
            ],
            fill=(shade, shade + 12, shade + 30),
        )
    image.save(path)


def _make_gif(path: str) -> None:
    """生成 12 帧的 GIF：一枚绕圈的光点，用于验证 GIF 背景动画。"""
    size = 300
    frames = []
    for index in range(12):
        image = Image.new("RGB", (size, size), (12, 18, 34))
        draw = ImageDraw.Draw(image)
        for y in range(size):
            ratio = y / (size - 1)
            color = (int(12 + 26 * ratio), int(18 + 34 * ratio), int(34 + 62 * ratio))
            draw.line([(0, y), (size, y)], fill=color)
        angle = 2.0 * math.pi * index / 12
        cx = 150 + int(100 * math.cos(angle))
        cy = 150 + int(100 * math.sin(angle))
        draw.ellipse([cx - 46, cy - 46, cx + 46, cy + 46], fill=(30, 90, 150))
        draw.ellipse([cx - 34, cy - 34, cx + 34, cy + 34], fill=(80, 220, 255))
        frames.append(image)
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=120,
        loop=0,
        disposal=2,
    )


def _make_video(path: str) -> bool:
    """生成 60 帧 mp4；缺少 imageio/numpy 时返回 False。"""
    try:
        import numpy
        import imageio
    except ImportError:
        return False
    width = height = 480
    writer = imageio.get_writer(path, fps=30, macro_block_size=1)
    try:
        for index in range(60):
            ratio = index / 59.0
            frame = numpy.zeros((height, width, 3), dtype=numpy.uint8)
            rows = numpy.linspace(0.0, 1.0, height, dtype=numpy.float32)[:, None]
            frame[:, :, 0] = (20 + 70 * rows + 60 * ratio).astype(numpy.uint8)
            frame[:, :, 1] = (26 + 90 * rows + 30 * ratio).astype(numpy.uint8)
            frame[:, :, 2] = (56 + 140 * rows - 40 * ratio).astype(numpy.uint8)
            cx = int(80 + 320 * ratio)
            cy = int(220 + 70 * math.sin(ratio * math.pi * 2))
            ys, xs = numpy.ogrid[:height, :width]
            distance = numpy.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
            glow = numpy.clip(1.0 - distance / 90.0, 0.0, 1.0) ** 1.5
            frame = numpy.clip(
                frame.astype(numpy.float32)
                + glow[:, :, None] * numpy.array([255, 236, 170], dtype=numpy.float32),
                0,
                255,
            ).astype(numpy.uint8)
            writer.append_data(frame)
    finally:
        writer.close()
    return True


# ---------------------------------------------------------------- 渲染与拼版
def _render_to(path: str, scene: dict, t: float) -> Image.Image:
    image = render_scene(scene, t)
    image.save(path)
    return image


def _benchmark(scene: dict, count: int = 60, warmup: int = 3) -> float:
    for index in range(warmup):
        render_scene(scene, index * 0.05)
    started = time.perf_counter()
    for index in range(count):
        render_scene(scene, index * 0.05)
    return (time.perf_counter() - started) * 1000.0 / count


def _contact_sheet(tiles: list[tuple[str, Image.Image]], path: str) -> None:
    columns = 4
    thumb = 320
    gap = 14
    caption = 34
    rows = math.ceil(len(tiles) / columns)
    width = columns * thumb + (columns + 1) * gap
    height = rows * (thumb + caption) + (rows + 1) * gap
    sheet = Image.new("RGB", (width, height), (18, 20, 26))
    draw = ImageDraw.Draw(sheet)
    font = load_font(20, bold=True)
    for index, (label, image) in enumerate(tiles):
        row, column = divmod(index, columns)
        x = gap + column * (thumb + gap)
        y = gap + row * (thumb + caption + gap)
        sheet.paste(image.resize((thumb, thumb), Image.LANCZOS), (x, y))
        draw.text((x + 2, y + thumb + 8), label, font=font, fill=(220, 228, 240))
    sheet.save(path)


def main() -> int:
    _ensure_dirs()
    photo = os.path.join(ASSETS_DIR, "demo_photo.png")
    gif = os.path.join(ASSETS_DIR, "demo_anim.gif")
    video = os.path.join(ASSETS_DIR, "demo_clip.mp4")
    if not os.path.isfile(photo):
        _make_photo(photo)
    if not os.path.isfile(gif):
        _make_gif(gif)
    has_video = os.path.isfile(video) or _make_video(video)

    # 让 CPU 采样器先跑一次，避免预览里的 CPU 数字全是 0
    render_scene(preset_scenes()["系统监控"], 0.0)
    time.sleep(0.4)

    presets = preset_scenes()
    tiles: list[tuple[str, Image.Image]] = []
    benchmarks: list[tuple[str, float]] = []

    for index, (name, scene) in enumerate(presets.items(), start=1):
        slug = SLUGS.get(name, f"preset-{index}")
        path = os.path.join(PREVIEW_DIR, f"{index:02d}-{slug}.png")
        image = _render_to(path, copy.deepcopy(scene), 1.2)
        tiles.append((f"{index:02d} {name}", image))
        benchmarks.append((name, _benchmark(copy.deepcopy(scene), count=40)))
        print(f"[preview] {path}")

    demos = [
        (
            "12-image-bg-demo",
            "图片背景",
            {
                "background": {"type": "image", "path": photo, "fit": "crop"},
                "tint": {"color": "#0A1428", "alpha": 0.28},
                "layers": [
                    {"type": "clock", "x": 0.5, "y": 0.42, "size": 96, "show_date": True},
                    {
                        "type": "text",
                        "text": "LG600 · 图片背景",
                        "x": 0.5,
                        "y": 0.72,
                        "size": 34,
                        "color": "#FFFFFF",
                        "anchor": "mm",
                    },
                ],
            },
        ),
        (
            "13-gif-bg-frame-a",
            "GIF 背景（帧 1）",
            {
                "background": {"type": "gif", "path": gif, "fit": "crop"},
                "layers": [{"type": "clock", "x": 0.5, "y": 0.5, "size": 88, "show_date": False}],
            },
        ),
        (
            "14-gif-bg-frame-b",
            "GIF 背景（帧 7）",
            {
                "background": {"type": "gif", "path": gif, "fit": "crop"},
                "layers": [{"type": "clock", "x": 0.5, "y": 0.5, "size": 88, "show_date": False}],
            },
        ),
    ]
    frame_b_t = 0.12 * 6 + 0.02
    for slug, label, scene in demos:
        time_value = frame_b_t if slug.endswith("frame-b") else 0.02
        path = os.path.join(PREVIEW_DIR, f"{slug}.png")
        image = _render_to(path, scene, time_value)
        tiles.append((label, image))
        print(f"[preview] {path}")

    if has_video:
        scene = {
            "background": {"type": "video", "path": video, "fit": "crop"},
            "layers": [
                {
                    "type": "text",
                    "text": "LG600 · 视频背景",
                    "x": 0.5,
                    "y": 0.82,
                    "size": 32,
                    "color": "#FFFFFF",
                    "anchor": "mm",
                },
                {
                    "type": "sysinfo",
                    "fields": ["cpu", "ram"],
                    "x": 0.5,
                    "y": 0.16,
                    "size": 28,
                    "align": "center",
                },
            ],
        }
        path = os.path.join(PREVIEW_DIR, "15-video-bg-demo.png")
        image = _render_to(path, scene, 1.1)
        tiles.append(("15 视频背景", image))
        benchmarks.append(("视频背景", _benchmark(scene, count=30)))
        print(f"[preview] {path}")
    else:
        print("[preview] 跳过视频背景：未安装 numpy/imageio")

    sheet_path = os.path.join(PREVIEW_DIR, "contact-sheet.png")
    _contact_sheet(tiles, sheet_path)
    print(f"[preview] {sheet_path}")

    static_scene = copy.deepcopy(presets["纯色标语"])
    dynamic_scene = copy.deepcopy(presets["霓虹仪表盘"])
    benchmarks.append(("静态（纯色标语）", _benchmark(static_scene, count=200)))
    benchmarks.append(("动态（霓虹仪表盘）", _benchmark(dynamic_scene, count=200)))

    print("\n[benchmark] 单帧平均耗时（480x480 RGB，含全流程）")
    for label, milliseconds in benchmarks:
        print(f"  {label:<16} {milliseconds:6.2f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
