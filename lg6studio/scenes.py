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
"""

# MIT License
#
# Copyright (c) 2026 LG600 Studio contributors
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

__all__ = [
    "SCENE_VERSION",
    "DEFAULT_BACKGROUND_COLOR",
    "normalize_scene",
    "preset_scenes",
    "load_project",
    "save_project",
]

SCENE_VERSION = 1
DEFAULT_BACKGROUND_COLOR = "#0B0F14"
DEFAULT_TEXT_COLOR = "#FFFFFF"

BACKGROUND_TYPES = ("solid", "image", "gif", "video")
FIT_MODES = ("crop", "fit", "stretch")

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


def _norm_layer(layer: Any) -> dict | None:
    """规范化单个图层；未知类型返回 ``None``（由调用方丢弃）。"""
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
                "fields": _as_fields(layer.get("fields"), _SYSINFO_FIELDS, _SYSINFO_FIELDS),
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
                "items": _as_fields(layer.get("items"), _SYSINFO_FIELDS, _SYSINFO_FIELDS),
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

    return None


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
    :func:`lg6studio.render.render_scene` 以及保存项目。
    """
    raw = _as_dict(scene)

    background = _as_dict(raw.get("background"))
    bg_type = _as_str(background.get("type")).strip().lower()
    if bg_type not in BACKGROUND_TYPES:
        bg_type = "solid"
    normalized_bg = {
        "type": bg_type,
        "color": _as_color(background.get("color"), DEFAULT_BACKGROUND_COLOR),
        "path": _as_str(background.get("path")),
        "fit": _as_fit(background.get("fit")),
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
                text="LG600 STUDIO",
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
                size=36,
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

    return {name: normalize_scene(scene) for name, scene in presets.items()}


def load_project(path: str) -> dict:
    """读取 ``.lg6.json`` 项目文件并返回规范化场景。

    文件不存在抛 :class:`FileNotFoundError`；JSON 非法或顶层不是对象时抛
    :class:`ValueError`（中文错误信息，GUI 可直接展示）。
    """
    if not path:
        raise ValueError("项目路径为空")
    with open(path, "r", encoding="utf-8-sig") as handle:
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
    """把场景规范化后写入 ``.lg6.json``（UTF-8、缩进 2、原子替换）。"""
    if not path:
        raise ValueError("项目路径为空")
    normalized = normalize_scene(scene)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
    fd, temp_path = tempfile.mkstemp(prefix=".lg6-", suffix=".json", dir=directory)
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
