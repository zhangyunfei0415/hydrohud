"""LG600 Studio 的 Tkinter 图形界面。

启动方式：``python -m lg6studio``。

设计要点：

- 场景 dict 是唯一状态源。任何编辑都经 ``scenes.normalize_scene`` 归一化后，
  由后台渲染线程画成 480x480 预览图（约 15 fps），UI 线程只负责贴图；
- 推流线程是 daemon 线程：静态场景每 2 秒重推一帧（固件看门狗会熄灭屏幕），
  动态场景（时钟/动图/监控/进度条）按所选帧率逐帧渲染并推送；
- 设备只在用户点击动作按钮时 ``open()``。启动阶段只做 HID 枚举，不打开句柄，
  因此官方 TCOMAS 软件运行中也能正常启动本程序；
- ``render`` / ``scenes`` / ``device`` 任一模块缺失或损坏时界面仍能打开，
  相关能力在状态栏给出中文提示（优雅降级）。

``--smoke-shot <png>`` 供无人值守验证：启动窗口，1.2 秒后截取预览区，再 0.4 秒
后自动退出（退出码 0，失败也会打印原因）。
"""

from __future__ import annotations

import argparse
import copy
import importlib
import io
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
from functools import lru_cache
from typing import Any, Callable

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageFont, ImageTk

# --------------------------------------------------------------------------- #
# 可选依赖模块（由队友/构建提供，缺失时优雅降级）
# --------------------------------------------------------------------------- #
_LOAD_WARNINGS: list[str] = []
_WARNED: set[str] = set()


def _warn_once(message: str) -> None:
    """只在 stderr 打印一次同类告警，避免线程里每帧刷屏。"""
    if message in _WARNED:
        return
    _WARNED.add(message)
    print(f"[lg6studio] {message}", file=sys.stderr)


_TIMER_PERIOD_RAISED = False


def _raise_timer_resolution() -> None:
    """请求 1 ms 的 Windows 定时器精度。

    Windows 默认调度粒度约 15.6 ms，``Event.wait(1/30 s)`` 实际会睡 30-45 ms，
    使 30 fps 的推流目标只能跑到约 21 fps。``timeBeginPeriod(1)`` 把粒度降到 1 ms；
    进程退出时系统自动恢复，不需要配对调用。
    """
    global _TIMER_PERIOD_RAISED
    if _TIMER_PERIOD_RAISED or sys.platform != "win32":
        return
    _TIMER_PERIOD_RAISED = True
    try:
        import ctypes

        ctypes.WinDLL("winmm").timeBeginPeriod(1)
    except Exception as exc:  # 定时器精度提升失败只影响帧率，不影响功能
        _warn_once(f"无法提升定时器精度，帧率可能低于目标：{exc}")


def _import_optional(module_name: str) -> Any:
    """先按包内相对导入，再按顶层绝对导入加载可选模块。"""
    if __package__ not in (None, ""):
        candidates: list[tuple[str, str | None]] = [(f".{module_name}", __package__)]
    else:
        candidates = []
    candidates.append((module_name, None))
    # 直接运行 app.py（而不是 python -m lg6studio）时把包目录加入搜索路径。
    if __package__ in (None, ""):
        package_dir = os.path.dirname(os.path.abspath(__file__))
        if package_dir not in sys.path:
            sys.path.insert(0, package_dir)
    first_error: Exception | None = None
    for name, package in candidates:
        try:
            return importlib.import_module(name, package)
        except Exception as exc:  # 模块缺失、语法未完成、可选依赖缺失都不应阻止 GUI 启动
            if first_error is None:
                first_error = exc
    if first_error is not None:
        _LOAD_WARNINGS.append(f"lg6studio.{module_name} 加载失败：{first_error}")
    return None


device_mod = _import_optional("device")
render_mod = _import_optional("render")
scenes_mod = _import_optional("scenes")
sysmon_mod = _import_optional("sysmon")

try:  # 版本号来自包 __init__，缺失时退回内置值
    from . import __version__ as APP_VERSION
except Exception:  # pragma: no cover - 直接运行 app.py 时没有包上下文
    APP_VERSION = "1.0.0"

SCREEN_SIZE = 480
MAX_JPEG_BYTES = int(getattr(device_mod, "MAX_FRAME_BYTES", 15300)) if device_mod else 15300
STATIC_REFRESH_SECONDS = 2.0
PREVIEW_INTERVAL_MS = 60
PREVIEW_STREAMING_INTERVAL_MS = 220
RECONNECT_HINT = "提示：官方 TCOMAS 软件（DeviceDriver.exe）运行时设备被占用，请先退出官方软件。"

# --------------------------------------------------------------------------- #
# 主题
# --------------------------------------------------------------------------- #
FONT_FAMILY = "Microsoft YaHei UI"
BG = "#0B0F14"
CARD = "#111827"
CARD_HI = "#172033"
CARD_2 = "#0F172A"
BORDER = "#1F2A37"
TEXT = "#E5E7EB"
MUTED = "#94A3B8"
ACCENT = "#38BDF8"
ACCENT_2 = "#22D3EE"
OK = "#34D399"
WARN = "#FBBF24"
ERR = "#F87171"
BTN = "#1B2434"
BTN_HOVER = "#243247"
BTN_ACTIVE = "#0E1622"
ENTRY_BG = "#0A1017"
SELECT_BG = "#0E4A63"
DARK_INK = "#04121C"

BG_TYPE_LABELS = ("纯色", "图片", "GIF 动图", "视频")
BG_TYPE_BY_LABEL = {"纯色": "solid", "图片": "image", "GIF 动图": "gif", "视频": "video"}
BG_LABEL_BY_TYPE = {value: label for label, value in BG_TYPE_BY_LABEL.items()}
FIT_LABELS = ("裁剪填满", "完整显示", "拉伸铺满")
FIT_BY_LABEL = {"裁剪填满": "crop", "完整显示": "fit", "拉伸铺满": "stretch"}
FIT_LABEL_BY_CODE = {value: label for label, value in FIT_BY_LABEL.items()}

IMG_FILETYPES = [
    ("图片", "*.png *.jpg *.jpeg *.webp *.bmp"),
    ("GIF 动图", "*.gif *.webp"),
    ("视频", "*.mp4 *.avi *.webm *.mov *.mkv"),
    ("所有文件", "*.*"),
]
PROJECT_FILETYPES = [("LG600 项目", "*.lg6.json"), ("JSON 文件", "*.json"), ("所有文件", "*.*")]

try:  # Pillow 12 里 LANCZOS 位于 Resampling 下
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover - 老版本 Pillow
    _RESAMPLE = Image.LANCZOS

_RENDER_LOCK = threading.Lock()
"""渲染模块可能不是线程安全的：预览线程与推流线程串行调用 `render_scene`。"""


# --------------------------------------------------------------------------- #
# 小工具：字体、占位图、错误文案、场景判断
# --------------------------------------------------------------------------- #
_PIL_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
)


@lru_cache(maxsize=40)
def _pil_font(size: int, bold: bool = False) -> Any:
    """加载可显示中文的 TrueType 字体，失败时退回 Pillow 内置位图字体。"""
    size = max(8, int(size))
    candidates = _PIL_FONT_CANDIDATES
    if bold:
        candidates = (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc") + candidates[2:]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: Any, max_width: float) -> list[str]:
    """按像素宽度逐字换行（中文没有空格，不能按词切）。"""
    lines: list[str] = []
    current = ""
    for char in text:
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        if current and draw.textlength(current + char, font=font) > max_width:
            lines.append(current)
            current = char
        else:
            current += char
    lines.append(current)
    return lines[:5]


@lru_cache(maxsize=24)
def _placeholder_image(lines: tuple[str, ...], side: int = SCREEN_SIZE) -> Image.Image:
    """生成暗色占位图：没有渲染引擎或渲染失败时预览区不出现空洞。"""
    side = int(side)
    image = Image.new("RGB", (side, side), BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, side - 1, side - 1), outline=BORDER)
    draw.rectangle((0, 0, side, max(3, side // 100)), fill=ACCENT)
    title_font = _pil_font(max(15, side // 24), bold=True)
    body_font = _pil_font(max(11, side // 34))
    y = side * 0.36
    if lines:
        draw.text((side / 2, y), lines[0], font=title_font, fill=TEXT, anchor="mm")
        y += side * 0.09
    for raw_line in lines[1:]:
        for wrapped in _wrap_text(draw, raw_line, body_font, side * 0.82):
            draw.text((side / 2, y), wrapped, font=body_font, fill=MUTED, anchor="mm")
            y += side * 0.055
    draw.text((side - 14, side - 14), "LG600 Studio", font=body_font, fill="#334155", anchor="rb")
    return image


def make_placeholder(message: str) -> Image.Image:
    """把中文提示渲染成 480x480 占位图。"""
    return _placeholder_image(tuple(str(message).split("\n")), SCREEN_SIZE)


def friendly_device_error(exc: BaseException) -> str:
    """把 device.py 的英文异常翻译成可执行的中文状态栏提示。"""
    text = str(exc).strip() or exc.__class__.__name__
    low = text.lower()
    if "device.py" in text or "设备模块" in text:
        return "设备模块未就绪：lg6studio/device.py 缺失或加载失败。"
    if (
        "cannot open" in low
        or "error 32" in low
        or "error 5" in low
        or "access" in low
        or "busy" in low
        or "permission" in low
        or "拒绝访问" in text
    ):
        return "无法打开设备：已被官方 TCOMAS 软件或其它程序占用。" + RECONNECT_HINT
    if "no hid device" in low or "not found" in low or "no 1025" in low or "no 441" in low:
        return "未找到 TCOMAS LG600 屏幕：请确认水冷屏 USB 线已连接并已上电。"
    if "did not answer" in low:
        return "设备没有响应：请重新插拔屏幕 USB，或在官方软件里确认屏幕正常。"
    if "writefile failed" in low:
        return "写入设备失败：USB 连接可能已断开，请重新插拔后重试。"
    if len(text) > 180:
        text = text[:177] + "..."
    return f"设备操作失败：{text}"


def scene_is_dynamic(scene: dict) -> bool:
    """时钟/监控/进度条/动图/视频需要逐帧渲染，其余静态场景可 2 秒重推一次。"""
    background = scene.get("background") or {}
    if isinstance(background, dict) and background.get("type") in ("gif", "video"):
        return True
    for layer in scene.get("layers") or []:
        if isinstance(layer, dict) and layer.get("type") in ("clock", "sysinfo", "bars"):
            return True
    return False


def layer_label(layer: dict) -> str:
    """图层列表里显示的一行文案。"""
    kind = layer.get("type")
    if kind == "text":
        text = " ".join(str(layer.get("text") or "").split())
        return f"文字 · {text[:14]}" if text else "文字 · (空文本)"
    if kind == "clock":
        return f"时钟 · {layer.get('format') or '%H:%M'}"
    if kind == "sysinfo":
        return "系统监控 · " + "/".join(str(item) for item in layer.get("fields") or ["cpu", "ram"])
    if kind == "bars":
        return "进度条 · " + "/".join(str(item) for item in layer.get("items") or ["cpu", "ram"])
    if kind == "image":
        path = str(layer.get("path") or "")
        return f"图片 · {os.path.basename(path) if path else '未选择'}"
    return f"图层 · {kind}"


def _safe_float(
    value: Any, default: float, low: float | None = None, high: float | None = None
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


# --------------------------------------------------------------------------- #
# 场景归一化 / 预设 / 项目文件（scenes.py 缺席时的兜底实现）
# --------------------------------------------------------------------------- #
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _norm_color(value: Any, default: str) -> str:
    if isinstance(value, str) and _HEX_COLOR.match(value):
        return value.upper()
    return default


def _fallback_normalize(scene: Any) -> dict:
    """在不依赖 scenes.py 的情况下，按 scene schema v1 补全默认值。"""
    raw = copy.deepcopy(scene) if isinstance(scene, dict) else {}
    name = str(raw.get("name") or "未命名场景")
    bg_raw = raw.get("background") if isinstance(raw.get("background"), dict) else {}
    bg_type = bg_raw.get("type") if bg_raw.get("type") in BG_LABEL_BY_TYPE else "solid"
    background = {
        "type": bg_type,
        "color": _norm_color(bg_raw.get("color"), BG),
        "path": str(bg_raw.get("path") or ""),
        "fit": bg_raw.get("fit") if bg_raw.get("fit") in FIT_LABEL_BY_CODE else "crop",
    }
    tint_raw = raw.get("tint") if isinstance(raw.get("tint"), dict) else {}
    tint = {
        "color": _norm_color(tint_raw.get("color"), "#000000"),
        "alpha": _safe_float(tint_raw.get("alpha"), 0.0, 0.0, 1.0),
    }
    allowed_anchors = (
        "center",
        "top-left",
        "top",
        "top-right",
        "left",
        "right",
        "bottom",
        "bottom-left",
        "bottom-right",
    )
    layers: list[dict] = []
    for item in raw.get("layers") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        base = {
            "x": _safe_float(item.get("x"), 0.5, 0.0, 1.0),
            "y": _safe_float(item.get("y"), 0.5, 0.0, 1.0),
        }
        if kind == "text":
            layers.append(
                {
                    "type": "text",
                    "text": str(item.get("text") or ""),
                    **base,
                    "anchor": (
                        item.get("anchor") if item.get("anchor") in allowed_anchors else "center"
                    ),
                    "size": int(_safe_float(item.get("size"), 56, 8, 400)),
                    "color": _norm_color(item.get("color"), "#FFFFFF"),
                    "bold": bool(item.get("bold", True)),
                    "align": (
                        item.get("align")
                        if item.get("align") in ("center", "left", "right")
                        else "center"
                    ),
                    "shadow": bool(item.get("shadow", True)),
                }
            )
        elif kind == "clock":
            layers.append(
                {
                    "type": "clock",
                    "format": str(item.get("format") or "%H:%M"),
                    "show_date": bool(item.get("show_date", False)),
                    "date_format": str(item.get("date_format") or "%Y-%m-%d"),
                    **base,
                    "size": int(_safe_float(item.get("size"), 120, 8, 400)),
                    "color": _norm_color(item.get("color"), "#FFFFFF"),
                    "bold": bool(item.get("bold", True)),
                    "shadow": bool(item.get("shadow", True)),
                }
            )
        elif kind == "sysinfo":
            fields = item.get("fields")
            if not isinstance(fields, list) or not fields:
                fields = ["cpu", "ram"]
            layers.append(
                {
                    "type": "sysinfo",
                    "fields": [str(field) for field in fields],
                    **base,
                    "size": int(_safe_float(item.get("size"), 30, 8, 400)),
                    "color": _norm_color(item.get("color"), "#8FD3FF"),
                    "shadow": bool(item.get("shadow", True)),
                }
            )
        elif kind == "bars":
            items = item.get("items")
            if not isinstance(items, list) or not items:
                items = ["cpu", "ram"]
            layers.append(
                {
                    "type": "bars",
                    "items": [str(entry) for entry in items],
                    "x": _safe_float(item.get("x"), 0.08, 0.0, 1.0),
                    "y": _safe_float(item.get("y"), 0.62, 0.0, 1.0),
                    "w": _safe_float(item.get("w"), 0.84, 0.0, 1.0),
                    "h": _safe_float(item.get("h"), 0.10, 0.0, 1.0),
                    "color": _norm_color(item.get("color"), "#38BDF8"),
                    "color2": _norm_color(item.get("color2"), "#F472B6"),
                    "bg": _norm_color(item.get("bg"), "#1E293B"),
                    "label": bool(item.get("label", True)),
                    "size": int(_safe_float(item.get("size"), 22, 8, 400)),
                }
            )
        elif kind == "image":
            layers.append(
                {
                    "type": "image",
                    "path": str(item.get("path") or ""),
                    **base,
                    "scale": _safe_float(item.get("scale"), 0.5, 0.01, 1.0),
                    "opacity": _safe_float(item.get("opacity"), 1.0, 0.0, 1.0),
                }
            )
    return {"name": name, "background": background, "tint": tint, "layers": layers}


def _fallback_presets() -> dict[str, dict]:
    """scenes.py 缺席时的最小可用预设（结构与 schema v1 一致）。"""

    def solid(color: str = BG) -> dict:
        return {"type": "solid", "color": color, "path": "", "fit": "crop"}

    def base(name: str, background: dict, layers: list[dict], tint_alpha: float = 0.0) -> dict:
        return {
            "name": name,
            "background": background,
            "tint": {"color": "#000000", "alpha": tint_alpha},
            "layers": layers,
        }

    return {
        "霓虹时钟": base(
            "霓虹时钟",
            solid(),
            [
                {
                    "type": "clock",
                    "format": "%H:%M",
                    "show_date": False,
                    "date_format": "%Y-%m-%d",
                    "x": 0.5,
                    "y": 0.46,
                    "size": 128,
                    "color": "#F8FAFC",
                    "bold": True,
                    "shadow": True,
                },
                {
                    "type": "text",
                    "text": "TCOMAS LG600",
                    "x": 0.5,
                    "y": 0.68,
                    "anchor": "center",
                    "size": 26,
                    "color": ACCENT,
                    "bold": False,
                    "align": "center",
                    "shadow": True,
                },
            ],
        ),
        "系统监控": base(
            "系统监控",
            solid(),
            [
                {
                    "type": "sysinfo",
                    "fields": ["cpu", "ram"],
                    "x": 0.5,
                    "y": 0.28,
                    "size": 30,
                    "color": "#8FD3FF",
                    "shadow": True,
                },
                {
                    "type": "bars",
                    "items": ["cpu", "ram"],
                    "x": 0.08,
                    "y": 0.56,
                    "w": 0.84,
                    "h": 0.10,
                    "color": "#38BDF8",
                    "color2": "#F472B6",
                    "bg": "#1E293B",
                    "label": True,
                    "size": 22,
                },
            ],
        ),
        "极简日期": base(
            "极简日期",
            solid(),
            [
                {
                    "type": "clock",
                    "format": "%H:%M",
                    "show_date": True,
                    "date_format": "%Y-%m-%d",
                    "x": 0.5,
                    "y": 0.44,
                    "size": 112,
                    "color": "#FFFFFF",
                    "bold": True,
                    "shadow": True,
                },
            ],
        ),
        "双色进度条": base(
            "双色进度条",
            solid(),
            [
                {
                    "type": "text",
                    "text": "PERFORMANCE",
                    "x": 0.5,
                    "y": 0.24,
                    "anchor": "center",
                    "size": 30,
                    "color": ACCENT_2,
                    "bold": True,
                    "align": "center",
                    "shadow": True,
                },
                {
                    "type": "bars",
                    "items": ["cpu", "ram"],
                    "x": 0.10,
                    "y": 0.44,
                    "w": 0.80,
                    "h": 0.12,
                    "color": "#38BDF8",
                    "color2": "#F472B6",
                    "bg": "#1E293B",
                    "label": True,
                    "size": 22,
                },
                {
                    "type": "sysinfo",
                    "fields": ["cpu", "ram"],
                    "x": 0.5,
                    "y": 0.78,
                    "size": 26,
                    "color": "#F8FAFC",
                    "shadow": True,
                },
            ],
        ),
        "图片画廊": base(
            "图片画廊",
            {"type": "image", "color": BG, "path": "", "fit": "crop"},
            [
                {
                    "type": "text",
                    "text": "在「背景与叠加」页选择图片",
                    "x": 0.5,
                    "y": 0.86,
                    "anchor": "center",
                    "size": 22,
                    "color": "#FFFFFF",
                    "bold": False,
                    "align": "center",
                    "shadow": True,
                },
            ],
        ),
        "动态 GIF": base(
            "动态 GIF",
            {"type": "gif", "color": BG, "path": "", "fit": "crop"},
            [
                {
                    "type": "clock",
                    "format": "%H:%M",
                    "show_date": False,
                    "date_format": "%Y-%m-%d",
                    "x": 0.5,
                    "y": 0.12,
                    "size": 54,
                    "color": "#FFFFFF",
                    "bold": True,
                    "shadow": True,
                },
            ],
        ),
    }


# --------------------------------------------------------------------------- #
# 预览渲染线程
# --------------------------------------------------------------------------- #
class RenderWorker:
    """后台预览渲染线程：只保留最新请求，结果通过队列交给 UI 线程。"""

    def __init__(self, app: "App") -> None:
        self.app = app
        self.results: queue.Queue = queue.Queue(maxsize=4)
        self._cond = threading.Condition()
        self._request: tuple[dict, float, int] | None = None
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True, name="lg6-preview")
        self._thread.start()

    def update(self, scene: dict, t: float, side: int) -> None:
        with self._cond:
            self._request = (scene, t, side)
            self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify()

    def _loop(self) -> None:
        while True:
            with self._cond:
                while self._request is None and not self._stop:
                    self._cond.wait(0.4)
                if self._stop:
                    return
                request = self._request
                self._request = None
            assert request is not None
            scene, t, _side = request
            started = time.perf_counter()
            try:
                image = self.app.render_frame(scene, t)
                if image.size != (SCREEN_SIZE, SCREEN_SIZE):
                    image = image.convert("RGB").resize((SCREEN_SIZE, SCREEN_SIZE), _RESAMPLE)
                jpeg_len: int | None
                jpeg_error = ""
                try:
                    jpeg_len = len(self.app.encode_frame(image))
                except Exception as exc:  # JPEG 压不进 15300 字节也要继续显示预览
                    jpeg_len = None
                    jpeg_error = str(exc)
                item: tuple = (
                    "ok",
                    image,
                    jpeg_len,
                    jpeg_error,
                    (time.perf_counter() - started) * 1000.0,
                )
            except Exception as exc:
                message = str(exc).strip() or exc.__class__.__name__
                if len(message) > 140:
                    message = message[:137] + "..."
                image = _placeholder_image(("预览渲染失败", message), SCREEN_SIZE)
                item = ("err", image, None, message, (time.perf_counter() - started) * 1000.0)
            self._put(item)

    def _put(self, item: tuple) -> None:
        try:
            self.results.put_nowait(item)
        except queue.Full:
            try:
                self.results.get_nowait()
            except queue.Empty:
                pass
            try:
                self.results.put_nowait(item)
            except queue.Full:
                pass


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    """LG600 Studio 主窗口：设备控制、实时预览、场景编辑、项目文件。"""

    def __init__(self, smoke_shot: str | None = None) -> None:
        super().__init__()
        self.smoke_shot = smoke_shot
        _raise_timer_resolution()
        self._closing = False
        self._scene_lock = threading.RLock()
        self._ctl_lock = threading.Lock()
        self._control: Any | None = None
        self._detected: list[dict] | None = None
        self._stream_stop = threading.Event()
        self._stream_thread: threading.Thread | None = None
        self._stream_fps = 30.0
        self._stream_frames = 0
        self._stream_started_at = 0.0
        self._photo: ImageTk.PhotoImage | None = None
        self._canvas_img_id: int | None = None
        self._last_frame_img: Image.Image | None = None
        self._last_render_ms = 0.0
        self._last_preview_error = ""
        self._preview_side = SCREEN_SIZE
        self._layer_index = -1
        self._suppress_layer_event = False
        self._suppress_preset_event = False
        self._syncing = False
        self._dirty = False
        self.project_path: str | None = None
        self._current_preset_name: str | None = None
        self._preset_cache: dict[str, dict] | None = None
        self._timeline_start = time.monotonic()
        self._events: queue.Queue = queue.Queue()
        self._status_label: tk.Label | None = None
        self._conn_label: tk.Label | None = None
        self._path_label: tk.Label | None = None
        self._frames_label: tk.Label | None = None
        self._bright_value: tk.Label | None = None
        self._jpeg_label: tk.Label | None = None
        self._preview_render_label: tk.Label | None = None
        self._device_reply: tk.Label | None = None
        self._dev_iface_label: tk.Label | None = None
        self._device_hint: tk.Label | None = None
        self._tips_card: tk.Frame | None = None
        self._tips_visible = True
        self._compact_mode = False
        self._layer_up_btn: ttk.Button | None = None
        self._layer_down_btn: ttk.Button | None = None
        self._layer_delete_btn: ttk.Button | None = None

        self.scene: dict = self._initial_scene()

        self.title("LG600 Studio — TCOMAS LG600 开源控制台")
        self.geometry("1180x780")
        self.minsize(1080, 700)
        self.configure(bg=BG)
        self.report_callback_exception = self._on_tk_callback_error

        self._build_theme()
        self._build_menu()
        self._build_ui()
        self._refresh_presets()
        self._sync_ui_from_scene()
        self._show_preview_image(make_placeholder("正在初始化预览…"))
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Configure>", self._on_root_configure, add="+")

        self.bind_all("<Control-n>", lambda _event: self._on_new_project())
        self.bind_all("<Control-o>", lambda _event: self._on_open_project())
        self.bind_all("<Control-s>", lambda _event: self._on_save_project())
        self.bind_all("<Control-Shift-S>", lambda _event: self._on_save_project_as())

        self._preview_worker = RenderWorker(self)
        self.after(PREVIEW_INTERVAL_MS, self._preview_tick)
        self.after(40, self._poll_render)
        self.after(80, self._poll_events)
        self.after(500, lambda: self._detect_device(auto=True))

        if self.smoke_shot:
            self._schedule_smoke()
        if _LOAD_WARNINGS:
            detail = "；".join(_LOAD_WARNINGS)
            print(f"[lg6studio] 可选模块告警：{detail}", file=sys.stderr)

    # ------------------------------------------------------------------ #
    # 初始化：主题 / 菜单 / 布局
    # ------------------------------------------------------------------ #
    def _build_theme(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        base_font = (FONT_FAMILY, 10)
        for font_name, size in (
            ("TkDefaultFont", 10),
            ("TkTextFont", 10),
            ("TkMenuFont", 10),
            ("TkHeadingFont", 10),
        ):
            try:
                tkfont.nametofont(font_name).configure(family=FONT_FAMILY, size=size)
            except tk.TclError:
                pass
        style.configure(".", background=BG, foreground=TEXT, font=base_font)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT, font=base_font)
        style.configure(
            "TButton",
            background=BTN,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=BTN,
            darkcolor=BTN,
            focuscolor=BG,
            borderwidth=1,
            focusthickness=0,
            padding=(10, 6),
            font=base_font,
        )
        style.map(
            "TButton",
            background=[("pressed", BTN_ACTIVE), ("active", BTN_HOVER), ("disabled", "#141A24")],
            foreground=[("disabled", "#4B5563")],
            bordercolor=[("active", ACCENT)],
        )
        style.configure(
            "Small.TButton",
            background=BTN,
            foreground=TEXT,
            bordercolor=BORDER,
            padding=(6, 3),
            font=(FONT_FAMILY, 9),
        )
        style.map(
            "Small.TButton",
            background=[("pressed", BTN_ACTIVE), ("active", BTN_HOVER), ("disabled", "#141A24")],
            foreground=[("disabled", "#4B5563")],
        )
        style.configure(
            "Ghost.TButton",
            background=CARD,
            foreground=MUTED,
            bordercolor=BORDER,
            padding=(8, 5),
            font=(FONT_FAMILY, 9),
        )
        style.map(
            "Ghost.TButton",
            background=[("pressed", BTN_ACTIVE), ("active", CARD_HI)],
            foreground=[("active", TEXT)],
        )
        style.configure(
            "Accent.TButton",
            background=ACCENT,
            foreground=DARK_INK,
            bordercolor=ACCENT,
            padding=(14, 7),
            font=(FONT_FAMILY, 10, "bold"),
        )
        style.map(
            "Accent.TButton",
            background=[("pressed", "#0284C7"), ("active", "#7DD3FC"), ("disabled", "#1E3A4A")],
            foreground=[("disabled", "#6B7A86")],
        )
        style.configure(
            "Danger.TButton",
            background="#3B1220",
            foreground="#FCA5A5",
            bordercolor="#7F1D1D",
            padding=(6, 3),
            font=(FONT_FAMILY, 9),
        )
        style.map(
            "Danger.TButton",
            background=[("pressed", "#4C1D24"), ("active", "#4C1D24")],
            foreground=[("active", "#FECACA")],
        )
        style.configure(
            "TCombobox",
            fieldbackground=ENTRY_BG,
            background=BTN,
            foreground=TEXT,
            arrowcolor=MUTED,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            insertcolor=TEXT,
            selectbackground=ACCENT,
            selectforeground=DARK_INK,
            padding=(6, 3),
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", ENTRY_BG), ("disabled", "#141A24")],
            foreground=[("readonly", TEXT), ("disabled", "#4B5563")],
            background=[("active", BTN_HOVER)],
        )
        style.configure(
            "TEntry",
            fieldbackground=ENTRY_BG,
            foreground=TEXT,
            insertcolor=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=(6, 4),
        )
        style.map(
            "TEntry",
            fieldbackground=[("disabled", "#141A24")],
            foreground=[("disabled", "#4B5563")],
        )
        style.configure(
            "TScale",
            background=CARD,
            troughcolor=CARD_2,
            bordercolor=BORDER,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            troughrelief="flat",
            sliderlength=16,
        )
        style.map("TScale", background=[("active", ACCENT)], sliderrelief=[("active", "flat")])
        style.configure("TNotebook", background=CARD, borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure(
            "TNotebook.Tab",
            background=CARD,
            foreground=MUTED,
            bordercolor=BORDER,
            lightcolor=CARD,
            darkcolor=CARD,
            padding=(14, 7),
            font=(FONT_FAMILY, 9),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", CARD_HI), ("active", CARD_HI)],
            foreground=[("selected", ACCENT), ("active", TEXT)],
        )
        style.configure("TSeparator", background=BORDER)
        style.configure(
            "Vertical.TScrollbar",
            background=BTN,
            troughcolor=CARD,
            bordercolor=CARD,
            arrowcolor=MUTED,
            lightcolor=BTN,
            darkcolor=BTN,
            width=10,
        )
        style.map("Vertical.TScrollbar", background=[("active", BTN_HOVER)])
        self.option_add("*TCombobox*Listbox.background", ENTRY_BG)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", DARK_INK)
        self.option_add("*TCombobox*Listbox.font", base_font)

    def _build_menu(self) -> None:
        menu_options = dict(
            bg=CARD,
            fg=TEXT,
            activebackground=ACCENT,
            activeforeground=DARK_INK,
            bd=0,
            tearoff=0,
            font=(FONT_FAMILY, 10),
        )
        menubar = tk.Menu(self, **menu_options)
        file_menu = tk.Menu(menubar, **menu_options)
        file_menu.add_command(label="新建项目", accelerator="Ctrl+N", command=self._on_new_project)
        file_menu.add_command(
            label="打开项目…", accelerator="Ctrl+O", command=self._on_open_project
        )
        file_menu.add_separator()
        file_menu.add_command(label="保存项目", accelerator="Ctrl+S", command=self._on_save_project)
        file_menu.add_command(
            label="另存为…", accelerator="Ctrl+Shift+S", command=self._on_save_project_as
        )
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.on_close)
        menubar.add_cascade(label="文件", menu=file_menu)

        device_menu = tk.Menu(menubar, **menu_options)
        device_menu.add_command(label="检测设备（只枚举，不打开）", command=self._on_detect)
        device_menu.add_command(label="读取设备状态", command=self._on_query)
        device_menu.add_separator()
        device_menu.add_command(label="开始推送到屏幕", command=self._on_start_stream)
        device_menu.add_command(label="停止推送", command=self._on_stop_stream)
        menubar.add_cascade(label="设备", menu=device_menu)

        help_menu = tk.Menu(menubar, **menu_options)
        help_menu.add_command(label="关于 LG600 Studio", command=self._on_about)
        menubar.add_cascade(label="帮助", menu=help_menu)
        self.configure(menu=menubar)

    def _build_ui(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        header = tk.Frame(self, bg=BG)
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 4))
        brand = tk.Frame(header, bg=BG)
        brand.pack(side="left")
        tk.Label(brand, text="LG600 Studio", bg=BG, fg=TEXT, font=(FONT_FAMILY, 18, "bold")).pack(
            anchor="w"
        )
        tk.Label(
            brand,
            text="TCOMAS LG600 水冷屏 · 开源控制台 · MIT License",
            bg=BG,
            fg=MUTED,
            font=(FONT_FAMILY, 9),
        ).pack(anchor="w")
        self._header_hint = tk.Label(
            header,
            text=f"v{APP_VERSION} · 启动不会连接设备",
            bg=BG,
            fg=MUTED,
            font=(FONT_FAMILY, 9),
        )
        self._header_hint.pack(side="right")

        body = tk.Frame(self, bg=BG)
        body.grid(row=1, column=0, sticky="nsew", padx=12, pady=8)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1, minsize=360)

        left = tk.Frame(body, bg=BG, width=268)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.grid_propagate(False)
        middle = tk.Frame(body, bg=BG)
        middle.grid(row=0, column=1, sticky="nsew")
        middle.grid_rowconfigure(0, weight=1)
        middle.grid_columnconfigure(0, weight=1)
        right = tk.Frame(body, bg=BG, width=392)
        right.grid(row=0, column=2, sticky="nsew", padx=(12, 0))
        right.grid_propagate(False)

        self._build_device_panel(left)
        self._build_preview_panel(middle)
        self._build_editor_panel(right)
        self._build_bottom_bar()

    # -- 通用控件 ------------------------------------------------------ #
    def _label(
        self,
        parent: tk.Widget,
        text: str,
        *,
        color: str = TEXT,
        size: int = 10,
        bold: bool = False,
        wrap: int = 0,
        anchor: str = "w",
        bg: str = CARD,
    ) -> tk.Label:
        font = (FONT_FAMILY, size, "bold") if bold else (FONT_FAMILY, size)
        return tk.Label(
            parent,
            text=text,
            bg=bg,
            fg=color,
            font=font,
            anchor=anchor,
            justify="left",
            wraplength=wrap,
        )

    @staticmethod
    def _card(parent: tk.Widget, title: str, subtitle: str = "") -> tuple[tk.Frame, tk.Frame]:
        """卡片式分区：暗色底 + 1px 边框 + 标题（左侧强调色竖条）。"""
        card = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER, bd=0)
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x", padx=14, pady=(12, 6))
        tk.Frame(head, bg=ACCENT, width=3, height=14).pack(side="left", padx=(0, 8))
        tk.Label(head, text=title, bg=CARD, fg=TEXT, font=(FONT_FAMILY, 11, "bold")).pack(
            side="left"
        )
        if subtitle:
            tk.Label(head, text=subtitle, bg=CARD, fg=MUTED, font=(FONT_FAMILY, 9)).pack(
                side="right"
            )
        body = tk.Frame(card, bg=CARD)
        body.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        return card, body

    def _dark_listbox(self, parent: tk.Widget, height: int = 6) -> tk.Listbox:
        return tk.Listbox(
            parent,
            bg=ENTRY_BG,
            fg=TEXT,
            selectbackground=SELECT_BG,
            selectforeground="#FFFFFF",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            bd=0,
            relief="flat",
            activestyle="none",
            font=(FONT_FAMILY, 10),
            height=height,
            exportselection=False,
        )

    @staticmethod
    def _attach_scrollbar(listbox: tk.Listbox, parent: tk.Widget) -> ttk.Scrollbar:
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=scrollbar.set)
        return scrollbar

    def _scrollable_content(self, tab: tk.Frame) -> tk.Frame:
        """标签页内容容器：窗口变小时内部滚动，而不是被裁切。"""
        canvas = tk.Canvas(tab, bg=CARD, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        wrapper = tk.Frame(canvas, bg=CARD)
        window = canvas.create_window((0, 0), window=wrapper, anchor="nw")
        inner = tk.Frame(wrapper, bg=CARD)
        inner.pack(fill="both", expand=True, padx=10, pady=10)

        def _sync_scrollregion(_event: tk.Event) -> None:
            bbox = canvas.bbox("all")
            if bbox is None:
                return
            canvas.configure(scrollregion=bbox)
            needs_bar = bbox[3] - bbox[1] > canvas.winfo_height() + 1
            if needs_bar and not scrollbar.winfo_ismapped():
                scrollbar.pack(side="right", fill="y", before=canvas)
            elif not needs_bar and scrollbar.winfo_ismapped():
                scrollbar.pack_forget()

        def _sync_window(event: tk.Event) -> None:
            canvas.itemconfigure(window, width=event.width)
            height = max(event.height, wrapper.winfo_reqheight())
            if abs(int(float(canvas.itemcget(window, "height") or 0)) - height) > 1:
                canvas.itemconfigure(window, height=height)

        def _wheel(event: tk.Event) -> str:
            canvas.yview_scroll(-int(event.delta / 120), "units")
            return "break"

        def _on_canvas_configure(event: tk.Event) -> None:
            _sync_window(event)
            _sync_scrollregion(event)

        wrapper.bind("<Configure>", _sync_scrollregion)
        canvas.bind("<Configure>", _on_canvas_configure)
        for widget in (canvas, wrapper, inner):
            widget.bind("<MouseWheel>", _wheel)
        return inner

    # -- 左栏：设备 ---------------------------------------------------- #
    def _build_device_panel(self, parent: tk.Frame) -> None:
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        card, body = self._card(parent, "设备")
        card.grid(row=0, column=0, sticky="nsew")

        self._conn_label = tk.Label(
            body, text="● 未连接", bg=CARD, fg=MUTED, font=(FONT_FAMILY, 11, "bold"), anchor="w"
        )
        self._conn_label.pack(fill="x")
        self._label(body, "TCOMAS LG600 · 1A2C:1520", size=9, color=TEXT).pack(
            fill="x", pady=(4, 0)
        )
        self._dev_iface_label = self._label(body, "接口：尚未检测", size=9, color=MUTED, wrap=224)
        self._dev_iface_label.pack(fill="x", pady=(2, 8))

        detect_row = tk.Frame(body, bg=CARD)
        detect_row.pack(fill="x")
        ttk.Button(
            detect_row, text="检测设备", style="Small.TButton", command=self._on_detect
        ).pack(side="left")
        self._label(detect_row, "只枚举，不打开", size=8, color=MUTED).pack(
            side="left", padx=(6, 0)
        )

        ttk.Separator(body, orient="horizontal").pack(fill="x", pady=10)

        bright_head = tk.Frame(body, bg=CARD)
        bright_head.pack(fill="x")
        self._label(bright_head, "背光亮度", size=9, color=MUTED).pack(side="left")
        self._bright_value = self._label(bright_head, "100", size=9, color=ACCENT, bold=True)
        self._bright_value.pack(side="right")
        self._bright_var = tk.DoubleVar(value=100.0)
        self._bright_scale = ttk.Scale(
            body, from_=0, to=100, variable=self._bright_var, command=self._on_brightness_move
        )
        self._bright_scale.pack(fill="x", pady=(2, 4))
        self._bright_scale.bind("<ButtonRelease-1>", self._on_brightness_release)

        power_row = tk.Frame(body, bg=CARD)
        power_row.pack(fill="x", pady=(6, 0))
        power_row.grid_columnconfigure(0, weight=1)
        power_row.grid_columnconfigure(1, weight=1)
        ttk.Button(
            power_row,
            text="打开屏幕",
            style="Small.TButton",
            command=lambda: self._control_job(
                "power-on", lambda control: control.set_power(True), "已发送开屏指令"
            ),
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(
            power_row,
            text="关闭屏幕",
            style="Small.TButton",
            command=lambda: self._control_job(
                "power-off", lambda control: control.set_power(False), "已发送关屏指令"
            ),
        ).grid(row=0, column=1, sticky="ew", padx=(4, 0))

        rotate_row = tk.Frame(body, bg=CARD)
        rotate_row.pack(fill="x", pady=(10, 0))
        self._label(rotate_row, "屏幕方向", size=9, color=MUTED).pack(side="left")
        self._rotate_var = tk.StringVar(value="0°")
        rotate_combo = ttk.Combobox(
            rotate_row,
            textvariable=self._rotate_var,
            values=("0°", "90°", "180°", "270°"),
            width=6,
            state="readonly",
        )
        rotate_combo.pack(side="right")
        rotate_combo.bind("<<ComboboxSelected>>", self._on_rotation_selected)
        self._label(body, "仅保存到设备配置，不会旋转推送画面", size=8, color=MUTED, wrap=224).pack(
            fill="x", pady=(2, 0)
        )

        ttk.Button(body, text="读取设备状态", style="Ghost.TButton", command=self._on_query).pack(
            fill="x", pady=(10, 0)
        )
        self._device_reply = self._label(body, "设备回复：—", size=8, color=MUTED, wrap=224)
        self._device_reply.pack(fill="x", pady=(6, 0))

        self._device_hint = self._label(
            body, "官方软件占用时，先退出 DeviceDriver.exe", size=8, color=MUTED, wrap=224
        )
        self._device_hint.pack(fill="x", side="bottom", pady=(8, 0))

        tips_card, tips_body = self._card(parent, "上手提示")
        tips_card.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self._tips_card = tips_card
        for line in (
            "· 启动不连接设备，点「开始推送」才打开",
            "· 静态画面每 2 秒重推，防屏幕休眠",
            "· 30 fps 已足够，过高会占用 CPU",
        ):
            self._label(tips_body, line, size=8, color=MUTED, wrap=232).pack(fill="x")

    # -- 中栏：预览 ---------------------------------------------------- #
    def _build_preview_panel(self, parent: tk.Frame) -> None:
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        card, body = self._card(parent, "实时预览", f"{SCREEN_SIZE} × {SCREEN_SIZE}")
        card.grid(row=0, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)

        holder = tk.Frame(body, bg="#05080C")
        holder.grid(row=0, column=0, sticky="nsew")
        self._preview_holder = holder
        self._canvas = tk.Canvas(
            holder,
            bg="#05080C",
            highlightthickness=1,
            highlightbackground=BORDER,
            bd=0,
            width=SCREEN_SIZE,
            height=SCREEN_SIZE,
        )
        self._canvas.place(relx=0.5, rely=0.5, anchor="center")
        holder.bind("<Configure>", self._on_preview_resize)

        info = tk.Frame(body, bg=CARD)
        info.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self._preview_render_label = self._label(info, "渲染 —", size=9, color=MUTED)
        self._preview_render_label.pack(side="left")
        self._jpeg_label = self._label(info, "JPEG —", size=9, color=MUTED)
        self._jpeg_label.pack(side="right")

    # -- 右栏：内容与编辑 ---------------------------------------------- #
    def _build_editor_panel(self, parent: tk.Frame) -> None:
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        card, body = self._card(parent, "内容与编辑")
        card.grid(row=0, column=0, sticky="nsew")

        notebook = ttk.Notebook(body)
        notebook.pack(fill="both", expand=True)
        self._notebook = notebook
        tab_presets = tk.Frame(notebook, bg=CARD)
        tab_background = tk.Frame(notebook, bg=CARD)
        tab_layers = tk.Frame(notebook, bg=CARD)
        notebook.add(tab_presets, text="预设场景")
        notebook.add(tab_background, text="背景与叠加")
        notebook.add(tab_layers, text="图层编辑")
        self._build_presets_tab(tab_presets)
        self._build_background_tab(tab_background)
        self._build_layers_tab(tab_layers)

        project_row = tk.Frame(body, bg=CARD)
        project_row.pack(fill="x", pady=(10, 0))
        for text, command in (
            ("新建", self._on_new_project),
            ("打开", self._on_open_project),
            ("保存", self._on_save_project),
            ("另存为", self._on_save_project_as),
        ):
            ttk.Button(project_row, text=text, style="Small.TButton", command=command).pack(
                side="left", padx=(0, 6)
            )
        self._path_label = self._label(body, "未命名项目", size=8, color=MUTED, wrap=360)
        self._path_label.pack(fill="x", pady=(6, 0))

    def _build_presets_tab(self, tab: tk.Frame) -> None:
        content = self._scrollable_content(tab)
        self._label(
            content,
            "单击预设立即载入当前场景；改动会实时同步到预览和推流。",
            size=8,
            color=MUTED,
            wrap=350,
        ).pack(fill="x", pady=(0, 6))

        list_row = tk.Frame(content, bg=CARD)
        list_row.pack(fill="both", expand=True)
        self._preset_list = self._dark_listbox(list_row, height=8)
        self._preset_list.pack(side="left", fill="both", expand=True)
        scrollbar = self._attach_scrollbar(self._preset_list, list_row)
        scrollbar.pack(side="right", fill="y")
        self._preset_list.bind("<<ListboxSelect>>", self._on_preset_selected)

        buttons = tk.Frame(content, bg=CARD)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(
            buttons, text="重新载入", style="Small.TButton", command=self._on_reload_preset
        ).pack(side="left")
        ttk.Button(
            buttons, text="刷新预设", style="Small.TButton", command=self._on_refresh_presets
        ).pack(side="left", padx=(6, 0))

        ttk.Separator(content, orient="horizontal").pack(fill="x", pady=10)
        name_row = tk.Frame(content, bg=CARD)
        name_row.pack(fill="x")
        self._label(name_row, "场景名称", size=9, color=MUTED).pack(side="left")
        self._name_var = tk.StringVar(value=self.scene.get("name") or "未命名场景")
        name_entry = ttk.Entry(name_row, textvariable=self._name_var)
        name_entry.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self._name_var.trace_add("write", lambda *_args: self._on_scene_name_changed())

    def _build_background_tab(self, tab: tk.Frame) -> None:
        content = self._scrollable_content(tab)
        content.columnconfigure(1, weight=1)
        self._bg_type_var = tk.StringVar(value=BG_LABEL_BY_TYPE["solid"])
        self._bg_fit_var = tk.StringVar(value=FIT_LABEL_BY_CODE["crop"])
        self._tint_alpha_var = tk.DoubleVar(value=0.0)
        row = 0

        self._grid_label(content, row, "背景类型")
        self._bg_type_combo = ttk.Combobox(
            content,
            textvariable=self._bg_type_var,
            values=BG_TYPE_LABELS,
            state="readonly",
        )
        self._bg_type_combo.grid(row=row, column=1, sticky="ew", pady=2)
        self._bg_type_combo.bind("<<ComboboxSelected>>", self._on_bg_type_changed)
        row += 1

        self._grid_label(content, row, "背景颜色")
        holder = tk.Frame(content, bg=CARD)
        holder.grid(row=row, column=1, sticky="ew", pady=2)
        self._bg_color_swatch = tk.Label(
            holder,
            text="",
            bg=BG,
            width=3,
            height=1,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        self._bg_color_swatch.pack(side="left")
        self._bg_color_hex = self._label(holder, BG, size=8, color=MUTED)
        self._bg_color_hex.pack(side="left", padx=(8, 0))
        self._bg_color_btn = ttk.Button(
            holder, text="选择…", style="Small.TButton", command=self._pick_bg_color
        )
        self._bg_color_btn.pack(side="right")
        row += 1

        self._grid_label(content, row, "媒体文件")
        holder = tk.Frame(content, bg=CARD)
        holder.grid(row=row, column=1, sticky="ew", pady=2)
        self._bg_path_label = self._label(holder, "未选择", size=8, color=MUTED, wrap=180)
        self._bg_path_label.pack(side="left", fill="x", expand=True)
        self._bg_file_btn = ttk.Button(
            holder, text="选择…", style="Small.TButton", command=self._choose_background_file
        )
        self._bg_file_btn.pack(side="right")
        row += 1

        self._grid_label(content, row, "适配方式")
        self._bg_fit_combo = ttk.Combobox(
            content,
            textvariable=self._bg_fit_var,
            values=FIT_LABELS,
            state="readonly",
        )
        self._bg_fit_combo.grid(row=row, column=1, sticky="ew", pady=2)
        self._bg_fit_combo.bind("<<ComboboxSelected>>", self._on_bg_fit_changed)
        row += 1

        ttk.Separator(content, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=10
        )
        row += 1

        self._grid_label(content, row, "叠色")
        holder = tk.Frame(content, bg=CARD)
        holder.grid(row=row, column=1, sticky="ew", pady=2)
        self._tint_swatch = tk.Label(
            holder,
            text="",
            bg="#000000",
            width=3,
            height=1,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        self._tint_swatch.pack(side="left")
        self._tint_hex = self._label(holder, "#000000", size=8, color=MUTED)
        self._tint_hex.pack(side="left", padx=(8, 0))
        ttk.Button(holder, text="选择…", style="Small.TButton", command=self._pick_tint_color).pack(
            side="right"
        )
        row += 1

        self._grid_label(content, row, "叠色浓度")
        holder = tk.Frame(content, bg=CARD)
        holder.grid(row=row, column=1, sticky="ew", pady=2)
        self._tint_alpha_label = self._label(holder, "0%", size=8, color=ACCENT, bold=True)
        self._tint_alpha_label.pack(side="right")
        self._tint_scale = ttk.Scale(
            holder, from_=0, to=100, variable=self._tint_alpha_var, command=self._on_tint_alpha_move
        )
        self._tint_scale.pack(side="left", fill="x", expand=True, padx=(0, 8))
        row += 1

        self._label(
            content,
            "图片/GIF/视频按所选适配方式铺满 480×480；视频源需要 imageio + imageio-ffmpeg。",
            size=8,
            color=MUTED,
            wrap=350,
        ).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(12, 0))

    def _build_layers_tab(self, tab: tk.Frame) -> None:
        ops = tk.Frame(tab, bg=CARD)
        ops.pack(side="bottom", fill="x", padx=10, pady=(6, 10))
        self._layer_up_btn = ttk.Button(
            ops, text="上移", style="Small.TButton", command=lambda: self._move_layer(-1)
        )
        self._layer_up_btn.pack(side="left")
        self._layer_down_btn = ttk.Button(
            ops, text="下移", style="Small.TButton", command=lambda: self._move_layer(1)
        )
        self._layer_down_btn.pack(side="left", padx=(6, 0))
        self._layer_delete_btn = ttk.Button(
            ops, text="删除图层", style="Danger.TButton", command=self._delete_layer
        )
        self._layer_delete_btn.pack(side="right")

        content = self._scrollable_content(tab)
        list_row = tk.Frame(content, bg=CARD)
        list_row.pack(fill="x")
        self._layer_list = self._dark_listbox(list_row, height=6)
        self._layer_list.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(self._layer_list, list_row).pack(side="right", fill="y")
        self._layer_list.bind("<<ListboxSelect>>", self._on_layer_selected)

        add_row = tk.Frame(content, bg=CARD)
        add_row.pack(fill="x", pady=(8, 4))
        for column in range(3):
            add_row.grid_columnconfigure(column, weight=1)
        for index, (text, kind) in enumerate(
            (
                ("＋ 文字", "text"),
                ("＋ 时钟", "clock"),
                ("＋ 监控", "sysinfo"),
                ("＋ 进度条", "bars"),
                ("＋ 图片", "image"),
            )
        ):
            row_index, column_index = divmod(index, 3)
            ttk.Button(
                add_row,
                text=text,
                style="Small.TButton",
                command=lambda layer_kind=kind: self._add_layer(layer_kind),
            ).grid(row=row_index, column=column_index, sticky="ew", padx=2, pady=2)

        ttk.Separator(content, orient="horizontal").pack(fill="x", pady=6)
        self._layer_editor = tk.Frame(content, bg=CARD)
        self._layer_editor.pack(fill="both", expand=True)

    # -- 底部动作条 ---------------------------------------------------- #
    def _build_bottom_bar(self) -> None:
        bar = tk.Frame(self, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(4, 12))
        inner = tk.Frame(bar, bg=CARD)
        inner.pack(fill="x", padx=14, pady=10)

        self._label(inner, "推流帧率", size=9, color=MUTED, bg=CARD).pack(side="left")
        self._fps_var = tk.StringVar(value="30")
        self._fps_combo = ttk.Combobox(
            inner,
            textvariable=self._fps_var,
            values=("10", "20", "30", "60"),
            width=4,
            state="readonly",
        )
        self._fps_combo.pack(side="left", padx=(6, 14))

        self._btn_start = ttk.Button(
            inner, text="▶  开始推送到屏幕", style="Accent.TButton", command=self._on_start_stream
        )
        self._btn_start.pack(side="left")
        self._btn_stop = ttk.Button(
            inner, text="⏹  停止", style="Ghost.TButton", command=self._on_stop_stream
        )
        self._btn_stop.pack(side="left", padx=(8, 14))
        self._btn_stop.state(["disabled"])

        self._frames_label = self._label(inner, "已推 0 帧 · 0.0 fps", size=9, color=MUTED, bg=CARD)
        self._frames_label.pack(side="right")

        ttk.Separator(inner, orient="vertical").pack(side="left", fill="y", padx=(0, 12))
        self._status_label = self._label(
            inner, "就绪 · 未连接设备（不会自动连接）", size=9, color=MUTED, bg=CARD, wrap=520
        )
        self._status_label.pack(side="left", fill="x", expand=True)

    # ------------------------------------------------------------------ #
    # 场景 / 预设 / 项目
    # ------------------------------------------------------------------ #
    def _initial_scene(self) -> dict:
        presets = self._presets()
        if presets:
            name = next(iter(presets))
            self._current_preset_name = name
            return self._normalize(presets[name])
        return self._normalize(
            {
                "name": "空白场景",
                "background": {"type": "solid", "color": BG},
                "tint": {"color": "#000000", "alpha": 0.0},
                "layers": [],
            }
        )

    def _normalize(self, scene: Any) -> dict:
        if scenes_mod is not None and hasattr(scenes_mod, "normalize_scene"):
            try:
                normalized = scenes_mod.normalize_scene(copy.deepcopy(scene))
                if isinstance(normalized, dict):
                    return normalized
                _warn_once("normalize_scene() 未返回 dict，已退回内置归一化")
            except Exception as exc:
                _warn_once(f"normalize_scene() 失败，已退回内置归一化：{exc}")
        return _fallback_normalize(scene)

    def _presets(self) -> dict[str, dict]:
        if self._preset_cache is None:
            presets: dict[str, dict] = {}
            if scenes_mod is not None and hasattr(scenes_mod, "preset_scenes"):
                try:
                    raw = scenes_mod.preset_scenes()
                    if isinstance(raw, dict):
                        presets = {
                            str(name): value
                            for name, value in raw.items()
                            if isinstance(value, dict)
                        }
                except Exception as exc:
                    _warn_once(f"preset_scenes() 失败，已使用内置预设：{exc}")
            if not presets:
                presets = _fallback_presets()
            self._preset_cache = presets
        return self._preset_cache

    def _snapshot_scene(self) -> dict:
        with self._scene_lock:
            return copy.deepcopy(self.scene)

    def _edit_scene(self, mutator: Callable[[dict], dict]) -> None:
        """在锁内修改场景并归一化；任何异常都只提示，不崩 UI。"""
        try:
            with self._scene_lock:
                updated = mutator(copy.deepcopy(self.scene))
                self.scene = self._normalize(updated)
                self._dirty = True
        except Exception as exc:
            self._set_status(f"场景更新失败：{exc}", "error")
            return
        self._update_project_label()

    def _sync_ui_from_scene(self) -> None:
        self._syncing = True
        try:
            self._name_var.set(str(self.scene.get("name") or "未命名场景"))
            self._sync_background_controls()
            self._refresh_layer_list()
            self._select_preset_in_list()
        finally:
            self._syncing = False
        self._update_project_label()

    def _refresh_presets(self) -> None:
        self._preset_cache = None
        self._preset_list.delete(0, "end")
        for name in self._presets():
            self._preset_list.insert("end", name)
        self._select_preset_in_list()

    def _select_preset_in_list(self) -> None:
        names = list(self._presets())
        self._suppress_preset_event = True
        try:
            self._preset_list.selection_clear(0, "end")
            if self._current_preset_name in names:
                index = names.index(self._current_preset_name)
                self._preset_list.selection_set(index)
                self._preset_list.see(index)
        finally:
            self._suppress_preset_event = False

    def _load_preset(self, name: str) -> None:
        presets = self._presets()
        preset = presets.get(name)
        if preset is None:
            self._set_status(f"预设「{name}」不存在", "warn")
            return
        self.scene = self._normalize(copy.deepcopy(preset))
        self._layer_index = -1
        self.project_path = None
        self._dirty = False
        self._current_preset_name = name
        self._sync_ui_from_scene()
        self._set_status(f"已载入预设「{name}」", "ok")

    def _on_preset_selected(self, _event: tk.Event | None = None) -> None:
        if self._suppress_preset_event:
            return
        selection = self._preset_list.curselection()
        if not selection:
            return
        name = self._preset_list.get(selection[0])
        self._load_preset(str(name))

    def _on_reload_preset(self) -> None:
        selection = self._preset_list.curselection()
        if selection:
            self._load_preset(str(self._preset_list.get(selection[0])))

    def _on_refresh_presets(self) -> None:
        self._refresh_presets()
        self._set_status("预设列表已刷新", "ok")

    def _on_scene_name_changed(self) -> None:
        if self._syncing:
            return
        name = self._name_var.get().strip() or "未命名场景"
        self._edit_scene(lambda scene: {**scene, "name": name})
        self._current_preset_name = None
        self._select_preset_in_list()

    def _update_project_label(self) -> None:
        if self._path_label is None:
            return
        if self.project_path:
            label = os.path.basename(self.project_path)
        else:
            label = "未命名项目"
        if self._dirty:
            label += "  ● 未保存"
        else:
            label += "  ✓ 已保存"
        self._path_label.configure(text=label)
        title = "LG600 Studio — TCOMAS LG600 开源控制台"
        if self.project_path:
            title = f"LG600 Studio — {os.path.basename(self.project_path)}"
        try:
            self.title(title)
        except tk.TclError:
            pass

    # -- 项目文件 ------------------------------------------------------ #
    def _load_project_file(self, path: str) -> dict:
        if scenes_mod is not None and hasattr(scenes_mod, "load_project"):
            data = scenes_mod.load_project(path)
        else:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and "scene" in data and isinstance(data["scene"], dict):
                data = data["scene"]
        if not isinstance(data, dict):
            raise ValueError("项目文件内容不是场景对象")
        return data

    def _save_project_file(self, path: str, scene: dict) -> None:
        if scenes_mod is not None and hasattr(scenes_mod, "save_project"):
            scenes_mod.save_project(scene, path)
            return
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {"format": "lg6-studio.scene/1", "scene": scene},
                handle,
                ensure_ascii=False,
                indent=2,
            )

    def load_project(self, path: str, notify: bool = True) -> bool:
        """打开 .lg6.json 项目；返回是否成功（供命令行与菜单共用）。"""
        try:
            data = self._load_project_file(path)
            self.scene = self._normalize(data)
        except Exception as exc:
            message = f"打开项目失败：{exc}"
            self._set_status(message, "error")
            if notify:
                messagebox.showerror("打开项目失败", message, parent=self)
            return False
        self._layer_index = -1
        self.project_path = os.path.abspath(path)
        self._dirty = False
        self._current_preset_name = None
        self._sync_ui_from_scene()
        self._set_status(f"已打开项目：{os.path.basename(path)}", "ok")
        return True

    def _on_new_project(self) -> None:
        if self._dirty and not messagebox.askyesno(
            "新建项目", "当前修改尚未保存，确定要新建项目吗？", parent=self
        ):
            return
        self.scene = self._normalize(
            {
                "name": "未命名场景",
                "background": {"type": "solid", "color": BG},
                "tint": {"color": "#000000", "alpha": 0.0},
                "layers": [],
            }
        )
        self._layer_index = -1
        self.project_path = None
        self._dirty = False
        self._current_preset_name = None
        self._sync_ui_from_scene()
        self._set_status("已新建空白项目", "ok")

    def _on_open_project(self) -> None:
        path = filedialog.askopenfilename(
            title="打开 LG600 Studio 项目", filetypes=PROJECT_FILETYPES, parent=self
        )
        if path:
            self.load_project(path)

    def _on_save_project(self) -> None:
        if not self.project_path:
            self._on_save_project_as()
            return
        try:
            self._save_project_file(self.project_path, self._snapshot_scene())
        except Exception as exc:
            message = f"保存项目失败：{exc}"
            self._set_status(message, "error")
            messagebox.showerror("保存项目失败", message, parent=self)
            return
        self._dirty = False
        self._update_project_label()
        self._set_status(f"已保存：{os.path.basename(self.project_path)}", "ok")

    def _on_save_project_as(self) -> None:
        path = filedialog.asksaveasfilename(
            title="保存 LG600 Studio 项目",
            defaultextension=".lg6.json",
            filetypes=PROJECT_FILETYPES,
            parent=self,
        )
        if not path:
            return
        try:
            self._save_project_file(path, self._snapshot_scene())
        except Exception as exc:
            message = f"保存项目失败：{exc}"
            self._set_status(message, "error")
            messagebox.showerror("保存项目失败", message, parent=self)
            return
        self.project_path = os.path.abspath(path)
        self._dirty = False
        self._update_project_label()
        self._set_status(f"已另存为：{os.path.basename(path)}", "ok")

    def _on_about(self) -> None:
        messagebox.showinfo(
            "关于 LG600 Studio",
            f"LG600 Studio {APP_VERSION}\n"
            "TCOMAS LG600（1A2C:1520）水冷屏的开源控制台\n\n"
            "MIT License · Python + Tkinter + Pillow\n\n"
            f"{RECONNECT_HINT}",
            parent=self,
        )

    # ------------------------------------------------------------------ #
    # 预览
    # ------------------------------------------------------------------ #
    def render_frame(self, scene: dict, t: float) -> Image.Image:
        """渲染一帧 480x480 RGB；渲染引擎缺席时返回占位图。"""
        if render_mod is not None and hasattr(render_mod, "render_scene"):
            with _RENDER_LOCK:
                image = render_mod.render_scene(scene, t)
            if not isinstance(image, Image.Image):
                raise TypeError("render_scene() 必须返回 PIL.Image.Image")
            return image.convert("RGB")
        return _placeholder_image(
            ("渲染引擎未就绪", "lg6studio/render.py 缺失或加载失败", "界面仍可编辑并保存项目"),
            SCREEN_SIZE,
        )

    def encode_frame(self, image: Image.Image) -> bytes:
        """按 device.MAX_FRAME_BYTES 上限编码 JPEG（device.py 缺席时本地兜底）。"""
        if device_mod is not None and hasattr(device_mod, "encode_jpeg"):
            return device_mod.encode_jpeg(image)
        data = b""
        for quality in (95, 88, 80, 72, 64, 56, 48, 40, 32, 24):
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="JPEG", quality=quality, optimize=True)
            data = buffer.getvalue()
            if len(data) <= MAX_JPEG_BYTES:
                return data
        return data

    def _preview_tick(self) -> None:
        if self._closing:
            return
        scene = self._snapshot_scene()
        delay = PREVIEW_STREAMING_INTERVAL_MS if self._is_streaming() else PREVIEW_INTERVAL_MS
        try:
            self._preview_worker.update(
                scene, time.monotonic() - self._timeline_start, self._preview_side
            )
        except Exception as exc:  # 队列/线程异常也不能让 UI 卡死
            _warn_once(f"预览调度异常：{exc}")
        self.after(delay, self._preview_tick)

    def _poll_render(self) -> None:
        if self._closing:
            return
        latest: tuple | None = None
        while True:
            try:
                latest = self._preview_worker.results.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            self._apply_preview_result(latest)
        self.after(40, self._poll_render)

    def _apply_preview_result(self, item: tuple) -> None:
        kind, image, jpeg_len, jpeg_error, elapsed_ms = item
        self._show_preview_image(image)
        self._last_render_ms = float(elapsed_ms)
        if self._preview_render_label is not None:
            if elapsed_ms > 0:
                approx_fps = min(60.0, 1000.0 / float(elapsed_ms))
                self._preview_render_label.configure(
                    text=f"渲染 {elapsed_ms:.1f} ms · 约 {approx_fps:.0f} FPS"
                )
            else:
                self._preview_render_label.configure(text="渲染 —")
        if kind == "err":
            self._set_status(f"预览渲染失败：{jpeg_error}", "warn")
            if self._jpeg_label is not None:
                self._jpeg_label.configure(text="JPEG —", fg=MUTED)
            return
        if self._jpeg_label is None:
            return
        if jpeg_len is not None:
            fits = jpeg_len <= MAX_JPEG_BYTES
            self._jpeg_label.configure(
                text=f"JPEG {jpeg_len / 1024:.1f} KB / {MAX_JPEG_BYTES / 1024:.1f} KB 上限",
                fg=OK if fits else ERR,
            )
        else:
            short = jpeg_error if len(jpeg_error) <= 60 else jpeg_error[:57] + "..."
            self._jpeg_label.configure(text=f"JPEG 超限：{short}", fg=ERR)

    def _show_preview_image(self, source: Image.Image) -> None:
        self._last_frame_img = source
        side = self._preview_side
        display = source
        if source.size != (side, side):
            try:
                display = source.convert("RGB").resize((side, side), _RESAMPLE)
            except Exception:
                display = source
        try:
            photo = ImageTk.PhotoImage(display)
        except Exception as exc:
            _warn_once(f"预览贴图失败：{exc}")
            return
        self._photo = photo
        if self._canvas_img_id is None:
            self._canvas_img_id = self._canvas.create_image(side / 2, side / 2, image=photo)
        else:
            self._canvas.coords(self._canvas_img_id, side / 2, side / 2)
            self._canvas.itemconfigure(self._canvas_img_id, image=photo)

    def _on_preview_resize(self, event: tk.Event) -> None:
        side = int(max(220, min(event.width - 28, event.height - 28, 620)))
        side = max(200, side - side % 2)
        if side == self._preview_side:
            return
        self._preview_side = side
        self._canvas.configure(width=side, height=side)
        if self._canvas_img_id is not None:
            self._canvas.coords(self._canvas_img_id, side / 2, side / 2)
        if self._last_frame_img is not None:
            self._show_preview_image(self._last_frame_img)

    def _on_root_configure(self, event: tk.Event) -> None:
        """窗口高度不足时压缩左栏提示与列表高度，避免关键控件被裁切。"""
        if event.widget is not self or self._tips_card is None:
            return
        compact = event.height < 735
        if compact == self._compact_mode:
            return
        self._compact_mode = compact
        self._tips_visible = not compact
        if compact:
            self._tips_card.grid_remove()
        else:
            self._tips_card.grid()
        if getattr(self, "_preset_list", None) is not None:
            self._preset_list.configure(height=4 if compact else 8)
        if getattr(self, "_layer_list", None) is not None:
            self._layer_list.configure(height=4 if compact else 6)

    # ------------------------------------------------------------------ #
    # 背景与图层编辑
    # ------------------------------------------------------------------ #
    def _grid_label(self, parent: tk.Misc, row: int, text: str) -> tk.Label:
        label = self._label(parent, text, size=9, color=MUTED)
        label.grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        return label

    def _set_background(self, **changes: Any) -> None:
        if self._syncing:
            return

        def mutate(scene: dict) -> dict:
            background = scene.get("background")
            if not isinstance(background, dict):
                background = {}
            background.update(changes)
            scene["background"] = background
            return scene

        self._edit_scene(mutate)
        self._sync_background_controls()

    def _sync_background_controls(self) -> None:
        background = self.scene.get("background") or {}
        if not isinstance(background, dict):
            background = {}
        bg_type = background.get("type") if background.get("type") in BG_LABEL_BY_TYPE else "solid"
        self._bg_type_var.set(BG_LABEL_BY_TYPE[bg_type])
        self._bg_fit_var.set(
            FIT_LABEL_BY_CODE.get(str(background.get("fit") or "crop"), FIT_LABELS[0])
        )
        color = _norm_color(background.get("color"), BG)
        self._bg_color_swatch.configure(bg=color)
        self._bg_color_hex.configure(text=color)
        path = str(background.get("path") or "")
        self._bg_path_label.configure(
            text=os.path.basename(path) if path else "未选择", fg=TEXT if path else MUTED
        )
        tint = self.scene.get("tint") or {}
        if not isinstance(tint, dict):
            tint = {}
        tint_color = _norm_color(tint.get("color"), "#000000")
        alpha = _safe_float(tint.get("alpha"), 0.0, 0.0, 1.0)
        self._tint_swatch.configure(bg=tint_color)
        self._tint_hex.configure(text=tint_color)
        self._tint_alpha_var.set(alpha * 100.0)
        self._tint_alpha_label.configure(text=f"{int(round(alpha * 100))}%")
        is_solid = bg_type == "solid"
        self._set_widget_enabled(self._bg_color_btn, is_solid)
        self._set_widget_enabled(self._bg_file_btn, not is_solid)
        self._set_widget_enabled(self._bg_fit_combo, not is_solid)
        self._bg_color_swatch.configure(fg=BORDER if is_solid else MUTED)

    @staticmethod
    def _set_widget_enabled(widget: tk.Widget, enabled: bool) -> None:
        try:
            if isinstance(widget, ttk.Widget):
                widget.state(["!disabled"] if enabled else ["disabled"])
            else:
                widget.configure(state="normal" if enabled else "disabled")
        except tk.TclError:
            pass

    def _on_bg_type_changed(self, _event: tk.Event | None = None) -> None:
        target = BG_TYPE_BY_LABEL.get(self._bg_type_var.get(), "solid")
        background = self.scene.get("background") or {}
        if not isinstance(background, dict):
            background = {}
        if target == "solid":
            self._set_background(type="solid")
            self._set_status("已切换为纯色背景", "ok")
        else:
            self._set_background(type=target)
            if not background.get("path"):
                self._set_status(f"已切换为{BG_LABEL_BY_TYPE[target]}背景：请选择媒体文件", "warn")
            else:
                self._set_status(f"背景类型：{BG_LABEL_BY_TYPE[target]}", "ok")

    def _on_bg_fit_changed(self, _event: tk.Event | None = None) -> None:
        self._set_background(fit=FIT_BY_LABEL.get(self._bg_fit_var.get(), "crop"))

    def _choose_background_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择背景图片 / GIF / 视频", filetypes=IMG_FILETYPES, parent=self
        )
        if not path:
            return
        extension = os.path.splitext(path)[1].lower()
        if extension in (".gif",):
            bg_type = "gif"
        elif extension in (".mp4", ".avi", ".webm", ".mov", ".mkv"):
            bg_type = "video"
        else:
            bg_type = "image"
        self._set_background(type=bg_type, path=path)
        self._set_status(f"背景已设置为：{os.path.basename(path)}", "ok")
        # 让预览缓存丢弃旧解码帧（render.clear_cache 是可选能力）
        if render_mod is not None and hasattr(render_mod, "clear_cache"):
            try:
                render_mod.clear_cache()
            except Exception as exc:
                _warn_once(f"render.clear_cache() 失败：{exc}")

    def _pick_bg_color(self) -> None:
        current = (self.scene.get("background") or {}).get("color") or BG
        _, hex_color = colorchooser.askcolor(color=current, parent=self, title="选择背景颜色")
        if hex_color:
            self._set_background(color=hex_color.upper())
            self._bg_color_swatch.configure(bg=hex_color.upper())
            self._bg_color_hex.configure(text=hex_color.upper())

    def _pick_tint_color(self) -> None:
        current = (self.scene.get("tint") or {}).get("color") or "#000000"
        _, hex_color = colorchooser.askcolor(color=current, parent=self, title="选择叠色")
        if hex_color:

            def mutate(scene: dict) -> dict:
                tint = scene.get("tint")
                if not isinstance(tint, dict):
                    tint = {"color": "#000000", "alpha": 0.0}
                tint["color"] = hex_color.upper()
                scene["tint"] = tint
                return scene

            self._edit_scene(mutate)
            self._sync_background_controls()

    def _on_tint_alpha_move(self, value: str) -> None:
        try:
            percent = float(value)
        except (TypeError, ValueError):
            return
        if self._tint_alpha_label is not None:
            self._tint_alpha_label.configure(text=f"{int(round(percent))}%")
        if self._syncing:
            return

        def mutate(scene: dict) -> dict:
            tint = scene.get("tint")
            if not isinstance(tint, dict):
                tint = {"color": "#000000", "alpha": 0.0}
            tint["alpha"] = round(percent / 100.0, 3)
            scene["tint"] = tint
            return scene

        self._edit_scene(mutate)

    # -- 图层 ---------------------------------------------------------- #
    def _selected_layer(self) -> dict | None:
        layers = self.scene.get("layers") or []
        if 0 <= self._layer_index < len(layers) and isinstance(layers[self._layer_index], dict):
            return layers[self._layer_index]
        return None

    def _refresh_layer_list(self) -> None:
        layers = self.scene.get("layers") or []
        index = self._layer_index
        if index >= len(layers):
            index = len(layers) - 1
        self._suppress_layer_event = True
        try:
            self._layer_list.delete(0, "end")
            for item_index, layer in enumerate(layers):
                self._layer_list.insert("end", f"{item_index + 1}. {layer_label(layer)}")
            if 0 <= index < len(layers):
                self._layer_list.selection_set(index)
                self._layer_list.activate(index)
                self._layer_list.see(index)
        finally:
            self._suppress_layer_event = False
        self._layer_index = index
        self._rebuild_layer_editor()
        self._update_layer_ops()

    def _update_layer_ops(self) -> None:
        if self._layer_up_btn is None:
            return
        layers = self.scene.get("layers") or []
        index = self._layer_index
        has_selection = 0 <= index < len(layers)
        for button in (self._layer_up_btn, self._layer_down_btn, self._layer_delete_btn):
            button.state(["!disabled"] if has_selection else ["disabled"])
        self._layer_up_btn.state(["!disabled"] if index > 0 else ["disabled"])
        self._layer_down_btn.state(
            ["!disabled"] if has_selection and index < len(layers) - 1 else ["disabled"]
        )

    def _update_layer_listitem(self, index: int) -> None:
        layers = self.scene.get("layers") or []
        if not (0 <= index < len(layers)):
            return
        self._suppress_layer_event = True
        try:
            self._layer_list.delete(index)
            self._layer_list.insert(index, f"{index + 1}. {layer_label(layers[index])}")
            self._layer_list.selection_clear(0, "end")
            self._layer_list.selection_set(index)
        finally:
            self._suppress_layer_event = False

    def _on_layer_selected(self, _event: tk.Event | None = None) -> None:
        if self._suppress_layer_event:
            return
        selection = self._layer_list.curselection()
        self._layer_index = int(selection[0]) if selection else -1
        self._rebuild_layer_editor()
        self._update_layer_ops()

    def _add_layer(self, kind: str) -> None:
        layer: dict
        if kind == "text":
            layer = {
                "type": "text",
                "text": "双击列表或用下方输入框编辑文本",
                "x": 0.5,
                "y": 0.5,
                "anchor": "center",
                "size": 40,
                "color": "#FFFFFF",
                "bold": True,
                "align": "center",
                "shadow": True,
            }
        elif kind == "clock":
            layer = {
                "type": "clock",
                "format": "%H:%M",
                "show_date": False,
                "date_format": "%Y-%m-%d",
                "x": 0.5,
                "y": 0.45,
                "size": 120,
                "color": "#FFFFFF",
                "bold": True,
                "shadow": True,
            }
        elif kind == "sysinfo":
            layer = {
                "type": "sysinfo",
                "fields": ["cpu", "ram"],
                "x": 0.5,
                "y": 0.8,
                "size": 30,
                "color": "#8FD3FF",
                "shadow": True,
            }
        elif kind == "bars":
            layer = {
                "type": "bars",
                "items": ["cpu", "ram"],
                "x": 0.08,
                "y": 0.62,
                "w": 0.84,
                "h": 0.10,
                "color": "#38BDF8",
                "color2": "#F472B6",
                "bg": "#1E293B",
                "label": True,
                "size": 22,
            }
        elif kind == "image":
            path = filedialog.askopenfilename(
                title="选择图片图层", filetypes=IMG_FILETYPES, parent=self
            )
            if not path:
                return
            layer = {
                "type": "image",
                "path": path,
                "x": 0.5,
                "y": 0.5,
                "scale": 0.6,
                "opacity": 1.0,
            }
        else:
            return

        def mutate(scene: dict) -> dict:
            layers = scene.get("layers")
            if not isinstance(layers, list):
                layers = []
            layers.append(layer)
            scene["layers"] = layers
            return scene

        self._edit_scene(mutate)
        self._layer_index = max(0, len(self.scene.get("layers") or []) - 1)
        self._refresh_layer_list()
        self._set_status(f"已添加{layer_label(layer)}", "ok")

    def _delete_layer(self) -> None:
        layers = self.scene.get("layers") or []
        if not (0 <= self._layer_index < len(layers)):
            self._set_status("请先选中要删除的图层", "warn")
            return
        index = self._layer_index

        def mutate(scene: dict) -> dict:
            current = scene.get("layers")
            if isinstance(current, list) and 0 <= index < len(current):
                current.pop(index)
            return scene

        self._edit_scene(mutate)
        self._layer_index = min(index, len(self.scene.get("layers") or []) - 1)
        self._refresh_layer_list()
        self._set_status("已删除图层", "ok")

    def _move_layer(self, delta: int) -> None:
        layers = self.scene.get("layers") or []
        index = self._layer_index
        target = index + delta
        if not (0 <= index < len(layers)) or not (0 <= target < len(layers)):
            return

        def mutate(scene: dict) -> dict:
            current = scene.get("layers")
            if isinstance(current, list) and 0 <= target < len(current):
                current[index], current[target] = current[target], current[index]
            return scene

        self._edit_scene(mutate)
        self._layer_index = target
        self._refresh_layer_list()

    def _apply_layer_field(self, key: str, value: Any) -> None:
        if self._syncing:
            return
        index = self._layer_index

        def mutate(scene: dict) -> dict:
            layers = scene.get("layers")
            if isinstance(layers, list) and 0 <= index < len(layers):
                layers[index][key] = value
            return scene

        self._edit_scene(mutate)
        self._update_layer_listitem(index)

    def _rebuild_layer_editor(self) -> None:
        for child in self._layer_editor.winfo_children():
            child.destroy()
        layer = self._selected_layer()
        if layer is None:
            self._label(
                self._layer_editor,
                "未选择图层。添加图层或在列表中选中一个后在此编辑属性。",
                size=9,
                color=MUTED,
                wrap=340,
            ).pack(anchor="w", pady=8)
            return

        kind = layer.get("type")
        type_names = {
            "text": "文字",
            "clock": "时钟",
            "sysinfo": "系统监控",
            "bars": "进度条",
            "image": "图片",
        }
        self._label(
            self._layer_editor,
            f"图层 {self._layer_index + 1} · {type_names.get(kind, kind)}",
            size=10,
            color=ACCENT,
            bold=True,
        ).pack(anchor="w", pady=(0, 8))

        grid = tk.Frame(self._layer_editor, bg=CARD)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        row = 0

        if kind == "text":
            self._grid_label(grid, row, "文本")
            entry_var = tk.StringVar(value=str(layer.get("text") or ""))
            entry = ttk.Entry(grid, textvariable=entry_var)
            entry.grid(row=row, column=1, sticky="ew", pady=2)
            entry_var.trace_add(
                "write", lambda *_args, var=entry_var: self._apply_layer_field("text", var.get())
            )
            row += 1
        elif kind == "clock":
            self._grid_label(grid, row, "时间格式")
            format_var = tk.StringVar(value=str(layer.get("format") or "%H:%M"))
            entry = ttk.Entry(grid, textvariable=format_var)
            entry.grid(row=row, column=1, sticky="ew", pady=2)
            format_var.trace_add(
                "write", lambda *_args, var=format_var: self._apply_layer_field("format", var.get())
            )
            row += 1
            self._grid_label(grid, row, "日期")
            date_var = tk.BooleanVar(value=bool(layer.get("show_date")))
            tk.Checkbutton(
                grid,
                text="同时显示日期",
                variable=date_var,
                bg=CARD,
                fg=TEXT,
                activebackground=CARD,
                activeforeground=TEXT,
                selectcolor=ENTRY_BG,
                highlightthickness=0,
                bd=0,
                font=(FONT_FAMILY, 9),
                command=lambda var=date_var: self._apply_layer_field("show_date", bool(var.get())),
            ).grid(row=row, column=1, sticky="w", pady=2)
            row += 1
        elif kind in ("sysinfo", "bars"):
            field_key = "fields" if kind == "sysinfo" else "items"
            self._grid_label(grid, row, "监控项")
            current = layer.get(field_key) or ["cpu", "ram"]
            fields_var = tk.StringVar(value=", ".join(str(item) for item in current))
            entry = ttk.Entry(grid, textvariable=fields_var)
            entry.grid(row=row, column=1, sticky="ew", pady=2)

            def apply_fields(
                *_args: Any, var: tk.StringVar = fields_var, key: str = field_key
            ) -> None:
                items = [part.strip() for part in var.get().split(",") if part.strip()]
                self._apply_layer_field(key, items or ["cpu", "ram"])

            fields_var.trace_add("write", apply_fields)
            row += 1
        elif kind == "image":
            self._grid_label(grid, row, "图片文件")
            holder = tk.Frame(grid, bg=CARD)
            holder.grid(row=row, column=1, sticky="ew", pady=2)
            path = str(layer.get("path") or "")
            path_label = self._label(
                holder,
                os.path.basename(path) if path else "未选择",
                size=8,
                color=TEXT if path else MUTED,
            )
            path_label.pack(side="left", fill="x", expand=True)

            def choose_image() -> None:
                selected = filedialog.askopenfilename(
                    title="选择图片图层", filetypes=IMG_FILETYPES, parent=self
                )
                if selected:
                    self._apply_layer_field("path", selected)
                    self._rebuild_layer_editor()

            ttk.Button(holder, text="选择…", style="Small.TButton", command=choose_image).pack(
                side="right"
            )
            row += 1

        if kind == "image":
            self._grid_label(grid, row, "缩放")
            scale_holder = tk.Frame(grid, bg=CARD)
            scale_holder.grid(row=row, column=1, sticky="ew", pady=2)
            scale_value = self._label(
                scale_holder,
                f"{float(layer.get('scale') or 0.5):.2f}",
                size=8,
                color=ACCENT,
                bold=True,
            )
            scale_value.pack(side="right", padx=(8, 0))
            scale = ttk.Scale(
                scale_holder,
                from_=0.05,
                to=1.0,
                value=_safe_float(layer.get("scale"), 0.5, 0.05, 1.0),
                command=lambda value, lbl=scale_value: self._apply_scale_value(
                    "scale", value, lbl, 2
                ),
            )
            scale.pack(side="left", fill="x", expand=True)
            row += 1
        else:
            self._grid_label(grid, row, "字号")
            size_var = tk.StringVar(value=str(int(_safe_float(layer.get("size"), 48, 8, 400))))
            spinbox = tk.Spinbox(
                grid,
                from_=8,
                to=400,
                increment=1,
                textvariable=size_var,
                width=6,
                bg=ENTRY_BG,
                fg=TEXT,
                insertbackground=TEXT,
                buttonbackground=BTN,
                relief="flat",
                highlightthickness=1,
                highlightbackground=BORDER,
                highlightcolor=ACCENT,
                font=(FONT_FAMILY, 9),
            )
            spinbox.grid(row=row, column=1, sticky="w", pady=2)
            size_var.trace_add("write", lambda *_args, var=size_var: self._apply_size_field(var))
            row += 1

        self._editor_color_row(
            grid, row, "颜色", _norm_color(layer.get("color"), "#FFFFFF"), "color"
        )
        row += 1
        if kind == "bars":
            self._editor_color_row(
                grid, row, "第二颜色", _norm_color(layer.get("color2"), "#F472B6"), "color2"
            )
            row += 1

        self._editor_scale_row(grid, row, "位置 X", _safe_float(layer.get("x"), 0.5, 0.0, 1.0), "x")
        row += 1
        self._editor_scale_row(grid, row, "位置 Y", _safe_float(layer.get("y"), 0.5, 0.0, 1.0), "y")
        row += 1

        if kind in ("text", "clock", "sysinfo"):
            self._grid_label(grid, row, "样式")
            holder = tk.Frame(grid, bg=CARD)
            holder.grid(row=row, column=1, sticky="w", pady=2)
            for key, label_text in (("bold", "粗体"), ("shadow", "阴影")):
                var = tk.BooleanVar(value=bool(layer.get(key, key == "shadow")))
                tk.Checkbutton(
                    holder,
                    text=label_text,
                    variable=var,
                    bg=CARD,
                    fg=TEXT,
                    activebackground=CARD,
                    activeforeground=TEXT,
                    selectcolor=ENTRY_BG,
                    highlightthickness=0,
                    bd=0,
                    font=(FONT_FAMILY, 9),
                    command=lambda field=key, widget_var=var: self._apply_layer_field(
                        field, bool(widget_var.get())
                    ),
                ).pack(side="left", padx=(0, 12))

        self._label(
            self._layer_editor,
            "提示：坐标用 0–1 归一化，便于换分辨率。",
            size=8,
            color=MUTED,
            wrap=340,
        ).pack(anchor="w", pady=(8, 0))

    def _apply_size_field(self, var: tk.StringVar) -> None:
        text = var.get().strip()
        if not text.isdigit():
            return
        value = int(text)
        if 8 <= value <= 400:
            self._apply_layer_field("size", value)

    def _apply_scale_value(self, key: str, raw: str, label: tk.Label | None, digits: int) -> None:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return
        if label is not None:
            label.configure(text=f"{value:.{digits}f}")
        self._apply_layer_field(key, round(value, digits))

    def _editor_color_row(
        self, grid: tk.Misc, row: int, label_text: str, current: str, key: str
    ) -> None:
        self._grid_label(grid, row, label_text)
        holder = tk.Frame(grid, bg=CARD)
        holder.grid(row=row, column=1, sticky="ew", pady=2)
        swatch = tk.Label(
            holder,
            text="",
            bg=current,
            width=3,
            height=1,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        swatch.pack(side="left")
        hex_label = self._label(holder, current, size=8, color=MUTED)
        hex_label.pack(side="left", padx=(8, 0))

        def pick() -> None:
            _, hex_color = colorchooser.askcolor(color=current, parent=self, title="选择图层颜色")
            if hex_color:
                color = hex_color.upper()
                swatch.configure(bg=color)
                hex_label.configure(text=color)
                self._apply_layer_field(key, color)

        ttk.Button(holder, text="选择…", style="Small.TButton", command=pick).pack(side="right")

    def _editor_scale_row(
        self, grid: tk.Misc, row: int, label_text: str, value: float, key: str
    ) -> None:
        self._grid_label(grid, row, label_text)
        holder = tk.Frame(grid, bg=CARD)
        holder.grid(row=row, column=1, sticky="ew", pady=2)
        value_label = self._label(holder, f"{value:.2f}", size=8, color=ACCENT, bold=True)
        value_label.pack(side="right", padx=(8, 0))
        scale = ttk.Scale(
            holder,
            from_=0.0,
            to=1.0,
            value=value,
            command=lambda raw, lbl=value_label, field=key: self._apply_scale_value(
                field, raw, lbl, 2
            ),
        )
        scale.pack(side="left", fill="x", expand=True)

    # ------------------------------------------------------------------ #
    # 设备控制
    # ------------------------------------------------------------------ #
    def _set_status(self, text: str, kind: str = "info") -> None:
        self._status_text = text
        label = self._status_label
        if label is None:
            return
        colors = {"info": MUTED, "ok": OK, "warn": WARN, "error": ERR, "stream": ACCENT_2}
        try:
            label.configure(text=text, fg=colors.get(kind, MUTED))
        except tk.TclError:
            pass

    def _set_connection(self, text: str, color: str) -> None:
        label = self._conn_label
        if label is None:
            return
        try:
            label.configure(text=text, fg=color)
        except tk.TclError:
            pass

    def _on_detect(self) -> None:
        self._detect_device(auto=False)

    def _detect_device(self, auto: bool = False) -> None:
        if device_mod is None:
            self._set_status("设备模块未就绪：lg6studio/device.py 缺失或加载失败", "warn")
            if self._dev_iface_label is not None:
                self._dev_iface_label.configure(text="接口：device.py 未就绪")
            return
        if not auto:
            self._set_status("正在枚举 HID 设备（不会打开设备）…", "info")

        def job() -> None:
            try:
                devices = device_mod.enumerate_devices()
            except Exception as exc:
                self._post("devices_error", friendly_device_error(exc))
                return
            self._post("devices", devices)

        threading.Thread(target=job, daemon=True, name="lg6-enum").start()

    def _apply_devices(self, devices: Any) -> None:
        if not isinstance(devices, list):
            devices = []
        self._detected = devices
        if not devices:
            self._set_connection("● 未检测到设备", MUTED)
            if self._dev_iface_label is not None:
                self._dev_iface_label.configure(
                    text="接口：未找到 1A2C:1520\n请确认屏幕 USB 已连接并上电，然后点「检测设备」。"
                )
            if self._device_hint is not None:
                self._device_hint.configure(text=RECONNECT_HINT)
            self._set_status("未检测到 TCOMAS LG600（1A2C:1520）", "warn")
            return
        by_role = {item.get("role"): item for item in devices if isinstance(item, dict)}
        image = by_role.get("image")
        control = by_role.get("control")
        parts: list[str] = []
        if image:
            parts.append(f"图像口 {image.get('interface', '?')}（{image.get('output_len', '?')}B）")
        if control:
            parts.append(
                f"控制口 {control.get('interface', '?')}（{control.get('output_len', '?')}B）"
            )
        if not parts:
            parts.append(f"发现 {len(devices)} 个 HID 集合")
        if self._dev_iface_label is not None:
            self._dev_iface_label.configure(text="接口：" + " · ".join(parts))
        if image and control:
            self._set_connection("● 设备就绪（未连接）", ACCENT)
            self._set_status("已检测到 TCOMAS LG600，可以调节背光或开始推送", "ok")
        else:
            self._set_connection("● 设备接口不完整", WARN)
            self._set_status(
                "设备接口不完整：可能被官方软件占用或驱动异常。" + RECONNECT_HINT, "warn"
            )

    def _control_job(self, name: str, action: Callable[[Any], None], ok_message: str = "") -> None:
        """在后台线程里打开控制口并下发命令，异常转中文状态栏提示。"""

        def job() -> None:
            failure: Exception | None = None
            with self._ctl_lock:
                try:
                    self._open_control_locked()
                    action(self._control)
                except Exception as exc:
                    failure = exc
                    self._close_control_locked()
            if failure is not None:
                self._post("status", friendly_device_error(failure), "error")
            elif ok_message:
                self._post("status", ok_message, "ok")

        threading.Thread(target=job, daemon=True, name=f"lg6-ctl-{name}").start()

    def _open_control_locked(self) -> None:
        if device_mod is None:
            raise RuntimeError("设备模块 device.py 未就绪")
        if self._control is None:
            control = device_mod.Control()
            control.open()
            self._control = control

    def _close_control_locked(self) -> None:
        if self._control is not None:
            try:
                self._control.close()
            except Exception:
                pass
            self._control = None

    def _on_brightness_move(self, value: str) -> None:
        if self._bright_value is not None:
            try:
                self._bright_value.configure(text=f"{int(round(float(value)))}")
            except (TypeError, ValueError):
                pass

    def _on_brightness_release(self, _event: tk.Event | None = None) -> None:
        try:
            percent = int(round(float(self._bright_var.get())))
        except (TypeError, ValueError):
            return
        percent = max(0, min(100, percent))
        self._control_job(
            "brightness",
            lambda control: control.set_brightness(percent),
            f"亮度已设为 {percent}%",
        )

    def _on_rotation_selected(self, _event: tk.Event | None = None) -> None:
        raw = self._rotate_var.get().replace("°", "").strip()
        if not raw.isdigit():
            return
        degrees = int(raw)
        self._control_job(
            "rotation",
            lambda control: control.set_rotation(degrees),
            f"方向 {degrees}° 已保存到设备（不旋转推送画面）",
        )

    def _on_query(self) -> None:
        if device_mod is None:
            self._set_status("设备模块未就绪：无法读取设备状态", "warn")
            return
        self._set_status("正在读取设备状态…", "info")

        def job() -> None:
            failure: Exception | None = None
            reply: bytes = b""
            with self._ctl_lock:
                try:
                    self._open_control_locked()
                    reply = self._control.query()
                except Exception as exc:
                    failure = exc
                    self._close_control_locked()
            if failure is not None:
                self._post("status", friendly_device_error(failure), "error")
                return
            text, quadrant = _parse_control_reply(reply)
            self._post("control_reply", text)
            if quadrant is not None:
                self._post("rotation", quadrant * 90)

        threading.Thread(target=job, daemon=True, name="lg6-query").start()

    # ------------------------------------------------------------------ #
    # 推流
    # ------------------------------------------------------------------ #
    def _is_streaming(self) -> bool:
        return self._stream_thread is not None and self._stream_thread.is_alive()

    def _update_stream_buttons(self, streaming: bool) -> None:
        try:
            if streaming:
                self._btn_start.state(["disabled"])
                self._btn_stop.state(["!disabled"])
            else:
                self._btn_start.state(["!disabled"])
                self._btn_stop.state(["disabled"])
        except tk.TclError:
            pass

    def _fps_value(self) -> float:
        try:
            return max(1.0, float(self._fps_var.get()))
        except (TypeError, ValueError):
            return 30.0

    def _on_start_stream(self) -> None:
        if self._is_streaming():
            return
        if device_mod is None:
            self._set_status("无法推流：设备模块 device.py 缺失", "error")
            return
        if render_mod is None:
            self._set_status("无法推流：渲染引擎 render.py 缺失", "error")
            return
        vendor_running = False
        check_vendor = getattr(device_mod, "vendor_software_running", None)
        if callable(check_vendor):
            try:
                vendor_running = bool(check_vendor())
            except Exception as exc:  # 进程枚举失败不值得阻止推流
                _warn_once(f"无法检测官方软件是否在运行：{exc}")
        if vendor_running and self._device_hint is not None:
            self._device_hint.configure(
                text="检测到官方 TCOMAS 软件在运行：两边的画面会互相抢，建议先结束 DeviceDriver.exe"
            )
        self._stream_fps = self._fps_value()
        self._stream_frames = 0
        self._stream_started_at = time.monotonic()
        self._stream_stop.clear()
        self._stream_thread = threading.Thread(
            target=self._stream_loop, daemon=True, name="lg6-stream"
        )
        self._stream_thread.start()
        self._update_stream_buttons(True)
        self._set_connection("● 正在连接屏幕…", WARN)
        self._set_status("正在打开屏幕（推流线程）…", "info")

    def _on_stop_stream(self) -> None:
        if not self._is_streaming():
            self._update_stream_buttons(False)
            return
        self._stream_stop.set()
        try:
            self._btn_stop.state(["disabled"])
        except tk.TclError:
            pass
        self._set_status("正在停止推送…", "info")

    def _stream_loop(self) -> None:
        """推流线程：所有 Tk 访问都通过事件队列回到 UI 线程。"""
        screen: Any = None
        error_message: str | None = None
        frame_count = 0
        static_jpeg: bytes | None = None
        static_scene: dict | None = None
        try:
            fps = self._stream_fps
            self._post("stream_msg", "正在打开图像接口（MI_01）…", "info")
            screen = device_mod.Screen()
            screen.open()
            self._post("stream_state", "streaming")
            timeline = time.monotonic()
            last_stats = timeline
            stats_frames = 0
            stats_time = timeline
            while not self._stream_stop.is_set():
                scene = self._snapshot_scene()
                dynamic = scene_is_dynamic(scene)
                if dynamic:
                    frame_started = time.monotonic()
                    image = self.render_frame(scene, time.monotonic() - timeline)
                    jpeg = self.encode_frame(image)
                    screen.send_jpeg(jpeg)
                    frame_count += 1
                    interval = 1.0 / fps
                    remaining = interval - (time.monotonic() - frame_started)
                    if remaining > 0:
                        self._stream_stop.wait(remaining)
                else:
                    if static_jpeg is None or static_scene != scene:
                        image = self.render_frame(scene, time.monotonic() - timeline)
                        static_jpeg = self.encode_frame(image)
                        static_scene = copy.deepcopy(scene)
                    screen.send_jpeg(static_jpeg)
                    frame_count += 1
                    self._stream_stop.wait(STATIC_REFRESH_SECONDS)
                now = time.monotonic()
                if now - last_stats >= 0.4:
                    last_stats = now
                    # fps over the last window, not since the stream started, so the
                    # number reacts to changes instead of averaging them away.
                    window = max(0.001, now - stats_time)
                    achieved = (frame_count - stats_frames) / window
                    stats_frames = frame_count
                    stats_time = now
                    self._post("stream_stats", frame_count, achieved)
        except Exception as exc:
            error_message = friendly_device_error(exc)
            _warn_once(f"推流线程异常：{exc}")
        finally:
            if screen is not None:
                try:
                    screen.close()
                except Exception:
                    pass
            self._post("stream_state", "stopped", error_message, frame_count)

    # ------------------------------------------------------------------ #
    # 后台事件 -> UI
    # ------------------------------------------------------------------ #
    def _post(self, kind: str, *payload: Any) -> None:
        self._events.put((kind, payload))

    def _poll_events(self) -> None:
        if self._closing:
            return
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle_event(kind, payload)
            except Exception as exc:  # 单个事件处理失败不应中断事件泵
                _warn_once(f"事件处理失败（{kind}）：{exc}")
        self.after(80, self._poll_events)

    def _handle_event(self, kind: str, payload: tuple) -> None:
        if kind == "status":
            self._set_status(str(payload[0]), str(payload[1]) if len(payload) > 1 else "info")
        elif kind == "devices":
            self._apply_devices(payload[0])
        elif kind == "devices_error":
            self._set_status(str(payload[0]), "error")
        elif kind == "stream_msg":
            self._set_status(str(payload[0]), str(payload[1]) if len(payload) > 1 else "info")
        elif kind == "stream_state":
            state = str(payload[0])
            error = payload[1] if len(payload) > 1 else None
            frames = int(payload[2]) if len(payload) > 2 else 0
            if state == "streaming":
                self._set_connection("● 正在推流", ACCENT_2)
                self._update_stream_buttons(True)
                self._set_status(f"正在推送画面（{self._stream_fps:.0f} fps 目标）…", "stream")
            else:
                self._stream_thread = None
                self._update_stream_buttons(False)
                if error:
                    self._set_connection("● 设备异常", ERR)
                    self._set_status(str(error), "error")
                    if self._device_hint is not None:
                        self._device_hint.configure(text=str(error))
                else:
                    ready = bool(self._detected)
                    self._set_connection(
                        "● 设备就绪（未连接）" if ready else "● 未连接", ACCENT if ready else MUTED
                    )
                    self._set_status(f"已停止推送（共 {frames} 帧），屏幕约 10 秒后熄灭", "info")
                self._frames_label.configure(text=f"已推 {frames} 帧 · 已停止")
        elif kind == "stream_stats":
            frames = int(payload[0])
            fps = float(payload[1])
            self._stream_frames = frames
            if self._frames_label is not None:
                self._frames_label.configure(text=f"已推 {frames} 帧 · {fps:.1f} fps")
        elif kind == "control_reply":
            if self._device_reply is not None:
                self._device_reply.configure(text=f"设备回复：{payload[0]}")
            self._set_status("设备状态读取成功", "ok")
        elif kind == "rotation":
            try:
                degrees = int(payload[0]) % 360
            except (TypeError, ValueError):
                return
            if degrees in (0, 90, 180, 270):
                self._rotate_var.set(f"{degrees}°")

    def _on_tk_callback_error(self, exc_type: Any, exc: BaseException, tb: Any) -> None:
        """Tk 回调里的未捕获异常：提示到状态栏而不是弹出崩溃窗口。"""
        summary = "".join(traceback.format_exception_only(exc_type, exc)).strip()
        try:
            self._set_status(f"界面异常已捕获：{summary}", "error")
        except Exception:
            pass
        print(f"[lg6studio] 界面回调异常：{summary}", file=sys.stderr)

    # ------------------------------------------------------------------ #
    # 冒烟截图 / 关闭
    # ------------------------------------------------------------------ #
    def _schedule_smoke(self) -> None:
        self.after(1200, self._smoke_capture)
        self.after(1600, self._smoke_finish)
        threading.Thread(target=self._smoke_watchdog, daemon=True, name="lg6-smoke-wd").start()

    def _smoke_watchdog(self) -> None:
        time.sleep(20.0)
        print("[smoke] 超时兜底退出（20 秒）", flush=True)
        os._exit(0)

    def _smoke_capture(self) -> None:
        path = self.smoke_shot or ""
        if not path.lower().endswith(".png"):
            path += ".png"
        self.smoke_shot = path
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.update_idletasks()
            self.update()
            time.sleep(0.05)
            x = self._canvas.winfo_rootx()
            y = self._canvas.winfo_rooty()
            width = max(1, self._canvas.winfo_width())
            height = max(1, self._canvas.winfo_height())
            from PIL import ImageGrab

            image = ImageGrab.grab(bbox=(x, y, x + width, y + height))
            image.save(path)
            print(f"[smoke] 预览区截图已保存：{path}（{image.width}×{image.height}）", flush=True)
        except Exception as exc:
            print(f"[smoke] 预览区截图失败：{exc!r}", flush=True)
            try:
                fallback = self._last_frame_img
                if fallback is None:
                    fallback = make_placeholder("预览不可用")
                fallback.convert("RGB").save(path)
                print(f"[smoke] 已用渲染帧兜底保存：{path}", flush=True)
            except Exception as fallback_exc:
                print(f"[smoke] 兜底保存也失败：{fallback_exc!r}", flush=True)
        # 额外保存整窗截图，方便人工检查布局（不影响主交付路径）
        try:
            stem, extension = os.path.splitext(path)
            window_path = f"{stem}-window{extension or '.png'}"
            self.update_idletasks()
            x = self.winfo_rootx()
            y = self.winfo_rooty()
            width = max(1, self.winfo_width())
            height = max(1, self.winfo_height())
            from PIL import ImageGrab

            ImageGrab.grab(bbox=(x, y, x + width, y + height)).save(window_path)
            print(f"[smoke] 整窗截图已保存：{window_path}", flush=True)
        except Exception as exc:
            print(f"[smoke] 整窗截图失败（可忽略）：{exc!r}", flush=True)

    def _smoke_finish(self) -> None:
        print("[smoke] 自动退出", flush=True)
        self.on_close()

    def on_close(self) -> None:
        """关闭窗口：停止推流线程、关闭设备句柄、销毁窗口。"""
        if self._closing:
            return
        self._closing = True
        try:
            if self._stream_thread is not None and self._stream_thread.is_alive():
                self._stream_stop.set()
                self._stream_thread.join(2.5)
        except Exception:
            pass
        try:
            with self._ctl_lock:
                self._close_control_locked()
        except Exception:
            pass
        try:
            self._preview_worker.stop()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    def shutdown(self) -> None:
        """供 main() 的 finally 使用的幂等清理。"""
        if not self._closing:
            self.on_close()


def _parse_control_reply(reply: bytes) -> tuple[str, int | None]:
    """解析 0x81 查询回复（441 字节，buf[5]=旋转象限）。"""
    if not reply:
        return "空回复", None
    quadrant: int | None = None
    if len(reply) > 5:
        quadrant = int(reply[5]) & 0xFF
    head = " ".join(f"{byte:02X}" for byte in reply[:8])
    text = f"{len(reply)} 字节 · 象限 {quadrant if quadrant is not None else '?'} · {head}"
    return text, quadrant


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lg6studio",
        description="LG600 Studio：TCOMAS LG600 屏幕的开源控制台（Tkinter GUI）",
    )
    parser.add_argument(
        "--smoke-shot",
        metavar="PNG",
        default=None,
        help="启动后 1.2 秒把预览区截图保存为 PNG，再 0.4 秒自动退出（无人值守验证）",
    )
    parser.add_argument("--project", metavar="PATH", default=None, help="启动时打开 .lg6.json 项目")
    parser.add_argument(
        "--autostream",
        action="store_true",
        help="启动后自动开始推流（配合 --project 适合开机自启常驻）",
    )
    parser.add_argument("--version", action="version", version=f"LG600 Studio {APP_VERSION}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """启动 GUI；返回进程退出码（冒烟模式恒为 0）。"""
    args = parse_args(argv)
    smoke = args.smoke_shot
    try:
        app = App(smoke_shot=smoke)
    except Exception:
        if smoke:
            print("[smoke] 窗口启动失败：", flush=True)
            traceback.print_exc()
            return 0
        traceback.print_exc()
        return 1
    try:
        if args.project:
            app.load_project(args.project, notify=False)
        if args.autostream:
            app.after(700, app._on_start_stream)
        app.mainloop()
    except KeyboardInterrupt:
        pass
    except Exception:
        if smoke:
            print("[smoke] 主循环异常：", flush=True)
            traceback.print_exc()
            return 0
        traceback.print_exc()
        return 1
    finally:
        try:
            app.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
