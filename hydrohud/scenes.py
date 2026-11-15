"""场景（scene）定义：默认值补全、预设、项目文件读写。

场景是一个纯 JSON 字典，schema v1::

    {
      "name": "我的场景",
      "background": {"type": "solid", "color": "#0B0F14"},
      #  或 {"type": "image"|"gif"|"video", "path": "...", "fit": "crop"|"fit"|"stretch"}
      "tint": {"color": "#000000", "alpha": 0.0},
      "layers": [...]
    }

``normalize_scene`` 会把任意残缺/多余的字典补全成规范形式：
缺失字段使用默认值，未知字段/未知图层类型被丢弃，非法数值退回默认值，
因此 GUI 可以放心把用户随手拼的字典交给它，渲染层也可以直接信任返回值。
例外是**已注册的插件图层**（:mod:`hydrohud.plugins`）：它们只归一化位置与
锚点，其余字段原样保留，供插件自行解释。
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
import json
import math
import os
import re
import tempfile
from typing import Any

try:  # 插件模块是可选的：缺失时插件图层按未知类型丢弃
    from . import plugins as _plugins
except Exception:  # noqa: BLE001 - 任何导入失败都退化为"无插件"
    _plugins = None  # type: ignore[assignment]

__all__ = [
    "SCENE_VERSION",
    "DEFAULT_BACKGROUND_COLOR",
    "DEFAULT_TEXT_COLOR",
    "BACKGROUND_TYPES",
    "FIT_MODES",
    "METRIC_IDS",
    "FALLBACK_METRICS",
    "LAYER_TYPES",
    "LAYER_TYPE_LABELS",
    "WEATHER_FIELDS",
    "NOWPLAYING_FIELDS",
    "TICKER_DIRECTIONS",
    "QR_ERROR_LEVELS",
    "normalize_scene",
    "preset_scenes",
    "load_project",
    "save_project",
    "clone_scene",
]

SCENE_VERSION = 1
DEFAULT_BACKGROUND_COLOR = "#0B0F14"
DEFAULT_TEXT_COLOR = "#FFFFFF"

BACKGROUND_TYPES = ("solid", "image", "gif", "video", "capture")
FIT_MODES = ("crop", "fit", "stretch")

#: ``sensors`` 缺失时的指标兜底列表；正常路径下以 ``sensors.METRIC_IDS`` 为准。
FALLBACK_METRICS = (
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
)

try:  # 传感器模块是可选的：缺失时仍能编辑/渲染旧场景
    from .sensors import METRIC_IDS as _SENSOR_METRIC_IDS
except Exception:  # noqa: BLE001 - 任何导入失败都退化为兜底列表
    _SENSOR_METRIC_IDS = ()

METRIC_IDS: tuple[str, ...] = tuple(_SENSOR_METRIC_IDS) or FALLBACK_METRICS

#: 全部合法图层类型（顺序即 GUI "添加图层" 菜单顺序）。
LAYER_TYPES: tuple[str, ...] = (
    "text",
    "clock",
    "sysinfo",
    "bars",
    "chart",
    "gauge",
    "image",
    "thermometer",
    "countdown",
    "ticker",
    "sequence",
    "qrcode",
    "weather",
    "nowplaying",
)

#: 图层类型的中文名。
LAYER_TYPE_LABELS: dict[str, str] = {
    "text": "文字",
    "clock": "时钟",
    "sysinfo": "状态文字",
    "bars": "进度条",
    "chart": "折线图",
    "gauge": "仪表盘",
    "image": "图片",
    "thermometer": "温度计",
    "countdown": "倒计时",
    "ticker": "滚动文字",
    "sequence": "图片序列",
    "qrcode": "二维码",
    "weather": "天气",
    "nowplaying": "正在播放",
}

#: ``weather`` 图层可显示的字段。
WEATHER_FIELDS = ("temp", "feels", "humidity", "wind", "desc")

#: ``nowplaying`` 图层可显示的字段。
NOWPLAYING_FIELDS = ("title", "artist", "album")

#: ``sequence`` / ``ticker`` 等图层的子选项。
TICKER_DIRECTIONS = ("left", "right")
QR_ERROR_LEVELS = ("L", "M", "Q", "H")

#: 只接受 hex 颜色；tkinter.colorchooser 返回的也是这种格式。
_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

_ANCHORS = {
    "center": "center",
    "top-left": "top-left",
    "top": "top-center",
    "top-center": "top-center",
    "top-right": "top-right",
    "bottom-left": "bottom-left",
    "bottom": "bottom-center",
    "bottom-center": "bottom-center",
    "bottom-right": "bottom-right",
    "left": "center-left",
    "center-left": "center-left",
    "right": "center-right",
    "center-right": "center-right",
}

_SYSINFO_FIELDS = ("cpu", "ram")
_ALIGNS = ("left", "center", "right")

#: 图层里可以引用的指标白名单。旧场景里的 ``cpu`` / ``ram`` 天然在其内。
_METRIC_SET = frozenset(METRIC_IDS) or frozenset(FALLBACK_METRICS)


# --------------------------------------------------------------------- 工具函数
def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _as_size(value: Any, default: int, lo: int = 1, hi: int = 1000) -> int:
    number = _as_float(value, float(default))
    return int(max(lo, min(hi, round(number))))


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on", "是"):
            return True
        if lowered in ("0", "false", "no", "off", "否"):
            return False
    return default


def _as_color(value: Any, default: str) -> str:
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith("#"):
            text = "#" + text
        if _COLOR_RE.match(text):
            if len(text) == 4:  # #ABC -> #AABBCC
                text = "#" + "".join(ch * 2 for ch in text[1:])
            return text.upper()
    return default


def _as_anchor(value: Any, default: str = "center") -> str:
    if isinstance(value, str):
        key = value.strip().lower().replace("_", "-")
        if key in _ANCHORS:
            return _ANCHORS[key]
    return _ANCHORS.get(default, "center")


def _as_fit(value: Any) -> str:
    if isinstance(value, str) and value.strip().lower() in FIT_MODES:
        return value.strip().lower()
    return "crop"


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_text(value: Any, default: str = "") -> str:
    return _as_str(value, default)


def _as_fields(value: Any, allowed: tuple[str, ...], default: tuple[str, ...]) -> list[str]:
    result: list[str] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            key = _as_str(item).strip().lower()
            if key in allowed and key not in result:
                result.append(key)
    if not result:
        result = list(default)
    return result


def _as_metrics(value: Any, default: tuple[str, ...] = ("cpu", "ram"), limit: int = 8) -> list[str]:
    """把任意字符串列表过滤成合法指标名（保持顺序、去重、截断到 ``limit``）。"""
    result: list[str] = []
    if isinstance(value, str):
        items: tuple = (value,)
    elif isinstance(value, (list, tuple)):
        items = tuple(value)
    else:
        items = ()
    for item in items:
        for part in _as_str(item).split(","):
            key = part.strip().lower()
            if key in _METRIC_SET and key not in result:
                result.append(key)
            if len(result) >= limit:
                return result
    if not result:
        result = [key for key in default if key in _METRIC_SET][:limit]
    return result


def _as_metric(value: Any, default: str = "cpu") -> str:
    """把单个值规范成合法指标名。"""
    key = _as_str(value).strip().lower()
    if key in _METRIC_SET:
        return key
    return default if default in _METRIC_SET else next(iter(sorted(_METRIC_SET)))


def _as_bound(value: Any) -> float | None:
    """y 轴上下限：``None``/非法值表示"按指标自身范围自动"。"""
    if value is None:
        return None
    number = _as_float(value, float("nan"))
    if not math.isfinite(number):
        return None
    return number


def _as_paths(value: Any, limit: int = 64) -> list[str]:
    """把单个路径或路径列表规范成非空字符串列表（去重、限长）。"""
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    paths: list[str] = []
    for item in items:
        text = _as_str(item).strip()
        if text and text not in paths:
            paths.append(text)
        if len(paths) >= limit:
            break
    return paths


def _as_choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
    """把值规范成 ``allowed`` 之一，否则返回 ``default``。"""
    text = _as_str(value).strip().lower()
    return text if text in allowed else default


def _norm_layer(layer: Any) -> dict | None:
    """规范化单个图层；未知类型返回 ``None``（由调用方丢弃）。

    已注册的插件图层类型不做字段解释：只归一化位置/锚点并原样保留其它字段。
    """
    layer = _as_dict(layer)
    kind = _as_str(layer.get("type")).strip().lower()
    base = {
        "x": _as_float(layer.get("x"), 0.5),
        "y": _as_float(layer.get("y"), 0.5),
        "anchor": _as_anchor(layer.get("anchor"), "center"),
    }

    if kind == "text":
        base.update(
            {
                "type": "text",
                "text": _as_text(layer.get("text")),
                "size": _as_size(layer.get("size"), 56),
                "color": _as_color(layer.get("color"), DEFAULT_TEXT_COLOR),
                "bold": _as_bool(layer.get("bold"), False),
                "align": _as_align(layer.get("align"), "center"),
                "shadow": _as_bool(layer.get("shadow"), True),
            }
        )
        return base

    if kind == "clock":
        base.update(
            {
                "type": "clock",
                "format": _as_str(layer.get("format"), "%H:%M") or "%H:%M",
                "show_date": _as_bool(layer.get("show_date"), False),
                "date_format": _as_str(layer.get("date_format"), "%Y-%m-%d") or "%Y-%m-%d",
                "date_size": _as_size(
                    layer.get("date_size"), max(10, int(_as_size(layer.get("size"), 140) * 0.26))
                ),
                "size": _as_size(layer.get("size"), 140),
                "color": _as_color(layer.get("color"), DEFAULT_TEXT_COLOR),
                "bold": _as_bool(layer.get("bold"), True),
                "shadow": _as_bool(layer.get("shadow"), True),
            }
        )
        return base

    if kind == "sysinfo":
        base.update(
            {
                "type": "sysinfo",
                "fields": _as_metrics(layer.get("fields"), _SYSINFO_FIELDS),
                "size": _as_size(layer.get("size"), 30),
                "color": _as_color(layer.get("color"), "#8FD3FF"),
                "bold": _as_bool(layer.get("bold"), False),
                "shadow": _as_bool(layer.get("shadow"), True),
                "separator": _as_str(layer.get("separator"), "    ") or "    ",
            }
        )
        return base

    if kind == "bars":
        base.update(
            {
                "type": "bars",
                "items": _as_metrics(layer.get("items"), _SYSINFO_FIELDS),
                "w": _clamp(_as_float(layer.get("w"), 0.84), 0.01, 1.0),
                "h": _clamp(_as_float(layer.get("h"), 0.10), 0.01, 1.0),
                "gap": _clamp(_as_float(layer.get("gap"), 0.015), 0.0, 0.5),
                "color": _as_color(layer.get("color"), "#38BDF8"),
                "color2": _as_color(layer.get("color2"), "#F472B6"),
                "bg": _as_color(layer.get("bg"), "#1E293B"),
                "label": _as_bool(layer.get("label"), True),
                "label_color": _as_color(layer.get("label_color"), "#E2E8F0"),
                "size": _as_size(layer.get("size"), 22),
                "radius": _clamp(_as_float(layer.get("radius"), 1.0), 0.0, 1.0),
            }
        )
        return base

    if kind == "chart":
        base.update(
            {
                "type": "chart",
                "metric": _as_metric(layer.get("metric"), "cpu"),
                "w": _clamp(_as_float(layer.get("w"), 0.8), 0.05, 1.0),
                "h": _clamp(_as_float(layer.get("h"), 0.22), 0.05, 1.0),
                "color": _as_color(layer.get("color"), "#38BDF8"),
                "color2": _as_color(layer.get("color2"), "#0EA5E9"),
                "bg": _as_color(layer.get("bg"), "#0F172A"),
                "line_width": _as_size(layer.get("line_width"), 2, 1, 24),
                "fill": _as_bool(layer.get("fill"), True),
                "grid": _as_bool(layer.get("grid"), True),
                "label": _as_bool(layer.get("label"), True),
                "size": _as_size(layer.get("size"), 18),
                "radius": _clamp(_as_float(layer.get("radius"), 0.5), 0.0, 1.0),
                "min": _as_bound(layer.get("min")),
                "max": _as_bound(layer.get("max")),
                "points": _as_size(layer.get("points"), 120, 2, 480),
                "smooth": _as_bool(layer.get("smooth"), False),
            }
        )
        return base

    if kind == "gauge":
        base.update(
            {
                "type": "gauge",
                "metric": _as_metric(layer.get("metric"), "cpu"),
                "diameter": _clamp(_as_float(layer.get("diameter"), 0.5), 0.05, 1.5),
                "thickness": _clamp(_as_float(layer.get("thickness"), 0.06), 0.005, 0.5),
                "color": _as_color(layer.get("color"), "#38BDF8"),
                "track": _as_color(layer.get("track"), "#1E293B"),
                "label": _as_bool(layer.get("label"), True),
                "size": _as_size(layer.get("size"), 20),
                "value_size": _as_size(layer.get("value_size"), 48),
                "min": _as_bound(layer.get("min")),
                "max": _as_bound(layer.get("max")),
                "start_angle": _as_float(layer.get("start_angle"), 135.0),
                "sweep": _clamp(_as_float(layer.get("sweep"), 270.0), 1.0, 360.0),
                "show_unit": _as_bool(layer.get("show_unit"), True),
            }
        )
        return base

    if kind == "image":
        base.update(
            {
                "type": "image",
                "path": _as_str(layer.get("path")),
                "scale": _clamp(_as_float(layer.get("scale"), 0.4), 0.01, 10.0),
                "opacity": _clamp(_as_float(layer.get("opacity"), 1.0), 0.0, 1.0),
            }
        )
        return base

    if kind == "thermometer":
        base.update(
            {
                "type": "thermometer",
                "metric": _as_metric(layer.get("metric"), "cpu_temp"),
                "w": _clamp(_as_float(layer.get("w"), 0.14), 0.02, 1.0),
                "h": _clamp(_as_float(layer.get("h"), 0.62), 0.05, 1.0),
                "color": _as_color(layer.get("color"), "#FB7185"),
                "track": _as_color(layer.get("track"), "#1E293B"),
                "radius": _clamp(_as_float(layer.get("radius"), 0.5), 0.0, 1.0),
                "label": _as_bool(layer.get("label"), True),
                "show_value": _as_bool(layer.get("show_value"), True),
                "size": _as_size(layer.get("size"), 20),
                "value_size": _as_size(layer.get("value_size"), 34),
                "show_unit": _as_bool(layer.get("show_unit"), True),
                "min": _as_bound(layer.get("min")),
                "max": _as_bound(layer.get("max")),
            }
        )
        return base

    if kind == "countdown":
        base.update(
            {
                "type": "countdown",
                "target": _as_str(layer.get("target")),
                "title": _as_str(layer.get("title")),
                "done_text": _as_str(layer.get("done_text"), "时间到") or "时间到",
                "format": _as_str(layer.get("format"), "{d} 天 {h}:{m}:{s}")
                or "{d} 天 {h}:{m}:{s}",
                "size": _as_size(layer.get("size"), 44),
                "title_size": _as_size(layer.get("title_size"), 22),
                "color": _as_color(layer.get("color"), "#38BDF8"),
                "title_color": _as_color(layer.get("title_color"), "#94A3B8"),
                "shadow": _as_bool(layer.get("shadow"), True),
            }
        )
        return base

    if kind == "ticker":
        base.update(
            {
                "type": "ticker",
                "text": _as_text(layer.get("text")),
                "size": _as_size(layer.get("size"), 34),
                "color": _as_color(layer.get("color"), "#E2E8F0"),
                "bold": _as_bool(layer.get("bold"), False),
                "speed": _clamp(_as_float(layer.get("speed"), 60.0), 5.0, 600.0),
                "gap": _clamp(_as_float(layer.get("gap"), 0.5), 0.0, 4.0),
                "direction": _as_choice(layer.get("direction"), TICKER_DIRECTIONS, "left"),
                "band": _as_bool(layer.get("band"), False),
                "band_color": _as_color(layer.get("band_color"), "#0B0F14"),
                "shadow": _as_bool(layer.get("shadow"), True),
            }
        )
        return base

    if kind == "sequence":
        base.update(
            {
                "type": "sequence",
                "paths": _as_paths(layer.get("paths")),
                "fps": _clamp(_as_float(layer.get("fps"), 8.0), 0.5, 60.0),
                "fit": _as_fit(layer.get("fit")),
                "w": _clamp(_as_float(layer.get("w"), 0.5), 0.02, 1.0),
                "h": _clamp(_as_float(layer.get("h"), 0.5), 0.02, 1.0),
                "radius": _clamp(_as_float(layer.get("radius"), 0.0), 0.0, 1.0),
                "loop": _as_bool(layer.get("loop"), True),
                "opacity": _clamp(_as_float(layer.get("opacity"), 1.0), 0.0, 1.0),
            }
        )
        return base

    if kind == "qrcode":
        base.update(
            {
                "type": "qrcode",
                "data": _as_text(layer.get("data")),
                "size": _clamp(_as_float(layer.get("size"), 0.5), 0.05, 1.0),
                "fg": _as_color(layer.get("fg"), "#0B0F14"),
                "bg": _as_color(layer.get("bg"), "#FFFFFF"),
                "border": _as_size(layer.get("border"), 2, 0, 8),
                "error": _as_choice(layer.get("error"), QR_ERROR_LEVELS, "M"),
                "label": _as_bool(layer.get("label"), False),
                "label_size": _as_size(layer.get("label_size"), 18),
                "label_color": _as_color(layer.get("label_color"), DEFAULT_TEXT_COLOR),
            }
        )
        return base

    if kind == "weather":
        base.update(
            {
                "type": "weather",
                "lat": _as_bound(layer.get("lat")),
                "lon": _as_bound(layer.get("lon")),
                "place": _as_str(layer.get("place")),
                "fields": _as_fields(layer.get("fields"), WEATHER_FIELDS, ("temp", "desc")),
                "icon": _as_bool(layer.get("icon"), True),
                "icon_size": _as_size(layer.get("icon_size"), 56),
                "size": _as_size(layer.get("size"), 28),
                "color": _as_color(layer.get("color"), "#E2E8F0"),
                "shadow": _as_bool(layer.get("shadow"), True),
                "separator": _as_str(layer.get("separator"), "  ") or "  ",
            }
        )
        return base

    if kind == "nowplaying":
        base.update(
            {
                "type": "nowplaying",
                "fields": _as_fields(layer.get("fields"), NOWPLAYING_FIELDS, ("title", "artist")),
                "show_cover": _as_bool(layer.get("show_cover"), True),
                "cover_size": _clamp(_as_float(layer.get("cover_size"), 0.22), 0.0, 0.8),
                "size": _as_size(layer.get("size"), 26),
                "color": _as_color(layer.get("color"), "#E2E8F0"),
                "label_color": _as_color(layer.get("label_color"), "#94A3B8"),
                "separator": _as_str(layer.get("separator"), " · ") or " · ",
                "placeholder": _as_str(layer.get("placeholder"), "未在播放") or "未在播放",
                "scroll": _as_bool(layer.get("scroll"), False),
                "speed": _clamp(_as_float(layer.get("speed"), 40.0), 5.0, 600.0),
            }
        )
        return base

    if _is_plugin_layer(kind):
        return _norm_plugin_layer(layer, kind, base)

    return None


_UNSAFE = object()


def _is_plugin_layer(kind: str) -> bool:
    """``kind`` 是否是已注册的插件图层类型；``plugins`` 缺失时恒为 ``False``。"""
    module = _plugins
    if module is None or not kind:
        return False
    checker = getattr(module, "is_registered", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(kind))
    except Exception:  # noqa: BLE001 - 插件注册表异常时按未知类型处理
        return False


def _json_safe(value: Any) -> Any:
    """返回可直接进 JSON 的副本；无法序列化的值返回 :data:`_UNSAFE` 由调用方丢弃。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, float) and not math.isfinite(value):
            return _UNSAFE
        return value
    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        for item in value:
            safe = _json_safe(item)
            if safe is _UNSAFE:
                return _UNSAFE
            items.append(safe)
        return items
    if isinstance(value, dict):
        mapping: dict[str, Any] = {}
        for key, item in value.items():
            safe = _json_safe(item)
            if safe is _UNSAFE:
                return _UNSAFE
            mapping[str(key)] = safe
        return mapping
    return _UNSAFE


def _norm_plugin_layer(layer: dict, kind: str, base: dict) -> dict:
    """规范化插件图层：``type`` / ``x`` / ``y`` / ``anchor`` 归一化，其余字段原样保留。

    插件自行解释自己的字段，这里只保证值能写进 JSON（场景会被写盘、经 HTTP
    API 传输），无法序列化的字段被丢弃。
    """
    result: dict[str, Any] = {"type": kind}
    for key, value in layer.items():
        if key in ("type", "x", "y", "anchor"):
            continue
        safe = _json_safe(value)
        if safe is not _UNSAFE:
            result[str(key)] = safe
    result.update(base)
    return result


def _as_align(value: Any, default: str) -> str:
    if isinstance(value, str) and value.strip().lower() in _ALIGNS:
        return value.strip().lower()
    return default


def _clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else float(value)


# --------------------------------------------------------------------- 公开 API
def normalize_scene(scene: dict) -> dict:
    """补全场景默认值并丢弃非法内容，返回一个全新的规范字典。

    参数 ``scene`` 可以是任意字典（甚至 ``None``）；缺失字段用预设默认值补齐，
    未知字段和未知图层类型被忽略，因此不会抛异常。返回值可直接用于
    :func:`hydrohud.render.render_scene` 以及保存项目。
    """
    raw = _as_dict(scene)

    background = _as_dict(raw.get("background"))
    bg_type = _as_str(background.get("type")).strip().lower()
    if bg_type not in BACKGROUND_TYPES:
        bg_type = "solid"
    region_raw = background.get("region")
    region: list[float] = []
    if isinstance(region_raw, (list, tuple)) and len(region_raw) == 4:
        for value in region_raw:
            region.append(_clamp(_as_float(value, 0.0), 0.0, 1.0))
    if len(region) != 4:
        region = [0.0, 0.0, 1.0, 1.0]
    if region[2] <= 0.0:
        region[2] = 1.0
    if region[3] <= 0.0:
        region[3] = 1.0
    normalized_bg = {
        "type": bg_type,
        "color": _as_color(background.get("color"), DEFAULT_BACKGROUND_COLOR),
        "path": _as_str(background.get("path")),
        "fit": _as_fit(background.get("fit")),
        "region": region,
        "fps": _clamp(_as_float(background.get("fps"), 5.0), 0.5, 30.0),
        "monitor": _as_size(background.get("monitor"), 0, 0, 8),
    }

    tint_raw = _as_dict(raw.get("tint"))
    normalized_tint = {
        "color": _as_color(tint_raw.get("color"), "#000000"),
        "alpha": _clamp(_as_float(tint_raw.get("alpha"), 0.0), 0.0, 1.0),
    }

    layers = raw.get("layers")
    normalized_layers: list[dict] = []
    if isinstance(layers, (list, tuple)):
        for layer in layers:
            normalized = _norm_layer(layer)
            if normalized is not None:
                normalized_layers.append(normalized)

    return {
        "version": SCENE_VERSION,
        "name": _as_str(raw.get("name"), "未命名场景").strip() or "未命名场景",
        "background": normalized_bg,
        "tint": normalized_tint,
        "layers": normalized_layers,
    }


def _layer(kind: str, **kwargs: Any) -> dict:
    """构造预设图层（经 normalize 后一定合法）。"""
    layer: dict[str, Any] = {"type": kind}
    layer.update(kwargs)
    return _norm_layer(layer)  # type: ignore[return-value]


def preset_scenes() -> dict[str, dict]:
    """返回内置预设场景：``名称 -> 已规范化的 scene``（至少 6 个）。

    预设只依赖系统字体和纯色背景，不引用外部图片，保证开箱即用。
    键名即 GUI 预设列表显示的名称。
    """
    presets: dict[str, dict] = {}

    presets["极简时钟"] = {
        "name": "极简时钟",
        "background": {"type": "solid", "color": "#0B0F14"},
        "layers": [
            _layer(
                "clock",
                format="%H:%M",
                show_date=True,
                date_format="%Y-%m-%d",
                x=0.5,
                y=0.45,
                size=150,
                color="#F8FAFC",
                bold=True,
            ),
            _layer(
                "text",
                text="HYDROHUD",
                x=0.5,
                y=0.88,
                size=22,
                color="#64748B",
                shadow=False,
            ),
        ],
    }

    presets["霓虹仪表盘"] = {
        "name": "霓虹仪表盘",
        "background": {"type": "solid", "color": "#0B1220"},
        "tint": {"color": "#1E3A8A", "alpha": 0.12},
        "layers": [
            _layer("clock", x=0.5, y=0.2, size=96, color="#7DD3FC", bold=True),
            _layer(
                "bars",
                items=["cpu", "ram"],
                x=0.1,
                y=0.45,
                w=0.8,
                h=0.13,
                color="#38BDF8",
                color2="#F472B6",
                bg="#1E293B",
                size=20,
            ),
            _layer(
                "sysinfo",
                fields=["cpu", "ram"],
                x=0.5,
                y=0.85,
                size=26,
                color="#CBD5E1",
                shadow=False,
            ),
        ],
    }

    presets["系统监控"] = {
        "name": "系统监控",
        "background": {"type": "solid", "color": "#050A12"},
        "tint": {"color": "#0EA5E9", "alpha": 0.08},
        "layers": [
            _layer("text", text="系统监控", x=0.5, y=0.09, size=32, color="#94A3B8"),
            _layer(
                "sysinfo",
                fields=["cpu", "ram"],
                x=0.5,
                y=0.24,
                size=26,
                color="#22D3EE",
                bold=True,
            ),
            _layer(
                "bars",
                items=["cpu", "ram"],
                x=0.1,
                y=0.44,
                w=0.8,
                h=0.15,
                color="#22D3EE",
                color2="#A78BFA",
                bg="#0F172A",
                size=22,
            ),
            _layer("text", text="LG600 · 480×480", x=0.5, y=0.9, size=20, color="#475569"),
        ],
    }

    presets["极客终端"] = {
        "name": "极客终端",
        "background": {"type": "solid", "color": "#06120A"},
        "tint": {"color": "#00FF66", "alpha": 0.05},
        "layers": [
            _layer("clock", x=0.5, y=0.26, size=120, color="#22C55E", bold=True),
            _layer(
                "text",
                text="> 系统运行正常_",
                x=0.5,
                y=0.48,
                size=26,
                color="#4ADE80",
                shadow=False,
            ),
            _layer(
                "sysinfo",
                fields=["cpu", "ram"],
                x=0.5,
                y=0.65,
                size=26,
                color="#86EFAC",
                shadow=False,
            ),
            _layer(
                "bars",
                items=["cpu", "ram"],
                x=0.1,
                y=0.78,
                w=0.8,
                h=0.07,
                color="#22C55E",
                color2="#84CC16",
                bg="#052E16",
                size=18,
                gap=0.01,
            ),
        ],
    }

    presets["日落暖光"] = {
        "name": "日落暖光",
        "background": {"type": "solid", "color": "#1A0B0B"},
        "tint": {"color": "#F97316", "alpha": 0.2},
        "layers": [
            _layer(
                "clock",
                format="%H:%M",
                show_date=True,
                date_format="%m月%d日",
                x=0.5,
                y=0.42,
                size=140,
                color="#FDE68A",
                bold=True,
            ),
            _layer(
                "text",
                text="GOOD EVENING",
                x=0.5,
                y=0.76,
                size=24,
                color="#FDBA74",
                shadow=False,
            ),
            _layer(
                "sysinfo",
                fields=["cpu", "ram"],
                x=0.5,
                y=0.91,
                size=22,
                color="#FCA5A5",
                shadow=False,
            ),
        ],
    }

    presets["冰晶蓝调"] = {
        "name": "冰晶蓝调",
        "background": {"type": "solid", "color": "#0A1628"},
        "tint": {"color": "#38BDF8", "alpha": 0.1},
        "layers": [
            _layer("clock", x=0.5, y=0.36, size=132, color="#E0F2FE", bold=True),
            _layer(
                "bars",
                items=["cpu", "ram"],
                x=0.12,
                y=0.58,
                w=0.76,
                h=0.09,
                color="#0EA5E9",
                color2="#22D3EE",
                bg="#0F2A44",
                size=20,
            ),
            _layer(
                "sysinfo",
                fields=["cpu", "ram"],
                x=0.5,
                y=0.88,
                size=24,
                color="#7DD3FC",
                shadow=False,
            ),
        ],
    }

    presets["纯色标语"] = {
        "name": "纯色标语",
        "background": {"type": "solid", "color": "#111827"},
        "layers": [
            _layer("text", text="LG600", x=0.5, y=0.42, size=100, color="#F9FAFB", bold=True),
            _layer("text", text="水冷屏控制台", x=0.5, y=0.62, size=36, color="#9CA3AF"),
            _layer("text", text="480 × 480", x=0.5, y=0.76, size=20, color="#4B5563"),
        ],
    }

    presets["图片背景"] = {
        "name": "图片背景",
        "background": {"type": "image", "path": "", "fit": "crop"},
        "tint": {"color": "#000000", "alpha": 0.4},
        "layers": [
            _layer("clock", x=0.5, y=0.42, size=124, color="#FFFFFF", bold=True),
            _layer(
                "text",
                text="图片 / GIF / 视频背景",
                x=0.5,
                y=0.6,
                size=24,
                color="#E2E8F0",
                shadow=False,
            ),
        ],
    }

    presets["硬件温度"] = {
        "name": "硬件温度",
        "background": {"type": "solid", "color": "#07090F"},
        "tint": {"color": "#F97316", "alpha": 0.06},
        "layers": [
            _layer("text", text="硬件温度", x=0.5, y=0.08, size=26, color="#94A3B8"),
            _layer(
                "gauge",
                metric="cpu_temp",
                x=0.27,
                y=0.36,
                diameter=0.40,
                thickness=0.055,
                color="#38BDF8",
                track="#132133",
                size=16,
                value_size=34,
            ),
            _layer(
                "gauge",
                metric="gpu_temp",
                x=0.73,
                y=0.36,
                diameter=0.40,
                thickness=0.055,
                color="#FB7185",
                track="#2A1420",
                size=16,
                value_size=34,
            ),
            _layer("text", text="CPU", x=0.27, y=0.63, size=16, color="#7DD3FC"),
            _layer("text", text="GPU", x=0.73, y=0.63, size=16, color="#FDA4AF"),
            _layer(
                "sysinfo",
                fields=["cpu", "gpu", "ram"],
                x=0.5,
                y=0.78,
                size=18,
                color="#CBD5E1",
                shadow=False,
            ),
            _layer(
                "bars",
                items=["gpu_power", "gpu_fan"],
                x=0.1,
                y=0.85,
                w=0.8,
                h=0.09,
                color="#FBBF24",
                color2="#34D399",
                bg="#131C29",
                size=15,
                gap=0.012,
            ),
        ],
    }

    presets["性能曲线"] = {
        "name": "性能曲线",
        "background": {"type": "solid", "color": "#050A12"},
        "tint": {"color": "#22D3EE", "alpha": 0.06},
        "layers": [
            _layer(
                "text",
                text="{time}",
                x=0.5,
                y=0.08,
                size=46,
                color="#E2E8F0",
                bold=True,
                shadow=False,
            ),
            _layer(
                "chart",
                metric="cpu",
                x=0.08,
                y=0.2,
                w=0.84,
                h=0.24,
                color="#38BDF8",
                color2="#0EA5E9",
                bg="#0B1220",
                label=True,
                size=18,
            ),
            _layer(
                "chart",
                metric="gpu",
                x=0.08,
                y=0.48,
                w=0.84,
                h=0.24,
                color="#A78BFA",
                color2="#7C3AED",
                bg="#0B1220",
                label=True,
                size=18,
            ),
            _layer(
                "sysinfo",
                fields=["cpu", "gpu", "ram"],
                x=0.5,
                y=0.85,
                size=18,
                color="#94A3B8",
                shadow=False,
            ),
        ],
    }

    presets["极客面板"] = {
        "name": "极客面板",
        "background": {"type": "solid", "color": "#060D09"},
        "tint": {"color": "#22C55E", "alpha": 0.05},
        "layers": [
            _layer("clock", x=0.5, y=0.13, size=64, color="#4ADE80", bold=True),
            _layer(
                "gauge",
                metric="cpu",
                x=0.5,
                y=0.44,
                diameter=0.52,
                thickness=0.05,
                color="#22C55E",
                track="#0C2016",
                size=18,
                value_size=44,
            ),
            _layer(
                "bars",
                items=["gpu", "ram"],
                x=0.12,
                y=0.68,
                w=0.76,
                h=0.10,
                color="#34D399",
                color2="#22D3EE",
                bg="#0A1C14",
                size=17,
                gap=0.012,
            ),
            _layer(
                "sysinfo",
                fields=["cpu_temp", "gpu_temp", "disk"],
                x=0.5,
                y=0.87,
                size=17,
                color="#86EFAC",
                shadow=False,
            ),
        ],
    }

    return {name: normalize_scene(scene) for name, scene in presets.items()}


def load_project(path: str) -> dict:
    """读取 ``.hud.json`` 项目文件并返回规范化场景。

    文件不存在抛 :class:`FileNotFoundError`；JSON 非法或顶层不是对象时抛
    :class:`ValueError`（中文错误信息，GUI 可直接展示）。
    """
    if not path:
        raise ValueError("项目路径为空")
    with open(path, encoding="utf-8-sig") as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ValueError(f"项目文件不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("项目文件格式错误：顶层必须是 JSON 对象")
    # 兼容把场景直接放在 {"scene": {...}} 里的旧格式。
    if isinstance(data.get("scene"), dict):
        data = data["scene"]
    return normalize_scene(data)


def save_project(scene: dict, path: str) -> None:
    """把场景规范化后写入 ``.hud.json``（UTF-8、缩进 2、原子替换）。"""
    if not path:
        raise ValueError("项目路径为空")
    normalized = normalize_scene(scene)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
    fd, temp_path = tempfile.mkstemp(prefix=".hud-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def clone_scene(scene: dict) -> dict:
    """返回场景的深拷贝（便捷函数，GUI 编辑时避免共享引用）。"""
    return copy.deepcopy(normalize_scene(scene))
