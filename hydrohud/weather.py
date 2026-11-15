"""天气数据源：Open-Meteo 定时拉取、本地缓存与离线降级。

模块职责：

- :data:`WEATHER_CODE_TEXT` 把 WMO 天气代码翻成中文，:func:`code_text` /
  :func:`code_icon` 供渲染层显示文字与内置矢量图标；
- :class:`WeatherHub` 在后台线程里按 ``interval`` 刷新当前天气，成功时更新内存
  并原子写入 ``%APPDATA%\\HydroHUD\\weather-cache.json``；离线时继续使用缓存，
  所以界面上永远有上一份可用数据；
- 进程级单例由 :func:`get_hub` 提供，坐标变化时重建 hub 并关闭旧线程。

所有网络与文件异常都被吞掉并记录到 :func:`last_error`，不向渲染线程抛出。
导入本模块不发起任何请求、不启动线程：只有调用 :func:`get_hub` 或
:meth:`WeatherHub.start` 才会联网。
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

import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from hydrohud import settings

__all__ = [
    "API_URL",
    "CACHE_FILE_NAME",
    "DEFAULT_INTERVAL",
    "DEFAULT_LAT",
    "DEFAULT_LON",
    "TIMEOUT",
    "WEATHER_CODE_TEXT",
    "WeatherHub",
    "close_hub",
    "code_icon",
    "code_text",
    "get_hub",
    "last_error",
    "parse_payload",
]

#: Open-Meteo 当前天气接口（免 API key）。
API_URL = "https://api.open-meteo.com/v1/forecast"

#: 默认坐标：北京。
DEFAULT_LAT = 39.9042
DEFAULT_LON = 116.4074

#: 默认刷新间隔（秒）：天气变化慢，10 分钟足够，也避免触发免费额度。
DEFAULT_INTERVAL = 600.0

#: 单次 HTTP 请求超时（秒）。
TIMEOUT = 8.0

#: ``close()`` 等待后台线程退出的最长时间（秒）。
JOIN_TIMEOUT = 2.0

#: 缓存文件名，位于 :func:`hydrohud.settings.app_data_dir` 下。
CACHE_FILE_NAME = "weather-cache.json"

#: 请求的 ``current`` 字段，顺序即 URL 里的顺序。
CURRENT_FIELDS = (
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "wind_speed_10m",
    "weather_code",
)

#: 缓存文件里保存的键；``desc`` / ``icon`` 是读出后按 ``code`` 派生的，不落盘。
CACHE_KEYS = ("fetched", "place", "code", "temp", "feels", "humidity", "wind")

#: 未知／缺省天气的文字。
UNKNOWN_TEXT = "未知"

#: WMO 天气代码到中文描述。范围依据 Open-Meteo 文档的 ``weather_code`` 表。
WEATHER_CODE_TEXT: dict[int, str] = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴大冰雹",
}

#: 各天气图标的名字，供 ``render`` 画内置矢量图标。
ICON_NAMES = ("sun", "cloud", "rain", "snow", "fog", "storm")

#: 图标回落值：任何未覆盖的代码都按多云处理。
_FALLBACK_ICON = "cloud"

_LAST_ERROR: str | None = None
_last_error_lock = threading.Lock()


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


def code_text(code: int | None) -> str:
    """把 WMO 天气代码翻成中文；未知代码返回 :data:`UNKNOWN_TEXT`。"""
    if code is None:
        return UNKNOWN_TEXT
    return WEATHER_CODE_TEXT.get(int(code), UNKNOWN_TEXT)


def code_icon(code: int | None) -> str:
    """把 WMO 天气代码映射成图标名（``sun``/``cloud``/``rain``/``snow``/``fog``/``storm``）。"""
    if code is None:
        return _FALLBACK_ICON
    value = int(code)
    if value == 0:
        return "sun"
    if 1 <= value <= 3:
        return "cloud"
    if value in (45, 48):
        return "fog"
    if 95 <= value <= 99:
        return "storm"
    if 71 <= value <= 77 or value in (85, 86):
        return "snow"
    if 51 <= value <= 67 or 80 <= value <= 82:
        return "rain"
    return _FALLBACK_ICON


def _as_number(value: Any) -> float | None:
    """把 JSON 数值转成有限 float；``None``、非数字、NaN／Inf 一律返回 ``None``。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _as_int(value: Any) -> int | None:
    """把 JSON 数值转成 int；失败返回 ``None``（不抛异常）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clamp_percent(value: float | None) -> float | None:
    """把湿度之类的百分数夹到 ``0..100``；``None`` 原样返回。"""
    if value is None:
        return None
    return max(0.0, min(100.0, float(value)))


def _format_number(value: Any, digits: int = 1) -> str:
    """把数值格式化成短文本；缺失值返回全角破折号。"""
    number = _as_number(value)
    if number is None:
        return "—"
    if number == int(number):
        return str(int(number))
    return f"{number:.{digits}f}"


def parse_payload(payload: Any) -> dict:
    """解析 Open-Meteo 响应里的 ``current`` 段，返回本模块的原始字段 dict。

    缺 ``current`` 段或 ``weather_code`` 时抛 :class:`ValueError`（调用方会吞掉）；
    温度之类允许为 ``null``（Open-Meteo 对无数据点会返回 ``null``）。
    """
    if not isinstance(payload, dict):
        raise ValueError("响应不是 JSON 对象")
    current = payload.get("current")
    if not isinstance(current, dict):
        raise ValueError("响应缺少 current 段")
    code = _as_int(current.get("weather_code"))
    if code is None:
        raise ValueError("响应缺少 weather_code")
    return {
        "code": code,
        "temp": _as_number(current.get("temperature_2m")),
        "feels": _as_number(current.get("apparent_temperature")),
        "humidity": _clamp_percent(_as_number(current.get("relative_humidity_2m"))),
        "wind": _as_number(current.get("wind_speed_10m")),
    }


def _place_of(payload: Any, lat: float, lon: float) -> str:
    """取地点名：优先 Open-Meteo 回传的 ``timezone``，否则用坐标。"""
    if isinstance(payload, dict):
        zone = payload.get("timezone")
        if isinstance(zone, str) and zone:
            return zone
    return f"{lat:.4f},{lon:.4f}"


def _build_data(payload: Any, lat: float, lon: float) -> dict:
    """把响应整理成内存／缓存共用的 dict（含派生字段 desc、icon）。"""
    parsed = parse_payload(payload)
    code = parsed["code"]
    return {
        "fetched": time.time(),
        "place": _place_of(payload, lat, lon),
        "code": code,
        "temp": parsed["temp"],
        "feels": parsed["feels"],
        "humidity": parsed["humidity"],
        "wind": parsed["wind"],
        "desc": code_text(code),
        "icon": code_icon(code),
    }


class WeatherHub:
    """后台刷新当前天气的采样中心（一个坐标一个 hub）。

    :param lat: 纬度（-90..90）。
    :param lon: 经度（-180..180）。
    :param interval: 两次刷新的间隔秒数；``0`` 表示每次 ``refresh()`` 都联网。
    :param cache_path: 缓存文件路径；``None`` 时用
        ``%APPDATA%\\HydroHUD\\weather-cache.json``。

    线程是 daemon，:meth:`close` 会置位停止事件并等待退出。
    """

    def __init__(
        self,
        lat: float,
        lon: float,
        interval: float = DEFAULT_INTERVAL,
        cache_path: str | Path | None = None,
    ) -> None:
        self._lat = float(lat)
        self._lon = float(lon)
        self._interval = max(0.0, float(interval))
        self._cache_path = (
            Path(cache_path) if cache_path is not None else self._default_cache_path()
        )
        self._lock = threading.RLock()
        self._data: dict | None = None
        self._fetched = 0.0
        self._cache_checked = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- 基础属性
    @property
    def lat(self) -> float:
        """纬度。"""
        return self._lat

    @property
    def lon(self) -> float:
        """经度。"""
        return self._lon

    @property
    def interval(self) -> float:
        """刷新间隔（秒）。"""
        return self._interval

    @property
    def cache_path(self) -> Path:
        """缓存文件路径。"""
        return self._cache_path

    @staticmethod
    def _default_cache_path() -> Path:
        """默认缓存路径：``%APPDATA%\\HydroHUD\\weather-cache.json``。"""
        return settings.app_data_dir() / CACHE_FILE_NAME

    # ------------------------------------------------------------------ URL
    def url(self) -> str:
        """返回本次请求的完整 URL（调试与测试用）。"""
        query = urllib.parse.urlencode(
            {
                "latitude": f"{self._lat:.4f}",
                "longitude": f"{self._lon:.4f}",
                "current": ",".join(CURRENT_FIELDS),
                "timezone": "auto",
            }
        )
        return f"{API_URL}?{query}"

    # ---------------------------------------------------------------- 快照
    def snapshot(self) -> dict:
        """返回最近一次天气 dict；无数据时返回 ``{}``（永不为 ``None``）。

        第一次调用会尝试读缓存（只读一次），所以离线启动也能立刻显示上次数据。
        返回值是副本，调用方随意修改不会影响 hub。
        """
        with self._lock:
            if self._data is None and not self._cache_checked:
                self._cache_checked = True
                cached = self._read_cache()
                if cached is not None:
                    self._data = cached
                    self._fetched = float(cached.get("fetched") or 0.0)
            return dict(self._data) if self._data is not None else {}

    def describe(self) -> str:
        """返回一行中文调试描述（地点、天气、温度、数据来源）。"""
        data = self.snapshot()
        if not data:
            error = last_error()
            suffix = f"，上次错误：{error}" if error else ""
            return f"天气：{self._lat:.4f},{self._lon:.4f} 暂无数据{suffix}"
        return (
            f"天气：{data.get('place') or '未知地点'} {data.get('desc') or UNKNOWN_TEXT} "
            f"{_format_number(data.get('temp'))}°C（体感 {_format_number(data.get('feels'))}°C）"
            f" 湿度 {_format_number(data.get('humidity'))}% 风 {_format_number(data.get('wind'))} km/h"
            f" code={data.get('code')}"
        )

    # ---------------------------------------------------------------- 刷新
    def refresh(self, force: bool = False) -> bool:
        """联网刷新一次；成功返回 ``True``，失败（或未到间隔）返回 ``False``。

        :param force: 忽略 ``interval``，强制联网。

        任何异常都只记录到 :func:`last_error`，不向外抛。
        """
        if not force and self._fresh():
            return False
        try:
            with urllib.request.urlopen(self.url(), timeout=TIMEOUT) as response:
                raw = response.read()
            payload = json.loads(raw.decode("utf-8"))
            data = _build_data(payload, self._lat, self._lon)
        except Exception as exc:  # 网络/解析失败都算一次失败刷新，界面继续用旧数据
            _record_error("天气刷新失败", exc)
            return False
        with self._lock:
            self._data = data
            self._fetched = float(data["fetched"])
        self._write_cache(data)
        return True

    def _fresh(self) -> bool:
        """缓存数据是否仍在 ``interval`` 内（``interval <= 0`` 时永远为 False）。"""
        if self._interval <= 0.0:
            return False
        with self._lock:
            if self._data is None or self._fetched <= 0.0:
                return False
            return (time.time() - self._fetched) < self._interval

    # ---------------------------------------------------------------- 线程
    def start(self) -> None:
        """启动后台刷新线程（先回填缓存，再按 ``interval`` 刷新）。幂等。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if not self._stop.is_set():
                    return
                self._thread.join(timeout=0.5)  # 上一次 close() 还没退完，等一下子
            self._stop.clear()
            thread = threading.Thread(target=self._run, name="hydrohud-weather", daemon=True)
            self._thread = thread
            thread.start()

    def _run(self) -> None:
        """后台循环：读缓存 → 刷新 → 等待 ``interval``。"""
        try:
            self.snapshot()  # 缓存回填（只读一次）
            while not self._stop.is_set():
                try:
                    self.refresh()
                except Exception as exc:  # refresh 自己已兜住异常，这里是最后一道保险
                    _record_error("天气线程异常", exc)
                if self._stop.wait(self._interval):
                    break
        except Exception as exc:  # 线程里绝不能把异常交给 threading.excepthook
            _record_error("天气线程退出异常", exc)

    def close(self) -> None:
        """停止后台线程并等待它退出；可重复调用。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=JOIN_TIMEOUT)
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def running(self) -> bool:
        """后台线程是否还在跑（调试用）。"""
        thread = self._thread
        return thread is not None and thread.is_alive()

    # ---------------------------------------------------------------- 缓存
    def _read_cache(self) -> dict | None:
        """读缓存文件；不存在、损坏或不是对象时返回 ``None`` 并记录错误。"""
        path = self._cache_path
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            _record_error("读取天气缓存失败", exc)
            return None
        try:
            payload = json.loads(text)
        except ValueError as exc:
            _record_error("天气缓存不是合法 JSON", exc)
            return None
        if not isinstance(payload, dict):
            _record_error("天气缓存内容不是 JSON 对象")
            return None
        data = {key: payload.get(key) for key in CACHE_KEYS}
        data["fetched"] = _as_number(data.get("fetched")) or 0.0
        code = _as_int(data.get("code"))
        data["code"] = code
        data["desc"] = code_text(code)
        data["icon"] = code_icon(code)
        return data

    def _write_cache(self, data: dict) -> None:
        """原子写缓存：先写同目录临时文件，再 ``os.replace``。"""
        path = self._cache_path
        payload = {key: data.get(key) for key in CACHE_KEYS}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary, path)
        except OSError as exc:
            _record_error("写入天气缓存失败", exc)


_hub: WeatherHub | None = None
_hub_lock = threading.Lock()


def get_hub(lat: float | None = None, lon: float | None = None) -> WeatherHub:
    """返回进程级 :class:`WeatherHub` 单例，并确保后台线程已启动。

    省略坐标时用 :data:`DEFAULT_LAT` / :data:`DEFAULT_LON`（北京）；坐标变化会
    关闭旧 hub 的线程并重建。
    """
    global _hub
    target_lat = DEFAULT_LAT if lat is None else float(lat)
    target_lon = DEFAULT_LON if lon is None else float(lon)
    with _hub_lock:
        if _hub is not None and (_hub.lat != target_lat or _hub.lon != target_lon):
            _hub.close()
            _hub = None
        if _hub is None:
            _hub = WeatherHub(target_lat, target_lon)
        hub = _hub
    hub.start()
    return hub


def close_hub() -> None:
    """关闭并丢弃进程级单例（再次 :func:`get_hub` 会新建）。"""
    global _hub
    with _hub_lock:
        hub = _hub
        _hub = None
    if hub is not None:
        hub.close()
