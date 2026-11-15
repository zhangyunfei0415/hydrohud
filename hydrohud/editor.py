"""Tk 画布编辑器：在 480×480 逻辑画布上拖拽、缩放场景图层。

几何计算全部做成模块级纯函数（:func:`layer_bounds`、:func:`snap_offset`、
:func:`hit_test`、:func:`drag_selection`、:func:`resize_layer`），
:class:`CanvasEditor` 只负责 Tk 事件与绘制封装，因此没有显示环境也能测核心逻辑。

坐标约定：图层字典里的 ``x`` / ``y`` 是 0..1 的归一化锚点，绘制时乘以
``render.SCREEN_W`` / ``render.SCREEN_H``；``bars`` 的 ``x`` / ``y`` 是矩形左上角
（与 :func:`render.render_scene` 一致），其余图层按 ``anchor`` 换算。所有回传都
先 ``deepcopy`` 场景，再通过宿主传入的 ``apply_scene(scene, label)`` 写回，编辑器
自身不持有场景数据。
"""

from __future__ import annotations

import copy
import datetime as _dt
import logging
import math
import os
import tkinter as tk
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from PIL import Image, ImageDraw, ImageTk

from . import render

logger = logging.getLogger(__name__)

SCREEN_W = render.SCREEN_W
SCREEN_H = render.SCREEN_H

#: 吸附网格：屏幕八等分（60 逻辑像素）。
GRID_STEP = SCREEN_W / 8.0
#: 吸附 / 手柄命中容差（逻辑像素）。
SNAP_TOLERANCE = 6.0
#: 缩放手柄的边长（逻辑像素）。
HANDLE_SIZE = 8.0
#: 图层最小尺寸（逻辑像素）。
MIN_SIZE = 4.0
#: 只支持移动、不支持缩放的图层类型。
MOVE_ONLY_KINDS = frozenset(
    ("text", "clock", "sysinfo", "countdown", "ticker", "weather", "nowplaying")
)
#: 支持缩放的图层类型。
RESIZABLE_KINDS = frozenset(
    ("bars", "chart", "gauge", "image", "qrcode", "sequence", "thermometer")
)

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

_HORIZONTAL_FACTORS = {"left": 0.0, "center": 0.5, "right": 1.0}
_VERTICAL_FACTORS = {"top": 0.0, "center": 0.5, "bottom": 1.0}

_MEASURE_DRAW = ImageDraw.Draw(Image.new("L", (1, 1)))
_IMAGE_SIZE_CACHE: dict[tuple[str, int, int], tuple[int, int]] = {}

_SYSINFO_SAMPLES = {
    "cpu": "CPU 12%",
    "gpu": "GPU 34%",
    "ram": "内存 45%",
    "vram": "显存 52%",
    "cpu_temp": "CPU 45°C",
    "gpu_temp": "GPU 48°C",
    "water_temp": "水温 32°C",
    "pump": "泵速 3200RPM",
    "fan": "风扇 1200RPM",
}
_WEATHER_SAMPLES = {
    "temp": "25°",
    "feels": "27°",
    "humidity": "60%",
    "wind": "东北风 3 级",
    "desc": "晴",
}
_NOWPLAYING_SAMPLES = {"title": "歌名", "artist": "歌手", "album": "专辑"}
_COUNTDOWN_SAMPLES = {"d": "1", "h": "02", "m": "03", "s": "04", "ms": "123"}
_COUNTDOWN_TOKEN_RE = None


def anchor_parts(anchor: Any) -> tuple[str, str]:
    """把 9 种锚点名称拆成 ``(vertical, horizontal)``，非法值按 center 处理。"""
    return _ANCHOR_PARTS.get(str(anchor or ""), ("center", "center"))


def anchor_factors(anchor: Any) -> tuple[float, float]:
    """返回锚点在图层包围盒内的相对位置 ``(fx, fy)``（0=左/上，1=右/下）。"""
    vertical, horizontal = anchor_parts(anchor)
    return _HORIZONTAL_FACTORS[horizontal], _VERTICAL_FACTORS[vertical]


def clamp(value: float, low: float, high: float) -> float:
    """把 ``value`` 夹在 ``[low, high]`` 区间内。"""
    return max(low, min(high, float(value)))


def clamp_norm(value: float) -> float:
    """把归一化坐标夹到 0..1。"""
    return clamp(value, 0.0, 1.0)


def measure_block(lines: Sequence[tuple[str, Any]]) -> tuple[float, float]:
    """按 :func:`render._paint_text_block` 的规则量多行文字块的宽高。"""
    if not lines:
        return 0.0, 0.0
    metrics: list[tuple[int, int]] = []
    for text, font in lines:
        ascent, descent = font.getmetrics()
        height = ascent + descent
        bbox = _MEASURE_DRAW.textbbox((0, 0), text or " ", font=font, anchor="lt")
        metrics.append((height, max(1, bbox[2] - bbox[0])))
    spacing = max(2, int(metrics[0][0] * 0.12))
    total_height = sum(item[0] for item in metrics) + spacing * (len(lines) - 1)
    widest = max(item[1] for item in metrics)
    return float(widest), float(total_height)


def measure_text(text: str, size: float, bold: bool = False) -> tuple[float, float]:
    """量单行或多行文字的包围盒宽高（逻辑像素）。"""
    font = render.load_font(size, bold)
    return measure_block([(line, font) for line in str(text or "").split("\n")])


def _countdown_sample(layer: dict) -> str:
    """把倒计时的 ``format`` 模板替换成固定样例文本，便于量宽高。"""
    pattern = str(layer.get("format") or "{d} 天 {h}:{m}:{s}")
    return (
        pattern.replace("{d}", _COUNTDOWN_SAMPLES["d"])
        .replace("{h}", _COUNTDOWN_SAMPLES["h"])
        .replace("{m}", _COUNTDOWN_SAMPLES["m"])
        .replace("{s}", _COUNTDOWN_SAMPLES["s"])
        .replace("{ms}", _COUNTDOWN_SAMPLES["ms"])
        .replace("{p}", "40")
        .replace("{c}", "40")
        .replace("{t}", "80")
    )


def _join_samples(fields: Iterable[Any], samples: dict[str, str], separator: str) -> str:
    parts: list[str] = []
    for field in fields or ():
        parts.append(samples.get(str(field), str(field)))
    return separator.join(parts)


def text_lines_for(
    layer: dict, sample_time: _dt.datetime | None = None
) -> list[tuple[str, Any]] | None:
    """构造图层文字块的 ``[(文本, 字体)]``；非文字类图层返回 ``None``。

    ``sample_time`` 只影响时钟图层的样例文本，测试可传入固定时间来得到稳定宽高。
    """
    kind = layer.get("type")
    if kind == "text":
        size = layer.get("size", 56)
        bold = bool(layer.get("bold", False))
        font = render.load_font(size, bold)
        return [(line, font) for line in str(layer.get("text") or "").split("\n")]
    if kind == "clock":
        now = sample_time or _dt.datetime.now()
        size = layer.get("size", 140)
        bold = bool(layer.get("bold", True))
        fmt = str(layer.get("format") or "%H:%M")
        try:
            main = now.strftime(fmt)
        except (ValueError, TypeError):
            main = "00:00"
        lines: list[tuple[str, Any]] = [(main, render.load_font(size, bold))]
        if layer.get("show_date"):
            date_fmt = str(layer.get("date_format") or "%Y-%m-%d")
            try:
                date_text = now.strftime(date_fmt)
            except (ValueError, TypeError):
                date_text = "2026-01-01"
            date_size = layer.get("date_size") or max(10, int(float(size) * 0.26))
            lines.append((date_text, render.load_font(date_size)))
        return lines
    if kind == "sysinfo":
        fields = layer.get("fields") or ["cpu", "ram"]
        separator = str(layer.get("separator") or "    ")
        text = _join_samples(fields, _SYSINFO_SAMPLES, separator)
        font = render.load_font(layer.get("size", 30), bool(layer.get("bold", False)))
        return [(text, font)]
    if kind == "countdown":
        size = layer.get("size", 44)
        lines = []
        title = str(layer.get("title") or "")
        if title:
            title_font = render.load_font(layer.get("title_size", 22))
            lines.append((title, title_font))
        lines.append((_countdown_sample(layer), render.load_font(size)))
        return lines
    if kind == "ticker":
        text = str(layer.get("text") or "")
        font = render.load_font(layer.get("size", 34), bool(layer.get("bold", False)))
        return [(text, font)]
    if kind == "weather":
        fields = layer.get("fields") or ["temp", "desc"]
        separator = str(layer.get("separator") or "  ")
        text = _join_samples(fields, _WEATHER_SAMPLES, separator)
        font = render.load_font(layer.get("size", 28))
        return [(text, font)]
    if kind == "nowplaying":
        fields = layer.get("fields") or ["title", "artist"]
        separator = str(layer.get("separator") or " · ")
        text = _join_samples(fields, _NOWPLAYING_SAMPLES, separator)
        font = render.load_font(layer.get("size", 26))
        return [(text, font)]
    return None


def image_layer_size(layer: dict) -> tuple[float, float]:
    """返回 image 图层缩放后的显示尺寸（逻辑像素）；文件缺失时按占位框 150×80。"""
    path = str(layer.get("path") or "")
    scale = float(layer.get("scale") or 0.4)
    target_w = max(1.0, float(round(scale * SCREEN_W)))
    if not path:
        return 150.0, 80.0
    source = _source_size(path)
    if source is None:
        return 150.0, 80.0
    width, height = source
    target_h = max(1.0, float(round(height * (target_w / width))) if width else target_w)
    return target_w, target_h


def _source_size(path: str) -> tuple[int, int] | None:
    """读取图片尺寸并缓存（按绝对路径 + 修改时间失效）。"""
    try:
        absolute = os.path.abspath(path)
        stat = os.stat(absolute)
        key = (absolute, int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return None
    cached = _IMAGE_SIZE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        with Image.open(absolute) as image:
            size = (int(image.width), int(image.height))
    except (OSError, ValueError):
        return None
    if size[0] <= 0 or size[1] <= 0:
        return None
    _IMAGE_SIZE_CACHE[key] = size
    if len(_IMAGE_SIZE_CACHE) > 64:
        _IMAGE_SIZE_CACHE.clear()
    return size


def layer_extent(
    layer: dict, sample_time: _dt.datetime | None = None
) -> tuple[float, float] | None:
    """返回图层的显示宽高（逻辑像素），不换算位置；未知类型返回 ``None``。"""
    kind = layer.get("type")
    if kind == "bars":
        return (
            max(MIN_SIZE, float(layer.get("w", 0.84) or 0.84) * SCREEN_W),
            max(MIN_SIZE, float(layer.get("h", 0.10) or 0.10) * SCREEN_H),
        )
    if kind == "chart":
        width = max(8.0, min(float(SCREEN_W), float(layer.get("w", 0.8) or 0.8) * SCREEN_W))
        height = max(8.0, min(float(SCREEN_H), float(layer.get("h", 0.22) or 0.22) * SCREEN_H))
        return width, height
    if kind == "gauge":
        diameter = max(MIN_SIZE, min(720.0, float(layer.get("diameter", 0.5) or 0.5) * SCREEN_W))
        return diameter, diameter
    if kind == "image":
        return image_layer_size(layer)
    if kind in ("thermometer", "sequence"):
        return (
            max(MIN_SIZE, float(layer.get("w", 0.5) or 0.5) * SCREEN_W),
            max(MIN_SIZE, float(layer.get("h", 0.5) or 0.5) * SCREEN_H),
        )
    if kind == "qrcode":
        side = max(MIN_SIZE, float(layer.get("size", 0.5) or 0.5) * SCREEN_W)
        return side, side
    lines = text_lines_for(layer, sample_time)
    if lines is None:
        return None
    width, height = measure_block(lines)
    if kind == "nowplaying" and layer.get("show_cover", True):
        cover = float(layer.get("cover_size", 0.22) or 0.22) * SCREEN_W
        width, height = width + cover + 8.0, max(height, cover)
    return width, height


def layer_bounds(
    layer: dict, sample_time: _dt.datetime | None = None
) -> tuple[float, float, float, float] | None:
    """返回图层的逻辑包围盒 ``(x0, y0, x1, y1)``；无法绘制时返回 ``None``。

    几何规则与 :mod:`hydrohud.render` 的对应绘制函数保持一致：``bars`` 的
    ``x`` / ``y`` 是左上角，``chart`` 会按锚点换算并夹在屏幕内，``gauge``
    的 ``x`` / ``y`` 是圆弧锚点，其余（含文字类）按锚点居中。
    """
    kind = layer.get("type")
    if kind == "gauge":
        extent = layer_extent(layer, sample_time)
        if extent is None:
            return None
        radius = extent[0] / 2.0
        center_x = float(layer.get("x", 0.5) or 0.5) * SCREEN_W
        center_y = float(layer.get("y", 0.5) or 0.5) * SCREEN_H
        _vertical, horizontal = anchor_parts(layer.get("anchor"))
        factor_x, factor_y = anchor_factors(layer.get("anchor"))
        center_x += radius if factor_x == 0.0 else (-radius if factor_x == 1.0 else 0.0)
        center_y += radius if factor_y == 0.0 else (-radius if factor_y == 1.0 else 0.0)
        return (
            center_x - radius,
            center_y - radius,
            center_x + radius,
            center_y + radius,
        )
    extent = layer_extent(layer, sample_time)
    if extent is None:
        return None
    width, height = extent
    if kind == "bars":
        left = float(layer.get("x", 0.5) or 0.5) * SCREEN_W
        top = float(layer.get("y", 0.5) or 0.5) * SCREEN_H
        return (left, top, left + width, top + height)
    factor_x, factor_y = anchor_factors(layer.get("anchor"))
    left = float(layer.get("x", 0.5) or 0.5) * SCREEN_W - width * factor_x
    top = float(layer.get("y", 0.5) or 0.5) * SCREEN_H - height * factor_y
    if kind == "chart":
        left = clamp(left, 0.0, SCREEN_W - width)
        top = clamp(top, 0.0, SCREEN_H - height)
    return (left, top, left + width, top + height)


def hit_test(
    layers: Sequence[dict], x: float, y: float, sample_time: _dt.datetime | None = None
) -> int | None:
    """返回包含逻辑点 ``(x, y)`` 的最上层图层下标；没有命中时返回 ``None``。"""
    for index in range(len(layers) - 1, -1, -1):
        bounds = layer_bounds(layers[index], sample_time)
        if bounds is None:
            continue
        if bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]:
            return index
    return None


def selection_bounds(
    layers: Sequence[dict], indices: Iterable[int], sample_time: _dt.datetime | None = None
) -> tuple[float, float, float, float] | None:
    """返回若干选中图层包围盒的并集；全部不可绘制时返回 ``None``。"""
    boxes: list[tuple[float, float, float, float]] = []
    for index in indices:
        if 0 <= index < len(layers):
            bounds = layer_bounds(layers[index], sample_time)
            if bounds is not None:
                boxes.append(bounds)
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def snap_targets(
    layers: Sequence[dict], exclude: Iterable[int] = ()
) -> tuple[list[float], list[float]]:
    """收集吸附参考线：网格、屏幕中心线、屏幕边缘以及其它图层边缘/中心。"""
    skip = set(exclude)
    xs: list[float] = [0.0, SCREEN_W / 2.0, float(SCREEN_W)]
    ys: list[float] = [0.0, SCREEN_H / 2.0, float(SCREEN_H)]
    for start in range(1, 8):
        xs.append(start * GRID_STEP)
        ys.append(start * GRID_STEP)
    for index, layer in enumerate(layers):
        if index in skip:
            continue
        bounds = layer_bounds(layer)
        if bounds is None:
            continue
        xs.extend((bounds[0], (bounds[0] + bounds[2]) / 2.0, bounds[2]))
        ys.extend((bounds[1], (bounds[1] + bounds[3]) / 2.0, bounds[3]))
    return xs, ys


def snap_offset(
    values: Sequence[float], targets: Sequence[float], tolerance: float = SNAP_TOLERANCE
) -> tuple[float, float] | None:
    """在 ``tolerance`` 内找最接近的吸附位移，返回 ``(位移, 参考线坐标)``。"""
    best: tuple[float, float] | None = None
    for value in values:
        for target in targets:
            delta = float(target) - float(value)
            if abs(delta) > tolerance:
                continue
            if best is None or abs(delta) < abs(best[0]):
                best = (delta, float(target))
    return best


def handle_points(bounds: tuple[float, float, float, float]) -> dict[str, tuple[float, float]]:
    """返回 8 个缩放手柄的逻辑坐标（四角 + 四边中点）。"""
    left, top, right, bottom = bounds
    center_x = (left + right) / 2.0
    center_y = (top + bottom) / 2.0
    return {
        "nw": (left, top),
        "n": (center_x, top),
        "ne": (right, top),
        "e": (right, center_y),
        "se": (right, bottom),
        "s": (center_x, bottom),
        "sw": (left, bottom),
        "w": (left, center_y),
    }


def hit_handle(
    bounds: tuple[float, float, float, float], x: float, y: float, size: float = HANDLE_SIZE
) -> str | None:
    """返回命中的缩放手柄名（容差 ``size`` 逻辑像素）；未命中返回 ``None``。"""
    for name, (handle_x, handle_y) in handle_points(bounds).items():
        if abs(x - handle_x) <= size and abs(y - handle_y) <= size:
            return name
    return None


def can_resize(layer: dict) -> bool:
    """图层是否支持缩放手柄。"""
    return layer.get("type") in RESIZABLE_KINDS


def drag_selection(
    scene: dict,
    indices: Iterable[int],
    dx_px: float,
    dy_px: float,
    *,
    snap: bool = True,
    axis_lock: bool = False,
) -> tuple[dict, list[float], list[float]]:
    """整体平移选中的图层并回写 ``x`` / ``y``（归一化 0..1）。

    返回 ``(新场景, 竖直吸附线, 水平吸附线)``；``axis_lock`` 时只沿位移
    较大的轴移动。
    """
    result = copy.deepcopy(scene)
    layers = result.get("layers") if isinstance(result.get("layers"), list) else []
    valid = [index for index in indices if 0 <= index < len(layers)]
    if not valid:
        return result, [], []
    group = selection_bounds(layers, valid)
    if group is None:
        return result, [], []
    dx = float(dx_px)
    dy = float(dy_px)
    if axis_lock:
        if abs(dx) >= abs(dy):
            dy = 0.0
        else:
            dx = 0.0
    guides_x: list[float] = []
    guides_y: list[float] = []
    if snap:
        target_xs, target_ys = snap_targets(layers, valid)
        offset_x = snap_offset(
            [group[0] + dx, (group[0] + group[2]) / 2.0 + dx, group[2] + dx], target_xs
        )
        if offset_x is not None:
            dx += offset_x[0]
            guides_x.append(offset_x[1])
        offset_y = snap_offset(
            [group[1] + dy, (group[1] + group[3]) / 2.0 + dy, group[3] + dy], target_ys
        )
        if offset_y is not None:
            dy += offset_y[0]
            guides_y.append(offset_y[1])
    if group[0] + dx < 0.0:
        dx -= group[0] + dx
    elif group[2] + dx > SCREEN_W:
        dx -= group[2] + dx - SCREEN_W
    if group[1] + dy < 0.0:
        dy -= group[1] + dy
    elif group[3] + dy > SCREEN_H:
        dy -= group[3] + dy - SCREEN_H
    for index in valid:
        layer = layers[index]
        if not isinstance(layer, dict):
            continue
        layer["x"] = clamp_norm(float(layer.get("x", 0.5) or 0.5) + dx / SCREEN_W)
        layer["y"] = clamp_norm(float(layer.get("y", 0.5) or 0.5) + dy / SCREEN_H)
    return result, guides_x, guides_y


def _rect_from_handle(
    bounds: tuple[float, float, float, float], handle: str, x: float, y: float
) -> tuple[float, float, float, float]:
    """按手柄把矩形对应边拖到 ``(x, y)``，返回新矩形（保证最小尺寸）。"""
    left, top, right, bottom = bounds
    if "w" in handle:
        left = min(float(x), right - MIN_SIZE)
    if "e" in handle:
        right = max(float(x), left + MIN_SIZE)
    if "n" in handle:
        top = min(float(y), bottom - MIN_SIZE)
    if "s" in handle:
        bottom = max(float(y), top + MIN_SIZE)
    return left, top, right, bottom


def _anchor_xy(
    kind: str, left: float, top: float, width: float, height: float, anchor: Any, layer: dict
) -> tuple[float, float]:
    """把逻辑矩形换算回图层的归一化 ``x`` / ``y``。"""
    if kind == "bars":
        return left / SCREEN_W, top / SCREEN_H
    factor_x, factor_y = anchor_factors(anchor)
    return (left + width * factor_x) / SCREEN_W, (top + height * factor_y) / SCREEN_H


def resize_layer(layer: dict, handle: str, pointer_x: float, pointer_y: float) -> dict:
    """按手柄与指针逻辑坐标缩放图层，返回修改后的副本（不修改原 dict）。"""
    result = copy.deepcopy(layer)
    kind = result.get("type")
    if kind not in RESIZABLE_KINDS:
        return result
    bounds = layer_bounds(result)
    if bounds is None:
        return result
    if kind == "gauge":
        left, top, right, bottom = bounds
        center_x, center_y = (left + right) / 2.0, (top + bottom) / 2.0
        radius = math.hypot(float(pointer_x) - center_x, float(pointer_y) - center_y)
        diameter = clamp(max(MIN_SIZE, 2.0 * radius), MIN_SIZE, 720.0) / SCREEN_W
        result["diameter"] = clamp(diameter, 0.05, 1.5)
        return result
    left, top, right, bottom = _rect_from_handle(bounds, handle, pointer_x, pointer_y)
    width, height = right - left, bottom - top
    anchor = result.get("anchor")
    if kind == "qrcode":
        side = max(width, height, MIN_SIZE)
        center_x, center_y = (left + right) / 2.0, (top + bottom) / 2.0
        result["size"] = clamp(side / SCREEN_W, 0.05, 1.0)
        result["x"] = clamp_norm(center_x / SCREEN_W)
        result["y"] = clamp_norm(center_y / SCREEN_H)
        return result
    if kind == "image":
        aspect = _source_size(str(result.get("path") or ""))
        scale_width = width / SCREEN_W
        if aspect is not None and handle in ("n", "s"):
            scale_width = height * (aspect[0] / max(1, aspect[1])) / SCREEN_W
        result["scale"] = clamp(scale_width, 0.01, 10.0)
        return result
    result["w"] = clamp(width / SCREEN_W, 0.01, 1.0)
    result["h"] = clamp(height / SCREEN_H, 0.01, 1.0)
    x, y = _anchor_xy(kind, left, top, width, height, anchor, result)
    result["x"] = clamp_norm(x)
    result["y"] = clamp_norm(y)
    return result


def nudge_selection(scene: dict, indices: Iterable[int], dx_px: float, dy_px: float) -> dict:
    """方向键微移（不吸附），返回新场景。"""
    return drag_selection(scene, indices, dx_px, dy_px, snap=False)[0]


# ------------------------------------------------------------------ Tk 封装
class CanvasEditor(tk.Frame):
    """480×480 逻辑画布的图层编辑器。

    ``get_scene`` / ``apply_scene`` 由宿主提供：编辑器通过 ``get_scene()``
    读取当前场景，任何修改都 ``deepcopy`` 后调用 ``apply_scene(scene, label)``
    交回宿主（宿主负责归一化、写历史与刷新预览）。``on_select(index)`` 在
    选中变化时回调，``on_commit(label)`` 在一次鼠标拖拽结束时回调。
    """

    def __init__(
        self,
        parent: tk.Misc,
        get_scene: Callable[[], dict],
        apply_scene: Callable[[dict, str], None],
        on_select: Callable[[int | None], None] | None = None,
        on_commit: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("bg", "#0B0F14")
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(parent, **kwargs)
        self._get_scene = get_scene
        self._apply_scene = apply_scene
        self._on_select = on_select
        self._on_commit = on_commit
        self._zoom = 1.0
        self._image: Image.Image | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._selected: list[int] = []
        self._primary: int | None = None
        self._drag: dict | None = None
        self._guides: tuple[list[float], list[float]] = ([], [])
        self._pending_scene: dict | None = None
        self._canvas = tk.Canvas(
            self,
            width=int(SCREEN_W),
            height=int(SCREEN_H),
            bg="#111827",
            highlightthickness=0,
            bd=0,
        )
        self._canvas.pack(fill="both", expand=True)
        self._canvas.bind("<Button-1>", self._on_press)
        self._canvas.bind("<Control-Button-1>", self._on_press)
        self._canvas.bind("<B1-Motion>", self._on_motion)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)
        self._canvas.bind("<Key>", self._on_key)
        self._canvas.configure(takefocus=True)

    # ------------------------------------------------------------ 公共属性
    @property
    def selected(self) -> int | None:
        """主选中图层下标；没有选中时返回 ``None``。"""
        return self._primary

    @property
    def selected_indices(self) -> tuple[int, ...]:
        """当前选中的全部图层下标（多选时为多个）。"""
        return tuple(self._selected)

    @property
    def zoom(self) -> float:
        """当前显示缩放倍数。"""
        return self._zoom

    # ------------------------------------------------------------ 公共方法
    def get_scene(self) -> dict:
        """读取宿主当前场景。"""
        scene = self._get_scene()
        return scene if isinstance(scene, dict) else {}

    def set_zoom(self, zoom: float) -> None:
        """设置显示缩放（逻辑坐标不变），范围 0.25..4。"""
        self._zoom = clamp(float(zoom), 0.25, 4.0)
        side = int(round(SCREEN_W * self._zoom))
        self._canvas.configure(width=side, height=side)
        self.refresh()

    def set_image(self, image: Image.Image | None) -> None:
        """设置画布底图（宿主渲染的预览图），``None`` 表示清空。"""
        self._image = image
        self.refresh()

    def select(self, index: int | None) -> None:
        """选中单个图层（``None`` 表示取消选中）。"""
        layers = self._layers(self._scene_for_draw())
        if index is None or not (0 <= int(index) < len(layers)):
            self.clear_selection()
            return
        self._selected = [int(index)]
        self._primary = int(index)
        self._notify_select()
        self.refresh()

    def clear_selection(self) -> None:
        """清空选中状态。"""
        if not self._selected and self._primary is None:
            return
        self._selected = []
        self._primary = None
        self._notify_select()
        self.refresh()

    def refresh(self) -> None:
        """重画底图、图层选择框、缩放手柄与吸附辅助线。"""
        canvas = self._canvas
        canvas.delete("all")
        side = int(round(SCREEN_W * self._zoom))
        if self._image is not None:
            try:
                scaled = self._image.resize((side, side), Image.Resampling.LANCZOS)
                self._photo = ImageTk.PhotoImage(scaled)
                canvas.create_image(0, 0, anchor="nw", image=self._photo)
            except (OSError, ValueError, tk.TclError):
                self._photo = None
        layers = self._layers(self._scene_for_draw())
        for index, layer in enumerate(layers):
            bounds = layer_bounds(layer)
            if bounds is None:
                continue
            selected = index in self._selected
            rect = tuple(value * self._zoom for value in bounds)
            canvas.create_rectangle(
                rect[0],
                rect[1],
                rect[2],
                rect[3],
                outline="#38BDF8" if selected else "#64748B",
                width=2 if selected else 1,
                dash=() if selected else (4, 4),
            )
            if selected and len(self._selected) == 1 and can_resize(layer):
                self._draw_handles(bounds)
        self._draw_guides()

    # ------------------------------------------------------------ 事件处理
    def _on_press(self, event: tk.Event) -> None:
        try:
            self._canvas.focus_set()
            x, y = self._logical(event.x, event.y)
            ctrl = bool(getattr(event, "state", 0) & 0x0004)
            scene = self.get_scene()
            layers = self._layers(scene)
            if self._primary is not None and self._primary < len(layers):
                current = layers[self._primary]
                bounds = layer_bounds(current)
                if bounds is not None and can_resize(current):
                    handle = hit_handle(bounds, x, y)
                    if handle is not None:
                        self._start_drag("resize", scene, x, y, handle=handle)
                        return
            hit = hit_test(layers, x, y)
            if hit is None:
                if not ctrl:
                    self.clear_selection()
                return
            if ctrl:
                if hit in self._selected:
                    self._selected = [index for index in self._selected if index != hit]
                    self._primary = self._selected[-1] if self._selected else None
                else:
                    self._selected = [*self._selected, hit]
                    self._primary = hit
            else:
                if hit not in self._selected:
                    self._selected = [hit]
                self._primary = hit
            self._notify_select()
            self._start_drag("move", scene, x, y)
            self.refresh()
        except Exception:  # pragma: no cover - Tk 回调异常不应中断主循环
            logger.exception("画布按下事件处理失败")

    def _on_motion(self, event: tk.Event) -> None:
        drag = self._drag
        if drag is None:
            return
        try:
            x, y = self._logical(event.x, event.y)
            shift = bool(getattr(event, "state", 0) & 0x0001)
            base_scene = drag["scene"]
            indices = drag["selection"]
            dx = x - drag["start_x"]
            dy = y - drag["start_y"]
            label = ""
            if drag["mode"] == "resize":
                index = drag["index"]
                layers = self._layers(base_scene)
                if not (0 <= index < len(layers)):
                    return
                layer = resize_layer(layers[index], drag["handle"], x, y)
                new_scene = copy.deepcopy(base_scene)
                new_layers = self._layers(new_scene)
                new_layers[index] = layer
                label = "缩放图层"
            else:
                new_scene, guides_x, guides_y = drag_selection(
                    base_scene, indices, dx, dy, snap=True, axis_lock=shift
                )
                self._guides = (guides_x, guides_y)
                count = len(indices)
                label = "移动图层" if count <= 1 else f"移动 {count} 个图层"
            self._pending_scene = new_scene
            self._apply(new_scene, label)
            self.refresh()
        except Exception:  # pragma: no cover - Tk 回调异常不应中断主循环
            logger.exception("画布拖动事件处理失败")

    def _on_release(self, event: tk.Event) -> None:
        drag = self._drag
        self._drag = None
        self._guides = ([], [])
        if drag is not None and drag.get("moved"):
            label = "缩放图层" if drag["mode"] == "resize" else "移动图层"
            self._pending_scene = None
            self.refresh()
            if self._on_commit is not None:
                try:
                    self._on_commit(label)
                except Exception:
                    logger.exception("on_commit 回调失败")
        elif drag is not None:
            self._pending_scene = None
            self.refresh()

    def _on_key(self, event: tk.Event) -> None:
        if not self._selected:
            return
        key = getattr(event, "keysym", "")
        step = 8.0 if bool(getattr(event, "state", 0) & 0x0001) else 1.0
        delta = {
            "Left": (-step, 0.0),
            "Right": (step, 0.0),
            "Up": (0.0, -step),
            "Down": (0.0, step),
        }
        if key not in delta:
            return
        try:
            scene = self.get_scene()
            new_scene = nudge_selection(scene, self._selected, *delta[key])
            self._pending_scene = new_scene
            self._apply(new_scene, "微调图层")
            self.refresh()
        except Exception:  # pragma: no cover
            logger.exception("键盘微移处理失败")

    # ------------------------------------------------------------ 内部工具
    def _logical(self, canvas_x: float, canvas_y: float) -> tuple[float, float]:
        return canvas_x / self._zoom, canvas_y / self._zoom

    def _scene_for_draw(self) -> dict:
        if self._pending_scene is not None:
            return self._pending_scene
        return self.get_scene()

    def _layers(self, scene: dict) -> list[dict]:
        layers = scene.get("layers") if isinstance(scene, dict) else None
        return layers if isinstance(layers, list) else []

    def _notify_select(self) -> None:
        if self._on_select is not None:
            try:
                self._on_select(self._primary)
            except Exception:
                logger.exception("on_select 回调失败")

    def _apply(self, scene: dict, label: str) -> None:
        try:
            self._apply_scene(scene, label)
        except Exception:
            logger.exception("apply_scene 回调失败")

    def _start_drag(self, mode: str, scene: dict, x: float, y: float, handle: str = "") -> None:
        index = self._primary if self._primary is not None else -1
        self._drag = {
            "mode": mode,
            "scene": copy.deepcopy(scene),
            "selection": list(self._selected),
            "index": index,
            "handle": handle,
            "start_x": x,
            "start_y": y,
            "moved": False,
        }
        self._pending_scene = scene if self._selected else None

    def _draw_handles(self, bounds: tuple[float, float, float, float]) -> None:
        for x, y in handle_points(bounds).values():
            display_x, display_y = x * self._zoom, y * self._zoom
            half = HANDLE_SIZE / 2.0
            self._canvas.create_rectangle(
                display_x - half,
                display_y - half,
                display_x + half,
                display_y + half,
                outline="#F8FAFC",
                fill="#38BDF8",
            )

    def _draw_guides(self) -> None:
        guides_x, guides_y = self._guides
        side = int(round(SCREEN_W * self._zoom))
        for guide in guides_x:
            display = guide * self._zoom
            self._canvas.create_line(display, 0, display, side, fill="#F472B6", dash=(3, 3))
        for guide in guides_y:
            display = guide * self._zoom
            self._canvas.create_line(0, display, side, display, fill="#F472B6", dash=(3, 3))


__all__ = [
    "HANDLE_SIZE",
    "MIN_SIZE",
    "MOVE_ONLY_KINDS",
    "RESIZABLE_KINDS",
    "SCREEN_H",
    "SCREEN_W",
    "SNAP_TOLERANCE",
    "CanvasEditor",
    "anchor_factors",
    "anchor_parts",
    "can_resize",
    "clamp",
    "clamp_norm",
    "drag_selection",
    "handle_points",
    "hit_handle",
    "hit_test",
    "image_layer_size",
    "layer_bounds",
    "layer_extent",
    "measure_block",
    "measure_text",
    "nudge_selection",
    "resize_layer",
    "selection_bounds",
    "snap_offset",
    "snap_targets",
]
