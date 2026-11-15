"""示例插件：火花线（sparkline）。

把本文件复制到 ``%APPDATA%\\HydroHUD\\plugins\\`` 后重启 HydroHUD，就会多出一种
名为 ``sparkline`` 的图层类型：在一个矩形里画一条随时间流动的正弦波。

插件协议见 :mod:`hydrohud.plugins`：声明 ``PLUGIN`` 字典（或 ``register(reg)``），
其中 ``draw(image, layer, ctx) -> bool`` 在 480×480 的 RGB 图像上绘制。
"""

from __future__ import annotations

import math

from PIL import ImageDraw

WIDTH = 480
HEIGHT = 480


def _number(value, default: float) -> float:
    """把图层字段转成浮点；非法值退回默认值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def draw(image, layer, ctx):
    """在图层矩形里画流动正弦波；返回 True 表示本帧已绘制。"""
    x = max(0.0, min(1.0, _number(layer.get("x"), 0.05)))
    y = max(0.0, min(1.0, _number(layer.get("y"), 0.55)))
    w = max(0.02, min(1.0 - x, _number(layer.get("w"), 0.9)))
    h = max(0.02, min(1.0 - y, _number(layer.get("h"), 0.3)))
    thickness = max(1, int(_number(layer.get("thickness"), 3)))
    frequency = max(0.5, _number(layer.get("frequency"), 3.0))
    color = str(layer.get("color") or "#38BDF8")
    background = str(layer.get("background") or "#111827")

    left, top = int(x * WIDTH), int(y * HEIGHT)
    right, bottom = int((x + w) * WIDTH), int((y + h) * HEIGHT)
    if right - left < 4 or bottom - top < 4:
        return False

    t = _number(ctx.get("t"), 0.0)
    painter = ImageDraw.Draw(image)
    painter.rectangle((left, top, right, bottom), fill=background)
    inner_height = bottom - top
    middle = top + inner_height / 2.0
    amplitude = inner_height * 0.38
    points = []
    steps = max(2, min(240, right - left))
    for index in range(steps + 1):
        ratio = index / steps
        px = left + ratio * (right - left)
        py = middle + amplitude * math.sin(ratio * math.tau * frequency - t * 2.0)
        points.append((px, py))
    painter.line(points, fill=color, width=thickness, joint="curve")
    return True


PLUGIN = {
    "name": "sparkline",
    "label": "火花线",
    "description": "在矩形里画一条随时间流动的正弦波（示例插件）",
    "defaults": {
        "x": 0.05,
        "y": 0.55,
        "w": 0.9,
        "h": 0.3,
        "color": "#38BDF8",
        "background": "#111827",
        "thickness": 3,
        "frequency": 3,
    },
    "params": [
        {
            "key": "thickness",
            "label": "线宽",
            "type": "int",
            "default": 3,
            "min": 1,
            "max": 12,
        },
        {
            "key": "frequency",
            "label": "波的密度",
            "type": "float",
            "default": 3.0,
            "min": 0.5,
            "max": 12.0,
            "step": 0.5,
        },
        {"key": "color", "label": "线颜色", "type": "color", "default": "#38BDF8"},
        {"key": "background", "label": "底色", "type": "color", "default": "#111827"},
    ],
    "draw": draw,
}
