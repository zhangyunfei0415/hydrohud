"""渲染引擎：把场景（scene）渲染成 480×480 的 RGB PIL 图像。

公开 API::

    SCREEN_W = SCREEN_H = 480
    render_scene(scene: dict, t: float) -> PIL.Image.Image   # RGB 480×480
    clear_cache() -> None
    load_font(size: int, bold: bool = False) -> PIL.ImageFont

设计要点：

* ``t`` 是流媒体时间轴（秒）：GIF / 视频背景按 ``t`` 取帧并循环播放，
  时钟取当前本地时间，系统监控 / 折线图 / 仪表取 :mod:`hydrohud.sensors` 的
  :class:`~hydrohud.sensors.SensorHub`（模块缺失时整层降级为 "—" / 0）。
* 图层类型：``text`` / ``clock`` / ``sysinfo`` / ``bars`` / ``chart`` / ``gauge``
  / ``image``，以及 v1.3 的 ``thermometer`` / ``countdown`` / ``ticker`` /
  ``sequence`` / ``qrcode`` / ``weather`` / ``nowplaying``；含 ``{metric}`` 占位符的
  text 图层按动态图层处理，每帧替换占位符后仍按最终字符串走图层缓存，不会重复真重绘。
* v1.3 的数据源（二维码 / 天气 / 媒体 / 屏幕捕获 / 插件）全部懒导入：模块缺失时
  图层画占位内容、capture 背景退回纯色、未知图层静默跳过，绝不抛异常。
* 解码结果（图片、GIF 全部帧、视频帧、字体）都进 LRU 缓存，
  ``clear_cache()`` 可清空（字体缓存保留，释放后重新加载代价很小）。
* 静态内容会合并成一张底图缓存，帧间只重绘时钟/监控/进度条等动态图层，
  因此典型场景单帧渲染在毫秒级。
* 文件缺失时渲染中文占位提示而不是抛异常；视频解码后端缺失时抛
  ``RuntimeError``（中文信息，由 GUI 状态栏提示并引导安装）。
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

import atexit
import datetime as _dt
import json
import math
import os
import re
import sys
import threading
from collections import OrderedDict
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFont

from .scenes import normalize_scene
from .sysmon import CpuSampler

__all__ = ["SCREEN_W", "SCREEN_H", "render_scene", "clear_cache", "load_font"]

SCREEN_W = 480
SCREEN_H = 480

_DYNAMIC_TYPES = frozenset(
    (
        "clock",
        "sysinfo",
        "bars",
        "chart",
        "gauge",
        "thermometer",
        "countdown",
        "ticker",
        "sequence",
        "weather",
        "nowplaying",
    )
)
_VALID_FITS = frozenset(("crop", "fit", "stretch"))

_SHADOW_OFFSET = 3
_SHADOW_COLOR = (0, 0, 0)


# ------------------------------------------------------------------ 字体
def _font_dirs() -> list[str]:
    """按平台列出字体目录：Windows 系统字体、macOS 字体库、Linux 常见字体路径。"""
    if sys.platform == "win32":
        return [
            os.path.join(os.environ.get("WINDIR") or r"C:\Windows", "Fonts"),
            os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "Fonts"),
        ]
    if sys.platform == "darwin":
        return ["/System/Library/Fonts", "/Library/Fonts", os.path.expanduser("~/Library/Fonts")]
    home = os.path.expanduser("~")
    return [
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        os.path.join(home, ".local", "share", "fonts"),
        os.path.join(home, ".fonts"),
    ]


_FONT_DIRS = _font_dirs()
# 候选字体按「中文覆盖 → 拉丁」排序；每项可以是字体文件名，也可以是字体目录下的相对路径。
_BOLD_FONTS = (
    "msyhbd.ttc",  # 微软雅黑 Bold，中文 + °/% 覆盖最好
    "msyh.ttc",
    "seguisb.ttf",
    "segoeuib.ttf",
    "arialbd.ttf",
    "simhei.ttf",
    # Linux / macOS 常见中文字体
    "notosanscjk-bold.ttc",
    "NotoSansCJK-Bold.ttc",
    "NotoSansSC-Bold.otf",
    "SourceHanSansSC-Bold.otf",
    "wqy-microhei.ttc",
    "PingFang.ttc",
    "Hiragino Sans GB.ttc",
    "DejaVuSans-Bold.ttf",
)
_REGULAR_FONTS = (
    "msyh.ttc",  # 微软雅黑
    "msyhbd.ttc",
    "segoeui.ttf",
    "arial.ttf",
    "simhei.ttf",
    # Linux / macOS 常见中文字体
    "notosanscjk-regular.ttc",
    "NotoSansCJK-Regular.ttc",
    "NotoSansSC-Regular.otf",
    "SourceHanSansSC-Regular.otf",
    "wqy-microhei.ttc",
    "PingFang.ttc",
    "Hiragino Sans GB.ttc",
    "DejaVuSans.ttf",
)

_font_cache: dict[tuple[int, bool], ImageFont.ImageFont] = {}
_color_cache: dict[str, tuple[int, int, int]] = {}


def _find_font_file(directory: str, name: str) -> str | None:
    """在字体目录里找一个字体文件。

    Windows 与 macOS 的字体基本平铺在目录下，Linux 则按厂商/家族分子目录
    （``/usr/share/fonts/truetype/noto/``），所以先找直接路径，再递归找一层。
    """
    direct = os.path.join(directory, name)
    if os.path.isfile(direct):
        return direct
    if not os.path.isdir(directory):
        return None
    try:
        for root, _dirs, files in os.walk(directory):
            if name in files:
                return os.path.join(root, name)
    except OSError:  # 权限不足的字体目录直接跳过
        return None
    return None


def load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """按字号加载中文字体，优先微软雅黑。

    参数 ``size`` 为像素字号，``bold`` 为是否加粗。依次尝试
    ``msyhbd.ttc`` / ``msyh.ttc`` / ``seguisb.ttf`` / ``arialbd.ttf`` 等
    Windows 自带字体，全部缺失时退回 Pillow 内置字体（中文会显示为方框）。
    结果按 ``(字号, 粗细)`` 缓存。
    """
    try:
        size = max(1, int(round(float(size))))
    except (TypeError, ValueError):
        size = 20
    key = (size, bool(bold))
    font = _font_cache.get(key)
    if font is not None:
        return font
    candidates = _BOLD_FONTS if bold else _REGULAR_FONTS
    other = _REGULAR_FONTS if bold else _BOLD_FONTS
    for name in candidates + other:
        for directory in _FONT_DIRS:
            path = _find_font_file(directory, name)
            if path is None:
                continue
            try:
                font = ImageFont.truetype(path, size)
            except OSError:
                continue
            _font_cache[key] = font
            return font
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 没有 size 参数
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


# ------------------------------------------------------------------ 颜色与贴图
def _rgb(color: str) -> tuple[int, int, int]:
    """把 ``#RGB`` / ``#RRGGBB`` 转成 RGB 元组（非法值时返回黑色）。"""
    cached = _color_cache.get(color)
    if cached is not None:
        return cached
    text = color.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    try:
        value = (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except (ValueError, IndexError):
        value = (0, 0, 0)
    _color_cache[color] = value
    return value


def _solid_image(color: str) -> Image.Image:
    """纯色 480×480 底图（缓存，调用方负责 copy）。"""
    key = color
    image = _solid_cache.get(key)
    if image is None:
        image = Image.new("RGB", (SCREEN_W, SCREEN_H), _rgb(color))
        _lru_put(_solid_cache, key, image, 32)
    else:
        _solid_cache.move_to_end(key)
    return image


_tint_cache: dict[tuple[str, int], tuple[Image.Image, Image.Image]] = {}


def _tint_effect(color: str, alpha: float) -> tuple[Image.Image, Image.Image] | None:
    """生成整屏着色覆盖层 ``(rgb, mask)``。"""
    level = int(round(max(0.0, min(1.0, float(alpha))) * 255))
    if level <= 0:
        return None
    key = (color, level)
    effect = _tint_cache.get(key)
    if effect is None:
        rgb = Image.new("RGB", (SCREEN_W, SCREEN_H), _rgb(color))
        mask = Image.new("L", (SCREEN_W, SCREEN_H), level)
        effect = (rgb, mask)
        _tint_cache[key] = effect
    return effect


def _placeholder_image(title: str, detail: str = "") -> Image.Image:
    """渲染 480×480 的中文占位底图（文件缺失/解码失败/未选择时使用）。

    占位图刻意做得低调：深色渐变 + 下半屏的图片图标与提示文字，这样时钟等
    图层仍然可读，同时让用户看到"这里需要一张图片"。
    """
    key = (title, detail)
    cached = _placeholder_cache.get(key)
    if cached is not None:
        return cached
    image = Image.new("RGB", (SCREEN_W, SCREEN_H), (10, 14, 20))
    draw = ImageDraw.Draw(image)
    top, bottom = (13, 19, 30), (28, 38, 54)
    for y in range(SCREEN_H):
        ratio = y / (SCREEN_H - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
        draw.line([(0, y), (SCREEN_W, y)], fill=color)

    icon_left, icon_top = 204, 336
    draw.rounded_rectangle(
        (icon_left, icon_top, icon_left + 72, icon_top + 54),
        radius=10,
        outline=(100, 116, 139),
        width=3,
    )
    draw.ellipse(
        (icon_left + 14, icon_top + 12, icon_left + 30, icon_top + 28),
        outline=(148, 163, 184),
        width=3,
    )
    draw.polygon(
        [
            (icon_left + 8, icon_top + 44),
            (icon_left + 28, icon_top + 22),
            (icon_left + 42, icon_top + 38),
            (icon_left + 52, icon_top + 30),
            (icon_left + 64, icon_top + 44),
        ],
        fill=(100, 116, 139),
    )
    title_font = load_font(24, bold=True)
    detail_font = load_font(16)
    draw.text((SCREEN_W // 2, 414), title, font=title_font, fill=(203, 213, 225), anchor="mm")
    if detail:
        text = detail if len(detail) <= 36 else "…" + detail[-34:]
        draw.text((SCREEN_W // 2, 442), text, font=detail_font, fill=(120, 136, 158), anchor="mm")
    _lru_put(_placeholder_cache, key, image, 24)
    return image


# ------------------------------------------------------------------ LRU 工具
def _lru_put(cache: OrderedDict, key: Any, value: Any, limit: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def _lru_touch(cache: OrderedDict, key: Any) -> Any:
    try:
        cache.move_to_end(key)
    except KeyError:
        return None
    return cache[key]


# 各缓存容量（条目数，按需保守估计内存：480×480 RGB ≈ 0.7 MB）
_scene_cache: OrderedDict = OrderedDict()
_solid_cache: OrderedDict = OrderedDict()
_source_cache: OrderedDict = OrderedDict()
_bg_cache: OrderedDict = OrderedDict()
_gif_cache: OrderedDict = OrderedDict()
_video_cache: OrderedDict = OrderedDict()
_video_frames: OrderedDict = OrderedDict()
_layer_cache: OrderedDict = OrderedDict()
_layer_image_cache: OrderedDict = OrderedDict()
_placeholder_cache = OrderedDict()
#: v1.3 新图层的缓存：二维码矩阵 / 已放大位图 / 序列帧 / 封面 / 正在播放文本 / 捕获帧
_qr_matrix_cache: OrderedDict = OrderedDict()
_qr_image_cache: OrderedDict = OrderedDict()
_sequence_cache: OrderedDict = OrderedDict()
_cover_cache: OrderedDict = OrderedDict()
_nowplaying_text_cache: OrderedDict = OrderedDict()
_capture_cache: OrderedDict = OrderedDict()

_LAYER_CACHE_LIMIT = 24
_QR_CACHE_LIMIT = 8
_SEQUENCE_CACHE_LIMIT = 12
_COVER_CACHE_LIMIT = 8
_NOWPLAYING_TEXT_LIMIT = 16
_CAPTURE_CACHE_LIMIT = 4
# 解码缓存按"已缩放到 480×480 的图像"保存，单帧约 0.69 MB，容量按内存预算取值：
# 视频帧缓存 48 帧 ≈ 33 MB；GIF 最多 2 个、每个最多 120 帧 ≈ 165 MB 上限。
_VIDEO_FRAME_LIMIT = 48
_VIDEO_READER_LIMIT = 2
_GIF_LIMIT = 2
_MAX_GIF_FRAMES = 120

_render_lock = threading.RLock()
_video_lock = threading.RLock()
_sampler = CpuSampler()

# ------------------------------------------------------------------ 系统指标
try:  # 传感器模块是可选的：缺失时 sysinfo / bars / chart / gauge 全部降级
    from . import sensors as _sensors
except Exception:  # noqa: BLE001 - 任何导入失败都视为没有传感器模块
    _sensors = None

#: 进程级 :class:`~hydrohud.sensors.SensorHub` 缓存，首次真正渲染时才懒创建
#: （import 时绝不调用 ``get_hub()``，测试可把它替换成假 hub）。
_sensor_hub: Any = None
_hub_lock = threading.Lock()

# ------------------------------------------------------------------ v1.3 可选数据源
# 二维码 / 天气 / 媒体 / 屏幕捕获 / 插件模块都由其他模块维护，可能尚未安装或依赖
# 不可用；懒导入后一切失败都降级成占位内容，绝不让 import 期抛异常。
try:  # 纯 Python 二维码编码器
    from . import qrmini as _qrmini
except Exception:  # noqa: BLE001 - 任何导入失败都视为没有二维码模块
    _qrmini = None

try:  # 天气数据源（后台线程由 weather.get_hub() 自己管理）
    from . import weather as _weather
except Exception:  # noqa: BLE001
    _weather = None

try:  # 媒体数据源（Windows SMTC / 播放器状态）
    from . import media as _media
except Exception:  # noqa: BLE001
    _media = None

try:  # 屏幕捕获
    from . import capture as _capture
except Exception:  # noqa: BLE001
    _capture = None

try:  # 图层插件注册表
    from . import plugins as _plugins
except Exception:  # noqa: BLE001
    _plugins = None

#: 测试注入点：非 ``None`` 时优先于模块的 ``get_hub()``（与 ``_sensor_hub`` 同一约定）。
_weather_hub: Any = None
_media_hub: Any = None

#: ``(lat, lon) -> WeatherHub`` 的有界缓存，避免每帧重复调 ``weather.get_hub()``。
_weather_hubs: OrderedDict = OrderedDict()
_WEATHER_HUB_LIMIT = 4

#: ``media.get_hub()`` 是进程级单例，但要在锁内懒创建。
_media_lock = threading.Lock()

#: sensors 缺失时的指标元数据兜底，字段与 ``sensors.METRIC_SPECS`` 保持一致。
_FALLBACK_METRIC_SPECS: dict[str, dict] = {
    "cpu": {"label": "CPU 使用率", "unit": "%", "max": 100.0, "digits": 0, "suffix": "%"},
    "cpu_freq": {"label": "CPU 频率", "unit": "MHz", "max": 6000.0, "digits": 0, "suffix": " MHz"},
    "cpu_temp": {"label": "CPU 温度", "unit": "°C", "max": 100.0, "digits": 0, "suffix": "°C"},
    "cpu_power": {"label": "CPU 功耗", "unit": "W", "max": 300.0, "digits": 1, "suffix": " W"},
    "ram": {"label": "内存使用率", "unit": "%", "max": 100.0, "digits": 0, "suffix": "%"},
    "ram_used": {"label": "已用内存", "unit": "GB", "max": 128.0, "digits": 1, "suffix": " GB"},
    "ram_total": {"label": "内存总量", "unit": "GB", "max": 128.0, "digits": 1, "suffix": " GB"},
    "gpu": {"label": "GPU 使用率", "unit": "%", "max": 100.0, "digits": 0, "suffix": "%"},
    "gpu_temp": {"label": "GPU 温度", "unit": "°C", "max": 100.0, "digits": 0, "suffix": "°C"},
    "gpu_mem": {"label": "显存使用率", "unit": "%", "max": 100.0, "digits": 0, "suffix": "%"},
    "gpu_mem_used": {"label": "已用显存", "unit": "GB", "max": 32.0, "digits": 1, "suffix": " GB"},
    "gpu_mem_total": {"label": "显存总量", "unit": "GB", "max": 32.0, "digits": 1, "suffix": " GB"},
    "gpu_power": {"label": "GPU 功耗", "unit": "W", "max": 600.0, "digits": 1, "suffix": " W"},
    "gpu_fan": {"label": "GPU 风扇", "unit": "%", "max": 100.0, "digits": 0, "suffix": "%"},
    "gpu_clock": {"label": "GPU 频率", "unit": "MHz", "max": 4000.0, "digits": 0, "suffix": " MHz"},
    "disk": {"label": "磁盘使用率", "unit": "%", "max": 100.0, "digits": 0, "suffix": "%"},
    "disk_read": {
        "label": "磁盘读取",
        "unit": "MB/s",
        "max": 2000.0,
        "digits": 1,
        "suffix": " MB/s",
    },
    "disk_write": {
        "label": "磁盘写入",
        "unit": "MB/s",
        "max": 2000.0,
        "digits": 1,
        "suffix": " MB/s",
    },
    "net_down": {
        "label": "网络下载",
        "unit": "Mbps",
        "max": 1000.0,
        "digits": 1,
        "suffix": " Mbps",
    },
    "net_up": {"label": "网络上传", "unit": "Mbps", "max": 1000.0, "digits": 1, "suffix": " Mbps"},
    "uptime": {"label": "运行时间", "unit": "小时", "max": 720.0, "digits": 1, "suffix": " 小时"},
    "fan": {"label": "风扇转速", "unit": "RPM", "max": 3000.0, "digits": 0, "suffix": " RPM"},
    "cpu_cores": {"label": "CPU 核心数", "unit": "核", "max": 64.0, "digits": 0, "suffix": " 核"},
}

#: 未知 metric 的回退元数据，保证格式化永不抛 ``KeyError``。
_FALLBACK_DEFAULT_SPEC: dict = {"label": "", "unit": "", "max": 100.0, "digits": 1, "suffix": ""}

#: 合法 metric 名：优先 sensors.METRIC_IDS，缺失时用兜底表。
_METRIC_IDS: tuple[str, ...] = tuple(getattr(_sensors, "METRIC_IDS", ()) or ()) or tuple(
    _FALLBACK_METRIC_SPECS
)
_METRIC_ID_SET = frozenset(_METRIC_IDS)

#: text 图层模板占位符：``{cpu}`` / ``{time}`` / ``{date}`` …
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class VideoBackendError(RuntimeError):
    """缺少可用的视频解码后端（imageio / imageio-ffmpeg / opencv-python）。

    与单个视频文件解码失败区分开：缺后端是环境问题，需要把安装提示原样抛给用户。
    """


# ------------------------------------------------------------------ 文件与解码
def _file_sig(path: str) -> tuple[int, int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _source_image(path: str) -> Image.Image:
    """解码图片文件（RGB），按路径 + mtime + 大小缓存。"""
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(path)
    signature = _file_sig(path)
    key = (os.path.abspath(path), signature)
    cached = _lru_touch(_source_cache, key)
    if cached is not None:
        return cached
    with Image.open(path) as handle:
        # draft 让 JPEG 解码直接按小尺寸出图，大图快很多。
        try:
            handle.draft("RGB", (SCREEN_W * 2, SCREEN_W * 2))
        except Exception:  # 某些格式不支持 draft，忽略即可
            pass
        image = handle.convert("RGB")
    _lru_put(_source_cache, key, image, 16)
    return image


def _fit_to_screen(image: Image.Image, fit: str) -> Image.Image:
    """把图片按 ``crop`` / ``fit`` / ``stretch`` 适配成 480×480 RGB。"""
    if fit not in _VALID_FITS:
        fit = "crop"
    width, height = image.size
    if fit == "stretch":
        return image.resize((SCREEN_W, SCREEN_H), Image.Resampling.LANCZOS)
    if width <= 0 or height <= 0:
        return Image.new("RGB", (SCREEN_W, SCREEN_H), (0, 0, 0))
    scale = (
        SCREEN_W / min(width, height) if fit == "crop" else min(SCREEN_W / width, SCREEN_H / height)
    )
    target_w = max(1, int(round(width * scale)))
    target_h = max(1, int(round(height * scale)))
    resized = image.resize((target_w, target_h), Image.Resampling.LANCZOS)
    if fit == "crop":
        left = (target_w - SCREEN_W) // 2
        top = (target_h - SCREEN_H) // 2
        return resized.crop((left, top, left + SCREEN_W, top + SCREEN_H))
    canvas = Image.new("RGB", (SCREEN_W, SCREEN_H), (0, 0, 0))
    canvas.paste(resized, ((SCREEN_W - target_w) // 2, (SCREEN_H - target_h) // 2))
    return canvas


def _background_image(background: dict, t: float) -> Image.Image:
    """按场景背景生成一帧底图（返回新的 RGB 图像，调用方可直接绘制）。"""
    kind = background.get("type", "solid")
    if kind == "solid":
        return _solid_image(background.get("color", "#0B0F14")).copy()
    path = background.get("path") or ""
    fit = background.get("fit", "crop")
    if kind == "image":
        if not path:
            return _placeholder_image("尚未选择背景图片", "在背景设置里选择一张图片").copy()
        key = (os.path.abspath(path), _file_sig(path), fit)
        cached = _lru_touch(_bg_cache, key)
        if cached is None:
            try:
                cached = _fit_to_screen(_source_image(path), fit)
            except (FileNotFoundError, OSError, ValueError):
                cached = _placeholder_image("找不到背景图片", path)
            _lru_put(_bg_cache, key, cached, 8)
        return cached.copy()
    if kind == "gif":
        if not path:
            return _placeholder_image("尚未选择 GIF 背景", "在背景设置里选择一个 GIF 文件").copy()
        try:
            frames, durations = _gif_frames(path, fit)
        except (FileNotFoundError, OSError, ValueError):
            return _placeholder_image("无法读取 GIF 背景", path).copy()
        return frames[_gif_frame_index(durations, t)].copy()
    if kind == "video":
        if not path:
            return _placeholder_image("尚未选择视频背景", "在背景设置里选择一个视频文件").copy()
        try:
            frame = _video_frame(path, fit, t)
        except FileNotFoundError:
            return _placeholder_image("找不到视频文件", path).copy()
        except VideoBackendError:
            raise
        except Exception as exc:  # 解码失败 → 占位提示，避免整个界面崩
            return _placeholder_image("视频解码失败", str(exc)).copy()
        return frame.copy()
    if kind == "capture":
        return _capture_frame(background, t)
    return _solid_image("#0B0F14").copy()


def _capture_frame(background: dict, t: float) -> Image.Image:
    """屏幕捕获背景：按 ``fps`` 节流截图；capture 模块缺失时退回纯色底。

    节流键是 ``int(t * fps)``，所以同一时间槽内连续渲染复用同一张图，
    不会因为预览以 60 fps 刷新就把截屏频率也拉到 60。
    """
    if _capture is None:
        return _solid_image("#0B0F14").copy()
    region = background.get("region")
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        region = (0.0, 0.0, 1.0, 1.0)
    else:
        region = tuple(_clamped(value, 0.0, 1.0, 0.0) for value in region)
    monitor = int(round(_clamped(background.get("monitor", 0), 0, 8, 0)))
    fps = _clamped(background.get("fps", 5.0), 0.5, 30.0, 5.0)
    key = (
        int(t * fps),
        tuple(round(float(value), 6) for value in region),
        monitor,
        SCREEN_W,
    )
    cached = _lru_touch(_capture_cache, key)
    if cached is not None:
        return cached.copy()
    try:
        grabber = getattr(_capture, "grab", None) or getattr(_capture, "grab_cached", None)
        image = grabber(region, monitor, SCREEN_W)
    except Exception:  # noqa: BLE001 - 截图失败绝不能让整帧渲染失败
        return _solid_image("#0B0F14").copy()
    if not isinstance(image, Image.Image):
        return _solid_image("#0B0F14").copy()
    if image.size != (SCREEN_W, SCREEN_H):
        image = _fit_to_screen(image.convert("RGB"), "crop")
    result = image.convert("RGB")
    _lru_put(_capture_cache, key, result, _CAPTURE_CACHE_LIMIT)
    return result.copy()


def _gif_frames(path: str, fit: str) -> tuple[list[Image.Image], list[int]]:
    """解码 GIF 全部帧（预缩放到 480×480）并缓存。

    超过 ``_MAX_GIF_FRAMES`` 帧时按固定步长抽帧（保留总循环时长），
    避免超大 GIF 占用过多内存。
    """
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(path)
    signature = _file_sig(path)
    key = (os.path.abspath(path), signature, fit)
    cached = _lru_touch(_gif_cache, key)
    if cached is not None:
        return cached
    frames: list[Image.Image] = []
    durations: list[int] = []
    with Image.open(path) as handle:
        total = getattr(handle, "n_frames", 1) or 1
        all_durations: list[int] = []
        for index in range(total):
            handle.seek(index)
            value = handle.info.get("duration", 100) or 100
            all_durations.append(max(20, int(value)))
        step = max(1, math.ceil(total / _MAX_GIF_FRAMES))
        for index in range(0, total, step):
            handle.seek(index)
            frame = handle.convert("RGB")
            frames.append(_fit_to_screen(frame, fit))
            span = all_durations[index : index + step]
            durations.append(max(20, sum(span)))
    _lru_put(_gif_cache, key, (frames, durations), _GIF_LIMIT)
    return frames, durations


def _gif_frame_index(durations: list[int], t: float) -> int:
    """按时间轴选 GIF 帧（循环播放）。"""
    total = sum(durations) / 1000.0
    if total <= 0 or not durations:
        return 0
    position = (float(t) % total) * 1000.0
    accumulated = 0.0
    for index, duration in enumerate(durations):
        accumulated += duration
        if position < accumulated:
            return index
    return len(durations) - 1


def _video_backend() -> str:
    """探测可用的视频解码后端，返回 ``"imageio"`` 或 ``"cv2"``。"""
    try:
        import imageio  # noqa: F401
    except ImportError:
        pass
    else:
        return "imageio"
    try:
        import cv2  # noqa: F401
    except ImportError:
        pass
    else:
        return "cv2"
    raise VideoBackendError(
        "无法解码视频：未安装 imageio / imageio-ffmpeg 或 opencv-python。"
        "请运行：pip install imageio imageio-ffmpeg"
    )


def _video_reader(path: str) -> dict:
    """打开并缓存视频读取器（imageio 或 cv2 实现）。"""
    signature = _file_sig(path)
    if signature is None:
        raise FileNotFoundError(path)
    key = (os.path.abspath(path), signature)
    with _video_lock:
        cached = _lru_touch(_video_cache, key)
        if cached is not None:
            return cached
        backend = _video_backend()
        if backend == "imageio":
            import imageio

            reader = imageio.get_reader(path, "ffmpeg")
            meta = reader.get_meta_data() or {}
            fps = float(meta.get("fps") or 30.0)
            fps = fps if math.isfinite(fps) and fps > 0 else 30.0
            duration = float(meta.get("duration") or 0.0)
            duration = duration if math.isfinite(duration) and duration > 0 else 0.0
            nframes = meta.get("nframes")
            if (
                not isinstance(nframes, (int, float))
                or not math.isfinite(float(nframes))
                or nframes <= 0
            ):
                nframes = int(round(duration * fps)) if duration > 0 else 0
            entry = {
                "backend": "imageio",
                "reader": reader,
                "fps": fps,
                "nframes": int(nframes or 0),
            }
        else:
            import cv2

            capture = cv2.VideoCapture(path)
            if not capture.isOpened():
                raise RuntimeError(f"视频无法打开：{path}")
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 30.0) or 30.0
            nframes = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            entry = {
                "backend": "cv2",
                "reader": capture,
                "fps": fps,
                "nframes": max(0, nframes),
            }
        _lru_put(_video_cache, key, entry, _VIDEO_READER_LIMIT)
        while len(_video_cache) > _VIDEO_READER_LIMIT:
            _, evicted = _video_cache.popitem(last=False)
            _close_video(evicted)
        return entry


def _close_video(entry: dict) -> None:
    try:
        if entry.get("backend") == "imageio":
            entry["reader"].close()
        else:
            entry["reader"].release()
    except Exception:
        pass


def _video_frame(path: str, fit: str, t: float) -> Image.Image:
    """按时间轴取视频帧（预缩放并缓存最近若干帧）。"""
    entry = _video_reader(path)
    fps = entry["fps"] or 30.0
    nframes = entry["nframes"]
    index = int(float(t) * fps)
    if nframes > 0:
        index %= nframes
    else:
        index = 0
    frame_key = (os.path.abspath(path), _file_sig(path), fit, index)
    cached = _lru_touch(_video_frames, frame_key)
    if cached is not None:
        return cached
    with _video_lock:
        try:
            if entry["backend"] == "imageio":
                pixels = entry["reader"].get_data(index)
                frame = Image.fromarray(pixels).convert("RGB")
            else:
                import cv2

                capture = entry["reader"]
                capture.set(cv2.CAP_PROP_POS_FRAMES, index)
                ok, pixels = capture.read()
                if not ok or pixels is None:
                    raise RuntimeError("读取视频帧失败")
                frame = Image.fromarray(cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB))
        except FileNotFoundError:
            raise
        except Exception as exc:
            raise RuntimeError(f"视频解码失败：{exc}") from exc
    fitted = _fit_to_screen(frame, fit)
    _lru_put(_video_frames, frame_key, fitted, _VIDEO_FRAME_LIMIT)
    return fitted


# ------------------------------------------------------------------ 图层绘制
_ANCHOR_PARTS = {
    "center": ("center", "center"),
    "top-left": ("top", "left"),
    "top-center": ("top", "center"),
    "top-right": ("top", "right"),
    "bottom-left": ("bottom", "left"),
    "bottom-center": ("bottom", "center"),
    "bottom-right": ("bottom", "right"),
    "center-left": ("center", "left"),
    "center-right": ("center", "right"),
}


def _anchor_parts(anchor: str) -> tuple[str, str]:
    return _ANCHOR_PARTS.get(anchor, ("center", "center"))


def _paint_text_block(
    draw: ImageDraw.ImageDraw,
    lines: list[tuple[str, ImageFont.ImageFont, tuple[int, int, int]]],
    x: float,
    y: float,
    anchor: str,
    align: str = "center",
    shadow: bool = True,
) -> None:
    """在 ``(x, y)`` 按锚点绘制多行文字块，可选阴影。"""
    if not lines:
        return
    vertical, horizontal = _anchor_parts(anchor)
    metrics: list[tuple[int, int]] = []
    for text, font, _ in lines:
        ascent, descent = font.getmetrics()
        height = ascent + descent
        bbox = draw.textbbox((0, 0), text or " ", font=font, anchor="lt")
        metrics.append((height, max(1, bbox[2] - bbox[0])))
    spacing = max(2, int(metrics[0][0] * 0.12))
    total_height = sum(item[0] for item in metrics) + spacing * (len(lines) - 1)
    if vertical == "top":
        start_y = y
    elif vertical == "bottom":
        start_y = y - total_height
    else:
        start_y = y - total_height / 2.0
    widest = max(item[1] for item in metrics)
    if horizontal == "left":
        block_left = x
    elif horizontal == "right":
        block_left = x - widest
    else:
        block_left = x - widest / 2.0
    cursor = start_y
    for (text, font, fill), (height, _width) in zip(lines, metrics, strict=True):
        if not text:
            cursor += height + spacing
            continue
        if align == "left":
            line_x, line_anchor = block_left, "lt"
        elif align == "right":
            line_x, line_anchor = block_left + widest, "rt"
        else:
            line_x, line_anchor = block_left + widest / 2.0, "mt"
        if shadow:
            draw.text(
                (line_x + _SHADOW_OFFSET, cursor + _SHADOW_OFFSET),
                text,
                font=font,
                fill=_SHADOW_COLOR,
                anchor=line_anchor,
            )
        draw.text((line_x, cursor), text, font=font, fill=fill, anchor=line_anchor)
        cursor += height + spacing


def _safe_strftime(now: _dt.datetime, fmt: str) -> str:
    try:
        return now.strftime(fmt)
    except (ValueError, TypeError):
        return str(fmt)


def _sample_values(ctx: dict) -> tuple[float, float]:
    """每帧最多采样一次 CPU / 内存（旧契约，保留给外部调用）。

    新的图层绘制走 :func:`_metric_value`；这个函数仍然返回 ``(cpu, ram)`` 两个
    0~1 比例，便于旧代码复用。
    """
    values = ctx.get("values")
    if values is None:
        values = _sampler.sample()
        ctx["values"] = values
    return values


# ------------------------------------------------------------------ 指标辅助
def _finite_or_none(value: Any) -> float | None:
    """把任意数值规范化成有限 float；``None`` / 非数值 / NaN / Inf 返回 ``None``。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _hub_or_none() -> Any:
    """返回进程级 ``SensorHub``；传感器模块缺失或初始化失败时返回 ``None``。

    模块级 ``_sensor_hub`` 可以直接被测试替换成假 hub（先于 sensors 模块检查），
    import 时绝不调用 ``get_hub()``。
    """
    global _sensor_hub
    hub = _sensor_hub
    if hub is not None:
        return hub
    if _sensors is None:
        return None
    getter = getattr(_sensors, "get_hub", None)
    if not callable(getter):
        return None
    with _hub_lock:
        if _sensor_hub is None:
            try:
                _sensor_hub = getter()
            except Exception:  # noqa: BLE001 - 传感器初始化失败时整层降级
                return None
        return _sensor_hub


def _metric_spec(metric: str) -> dict:
    """返回 metric 的展示元数据；优先 ``sensors.METRIC_SPECS``，否则用内置兜底。"""
    specs = getattr(_sensors, "METRIC_SPECS", None) if _sensors is not None else None
    spec = specs.get(metric) if isinstance(specs, dict) else None
    if not isinstance(spec, dict):
        spec = _FALLBACK_METRIC_SPECS.get(metric)
    return spec if isinstance(spec, dict) else _FALLBACK_DEFAULT_SPEC


def _metric_label(metric: str) -> str:
    """返回 metric 的中文标签（未知 metric 退回 metric 本身）。"""
    getter = getattr(_sensors, "metric_label", None) if _sensors is not None else None
    if callable(getter):
        try:
            return str(getter(metric))
        except Exception:  # noqa: BLE001 - 第三方实现异常时退回内置规格
            pass
    return str(_metric_spec(metric).get("label") or metric)


def _metric_max(metric: str) -> float:
    """返回 metric 的归一化上限（始终为正的有限值）。"""
    getter = getattr(_sensors, "metric_max", None) if _sensors is not None else None
    if callable(getter):
        try:
            number = _finite_or_none(getter(metric))
            if number is not None and number > 0:
                return number
        except Exception:  # noqa: BLE001 - 第三方实现异常时退回内置规格
            pass
    number = _finite_or_none(_metric_spec(metric).get("max"))
    return number if number is not None and number > 0 else 100.0


def _format_value(metric: str, value: Any) -> str:
    """把 metric 数值格式化成 ``"32%"`` / ``"46°C"``；``None`` 显示 ``"—"``。"""
    normalized = _finite_or_none(value)
    getter = getattr(_sensors, "format_value", None) if _sensors is not None else None
    if callable(getter):
        try:
            return str(getter(metric, normalized))
        except Exception:  # noqa: BLE001 - 第三方实现异常时退回内置格式
            pass
    if normalized is None:
        return "—"
    spec = _metric_spec(metric)
    try:
        digits = max(0, min(6, int(spec.get("digits", 1))))
    except (TypeError, ValueError):
        digits = 1
    return f"{normalized:.{digits}f}{spec.get('suffix') or ''}"


def _format_metric(metric: str, value: Any) -> str:
    """返回 ``"GPU 温度 46°C"`` 形式的完整文本。"""
    normalized = _finite_or_none(value)
    getter = getattr(_sensors, "format_metric", None) if _sensors is not None else None
    if callable(getter):
        try:
            return str(getter(metric, normalized))
        except Exception:  # noqa: BLE001 - 第三方实现异常时退回内置格式
            pass
    return f"{_metric_label(metric)} {_format_value(metric, normalized)}"


def _metric_value(metric: str, ctx: dict) -> float | None:
    """取单个 metric 的当前值；同一次渲染内只采样一次（缓存在 ``ctx["metrics"]``）。"""
    key = str(metric or "").strip().lower()
    metrics = ctx.get("metrics")
    if metrics is not None:
        return metrics.get(key)
    hub = _hub_or_none()
    metrics = {}
    if hub is not None:
        sampler = getattr(hub, "sample", None)
        sampled = None
        if callable(sampler):
            try:
                sampled = sampler()
            except Exception:  # noqa: BLE001 - 采样失败按"无数据"处理
                sampled = None
        if isinstance(sampled, dict):
            metrics = {
                str(name).strip().lower(): _finite_or_none(value) for name, value in sampled.items()
            }
        getter = getattr(hub, "value", None)
        if key not in metrics and callable(getter):
            try:
                metrics[key] = _finite_or_none(getter(key))
            except Exception:  # noqa: BLE001 - 采样失败按"无数据"处理
                metrics[key] = None
    ctx["metrics"] = metrics
    return metrics.get(key)


def _metric_history(metric: str, ctx: dict) -> list[float]:
    """取 metric 的历史值（旧的在前，跳过 None）；同一次渲染内只查一次。"""
    key = str(metric or "").strip().lower()
    cache = ctx.get("history")
    if cache is None:
        cache = {}
        ctx["history"] = cache
    if key in cache:
        return cache[key]
    result: list[float] = []
    hub = _hub_or_none()
    getter = getattr(hub, "history", None) if hub is not None else None
    if callable(getter):
        try:
            raw = getter(key)
        except Exception:  # noqa: BLE001 - 历史读取失败按"无历史"处理
            raw = None
        for item in raw or []:
            number = _finite_or_none(item)
            if number is not None:
                result.append(number)
    cache[key] = result
    return result


def _is_known_metric(name: str) -> bool:
    """metric 名是否合法（含 sensors 缺失时的内置兜底表）。"""
    return name in _METRIC_ID_SET or name in _FALLBACK_METRIC_SPECS


def _text_needs_refresh(layer: dict) -> bool:
    """text 图层是否含 ``{占位符}``（是则必须按动态图层逐帧重算）。"""
    if str(layer.get("type") or "").strip().lower() != "text":
        return False
    text = layer.get("text")
    return isinstance(text, str) and _PLACEHOLDER_RE.search(text) is not None


def _qr_needs_refresh(layer: dict) -> bool:
    """qrcode 图层是否含 ``{占位符}``（是则逐帧重算，否则可当静态图层缓存）。"""
    if str(layer.get("type") or "").strip().lower() != "qrcode":
        return False
    data = layer.get("data")
    return isinstance(data, str) and _PLACEHOLDER_RE.search(data) is not None


def _is_dynamic_layer(layer: dict) -> bool:
    """图层是否需要逐帧重绘（类型动态、text 带占位符、qrcode 带占位符或插件图层）。"""
    kind = str(layer.get("type") or "").strip().lower()
    if kind in _DYNAMIC_TYPES:
        return True
    if kind == "text" and _text_needs_refresh(layer):
        return True
    if _qr_needs_refresh(layer):
        return True
    return _is_plugin_layer(kind)


def _is_plugin_layer(kind: str) -> bool:
    """``kind`` 是否是已注册的插件图层类型（``plugins`` 缺失时为 ``False``）。"""
    module = _plugins
    if module is None or not kind:
        return False
    checker = getattr(module, "is_registered", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(kind))
    except Exception:  # noqa: BLE001 - 注册表异常按非插件处理
        return False


def _resolve_template(text: str, ctx: dict) -> str:
    """替换 text 图层里的模板占位符，未知占位符原样保留。

    支持 ``{metric_id}``（如 ``{cpu}`` / ``{gpu_temp}``）、``{time}``（``%H:%M``）、
    ``{date}``（``%Y-%m-%d``）与 ``{uptime}``（小时数）。没有任何传感器来源时
    指标类占位符原样保留，与旧场景一致。
    """
    if not text:
        return ""
    now = _dt.datetime.now()
    hub_available = _hub_or_none() is not None

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key == "time":
            return _safe_strftime(now, "%H:%M")
        if key == "date":
            return _safe_strftime(now, "%Y-%m-%d")
        if key == "uptime":
            value = _metric_value("uptime", ctx)
            return match.group(0) if value is None else f"{value:.1f}"
        if _is_known_metric(key):
            value = _metric_value(key, ctx)
            if value is None and not hub_available:
                return match.group(0)
            return _format_value(key, value)
        return match.group(0)

    return _PLACEHOLDER_RE.sub(replace, text)


# ------------------------------------------------------------------ 文本图层
def _draw_text_layer(draw: ImageDraw.ImageDraw, layer: dict, ctx: dict | None = None) -> None:
    text = layer.get("text", "")
    if not text:
        return
    if ctx is not None and _text_needs_refresh(layer):
        text = _resolve_template(text, ctx)
    font = load_font(layer["size"], layer.get("bold", False))
    fill = _rgb(layer.get("color", "#FFFFFF"))
    parts = text.split("\n")
    lines = [(part, font, fill) for part in parts]
    _paint_text_block(
        draw,
        lines,
        layer["x"] * SCREEN_W,
        layer["y"] * SCREEN_H,
        layer.get("anchor", "center"),
        align=layer.get("align", "center"),
        shadow=layer.get("shadow", True),
    )


def _draw_clock_layer(draw: ImageDraw.ImageDraw, layer: dict, now: _dt.datetime) -> None:
    font = load_font(layer["size"], layer.get("bold", False))
    fill = _rgb(layer.get("color", "#FFFFFF"))
    lines = [(_safe_strftime(now, layer.get("format", "%H:%M")), font, fill)]
    if layer.get("show_date"):
        date_font = load_font(layer.get("date_size", 36), layer.get("bold", False))
        lines.append((_safe_strftime(now, layer.get("date_format", "%Y-%m-%d")), date_font, fill))
    _paint_text_block(
        draw,
        lines,
        layer["x"] * SCREEN_W,
        layer["y"] * SCREEN_H,
        layer.get("anchor", "center"),
        align="center",
        shadow=layer.get("shadow", True),
    )


def _draw_sysinfo_layer(draw: ImageDraw.ImageDraw, layer: dict, ctx: dict) -> None:
    fields = layer.get("fields") or ["cpu", "ram"]
    separator = layer.get("separator", "    ")
    text = separator.join(_format_metric(field, _metric_value(field, ctx)) for field in fields)
    font = load_font(layer["size"], layer.get("bold", False))
    _paint_text_block(
        draw,
        [(text, font, _rgb(layer.get("color", "#8FD3FF")))],
        layer["x"] * SCREEN_W,
        layer["y"] * SCREEN_H,
        layer.get("anchor", "center"),
        align="center",
        shadow=layer.get("shadow", True),
    )


def _draw_bars_layer(draw: ImageDraw.ImageDraw, layer: dict, ctx: dict) -> None:
    items = layer.get("items") or ["cpu", "ram"]
    if not items:
        return
    area_x = layer["x"] * SCREEN_W
    area_y = layer["y"] * SCREEN_H
    area_w = max(4.0, layer["w"] * SCREEN_W)
    area_h = max(4.0, layer["h"] * SCREEN_H)
    gap = max(0.0, layer.get("gap", 0.015) * SCREEN_H)
    row_h = (area_h - gap * (len(items) - 1)) / len(items)
    if row_h <= 1:
        return
    radius = min(row_h / 2.0, layer.get("radius", 0.5) * row_h / 2.0, 18.0)
    radius = max(0.0, radius)
    bg_color = _rgb(layer.get("bg", "#1E293B"))
    colors = [_rgb(layer.get("color", "#38BDF8")), _rgb(layer.get("color2", "#F472B6"))]
    label_color = _rgb(layer.get("label_color", "#E2E8F0"))
    show_label = layer.get("label", True)
    font = load_font(max(9, min(layer["size"], int(row_h * 0.62))), bold=True)
    for index, field in enumerate(items):
        value = _metric_value(field, ctx)
        maximum = _metric_max(field)
        ratio = 0.0 if value is None else max(0.0, min(1.0, value / maximum))
        top = area_y + index * (row_h + gap)
        box = (area_x, top, area_x + area_w, top + row_h)
        draw.rounded_rectangle(box, radius=radius, fill=bg_color)
        fill_w = ratio * area_w
        if fill_w > 0.5:
            fill_radius = min(radius, fill_w / 2.0)
            draw.rounded_rectangle(
                (area_x, top, area_x + fill_w, top + row_h),
                radius=fill_radius,
                fill=colors[index % 2],
            )
        if show_label:
            label = _metric_label(field)
            right_text = _format_value(field, value)
            text_y = top + row_h / 2.0
            draw.text((area_x + 10, text_y), label, font=font, fill=label_color, anchor="lm")
            draw.text(
                (area_x + area_w - 10, text_y),
                right_text,
                font=font,
                fill=label_color,
                anchor="rm",
            )


# ------------------------------------------------------------------ 折线图 / 仪表
def _layer_bound(layer: dict, name: str) -> float | None:
    """读取 ``min`` / ``max`` 字段（``None`` 表示按指标自身范围）。"""
    return _finite_or_none(layer.get(name))


def _lighten(color: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    """把颜色向白色按 ``amount`` 提亮（0~1）。"""
    factor = max(0.0, min(1.0, float(amount)))
    return tuple(int(round(channel + (255 - channel) * factor)) for channel in color)


def _smooth_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Chaikin 一次细分：把折线插值成更平滑的曲线（``smooth=True`` 时使用）。"""
    if len(points) < 3:
        return points
    smoothed: list[tuple[float, float]] = [points[0]]
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        smoothed.append((x0 * 0.75 + x1 * 0.25, y0 * 0.75 + y1 * 0.25))
        smoothed.append((x0 * 0.25 + x1 * 0.75, y0 * 0.25 + y1 * 0.75))
    smoothed.append(points[-1])
    return smoothed


def _chart_area(layer: dict) -> tuple[float, float, float, float]:
    """折线图区域 ``(left, top, width, height)``：``x, y`` 默认即左上角。"""
    width = max(8.0, min(SCREEN_W, float(layer.get("w", 0.8)) * SCREEN_W))
    height = max(8.0, min(SCREEN_H, float(layer.get("h", 0.22)) * SCREEN_H))
    vertical, horizontal = _anchor_parts(layer.get("anchor", "center"))
    left = float(layer.get("x", 0.5)) * SCREEN_W
    top = float(layer.get("y", 0.5)) * SCREEN_H
    if horizontal == "right":
        left -= width
    if vertical == "bottom":
        top -= height
    left = max(0.0, min(SCREEN_W - width, left))
    top = max(0.0, min(SCREEN_H - height, top))
    return left, top, width, height


def _draw_chart_layer(draw: ImageDraw.ImageDraw, layer: dict, ctx: dict) -> None:
    """折线图图层：背景 + 网格 + 半透明面积 + 折线 + 可选标题。"""
    metric = str(layer.get("metric") or "cpu")
    left, top, width, height = _chart_area(layer)
    right, bottom = left + width, top + height
    radius = max(0.0, min(0.5, float(layer.get("radius", 0.5))) * min(width, height) / 2.0)
    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=radius,
        fill=_rgb(layer.get("bg", "#0F172A")),
    )
    if layer.get("grid", True):
        grid_color = _lighten(_rgb(layer.get("bg", "#0F172A")), 0.2)
        for step in (1, 2, 3):
            line_y = top + height * step / 4.0
            draw.line([(left + 2, line_y), (right - 2, line_y)], fill=grid_color, width=1)

    try:
        points_limit = int(layer.get("points", 120))
    except (TypeError, ValueError):
        points_limit = 120
    points_limit = max(2, min(480, points_limit))
    current = _metric_value(metric, ctx)
    values = _metric_history(metric, ctx)[-points_limit:]
    if not values:
        values = [current if current is not None else 0.0]
    while len(values) < 2:
        values.append(values[-1])

    low = _layer_bound(layer, "min")
    if low is None:
        low = 0.0
    high = _layer_bound(layer, "max")
    if high is None:
        high = _metric_max(metric)
    if high <= low:
        high = low + 1.0

    count = len(values)
    raw_points: list[tuple[float, float]] = []
    for index, value in enumerate(values):
        x = left + (right - left) * index / (count - 1)
        ratio = max(0.0, min(1.0, (value - low) / (high - low)))
        raw_points.append((x, bottom - ratio * height))
    line_points = _smooth_points(raw_points) if layer.get("smooth", False) else raw_points

    if layer.get("fill", True):
        area_color = (*_rgb(layer.get("color2", "#0EA5E9")), 90)
        polygon = [(raw_points[0][0], bottom), *raw_points, (raw_points[-1][0], bottom)]
        draw.polygon(polygon, fill=area_color)
    draw.line(
        line_points,
        fill=_rgb(layer.get("color", "#38BDF8")),
        width=max(1, int(layer.get("line_width", 2))),
        joint="curve",
    )

    if layer.get("label", True):
        font = load_font(max(9, int(layer.get("size", 18))), bold=True)
        draw.text(
            (left + 8, top + 6),
            _format_metric(metric, current),
            font=font,
            fill=_rgb(layer.get("color", "#38BDF8")),
            anchor="lt",
        )


def _gauge_center(layer: dict, radius: float) -> tuple[float, float]:
    """仪表圆心：``x, y`` 是圆心；``top-left`` 等锚点按直径外接框换算。"""
    vertical, horizontal = _anchor_parts(layer.get("anchor", "center"))
    center_x = float(layer.get("x", 0.5)) * SCREEN_W
    center_y = float(layer.get("y", 0.5)) * SCREEN_H
    if horizontal == "left":
        center_x += radius
    elif horizontal == "right":
        center_x -= radius
    if vertical == "top":
        center_y += radius
    elif vertical == "bottom":
        center_y -= radius
    return center_x, center_y


def _gauge_numeric_text(metric: str, value: float | None, show_unit: bool) -> str:
    """仪表数值文字：``show_unit=False`` 时只保留数字。"""
    if show_unit:
        return _format_value(metric, value)
    if value is None:
        return "—"
    spec = _metric_spec(metric)
    try:
        digits = max(0, min(6, int(spec.get("digits", 1))))
    except (TypeError, ValueError):
        digits = 1
    return f"{value:.{digits}f}"


def _draw_gauge_layer(
    canvas: Image.Image, draw: ImageDraw.ImageDraw, layer: dict, ctx: dict
) -> None:
    """环形仪表图层：2× 超采样的轨道 + 值弧，圆心上下显示标签与数值。"""
    metric = str(layer.get("metric") or "cpu")
    diameter = max(4.0, min(SCREEN_W * 1.5, float(layer.get("diameter", 0.5)) * SCREEN_W))
    radius = diameter / 2.0
    thickness = max(2.0, min(radius, float(layer.get("thickness", 0.06)) * SCREEN_W))
    center_x, center_y = _gauge_center(layer, radius)

    value = _metric_value(metric, ctx)
    low = _layer_bound(layer, "min")
    if low is None:
        low = 0.0
    high = _layer_bound(layer, "max")
    if high is None:
        high = _metric_max(metric)
    if high <= low:
        high = low + 1.0
    ratio = 0.0 if value is None else max(0.0, min(1.0, (value - low) / (high - low)))

    try:
        start_angle = float(layer.get("start_angle", 135.0))
    except (TypeError, ValueError):
        start_angle = 135.0
    sweep = float(layer.get("sweep", 270.0))
    sweep = max(1.0, min(360.0, sweep))

    # 只在仪表外接框内做 2× 超采样，避免整屏 960×960 分配拖慢单帧。
    scale = 2
    box = max(4, int(math.ceil(diameter + thickness + 4)))
    size = box * scale
    region = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    region_draw = ImageDraw.Draw(region)
    middle = size / 2.0
    ring_radius = max(1.0, (diameter - thickness) / 2.0 * scale)
    ring_width = max(2, int(round(thickness * scale)))
    ring_box = (
        middle - ring_radius,
        middle - ring_radius,
        middle + ring_radius,
        middle + ring_radius,
    )
    region_draw.arc(
        ring_box,
        start=start_angle,
        end=start_angle + sweep,
        fill=(*_rgb(layer.get("track", "#1E293B")), 255),
        width=ring_width,
    )
    if ratio > 0.0:
        region_draw.arc(
            ring_box,
            start=start_angle,
            end=start_angle + max(0.5, sweep * ratio),
            fill=(*_rgb(layer.get("color", "#38BDF8")), 255),
            width=ring_width,
        )
    scaled = region.resize((box, box), Image.Resampling.LANCZOS)
    canvas.paste(
        scaled, (int(round(center_x - box / 2.0)), int(round(center_y - box / 2.0))), scaled
    )

    color = _rgb(layer.get("color", "#38BDF8"))
    value_text = _gauge_numeric_text(metric, value, layer.get("show_unit", True))
    value_font = load_font(max(9, int(layer.get("value_size", 48))), bold=True)
    draw.text(
        (center_x, center_y + diameter / 6.0),
        value_text,
        font=value_font,
        fill=color,
        anchor="mm",
    )
    if layer.get("label", True):
        label_font = load_font(max(9, int(layer.get("size", 20))))
        draw.text(
            (center_x, center_y - diameter / 6.0),
            _metric_label(metric),
            font=label_font,
            fill=(226, 232, 240),
            anchor="mm",
        )


def _layer_image(layer: dict) -> Image.Image:
    """图片图层：取（并缓存）按 scale / opacity 缩放后的 RGBA。"""
    path = layer.get("path") or ""
    signature = _file_sig(path) if path else None
    target_scale = layer.get("scale", 0.4)
    opacity = round(float(layer.get("opacity", 1.0)), 2)
    key = (os.path.abspath(path) if path else "", signature, target_scale, opacity)
    cached = _lru_touch(_layer_image_cache, key)
    if cached is not None:
        return cached
    source = _source_image(path)
    width, height = source.size
    target_w = max(1, int(round(target_scale * SCREEN_W)))
    target_h = max(1, int(round(height * (target_w / width)))) if width else target_w
    rgba = source.resize((target_w, target_h), Image.Resampling.LANCZOS).convert("RGBA")
    if opacity < 1.0:
        alpha = rgba.getchannel("A")
        alpha = ImageChops.multiply(alpha, Image.new("L", alpha.size, int(round(opacity * 255))))
        rgba.putalpha(alpha)
    _lru_put(_layer_image_cache, key, rgba, 24)
    return rgba


def _draw_image_layer(canvas: Image.Image, layer: dict, draw: ImageDraw.ImageDraw) -> None:
    try:
        image = _layer_image(layer)
    except (FileNotFoundError, OSError, ValueError):
        _draw_missing_image(draw, layer)
        return
    vertical, horizontal = _anchor_parts(layer.get("anchor", "center"))
    width, height = image.size
    center_x = layer["x"] * SCREEN_W
    center_y = layer["y"] * SCREEN_H
    factor_x = {"left": 0.0, "center": 0.5, "right": 1.0}[horizontal]
    factor_y = {"top": 0.0, "center": 0.5, "bottom": 1.0}[vertical]
    left = int(round(center_x - width * factor_x))
    top = int(round(center_y - height * factor_y))
    canvas.paste(image, (left, top), image)


def _draw_missing_image(draw: ImageDraw.ImageDraw, layer: dict) -> None:
    """图片文件缺失时的占位框。"""
    center_x = layer["x"] * SCREEN_W
    center_y = layer["y"] * SCREEN_H
    box_w = 150.0
    box_h = 80.0
    draw.rounded_rectangle(
        (center_x - box_w / 2, center_y - box_h / 2, center_x + box_w / 2, center_y + box_h / 2),
        radius=12,
        outline=(248, 113, 113),
        width=2,
        fill=(30, 20, 25),
    )
    font = load_font(18)
    draw.text((center_x, center_y), "图片缺失", font=font, fill=(252, 165, 165), anchor="mm")


# ------------------------------------------------------------------ v1.3 新图层公共工具
_CLAMPED_FONT_SIZES = (9, 400)
_O_COUNTDOWN_PLACEHOLDER = "设置目标时间"
_QR_PLACEHOLDER_TEXT = "二维码"
_SEQUENCE_PLACEHOLDER_TEXT = "图片序列缺失"
_TICKER_CYCLE_MIN = 8.0
_WEATHER_PLACEHOLDER_TEXT = "天气 —"
_WEATHER_ICON_KINDS = ("sun", "cloud", "rain", "snow", "fog", "storm")


def _clamped(value: Any, low: float, high: float, default: float) -> float:
    """把任意值夹到 ``[low, high]``；非法值用 ``default``。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(number):
        return float(default)
    return max(float(low), min(float(high), number))


def _layer_font_size(layer: dict, name: str, default: float, minimum: float = 9.0) -> int:
    """读取图层字号字段（``size`` / ``value_size`` / ``title_size`` …），夹到 9~400。"""
    return int(round(_clamped(layer.get(name, default), minimum, 400, default)))


def _text_width(font: ImageFont.ImageFont, text: str) -> float:
    """量文本宽度（优先 ``font.getlength``，失败时退回临时画布 ``textbbox``）。"""
    if not text:
        return 0.0
    getter = getattr(font, "getlength", None)
    if callable(getter):
        try:
            return max(0.0, float(getter(text)))
        except Exception:  # noqa: BLE001 - 字体实现异常时退回 textbbox
            pass
    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    bbox = scratch.textbbox((0, 0), text, font=font, anchor="lt")
    return max(0.0, float(bbox[2] - bbox[0]))


def _anchor_factors(anchor: Any) -> tuple[float, float]:
    """anchor → ``(水平因子, 垂直因子)``：left/top=0、center=0.5、right/bottom=1。"""
    vertical, horizontal = _anchor_parts(anchor if isinstance(anchor, str) else "center")
    factors_x = {"left": 0.0, "center": 0.5, "right": 1.0}
    factors_y = {"top": 0.0, "center": 0.5, "bottom": 1.0}
    return factors_x[horizontal], factors_y[vertical]


def _layer_placeholder(
    draw: ImageDraw.ImageDraw,
    layer: dict,
    label: str,
    box: tuple[float, float, float, float] | None = None,
) -> None:
    """画图层占位框（缺模块 / 缺数据时统一降级外观）。"""
    if box is None:
        center_x = float(layer.get("x", 0.5)) * SCREEN_W
        center_y = float(layer.get("y", 0.5)) * SCREEN_H
        box = (center_x - 85.0, center_y - 45.0, center_x + 85.0, center_y + 45.0)
    draw.rounded_rectangle(box, radius=12, outline=(100, 116, 139), width=2, fill=(17, 24, 39))
    draw.text(
        ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0),
        label,
        font=load_font(18),
        fill=(148, 163, 184),
        anchor="mm",
    )


# ------------------------------------------------------------------ 温度计图层
def _thermometer_box(layer: dict) -> tuple[float, float, float, float]:
    """温度计外接框 ``(left, top, width, height)``；``x, y`` 按左上角解释（与 bars 一致）。"""
    width = max(
        6.0, min(float(SCREEN_W), _clamped(layer.get("w", 0.14), 0.0, 2.0, 0.14) * SCREEN_W)
    )
    height = max(
        6.0, min(float(SCREEN_H), _clamped(layer.get("h", 0.62), 0.0, 2.0, 0.62) * SCREEN_H)
    )
    left = max(0.0, min(float(SCREEN_W) - width, float(layer.get("x", 0.5)) * SCREEN_W))
    top = max(0.0, min(float(SCREEN_H) - height, float(layer.get("y", 0.5)) * SCREEN_H))
    return left, top, width, height


def _draw_thermometer_layer(
    draw: ImageDraw.ImageDraw, layer: dict, ctx: dict, t: float = 0.0
) -> None:
    """竖向胶囊温度计：填充比例按 ``[min, max]`` 换算，数值/标签在胶囊上方或下方。"""
    metric = str(layer.get("metric") or "cpu")
    left, top, width, height = _thermometer_box(layer)
    right, bottom = left + width, top + height
    value = _metric_value(metric, ctx)
    low = _layer_bound(layer, "min")
    if low is None:
        low = 0.0
    high = _layer_bound(layer, "max")
    if high is None:
        high = _metric_max(metric)
    if high <= low:
        high = low + 1.0
    ratio = 0.0 if value is None else max(0.0, min(1.0, (value - low) / (high - low)))
    radius = _clamped(layer.get("radius", 0.5), 0.0, 1.0, 0.5) * width / 2.0
    track = _rgb(layer.get("track", "#1E293B"))
    color = _rgb(layer.get("color", "#FB7185"))
    draw.rounded_rectangle((left, top, right, bottom), radius=radius, fill=track)
    fill_height = ratio * height
    if fill_height > 0.5:
        draw.rounded_rectangle(
            (left, bottom - fill_height, right, bottom),
            radius=min(radius, fill_height / 2.0),
            fill=color,
        )
    if layer.get("label", True):
        label_font = load_font(_layer_font_size(layer, "size", 20))
        draw.text(
            (left + width / 2.0, max(2.0, top - 4.0)),
            _metric_label(metric),
            font=label_font,
            fill=(226, 232, 240),
            anchor="mb",
        )
    if layer.get("show_value", True):
        value_font = load_font(_layer_font_size(layer, "value_size", 34), bold=True)
        text = _gauge_numeric_text(metric, value, layer.get("show_unit", True))
        if width >= 56.0 and height >= 150.0:
            draw.text(
                (left + width / 2.0, bottom - 6.0),
                text,
                font=value_font,
                fill=(255, 255, 255),
                anchor="mb",
            )
        else:
            draw.text(
                (left + width / 2.0, bottom + 4.0),
                text,
                font=value_font,
                fill=color,
                anchor="mt",
            )


# ------------------------------------------------------------------ 倒计时图层
def _parse_target(value: Any) -> _dt.datetime | None:
    """解析 ``target``：支持 ``YYYY-MM-DD``、完整 ISO 8601 与时区后缀；失败返回 ``None``。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _countdown_parts(layer: dict, now: _dt.datetime | None = None) -> tuple[str, str, str]:
    """返回 ``(标题, 主文本, 状态)``；状态为 ``running`` / ``done`` / ``placeholder``。

    ``format`` 支持 ``{d}`` 天、``{h}`` / ``{m}`` / ``{s}``（时/分/秒，两位补零）。
    """
    moment = now or _dt.datetime.now()
    title = str(layer.get("title") or "").strip()
    target = _parse_target(layer.get("target"))
    if target is None:
        return title, _O_COUNTDOWN_PLACEHOLDER, "placeholder"
    remaining = (target - moment).total_seconds()
    if remaining <= 0.0:
        return title, str(layer.get("done_text") or "时间到"), "done"
    total = int(math.floor(remaining))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    template = str(layer.get("format") or "{d} 天 {h}:{m}:{s}")
    main = (
        template.replace("{d}", str(days))
        .replace("{h}", f"{hours:02d}")
        .replace("{m}", f"{minutes:02d}")
        .replace("{s}", f"{seconds:02d}")
    )
    return title, main, "running"


def _draw_countdown_layer(
    draw: ImageDraw.ImageDraw, layer: dict, ctx: dict | None = None, t: float = 0.0
) -> None:
    """倒计时：小号标题 + 大号剩余时间；target 为空/非法时画弱化的「设置目标时间」。"""
    title, main, state = _countdown_parts(layer)
    main_font = load_font(_layer_font_size(layer, "size", 44), bold=True)
    title_font = load_font(_layer_font_size(layer, "title_size", 22))
    weak = _rgb(layer.get("title_color", "#94A3B8"))
    strong = _rgb(layer.get("color", "#38BDF8"))
    lines: list[tuple[str, ImageFont.ImageFont, tuple[int, int, int]]] = []
    if title:
        lines.append((title, title_font, weak))
    lines.append((main, main_font, strong if state == "running" else weak))
    _paint_text_block(
        draw,
        lines,
        float(layer.get("x", 0.5)) * SCREEN_W,
        float(layer.get("y", 0.5)) * SCREEN_H,
        layer.get("anchor", "center"),
        align="center",
        shadow=bool(layer.get("shadow", True)),
    )


# ------------------------------------------------------------------ 滚动文字图层
def _ticker_metrics(layer: dict, t: float) -> tuple[float, float, float]:
    """返回 ``(相位, 文本宽度, 循环周期)``；相位只由 ``t`` 决定，图层本身无状态。"""
    text = str(layer.get("text") or "")
    font = load_font(_layer_font_size(layer, "size", 34), bool(layer.get("bold", False)))
    text_width = max(1.0, _text_width(font, text))
    gap = _clamped(layer.get("gap", 0.5), 0.0, 4.0, 0.5) * SCREEN_W
    cycle = max(_TICKER_CYCLE_MIN, text_width + gap)
    speed = max(5.0, min(600.0, _clamped(layer.get("speed", 60.0), 5.0, 600.0, 60.0)))
    phase = (max(0.0, float(t)) * speed) % cycle
    return phase, text_width, cycle


def _draw_ticker_layer(
    draw: ImageDraw.ImageDraw, layer: dict, t: float, ctx: dict | None = None
) -> None:
    """整屏跑马灯：按 ``gap`` 比例重复文本，相位 ``(t * speed) % 周期`` 决定偏移。"""
    text = str(layer.get("text") or "")
    if not text:
        return
    font = load_font(_layer_font_size(layer, "size", 34), bool(layer.get("bold", False)))
    color = _rgb(layer.get("color", "#E2E8F0"))
    phase, text_width, cycle = _ticker_metrics(layer, t)
    ascent, descent = font.getmetrics()
    line_height = ascent + descent
    center_y = float(layer.get("y", 0.5)) * SCREEN_H
    if layer.get("band", False):
        band_height = min(float(SCREEN_H), line_height + 16.0)
        band_top = center_y - band_height / 2.0
        draw.rectangle(
            (0.0, band_top, float(SCREEN_W), band_top + band_height),
            fill=_rgb(layer.get("band_color", "#0B0F14")),
        )
        text_top = band_top + 8.0
    else:
        text_top = center_y - line_height / 2.0
    forward = str(layer.get("direction", "left")) != "right"
    base = -phase if forward else phase - cycle
    copies = int(math.ceil((SCREEN_W + text_width) / cycle)) + 2
    shadow = bool(layer.get("shadow", True))
    for step in range(-2, copies + 2):
        x = base + step * cycle
        if x > SCREEN_W or x + text_width < 0:
            continue
        if shadow:
            draw.text(
                (x + _SHADOW_OFFSET, text_top + _SHADOW_OFFSET),
                text,
                font=font,
                fill=_SHADOW_COLOR,
                anchor="lt",
            )
        draw.text((x, text_top), text, font=font, fill=color, anchor="lt")


# ------------------------------------------------------------------ 图片序列图层
def _sequence_paths(layer: dict) -> list[str]:
    """规范化 ``paths``：只保留非空字符串路径，最多 64 个（与 scenes 的限制一致）。"""
    raw = layer.get("paths")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip() for item in raw if isinstance(item, str) and str(item).strip()][:64]


def _sequence_frame_path(layer: dict, t: float) -> tuple[str, int]:
    """按 ``int(t * fps)`` 选帧；``loop=False`` 时截断到末帧。"""
    paths = _sequence_paths(layer)
    if not paths:
        return "", 0
    fps = _clamped(layer.get("fps", 8.0), 0.5, 60.0, 8.0)
    index = int(math.floor(max(0.0, float(t)) * fps))
    if bool(layer.get("loop", True)):
        index %= len(paths)
    else:
        index = min(index, len(paths) - 1)
    return paths[index], index


def _fit_to_box(source: Image.Image, width: int, height: int, fit: str) -> Image.Image:
    """把图片塞进 ``width×height`` 盒子并返回 RGBA（``fit`` 语义与背景一致）。"""
    width = max(1, int(width))
    height = max(1, int(height))
    if fit not in _VALID_FITS:
        fit = "crop"
    src_w, src_h = source.size
    if src_w <= 0 or src_h <= 0:
        return Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if fit == "stretch":
        return source.resize((width, height), Image.Resampling.LANCZOS).convert("RGBA")
    if fit == "crop":
        scale = max(width / src_w, height / src_h)
        target_w = max(1, int(round(src_w * scale)))
        target_h = max(1, int(round(src_h * scale)))
        scaled = source.resize((target_w, target_h), Image.Resampling.LANCZOS)
        left = max(0, (target_w - width) // 2)
        top = max(0, (target_h - height) // 2)
        return scaled.crop((left, top, left + width, top + height)).convert("RGBA")
    scale = min(width / src_w, height / src_h)
    target_w = max(1, int(round(src_w * scale)))
    target_h = max(1, int(round(src_h * scale)))
    scaled = source.resize((target_w, target_h), Image.Resampling.LANCZOS).convert("RGBA")
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.paste(scaled, ((width - target_w) // 2, (height - target_h) // 2))
    return canvas


def _sequence_image(layer: dict, path: str, width: int, height: int) -> Image.Image:
    """取（并缓存）序列帧：键为 绝对路径 + mtime/大小 + 盒子尺寸 + fit/圆角/透明度。"""
    fit = str(layer.get("fit", "crop"))
    radius = _clamped(layer.get("radius", 0.0), 0.0, 1.0, 0.0)
    opacity = _clamped(layer.get("opacity", 1.0), 0.0, 1.0, 1.0)
    key = (
        os.path.abspath(path),
        _file_sig(path),
        width,
        height,
        fit,
        round(radius, 3),
        round(opacity, 3),
    )
    cached = _lru_touch(_sequence_cache, key)
    if cached is not None:
        return cached
    source = _source_image(path)
    image = _fit_to_box(source, width, height, fit)
    if radius > 0.0:
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, width - 1, height - 1),
            radius=int(round(min(width, height) * radius / 2.0)),
            fill=255,
        )
        image.putalpha(mask)
    if opacity < 1.0:
        alpha = image.getchannel("A")
        alpha = ImageChops.multiply(alpha, Image.new("L", alpha.size, int(round(opacity * 255))))
        image.putalpha(alpha)
    _lru_put(_sequence_cache, key, image, _SEQUENCE_CACHE_LIMIT)
    return image


def _draw_sequence_layer(
    canvas: Image.Image, draw: ImageDraw.ImageDraw, layer: dict, t: float
) -> None:
    """图片序列：按 fps 取帧，按 ``w×h`` 盒子 fit 后按 anchor 贴到 ``x, y``。"""
    path, _index = _sequence_frame_path(layer, t)
    width = max(1, int(round(_clamped(layer.get("w", 0.5), 0.0, 2.0, 0.5) * SCREEN_W)))
    height = max(1, int(round(_clamped(layer.get("h", 0.5), 0.0, 2.0, 0.5) * SCREEN_H)))
    if not path:
        _layer_placeholder(draw, layer, _SEQUENCE_PLACEHOLDER_TEXT)
        return
    try:
        image = _sequence_image(layer, path, width, height)
    except (FileNotFoundError, OSError, ValueError):
        _layer_placeholder(draw, layer, _SEQUENCE_PLACEHOLDER_TEXT)
        return
    factor_x, factor_y = _anchor_factors(layer.get("anchor", "center"))
    left = int(round(float(layer.get("x", 0.5)) * SCREEN_W - width * factor_x))
    top = int(round(float(layer.get("y", 0.5)) * SCREEN_H - height * factor_y))
    canvas.paste(image, (left, top), image)


# ------------------------------------------------------------------ 二维码图层
def _qr_data(layer: dict, ctx: dict | None) -> str:
    """二维码最终编码文本：先替换 ``{metric}`` 等模板占位符（无 ctx 时原样保留）。"""
    raw = str(layer.get("data") or "")
    if not raw:
        return ""
    if ctx is not None and _PLACEHOLDER_RE.search(raw):
        return _resolve_template(raw, ctx)
    return raw


def _qr_error_level(layer: dict) -> str:
    """纠错级别：只接受 L/M/Q/H，未知值退回 M。"""
    value = str(layer.get("error", "M")).strip().upper()
    return value if value in ("L", "M", "Q", "H") else "M"


def _qr_matrix(layer: dict, data: str) -> tuple[tuple[bool, ...], ...] | None:
    """调用 ``qrmini.make`` 并缓存矩阵（键 ``data + error``，容量有界）。"""
    if not data or _qrmini is None:
        return None
    error = _qr_error_level(layer)
    key = (data, error)
    cached = _lru_touch(_qr_matrix_cache, key)
    if cached is not None:
        return cached
    maker = getattr(_qrmini, "make", None)
    if not callable(maker):
        return None
    try:
        matrix = maker(data, error)
    except Exception:  # noqa: BLE001 - 编码失败（数据过长等）按"没有二维码"降级
        return None
    if not isinstance(matrix, (list, tuple)) or not matrix:
        return None
    rows: list[tuple[bool, ...]] = []
    for row in matrix:
        if not isinstance(row, (list, tuple)) or not row:
            return None
        rows.append(tuple(bool(cell) for cell in row))
    if not rows or len({len(row) for row in rows}) != 1:
        return None
    result = tuple(rows)
    _lru_put(_qr_matrix_cache, key, result, _QR_CACHE_LIMIT)
    return result


def _qr_geometry(layer: dict, count: int) -> tuple[int, int, int]:
    """返回 ``(module, actual, border)``：``module = floor(span / (n + 2*border))``。"""
    span = max(
        8.0, min(float(SCREEN_W), _clamped(layer.get("size", 0.5), 0.0, 2.0, 0.5) * SCREEN_W)
    )
    border = int(round(_clamped(layer.get("border", 2), 0.0, 8.0, 2.0)))
    total_modules = count + 2 * border
    module = int(math.floor(span / total_modules)) if total_modules > 0 else 0
    return module, module * total_modules, border


def _qr_image(
    layer: dict, matrix: tuple[tuple[bool, ...], ...], module: int, border: int
) -> Image.Image:
    """按矩阵渲染二维码位图（最近邻放大，键含 矩阵/边框/前景/背景/模块尺寸）。"""
    count = len(matrix)
    total = count + 2 * border
    fg = _rgb(layer.get("fg", "#0B0F14"))
    bg = _rgb(layer.get("bg", "#FFFFFF"))
    key = (matrix, border, fg, bg, module)
    cached = _lru_touch(_qr_image_cache, key)
    if cached is not None:
        return cached
    pixels = bytes(255 if cell else 0 for row in matrix for cell in row)
    mask_small = Image.frombytes("L", (count, count), pixels)
    mask = Image.new("L", (total, total), 0)
    mask.paste(mask_small, (border, border))
    if module > 1:
        mask = mask.resize((module * total, module * total), Image.Resampling.NEAREST)
    image = Image.new("RGB", mask.size, bg)
    image.paste(fg, (0, 0), mask)
    _lru_put(_qr_image_cache, key, image, _QR_CACHE_LIMIT)
    return image


def _draw_qr_placeholder(
    draw: ImageDraw.ImageDraw, center_x: float, center_y: float, span: float
) -> None:
    """module < 1 / 缺 qrmini / 数据为空时的二维码占位框。"""
    half = max(20.0, span / 2.0)
    _layer_placeholder(
        draw,
        {"x": center_x / SCREEN_W, "y": center_y / SCREEN_H},
        _QR_PLACEHOLDER_TEXT,
        box=(center_x - half, center_y - half, center_x + half, center_y + half),
    )


def _draw_qrcode_layer(
    canvas: Image.Image, draw: ImageDraw.ImageDraw, layer: dict, ctx: dict | None = None
) -> None:
    """二维码：``module < 1`` 或编码失败时画占位框；``label=True`` 时下方补一行可读文本。"""
    data = _qr_data(layer, ctx)
    span = max(
        8.0, min(float(SCREEN_W), _clamped(layer.get("size", 0.5), 0.0, 2.0, 0.5) * SCREEN_W)
    )
    center_x = float(layer.get("x", 0.5)) * SCREEN_W
    center_y = float(layer.get("y", 0.5)) * SCREEN_H
    matrix = _qr_matrix(layer, data)
    if matrix is None:
        _draw_qr_placeholder(draw, center_x, center_y, span)
        return
    module, actual, border = _qr_geometry(layer, len(matrix))
    if module < 1:
        _draw_qr_placeholder(draw, center_x, center_y, span)
        return
    image = _qr_image(layer, matrix, module, border)
    factor_x, factor_y = _anchor_factors(layer.get("anchor", "center"))
    left = int(round(center_x - actual * factor_x))
    top = int(round(center_y - actual * factor_y))
    canvas.paste(image, (left, top))
    if layer.get("label", False) and data:
        label_font = load_font(_layer_font_size(layer, "label_size", 18))
        label_text = data if len(data) <= 30 else data[:28] + "…"
        draw.text(
            (center_x, top + actual + 6.0),
            label_text,
            font=label_font,
            fill=_rgb(layer.get("label_color", "#FFFFFF")),
            anchor="mt",
        )


# ------------------------------------------------------------------ 天气图层
def _wmo_icon_kind(code: float | None) -> str:
    """WMO 天气代码 → 内置矢量图标类别（晴/云/雨/雪/雾/雷）。"""
    if code is None:
        return "cloud"
    value = int(code)
    if value <= 1:
        return "sun"
    if value <= 3:
        return "cloud"
    if value in (45, 48):
        return "fog"
    if 51 <= value <= 67 or 80 <= value <= 82:
        return "rain"
    if 71 <= value <= 77 or 85 <= value <= 86:
        return "snow"
    if 95 <= value <= 99:
        return "storm"
    return "cloud"


def _wmo_description(code: float | None) -> str:
    """WMO 代码 → 中文描述；优先用 weather 模块的 ``WEATHER_CODE_TEXT``。"""
    if code is None:
        return ""
    table = getattr(_weather, "WEATHER_CODE_TEXT", None) if _weather is not None else None
    if isinstance(table, dict):
        text = table.get(int(code))
        if isinstance(text, str) and text.strip():
            return text.strip()
    return {
        "sun": "晴",
        "cloud": "多云",
        "rain": "雨",
        "snow": "雪",
        "fog": "雾",
        "storm": "雷暴",
    }[_wmo_icon_kind(code)]


def _weather_icon_kind(snapshot: dict) -> str:
    """优先用 weather 模块的 ``code_icon``，否则按 WMO 代码走内置映射。"""
    code = _finite_or_none(snapshot.get("code"))
    getter = getattr(_weather, "code_icon", None) if _weather is not None else None
    if code is not None and callable(getter):
        try:
            kind = str(getter(int(code))).strip().lower()
            if kind in _WEATHER_ICON_KINDS:
                return kind
        except Exception:  # noqa: BLE001 - 第三方映射失败时用内置表
            pass
    return _wmo_icon_kind(code)


def _weather_hub_for(lat: float | None, lon: float | None) -> Any:
    """测试注入的 ``_weather_hub`` 优先；否则懒创建并缓存 ``weather.get_hub(lat, lon)``。"""
    hub = _weather_hub
    if hub is not None:
        return hub
    if _weather is None:
        return None
    getter = getattr(_weather, "get_hub", None)
    if not callable(getter):
        return None
    key = (
        None if lat is None else round(float(lat), 4),
        None if lon is None else round(float(lon), 4),
    )
    cached = _lru_touch(_weather_hubs, key)
    if cached is not None:
        return cached
    try:
        hub = getter() if lat is None and lon is None else getter(lat, lon)
    except Exception:  # noqa: BLE001 - 天气后端初始化失败视为无数据
        return None
    if hub is None:
        return None
    _lru_put(_weather_hubs, key, hub, _WEATHER_HUB_LIMIT)
    return hub


def _weather_snapshot(ctx: dict, layer: dict) -> dict:
    """取天气快照：同一次渲染内按 ``(lat, lon)`` 缓存，任何异常都返回 ``{}``。"""
    lat = _finite_or_none(layer.get("lat"))
    lon = _finite_or_none(layer.get("lon"))
    store = ctx.get("weather")
    if not isinstance(store, dict):
        store = {}
        ctx["weather"] = store
    key = (
        None if lat is None else round(lat, 4),
        None if lon is None else round(lon, 4),
    )
    if key in store:
        return store[key]
    snapshot: dict = {}
    hub = _weather_hub_for(lat, lon)
    getter = getattr(hub, "snapshot", None) if hub is not None else None
    if callable(getter):
        try:
            value = getter()
        except Exception:  # noqa: BLE001 - 快照异常时按无数据绘制
            value = None
        if isinstance(value, dict):
            snapshot = value
    store[key] = snapshot
    return snapshot


def _weather_text(layer: dict, snapshot: dict) -> str:
    """把快照里选中的字段拼成一行；无任何数据时返回「天气 —」。"""
    fields = layer.get("fields") or ("temp", "desc")
    separator = str(layer.get("separator") or "  ")
    parts: list[str] = []
    place = str(layer.get("place") or snapshot.get("place") or "").strip()
    if place:
        parts.append(place)
    for field in fields:
        name = str(field).strip().lower()
        if name == "temp":
            value = _finite_or_none(snapshot.get("temp"))
            if value is not None:
                parts.append(f"{value:.0f}°")
        elif name == "feels":
            value = _finite_or_none(snapshot.get("feels"))
            if value is not None:
                parts.append(f"体感 {value:.0f}°")
        elif name == "humidity":
            value = _finite_or_none(snapshot.get("humidity"))
            if value is not None:
                parts.append(f"湿度 {value:.0f}%")
        elif name == "wind":
            value = _finite_or_none(snapshot.get("wind"))
            if value is not None:
                parts.append(f"风 {value:.0f} km/h")
        elif name == "desc":
            text = str(snapshot.get("desc") or "").strip()
            if not text:
                text = _wmo_description(_finite_or_none(snapshot.get("code")))
            if text:
                parts.append(text)
    if not parts:
        return _WEATHER_PLACEHOLDER_TEXT
    return separator.join(parts)


_SUN_COLOR = (251, 191, 36)
_CLOUD_COLOR = (203, 213, 225)
_RAIN_COLOR = (96, 165, 250)
_SNOW_COLOR = (241, 245, 249)
_FOG_COLOR = (148, 163, 184)
_STORM_COLOR = (250, 204, 21)


def _draw_sun_icon(draw: ImageDraw.ImageDraw, cx: float, cy: float, radius: float) -> None:
    """太阳：中心圆 + 8 条光芒。"""
    core = radius * 0.42
    draw.ellipse((cx - core, cy - core, cx + core, cy + core), fill=_SUN_COLOR)
    for index in range(8):
        angle = math.pi * index / 4.0
        inner = core * 1.25
        outer = radius * 0.95
        draw.line(
            [
                (cx + math.cos(angle) * inner, cy + math.sin(angle) * inner),
                (cx + math.cos(angle) * outer, cy + math.sin(angle) * outer),
            ],
            fill=_SUN_COLOR,
            width=max(2, int(radius * 0.1)),
        )


def _draw_cloud_icon(
    draw: ImageDraw.ImageDraw, cx: float, cy: float, radius: float, color: tuple[int, int, int]
) -> None:
    """云朵：三团椭圆 + 底部圆角横条（雨/雪/雷图标复用它作底座）。"""
    base = cy + radius * 0.35
    thickness = radius * 0.55
    draw.ellipse(
        (cx - radius * 0.85, base - thickness, cx - radius * 0.05, base + thickness * 0.45),
        fill=color,
    )
    draw.ellipse(
        (cx + radius * 0.05, base - thickness * 1.15, cx + radius * 0.85, base + thickness * 0.45),
        fill=color,
    )
    draw.ellipse(
        (cx - radius * 0.45, base - thickness * 1.5, cx + radius * 0.35, base - thickness * 0.2),
        fill=color,
    )
    draw.rounded_rectangle(
        (cx - radius * 0.85, base - thickness * 0.25, cx + radius * 0.85, base + thickness * 0.5),
        radius=thickness * 0.4,
        fill=color,
    )


def _draw_weather_icon(
    draw: ImageDraw.ImageDraw, kind: str, center_x: float, center_y: float, size: float
) -> None:
    """用内置矢量图形画天气图标（不依赖任何图片资源或网络）。"""
    radius = max(4.0, float(size) / 2.0)
    if kind == "sun":
        _draw_sun_icon(draw, center_x, center_y, radius)
    elif kind == "rain":
        _draw_cloud_icon(draw, center_x, center_y, radius, _CLOUD_COLOR)
        for index in range(3):
            x = center_x - radius * 0.55 + index * radius * 0.55
            draw.line(
                [
                    (x, center_y + radius * 0.2),
                    (x - radius * 0.16, center_y + radius * 0.72),
                ],
                fill=_RAIN_COLOR,
                width=max(2, int(radius * 0.12)),
            )
    elif kind == "snow":
        _draw_cloud_icon(draw, center_x, center_y, radius * 0.92, _CLOUD_COLOR)
        for index in range(3):
            x = center_x - radius * 0.5 + index * radius * 0.5
            y = center_y + radius * 0.62
            dot = max(2.0, radius * 0.1)
            draw.ellipse((x - dot, y - dot, x + dot, y + dot), fill=_SNOW_COLOR)
            draw.line([(x - dot * 1.5, y), (x + dot * 1.5, y)], fill=_SNOW_COLOR, width=2)
    elif kind == "fog":
        for index, width_scale in enumerate((0.9, 0.75, 0.85)):
            y = center_y - radius * 0.3 + index * radius * 0.32
            half = radius * width_scale
            draw.line(
                [(center_x - half, y), (center_x + half, y)],
                fill=_FOG_COLOR,
                width=max(3, int(radius * 0.2)),
            )
    elif kind == "storm":
        _draw_cloud_icon(draw, center_x, center_y - radius * 0.1, radius * 0.9, _CLOUD_COLOR)
        draw.polygon(
            [
                (center_x + radius * 0.1, center_y + radius * 0.05),
                (center_x - radius * 0.3, center_y + radius * 0.7),
                (center_x + radius * 0.0, center_y + radius * 0.62),
                (center_x - radius * 0.2, center_y + radius * 1.05),
                (center_x + radius * 0.35, center_y + radius * 0.35),
                (center_x + radius * 0.05, center_y + radius * 0.42),
            ],
            fill=_STORM_COLOR,
        )
    else:
        _draw_cloud_icon(draw, center_x, center_y, radius, _CLOUD_COLOR)


def _draw_weather_layer(draw: ImageDraw.ImageDraw, layer: dict, ctx: dict, t: float = 0.0) -> None:
    """天气图层：左侧内置矢量图标 + 右侧一行天气文本；无数据时只画「天气 —」。"""
    snapshot = _weather_snapshot(ctx, layer)
    text = _weather_text(layer, snapshot)
    font = load_font(_layer_font_size(layer, "size", 28))
    fill = _rgb(layer.get("color", "#E2E8F0"))
    center_x = float(layer.get("x", 0.5)) * SCREEN_W
    center_y = float(layer.get("y", 0.5)) * SCREEN_H
    shadow = bool(layer.get("shadow", True))
    if not layer.get("icon", True) or not snapshot:
        _paint_text_block(
            draw,
            [(text, font, fill)],
            center_x,
            center_y,
            layer.get("anchor", "center"),
            align="center",
            shadow=shadow,
        )
        return
    icon_size = float(_layer_font_size(layer, "icon_size", 56, minimum=12.0))
    gap = max(6.0, icon_size * 0.15)
    text_width = _text_width(font, text)
    total_width = icon_size + gap + text_width
    factor_x, _factor_y = _anchor_factors(layer.get("anchor", "center"))
    left = center_x - total_width * factor_x
    _draw_weather_icon(
        draw, _weather_icon_kind(snapshot), left + icon_size / 2.0, center_y, icon_size
    )
    text_x = left + icon_size + gap
    if shadow:
        draw.text(
            (text_x + _SHADOW_OFFSET, center_y + _SHADOW_OFFSET),
            text,
            font=font,
            fill=_SHADOW_COLOR,
            anchor="lm",
        )
    draw.text((text_x, center_y), text, font=font, fill=fill, anchor="lm")


# ------------------------------------------------------------------ 正在播放图层
def _media_hub_or_none() -> Any:
    """返回进程级 MediaHub；media 模块缺失或初始化失败时返回 ``None``。"""
    global _media_hub
    hub = _media_hub
    if hub is not None:
        return hub
    if _media is None:
        return None
    getter = getattr(_media, "get_hub", None)
    if not callable(getter):
        return None
    with _media_lock:
        if _media_hub is None:
            try:
                _media_hub = getter()
            except Exception:  # noqa: BLE001 - 媒体后端不可用按无数据绘制
                return None
        return _media_hub


def _media_snapshot(ctx: dict) -> dict:
    """媒体快照；同一次渲染内只取一次，任何异常都返回 ``{}``。"""
    cached = ctx.get("media")
    if isinstance(cached, dict):
        return cached
    snapshot: dict = {}
    hub = _media_hub_or_none()
    getter = getattr(hub, "snapshot", None) if hub is not None else None
    if callable(getter):
        try:
            value = getter()
        except Exception:  # noqa: BLE001 - 快照异常时按无数据绘制
            value = None
        if isinstance(value, dict):
            snapshot = value
    ctx["media"] = snapshot
    return snapshot


def _nowplaying_lines(layer: dict, snapshot: dict) -> list[tuple[str, str]]:
    """按 ``fields`` 取文本行：``("title", 标题)`` 在前，``("meta", 作者/专辑)`` 在后。"""
    fields = [str(item).strip().lower() for item in (layer.get("fields") or ("title", "artist"))]
    title = str(snapshot.get("title") or "").strip()
    artist = str(snapshot.get("artist") or "").strip()
    album = str(snapshot.get("album") or "").strip()
    separator = str(layer.get("separator") or " · ")
    lines: list[tuple[str, str]] = []
    if "title" in fields and title:
        lines.append(("title", title))
    meta_parts: list[str] = []
    if "artist" in fields and artist:
        meta_parts.append(artist)
    if "album" in fields and album:
        meta_parts.append(album)
    if meta_parts:
        lines.append(("meta", separator.join(meta_parts)))
    return lines


def _nowplaying_text_image(layer: dict, lines: list[tuple[str, str]]) -> Image.Image:
    """把正在播放的文本渲染成透明底图（滚动时按窗口裁剪），按签名 + 文本缓存。"""
    key = (_layer_signature(layer), tuple(text for _, text in lines))
    cached = _lru_touch(_nowplaying_text_cache, key)
    if cached is not None:
        return cached
    size = _layer_font_size(layer, "size", 26)
    title_font = load_font(size, bold=True)
    meta_font = load_font(max(9, int(round(size * 0.78))))
    title_color = _rgb(layer.get("color", "#E2E8F0"))
    meta_color = _rgb(layer.get("label_color", "#94A3B8"))
    spacing = 4
    width = 1
    height = 0
    rendered: list[tuple[str, ImageFont.ImageFont, tuple[int, int, int], int]] = []
    for index, (_kind, text) in enumerate(lines):
        font = title_font if index == 0 else meta_font
        color = title_color if index == 0 else meta_color
        ascent, descent = font.getmetrics()
        line_height = ascent + descent
        width = max(width, int(math.ceil(_text_width(font, text))))
        height += line_height + (spacing if index else 0)
        rendered.append((text, font, color, line_height))
    canvas = Image.new(
        "RGBA",
        (max(1, width + _SHADOW_OFFSET + 3), max(1, height + _SHADOW_OFFSET + 2)),
        (0, 0, 0, 0),
    )
    draw = ImageDraw.Draw(canvas)
    y = 0
    for index, (text, font, color, line_height) in enumerate(rendered):
        if index:
            y += spacing
        draw.text(
            (_SHADOW_OFFSET, y + _SHADOW_OFFSET),
            text,
            font=font,
            fill=(*_SHADOW_COLOR, 255),
            anchor="lt",
        )
        draw.text((0, y), text, font=font, fill=color, anchor="lt")
        y += line_height
    _lru_put(_nowplaying_text_cache, key, canvas, _NOWPLAYING_TEXT_LIMIT)
    return canvas


def _cover_token(cover: Any) -> tuple | None:
    """封面缩略指纹：4×4 缩略图 + 原始尺寸（比整图比对便宜得多，可作缓存键）。"""
    if not isinstance(cover, Image.Image):
        return None
    try:
        probe = cover.convert("RGB").resize((4, 4), Image.Resampling.BILINEAR)
        return (cover.size, probe.tobytes())
    except Exception:  # noqa: BLE001 - 无法读取的封面视为没有封面
        return None


def _cover_image(cover: Any, pixels: int) -> Image.Image | None:
    """封面画成圆角方块：中心裁正方形 + LANCZOS 缩放 + 圆角遮罩，按指纹缓存。"""
    if not isinstance(cover, Image.Image):
        return None
    pixels = max(4, int(pixels))
    key = (_cover_token(cover), pixels)
    if key[0] is None:
        return None
    cached = _lru_touch(_cover_cache, key)
    if cached is not None:
        return cached
    try:
        rgb = cover.convert("RGB")
    except Exception:  # noqa: BLE001 - 已损坏的封面按没有封面处理
        return None
    width, height = rgb.size
    side = min(width, height)
    if side <= 0:
        return None
    left = (width - side) // 2
    top = (height - side) // 2
    square = rgb.crop((left, top, left + side, top + side))
    image = square.resize((pixels, pixels), Image.Resampling.LANCZOS).convert("RGBA")
    mask = Image.new("L", (pixels, pixels), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, pixels - 1, pixels - 1),
        radius=max(2, int(round(pixels * 0.12))),
        fill=255,
    )
    image.putalpha(mask)
    _lru_put(_cover_cache, key, image, _COVER_CACHE_LIMIT)
    return image


def _nowplaying_scroll_offset(layer: dict, t: float, natural_width: float) -> float:
    """滚动相位：``t * speed`` 对 ``文本宽度 + 间隙`` 取模（末尾留空隙后再循环）。"""
    speed = max(5.0, min(400.0, _clamped(layer.get("speed", 40.0), 5.0, 400.0, 40.0)))
    gap = max(8.0, float(SCREEN_W) * 0.05)
    period = max(1.0, float(natural_width) + gap)
    return (max(0.0, float(t)) * speed) % period


def _nowplaying_text_window(image: Image.Image, offset: float, width: int) -> Image.Image:
    """从滚动文本条取 ``width`` 宽窗口：三份副本按周期拼接，实现无缝循环。"""
    width = max(1, int(width))
    height = max(1, int(image.height))
    window = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gap = max(8.0, float(SCREEN_W) * 0.05)
    period = float(image.width) + gap
    for step in (0, 1, 2):
        x = int(round(-float(offset) + step * period))
        if x > width or x + image.width < 0:
            continue
        window.paste(image, (x, 0), image)
    return window


def _draw_nowplaying_layer(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    layer: dict,
    ctx: dict,
    t: float,
) -> None:
    """正在播放：可选圆角封面 + 标题/作者文本；``scroll=True`` 时文本横向滚动。"""
    snapshot = _media_snapshot(ctx)
    lines = _nowplaying_lines(layer, snapshot)
    placeholder = str(layer.get("placeholder") or "未在播放")
    weak = _rgb(layer.get("label_color", "#94A3B8"))
    placeholder_font = load_font(_layer_font_size(layer, "size", 26))
    center_x = float(layer.get("x", 0.5)) * SCREEN_W
    center_y = float(layer.get("y", 0.5)) * SCREEN_H
    factor_x, factor_y = _anchor_factors(layer.get("anchor", "center"))
    if not lines:
        _paint_text_block(
            draw,
            [(placeholder, placeholder_font, weak)],
            center_x,
            center_y,
            layer.get("anchor", "center"),
            align="center",
            shadow=False,
        )
        return
    text_image = _nowplaying_text_image(layer, lines)
    natural_width = text_image.width
    cover_pixels = 0
    if layer.get("show_cover", True):
        cover_pixels = int(
            round(_clamped(layer.get("cover_size", 0.22), 0.0, 1.0, 0.22) * SCREEN_W)
        )
    cover_image = _cover_image(snapshot.get("cover"), cover_pixels) if cover_pixels > 0 else None
    if cover_image is None:
        cover_pixels = 0
    gap = 14.0 if cover_pixels else 0.0
    margin = 16.0
    allowed = max(40.0, float(SCREEN_W) - 2.0 * margin - cover_pixels - gap)
    display_width = int(min(float(natural_width), allowed))
    if bool(layer.get("scroll", False)) and natural_width > allowed:
        offset = _nowplaying_scroll_offset(layer, t, natural_width)
        window = _nowplaying_text_window(text_image, offset, display_width)
    else:
        window = text_image.crop((0, 0, min(text_image.width, display_width), text_image.height))
    text_height = window.height
    total_height = max(float(cover_pixels), float(text_height))
    total_width = cover_pixels + gap + display_width
    left = center_x - total_width * factor_x
    top = center_y - total_height * factor_y
    if cover_image is not None:
        canv_left = int(round(left))
        canv_top = int(round(top + (total_height - cover_pixels) / 2.0))
        canvas.paste(cover_image, (canv_left, canv_top), cover_image)
    text_left = int(round(left + cover_pixels + gap))
    text_top = int(round(top + (total_height - text_height) / 2.0))
    canvas.paste(window, (text_left, text_top), window)


def _dispatch_plugin_layer(canvas: Image.Image, layer: dict, ctx: dict) -> None:
    """未知图层类型交给 ``hydrohud.plugins.draw_layer``；缺失/返回假值/异常都静默跳过。"""
    module = _plugins
    if module is None:
        return
    drawer = getattr(module, "draw_layer", None)
    if not callable(drawer):
        return
    staged, _staged_draw = _new_canvas()
    try:
        accepted = drawer(staged, layer, ctx)
    except Exception:  # noqa: BLE001 - 插件异常绝不能影响整帧渲染
        return
    if not accepted:
        return
    canvas.paste(staged, (0, 0), staged)


def _new_canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    canvas = Image.new("RGBA", (SCREEN_W, SCREEN_H), (0, 0, 0, 0))
    return canvas, ImageDraw.Draw(canvas)


def _render_layer(layer: dict, t: float, ctx: dict) -> Image.Image:
    """把一个图层画到透明画布上（返回 RGBA 全屏图像）。"""
    canvas, draw = _new_canvas()
    ctx["t"] = float(t)
    kind = layer.get("type")
    if kind == "text":
        _draw_text_layer(draw, layer, ctx)
    elif kind == "clock":
        _draw_clock_layer(draw, layer, _dt.datetime.now())
    elif kind == "sysinfo":
        _draw_sysinfo_layer(draw, layer, ctx)
    elif kind == "bars":
        _draw_bars_layer(draw, layer, ctx)
    elif kind == "chart":
        _draw_chart_layer(draw, layer, ctx)
    elif kind == "gauge":
        _draw_gauge_layer(canvas, draw, layer, ctx)
    elif kind == "image":
        _draw_image_layer(canvas, layer, draw)
    elif kind == "thermometer":
        _draw_thermometer_layer(draw, layer, ctx, t)
    elif kind == "countdown":
        _draw_countdown_layer(draw, layer, ctx, t)
    elif kind == "ticker":
        _draw_ticker_layer(draw, layer, t, ctx)
    elif kind == "sequence":
        _draw_sequence_layer(canvas, draw, layer, t)
    elif kind == "qrcode":
        _draw_qrcode_layer(canvas, draw, layer, ctx)
    elif kind == "weather":
        _draw_weather_layer(draw, layer, ctx, t)
    elif kind == "nowplaying":
        _draw_nowplaying_layer(canvas, draw, layer, ctx, t)
    else:
        _dispatch_plugin_layer(canvas, layer, ctx)
    return canvas


def _sysinfo_display_text(layer: dict, ctx: dict) -> str:
    """sysinfo 图层的展示文本（缓存键与绘制共用，保证两者一致）。"""
    fields = layer.get("fields") or ["cpu", "ram"]
    separator = layer.get("separator", "    ")
    return separator.join(_format_metric(field, _metric_value(field, ctx)) for field in fields)


def _layer_signature(layer: dict) -> str:
    try:
        return json.dumps(layer, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(layer)


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _chart_cache_key(layer: dict, signature: str, ctx: dict) -> tuple:
    """chart 的缓存键：签名 + 当前值 + 历史长度/末值 + 解析后的上下限。"""
    metric = str(layer.get("metric") or "cpu")
    history = _metric_history(metric, ctx)
    low = _layer_bound(layer, "min")
    high = _layer_bound(layer, "max")
    return (
        signature,
        _round_or_none(_metric_value(metric, ctx)),
        len(history),
        _round_or_none(history[-1]) if history else None,
        low,
        round(high if high is not None else _metric_max(metric), 3),
    )


def _layer_overlay(layer: dict, signature: str, t: float, ctx: dict) -> Image.Image:
    """取（并缓存）图层渲染结果，动态图层按内容字符串做缓存键。"""
    kind = layer.get("type")
    if kind == "text" and _text_needs_refresh(layer):
        resolved = _resolve_template(str(layer.get("text") or ""), ctx)
        key = (signature, resolved)
        cached = _lru_touch(_layer_cache, key)
        if cached is not None:
            return cached
        # 缓存键必须对应真正绘制的字符串：把替换后的文本先塞进临时图层。
        rendered = dict(layer)
        rendered["text"] = resolved
        overlay = _render_layer(rendered, t, ctx)
    elif kind == "clock":
        now = _dt.datetime.now()
        main_text = _safe_strftime(now, layer.get("format", "%H:%M"))
        date_text = (
            _safe_strftime(now, layer.get("date_format", "%Y-%m-%d"))
            if layer.get("show_date")
            else ""
        )
        key = (signature, main_text, date_text)
        cached = _lru_touch(_layer_cache, key)
        if cached is not None:
            return cached
        overlay = _render_layer(layer, t, ctx)
    elif kind == "sysinfo":
        key = (signature, _sysinfo_display_text(layer, ctx))
        cached = _lru_touch(_layer_cache, key)
        if cached is not None:
            return cached
        overlay = _render_layer(layer, t, ctx)
    elif kind == "bars":
        key = (signature, _bars_cache_key(layer, ctx))
        cached = _lru_touch(_layer_cache, key)
        if cached is not None:
            return cached
        overlay = _render_layer(layer, t, ctx)
    elif kind == "chart":
        key = _chart_cache_key(layer, signature, ctx)
        cached = _lru_touch(_layer_cache, key)
        if cached is not None:
            return cached
        overlay = _render_layer(layer, t, ctx)
    elif kind == "gauge":
        key = (
            signature,
            _round_or_none(_metric_value(str(layer.get("metric") or "cpu"), ctx)),
            _layer_bound(layer, "min"),
            _layer_bound(layer, "max"),
        )
        cached = _lru_touch(_layer_cache, key)
        if cached is not None:
            return cached
        overlay = _render_layer(layer, t, ctx)
    else:
        key = (signature,)
        cached = _lru_touch(_layer_cache, key)
        if cached is not None:
            return cached
        overlay = _render_layer(layer, t, ctx)
        # 空图层（没有可见内容）不占缓存也不贴图
        if overlay.getbbox() is None:
            _lru_put(_layer_cache, key, overlay, _LAYER_CACHE_LIMIT)
            return overlay
    _lru_put(_layer_cache, key, overlay, _LAYER_CACHE_LIMIT)
    return overlay


def _bars_cache_key(layer: dict, ctx: dict) -> tuple:
    """bars 的缓存键：每项的 (显示文本, 进度)。"""
    items = layer.get("items") or ["cpu", "ram"]
    parts: list[tuple[str, float]] = []
    for item in items:
        value = _metric_value(item, ctx)
        maximum = _metric_max(item)
        ratio = 0.0 if value is None else max(0.0, min(1.0, value / maximum))
        parts.append((_format_value(item, value), round(ratio, 3)))
    return tuple(parts)


# ------------------------------------------------------------------ 场景条目
def _scene_payload(scene: Any) -> Any:
    if not isinstance(scene, dict):
        return scene
    payload = dict(scene)
    # 忽略 GUI 可能塞进来的非数据字段（用于选择状态等）
    for key in ("version", "_ui", "_selected", "path"):
        payload.pop(key, None)
    return payload


def _scene_key(scene: Any) -> str:
    try:
        return json.dumps(_scene_payload(scene), sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return f"id:{id(scene)}"


def _flatten_static(layers: list[dict]) -> tuple[Image.Image, Image.Image] | None:
    """把连续的前置静态图层合并成一张覆盖层 ``(rgb, mask)``。"""
    if not layers:
        return None
    canvas, _ = _new_canvas()
    empty = True
    ctx: dict = {"values": None, "metrics": None, "history": None}
    for layer in layers:
        overlay = _layer_overlay(layer, _layer_signature(layer), 0.0, ctx)
        if overlay.getbbox() is None:
            continue
        canvas.paste(overlay, (0, 0), overlay)
        empty = False
    if empty:
        return None
    return canvas.convert("RGB"), canvas.getchannel("A")


def _build_entry(normalized: dict) -> dict:
    """构建场景渲染条目（静态部分一次性合成，动态图层留给逐帧绘制）。"""
    background = normalized["background"]
    layers = normalized["layers"]
    dynamic_background = background.get("type") in ("gif", "video", "capture")

    first_dynamic = len(layers)
    for index, layer in enumerate(layers):
        if _is_dynamic_layer(layer):
            first_dynamic = index
            break
    dynamic = dynamic_background or first_dynamic < len(layers)

    prefix = _flatten_static(layers[:first_dynamic])
    suffix = [(layer, _layer_signature(layer)) for layer in layers[first_dynamic:]]
    tint_alpha = normalized["tint"]["alpha"]
    tint = _tint_effect(normalized["tint"]["color"], tint_alpha)
    entry = {
        "background": background,
        "tint": tint,
        "prefix": prefix,
        "suffix": suffix,
        "dynamic": dynamic,
        "image": None,
    }
    if not dynamic:
        image = _background_image(background, 0.0)
        if tint is not None:
            image.paste(tint[0], (0, 0), tint[1])
        if prefix is not None:
            image.paste(prefix[0], (0, 0), prefix[1])
        entry["image"] = image
    return entry


# ------------------------------------------------------------------ 公开 API
def render_scene(scene: dict, t: float) -> Image.Image:
    """把场景渲染成 480×480 RGB 图像。

    参数 ``scene`` 是 scene schema v1 字典（残缺/多余字段都容忍，内部会先
    ``normalize_scene``）；``t`` 是秒级时间轴，GIF/视频背景据此循环取帧。
    返回值是一张新的 RGB ``PIL.Image.Image``，调用方可以自由修改。
    """
    try:
        time_value = float(t)
    except (TypeError, ValueError):
        time_value = 0.0
    key = _scene_key(scene)
    with _render_lock:
        entry = _lru_touch(_scene_cache, key)
        if entry is None:
            entry = _build_entry(normalize_scene(scene))
            _lru_put(_scene_cache, key, entry, 24)
        if not entry["dynamic"]:
            return entry["image"].copy()
        image = _background_image(entry["background"], time_value)
        tint = entry["tint"]
        if tint is not None:
            image.paste(tint[0], (0, 0), tint[1])
        prefix = entry["prefix"]
        if prefix is not None:
            image.paste(prefix[0], (0, 0), prefix[1])
        ctx: dict = {"values": None, "metrics": None, "history": None}
        for layer, signature in entry["suffix"]:
            overlay = _layer_overlay(layer, signature, time_value, ctx)
            if overlay.getbbox() is not None:
                image.paste(overlay, (0, 0), overlay)
        return image


def clear_cache() -> None:
    """清空图片 / GIF / 视频解码缓存与场景缓存。

    字体缓存保留（重新加载字体既慢又常用）。视频读取器会被关闭。
    ``SensorHub`` 是进程级单例（由 app 管理），这里只丢渲染缓存、不关闭它。
    """
    with _render_lock, _video_lock:
        for entry in list(_video_cache.values()):
            _close_video(entry)
        _video_cache.clear()
        _video_frames.clear()
        _scene_cache.clear()
        _solid_cache.clear()
        _source_cache.clear()
        _bg_cache.clear()
        _gif_cache.clear()
        _layer_cache.clear()
        _layer_image_cache.clear()
        _placeholder_cache.clear()
        _tint_cache.clear()
        _qr_matrix_cache.clear()
        _qr_image_cache.clear()
        _sequence_cache.clear()
        _cover_cache.clear()
        _nowplaying_text_cache.clear()
        _capture_cache.clear()


# 解释器退出时关闭 ffmpeg 子进程，避免残留进程与 "kill ffmpeg" 告警
atexit.register(clear_cache)
