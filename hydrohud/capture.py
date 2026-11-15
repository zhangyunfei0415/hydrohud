"""屏幕捕获数据源：虚拟桌面坐标换算、区域截图与节流缓存。

渲染层用本模块把"桌面某块区域"画到 480x480 的屏上：

- :func:`virtual_bounds` 取 Windows 虚拟桌面矩形（多显示器时原点可能为负）；
- :func:`grab` 按 0-1 归一化区域截图，中心裁正方形后缩到 ``size``；
- :func:`grab_cached` 在 ``fps`` 限制内复用上一次的图，避免每帧都截屏；
- :func:`available` 判断当前环境能否截屏（非 Windows 且无 DISPLAY 即为 headless）。

截屏失败不会抛给渲染线程：记录到 :func:`last_error` 并返回一张黑色占位图。
导入本模块不加载 ``user32``、也不 import ``PIL.ImageGrab``：两者都在第一次调用时
才懒加载，所以无显示环境里 import 依然安全。
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
import threading
import time
from collections import OrderedDict
from typing import Any, Sequence

from PIL import Image

__all__ = [
    "CACHE_SIZE",
    "FALLBACK_BOUNDS",
    "MAX_MONITOR",
    "SM_CXVIRTUALSCREEN",
    "SM_CYVIRTUALSCREEN",
    "SM_XVIRTUALSCREEN",
    "SM_YVIRTUALSCREEN",
    "available",
    "grab",
    "grab_cached",
    "last_error",
    "reset_cache",
    "virtual_bounds",
]

#: ``GetSystemMetrics`` 的索引：虚拟桌面左上角与尺寸。
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

#: 取不到虚拟桌面时的回落矩形（与常见 720p 屏一致）。
FALLBACK_BOUNDS = (0, 0, 1280, 720)

#: ``monitor`` 允许的最大值（当前只实现 0 = 整个虚拟桌面，其余预留）。
MAX_MONITOR = 8

#: 节流缓存最多保留的图数（LRU）。
CACHE_SIZE = 4

#: 输出图片的最小边长（像素）。
MIN_SIZE = 8

#: 输出图片的最大边长（像素）。
MAX_SIZE = 2048

#: 占位图颜色（纯黑，避免屏上闪白）。
FALLBACK_COLOR = (0, 0, 0)

_LAST_ERROR: str | None = None
_last_error_lock = threading.Lock()

#: 缓存的 ``user32`` 句柄；``None`` 表示尚未加载。测试可直接替换。
user32: Any = None

#: 缓存的 ``PIL.ImageGrab`` 模块；``None`` 表示尚未加载。测试可直接替换。
ImageGrab: Any = None

_cache_lock = threading.Lock()

#: 键为 ``(slot, region, monitor, size)``，值为已缩放的 PIL 图。
_cache: OrderedDict[tuple, Image.Image] = OrderedDict()


def last_error() -> str | None:
    """返回最近一次内部错误的中文描述；没有任何错误时返回 ``None``。"""
    with _last_error_lock:
        return _LAST_ERROR


def _record_error(context: str, exc: BaseException | None = None) -> str:
    """记录内部错误并返回描述文本（本模块永不向外抛异常）。"""
    global _LAST_ERROR
    text = context if exc is None else f"{context}: {exc}"
    with _last_error_lock:
        _LAST_ERROR = text
    return text


def _user32() -> Any:
    """懒加载 ``user32``（第一次调用才 ``WinDLL``）。"""
    global user32
    if user32 is None:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
    return user32


def _imagegrab() -> Any:
    """懒加载 ``PIL.ImageGrab``（第一次调用才 import，导入期不碰 DLL）。"""
    global ImageGrab
    if ImageGrab is None:
        from PIL import ImageGrab as module

        ImageGrab = module
    return ImageGrab


def virtual_bounds() -> tuple[int, int, int, int]:
    """返回虚拟桌面矩形 ``(x, y, width, height)``；取不到时返回 :data:`FALLBACK_BOUNDS`。

    多显示器并且副屏在主屏左侧/上方时 ``x`` / ``y`` 为负。
    """
    try:
        dll = _user32()
        metrics = dll.GetSystemMetrics
    except Exception as exc:
        _record_error("加载 user32 失败", exc)
        return FALLBACK_BOUNDS
    try:
        values = tuple(
            int(metrics(index))
            for index in (
                SM_XVIRTUALSCREEN,
                SM_YVIRTUALSCREEN,
                SM_CXVIRTUALSCREEN,
                SM_CYVIRTUALSCREEN,
            )
        )
    except Exception as exc:
        _record_error("读取虚拟桌面尺寸失败", exc)
        return FALLBACK_BOUNDS
    x, y, width, height = values
    if width <= 0 or height <= 0:
        _record_error("虚拟桌面尺寸非法")
        return FALLBACK_BOUNDS
    return (x, y, width, height)


def _clean_region(region: Sequence[float] | None) -> tuple[float, float, float, float]:
    """把归一化区域夹到 ``0..1``，并保证宽高大于 0；非法值抛 :class:`ValueError`。"""
    if region is None:
        return (0.0, 0.0, 1.0, 1.0)
    try:
        values = tuple(float(value) for value in region)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"region 必须是 4 个数字，收到 {region!r}") from exc
    if len(values) != 4:
        raise ValueError(f"region 必须是 (x, y, w, h)，收到 {region!r}")
    for value in values:
        if value != value:  # NaN
            raise ValueError(f"region 不能含 NaN，收到 {region!r}")
    x, y, width, height = (max(0.0, min(1.0, value)) for value in values)
    if width <= 0.0 or height <= 0.0:
        raise ValueError(f"region 的宽高必须大于 0，收到 {region!r}")
    if x + width > 1.0:
        width = 1.0 - x
    if y + height > 1.0:
        height = 1.0 - y
    return (x, y, width, height)


def _clean_size(size: int) -> int:
    """把输出边长夹到 ``MIN_SIZE..MAX_SIZE``。"""
    try:
        value = int(size)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"size 必须是整数，收到 {size!r}") from exc
    return max(MIN_SIZE, min(MAX_SIZE, value))


def _clean_monitor(monitor: int) -> int:
    """校验 ``monitor`` 索引（``0`` 表示整个虚拟桌面）。"""
    try:
        value = int(monitor)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"monitor 必须是整数，收到 {monitor!r}") from exc
    if not 0 <= value <= MAX_MONITOR:
        raise ValueError(f"monitor 超出范围 0..{MAX_MONITOR}，收到 {monitor!r}")
    return value


def _absolute_bbox(region: Sequence[float] | None, monitor: int = 0) -> tuple[int, int, int, int]:
    """把归一化区域换算成虚拟桌面上的绝对像素矩形。

    ``monitor`` 目前只支持 ``0``（整个虚拟桌面）；其他值与 ``0`` 等价，等后续
    接入逐显示器信息后再切分。
    """
    _clean_monitor(monitor)
    x, y, width, height = _clean_region(region)
    left, top, full_width, full_height = virtual_bounds()
    x0 = left + int(round(x * full_width))
    y0 = top + int(round(y * full_height))
    x1 = left + int(round((x + width) * full_width))
    y1 = top + int(round((y + height) * full_height))
    x1 = max(x0 + 1, x1)
    y1 = max(y0 + 1, y1)
    return (x0, y0, x1, y1)


def _center_square(image: Image.Image) -> Image.Image:
    """从图中心裁出最大的正方形（只裁不缩）。"""
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return image.crop((left, top, left + side, top + side))


def _fit_square(image: Image.Image, size: int) -> Image.Image:
    """中心裁正方形后 LANCZOS 缩放到 ``size`` × ``size``，结果必为 RGB。"""
    square = _center_square(image)
    if square.mode != "RGB":
        square = square.convert("RGB")
    if square.size == (size, size):
        return square
    return square.resize((size, size), Image.LANCZOS)


def _blank(size: int) -> Image.Image:
    """生成一张纯黑占位图（截屏失败时用，保证渲染层永远拿得到图）。"""
    return Image.new("RGB", (size, size), FALLBACK_COLOR)


def available() -> bool:
    """当前环境能否截屏：``PIL.ImageGrab`` 可用且不是 headless Linux。

    只做能力判断，不真的截图。
    """
    try:
        module = _imagegrab()
    except Exception as exc:
        _record_error("加载 ImageGrab 失败", exc)
        return False
    if not hasattr(module, "grab"):
        _record_error("ImageGrab 缺少 grab()")
        return False
    if sys.platform == "win32" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def grab(
    region: Sequence[float] = (0.0, 0.0, 1.0, 1.0), monitor: int = 0, size: int = 480
) -> Image.Image:
    """截取虚拟桌面上的一块区域，返回 ``size`` × ``size`` 的 RGB 图。

    :param region: 相对虚拟桌面的归一化 ``(x, y, w, h)``，取值 ``0..1``。
    :param monitor: 显示器索引；``0`` 表示整个虚拟桌面（当前唯一实现）。
    :param size: 输出边长像素。

    截屏本身失败时记录 :func:`last_error` 并返回黑色占位图，不抛异常。
    """
    side = _clean_size(size)
    bbox = _absolute_bbox(region, monitor)
    try:
        image = _imagegrab().grab(bbox=bbox, all_screens=True)
    except Exception as exc:
        _record_error("截屏失败", exc)
        return _blank(side)
    if image is None:
        _record_error("截屏返回空结果")
        return _blank(side)
    try:
        return _fit_square(image, side)
    except Exception as exc:
        _record_error("缩放截图失败", exc)
        return _blank(side)


def grab_cached(
    region: Sequence[float] = (0.0, 0.0, 1.0, 1.0),
    monitor: int = 0,
    size: int = 480,
    fps: float = 5.0,
    now: float | None = None,
) -> Image.Image:
    """``fps`` 限制内的截屏：同一个时间槽 + 同一个区域直接复用缓存图。

    :param fps: 每秒最多截几次；``<= 0`` 表示不缓存、每次都截。
    :param now: 当前时间（秒）；``None`` 时用 :func:`time.monotonic`。测试可注入。

    槽位是 ``int(now * fps)``，所以 ``fps=5`` 时每 0.2 秒换一个槽。缓存最多
    :data:`CACHE_SIZE` 张，超出按最近最少使用淘汰。
    """
    side = _clean_size(size)
    slot_monitor = _clean_monitor(monitor)
    x, y, width, height = _clean_region(region)
    rate = float(fps)
    if rate <= 0.0:
        return grab((x, y, width, height), slot_monitor, side)
    moment = time_now() if now is None else float(now)
    slot = int(moment * rate)
    key = (slot, round(x, 6), round(y, 6), round(width, 6), round(height, 6), slot_monitor, side)
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached
    image = grab((x, y, width, height), slot_monitor, side)
    with _cache_lock:
        _cache[key] = image
        _cache.move_to_end(key)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return image


def time_now() -> float:
    """返回单调时钟（秒）；独立成函数方便测试替换。"""
    return time.monotonic()


def reset_cache() -> None:
    """清空节流缓存（切换场景/尺寸后调用，避免旧图占内存）。"""
    with _cache_lock:
        _cache.clear()
