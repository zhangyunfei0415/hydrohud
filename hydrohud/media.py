"""正在播放数据源：Windows SMTC（System Media Transport Controls）。

``winsdk`` 是可选依赖（Windows 10 1809+ 的系统媒体接口）。本模块：

- :func:`available` 判断当前环境能否读 SMTC；缺 ``winsdk`` 或非 Windows 时返回
  ``False``，模块其余部分仍可正常导入；
- :class:`MediaHub` 在 daemon 线程里按 ``interval`` 轮询当前会话，:meth:`snapshot`
  返回 ``{"title","artist","album","status","source","cover"}``；没有播放会话时
  返回 ``{}``；
- 封面只在换歌时拉取一次（同一 title 复用），解码为 RGB 并缩到 96×96 以内，
  解码失败只是没有封面，不影响歌名显示。

所有 SMTC 异常都被吞掉并记录到 :func:`last_error`，绝不抛给渲染线程。
导入本模块不导入 ``winsdk``：只有调用 :func:`available` / :func:`get_hub` /
:meth:`MediaHub.start` 时才探测。
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

import asyncio
import inspect
import sys
import threading
from io import BytesIO
from typing import Any, Callable

from PIL import Image

__all__ = [
    "COVER_CACHE_SIZE",
    "COVER_MAX_SIZE",
    "DEFAULT_INTERVAL",
    "STATUS_TEXT",
    "MediaHub",
    "available",
    "close_hub",
    "get_hub",
    "last_error",
]

#: 默认轮询间隔（秒）。SMTC 元数据变化不频繁，1 秒足够。
DEFAULT_INTERVAL = 1.0

#: 封面缓存条数（按 title 去重，LRU）。
COVER_CACHE_SIZE = 8

#: 封面最长边（像素）。
COVER_MAX_SIZE = 96

#: ``close()`` 等待后台线程退出的最长时间（秒）。
JOIN_TIMEOUT = 2.0

#: 挂起等待时单次睡眠上限（秒），保证 ``close()`` 能尽快返回。
_WAIT_SLICE = 0.2

#: SMTC 的播放状态枚举值（``GlobalSystemMediaTransportControlsSessionPlaybackStatus``）。
STATUS_CLOSED = 0
STATUS_OPENED = 1
STATUS_CHANGING = 2
STATUS_PLAYING = 4
STATUS_PAUSED = 5
STATUS_STOPPED = 6

#: 播放状态标识（英文，供渲染层判断是否滚动/隐藏）到中文的映射。
STATUS_TEXT: dict[str, str] = {
    "playing": "播放中",
    "paused": "已暂停",
    "stopped": "已停止",
    "closed": "已关闭",
    "opened": "就绪",
    "changing": "切换中",
    "unknown": "未知",
}

_STATUS_NAMES: dict[int, str] = {
    STATUS_CLOSED: "closed",
    STATUS_OPENED: "opened",
    STATUS_CHANGING: "changing",
    STATUS_PLAYING: "playing",
    STATUS_PAUSED: "paused",
    STATUS_STOPPED: "stopped",
}

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


def _load_sdk() -> Any:
    """默认 SDK 加载器：返回带 ``control`` / ``streams`` 的命名空间，失败返回 ``None``。

    非 Windows 平台直接返回 ``None``，不尝试 import。
    """
    if sys.platform != "win32":
        return None
    try:
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as _Manager,
        )
        from winsdk.windows.storage.streams import DataReader as _DataReader
    except ImportError:
        return None

    class _Sdk:
        """把用到的 winsdk 成员收成一个命名空间，方便注入替身。"""

        control = type(
            "control", (), {"GlobalSystemMediaTransportControlsSessionManager": _Manager}
        )
        streams = type("streams", (), {"DataReader": _DataReader})

    return _Sdk


#: 可替换的加载器（测试注入替身时也可直接替换 :data:`_winsdk`）。
_sdk_loader: Callable[[], Any] | None = _load_sdk

#: 已加载的 SDK；非 ``None`` 时表示可用。测试可直接赋一个假对象。
_winsdk: Any = None

#: ``_winsdk`` 是否已探测过（避免每次轮询都尝试 import）。
_sdk_resolved = False

_sdk_lock = threading.Lock()


def _sdk() -> Any:
    """返回已缓存的 SDK；第一次调用时执行 :data:`_sdk_loader` 探测。"""
    global _winsdk, _sdk_resolved
    with _sdk_lock:
        if _winsdk is not None:
            return _winsdk
        if _sdk_resolved:
            return None
        _sdk_resolved = True
        loader = _sdk_loader
        if loader is None:
            return None
        try:
            _winsdk = loader()
        except Exception as exc:  # 探测本身失败不应影响界面
            _record_error("加载 winsdk 失败", exc)
            _winsdk = None
        return _winsdk


def _reset_sdk(sdk: Any = None) -> None:
    """测试用：重置 SDK 缓存（``sdk`` 非空时同时把它设为当前 SDK）。"""
    global _winsdk, _sdk_resolved
    with _sdk_lock:
        _winsdk = sdk
        _sdk_resolved = sdk is not None


def available() -> bool:
    """当前环境能否读 SMTC：Windows 且 ``winsdk`` 可导入。"""
    if sys.platform != "win32":
        return False
    try:
        return _sdk() is not None
    except Exception as exc:  # 兜底：探测异常等同不可用
        _record_error("探测 SMTC 失败", exc)
        return False


async def _maybe_await(value: Any) -> Any:
    """``value`` 是 awaitable 就 await，否则原样返回（兼容同步替身）。"""
    if inspect.isawaitable(value):
        return await value
    return value


def _call(target: Any, name: str) -> Any:
    """调用 ``target`` 上的零参方法；方法缺失或调用失败返回 ``None``。"""
    method = getattr(target, name, None)
    if method is None:
        return None
    try:
        return method()
    except Exception as exc:  # 单个同步方法失败不影响整次轮询
        _record_error(f"调用 {name} 失败", exc)
        return None


def _text(value: Any) -> str:
    """把 SMTC 的字符串字段转成干净 str（``None`` 变空串）。"""
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:  # pragma: no cover - 只可能是坏替身
        return ""


def _status_name(value: Any) -> str:
    """把播放状态枚举值映射成 ``playing`` / ``paused`` 之类的标识。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "unknown"
    return _STATUS_NAMES.get(number, "unknown")


def _buffer_bytes(buffer: Any) -> bytes | None:
    """把 WinRT ``IBuffer`` 转成 bytes；两种常见转换都失败时返回 ``None``。"""
    if buffer is None:
        return None
    if isinstance(buffer, (bytes, bytearray)):
        return bytes(buffer)
    for convert in (bytes, lambda item: bytes(memoryview(item))):
        try:
            return convert(buffer)
        except Exception:  # 一种转换失败就试下一种，全失败返回 None
            continue
    return None


async def _read_thumbnail_bytes(sdk: Any, reference: Any) -> bytes | None:
    """从 SMTC 缩略图引用读出原始字节（PNG/JPEG 等）；失败返回 ``None``。"""
    if reference is None:
        return None
    open_read = getattr(reference, "open_read_async", None)
    if open_read is None:
        return None
    try:
        stream = await _maybe_await(open_read())
    except Exception as exc:
        _record_error("读取封面流失败", exc)
        return None
    if stream is None:
        return None
    try:
        size = int(getattr(stream, "size", 0) or 0)
    except (TypeError, ValueError):
        size = 0
    streams = getattr(sdk, "streams", None)
    reader_class = getattr(streams, "DataReader", None) if streams is not None else None
    if reader_class is None or size <= 0:
        return None
    try:
        reader = reader_class(stream)
        load = getattr(reader, "load_async", None)
        if load is not None:
            await _maybe_await(load(size))
        read_buffer = getattr(reader, "read_buffer", None)
        if read_buffer is None:
            return None
        return _buffer_bytes(read_buffer(size))
    except Exception as exc:
        _record_error("解码封面缓冲失败", exc)
        return None


def _decode_cover(data: bytes | None) -> Image.Image | None:
    """把图片字节解码成 RGB 图并缩到 :data:`COVER_MAX_SIZE` 以内；失败返回 ``None``。"""
    if not data:
        return None
    try:
        image = Image.open(BytesIO(data))
        image.load()
        cover = image.convert("RGB")
        cover.thumbnail((COVER_MAX_SIZE, COVER_MAX_SIZE), Image.LANCZOS)
        return cover
    except Exception as exc:
        _record_error("解码封面图片失败", exc)
        return None


class MediaHub:
    """后台轮询 SMTC 当前会话的采样中心。

    :param interval: 轮询间隔（秒），最小 0.1。

    ``cover`` 是 PIL 图像；``snapshot()`` 返回的 dict 是浅拷贝，调用方可以随意
    使用，但不要修改 ``cover`` 对象本身（它被缓存复用）。
    """

    def __init__(self, interval: float = DEFAULT_INTERVAL) -> None:
        self._interval = max(0.1, float(interval))
        self._lock = threading.RLock()
        self._data: dict = {}
        self._cover_cache: dict[str, Image.Image] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- 基础属性
    @property
    def interval(self) -> float:
        """轮询间隔（秒）。"""
        return self._interval

    def running(self) -> bool:
        """后台线程是否还在跑（调试用）。"""
        thread = self._thread
        return thread is not None and thread.is_alive()

    # ---------------------------------------------------------------- 快照
    def snapshot(self) -> dict:
        """返回最近一次播放状态；没有会话或不可用时返回 ``{}``。"""
        with self._lock:
            data = self._data
            if not data:
                return {}
            return dict(data)

    def describe(self) -> str:
        """返回一行中文调试描述。"""
        data = self.snapshot()
        if not data:
            if not available():
                return "正在播放：SMTC 不可用（未安装 winsdk 或非 Windows）"
            error = last_error()
            return "正在播放：无活动会话" + (f"，上次错误：{error}" if error else "")
        status = STATUS_TEXT.get(str(data.get("status")), str(data.get("status") or "未知"))
        return (
            f"正在播放：{data.get('title') or '未知曲目'} - {data.get('artist') or '未知歌手'}"
            f"（{status}，封面 {'有' if data.get('cover') is not None else '无'}）"
        )

    # ---------------------------------------------------------------- 刷新
    def refresh(self) -> bool:
        """同步轮询一次并在有变化时更新快照；不可用或失败返回 ``False``。

        供测试与手动调试用；后台线程内部走 :meth:`_poll_async`。
        """
        if not available():
            self._clear()
            return False
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is not None:  # 已被异步代码调用：不能嵌套 asyncio.run
            _record_error("当前线程已有事件循环，无法同步轮询")
            return False
        try:
            asyncio.run(self._poll_async())
        except Exception as exc:
            _record_error("轮询 SMTC 失败", exc)
            return False
        return True

    def _clear(self) -> None:
        """清空快照（会话消失时调用）。"""
        with self._lock:
            self._data = {}

    async def _poll_async(self) -> dict:
        """读出当前会话的元数据与封面，写入 ``self._data`` 并返回它。"""
        sdk = _sdk()
        if sdk is None:
            return {}
        control = getattr(sdk, "control", None)
        manager_class = (
            getattr(control, "GlobalSystemMediaTransportControlsSessionManager", None)
            if control is not None
            else None
        )
        request_async = getattr(manager_class, "request_async", None)
        if request_async is None:
            return {}
        manager = await _maybe_await(request_async())
        if manager is None:
            self._clear()
            return {}
        session = _call(manager, "get_current_session")
        if session is None:
            self._clear()
            return {}
        properties = await _maybe_await(_call_async(session, "try_get_media_properties_async"))
        title = _text(getattr(properties, "title", None)) if properties is not None else ""
        artist = _text(getattr(properties, "artist", None)) if properties is not None else ""
        album = _text(getattr(properties, "album_title", None)) if properties is not None else ""
        if not title and not artist:
            self._clear()
            return {}
        playback = _call(session, "get_playback_info")
        status = _status_name(getattr(playback, "playback_status", None))
        data = {
            "title": title,
            "artist": artist,
            "album": album,
            "status": status,
            "source": _text(getattr(session, "source_app_user_model_id", None)),
            "cover": await self._cover_for(session, title),
        }
        with self._lock:
            self._data = data
        return data

    async def _cover_for(self, session: Any, title: str) -> Image.Image | None:
        """取封面：同一 title 命中缓存则复用，否则拉取并解码一次。"""
        if not title:
            return None
        with self._lock:
            cached = self._cover_cache.get(title)
            if cached is not None:
                self._cover_cache[title] = cached  # 命中即刷新 LRU 顺序
                return cached
        reference = await _maybe_await(_call_async(session, "try_get_thumbnail_async"))
        raw = await _read_thumbnail_bytes(_sdk(), reference)
        cover = _decode_cover(raw)
        if cover is None:
            return None
        with self._lock:
            self._cover_cache[title] = cover
            while len(self._cover_cache) > COVER_CACHE_SIZE:
                self._cover_cache.pop(next(iter(self._cover_cache)))
        return cover

    # ---------------------------------------------------------------- 线程
    def start(self) -> None:
        """启动后台轮询线程；SMTC 不可用时立即返回（不创建线程）。幂等。"""
        if not available():
            return
        with self._lock:
            thread = self._thread
            if thread is not None and thread.is_alive() and not self._stop.is_set():
                return
            if thread is not None and thread.is_alive():
                thread.join(timeout=0.5)  # 上一次 close() 还没退完
            self._stop.clear()
            thread = threading.Thread(target=self._run, name="hydrohud-media", daemon=True)
            self._thread = thread
            thread.start()

    def _run(self) -> None:
        """后台线程入口：在新事件循环里反复轮询。"""
        try:
            asyncio.run(self._run_loop())
        except Exception as exc:  # 线程里绝不能把异常交给 threading.excepthook
            _record_error("SMTC 线程退出异常", exc)

    async def _run_loop(self) -> None:
        """轮询循环：``interval`` 切片睡眠，保证 ``close()`` 及时返回。"""
        while not self._stop.is_set():
            try:
                await self._poll_async()
            except Exception as exc:  # 单次轮询失败不能让线程死掉
                _record_error("轮询 SMTC 失败", exc)
            waited = 0.0
            while waited < self._interval and not self._stop.is_set():
                await asyncio.sleep(min(_WAIT_SLICE, self._interval - waited))
                waited += _WAIT_SLICE
            if self._stop.is_set():
                break

    def close(self) -> None:
        """停止后台线程并等待退出；可重复调用。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=JOIN_TIMEOUT)
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def clear_cover_cache(self) -> None:
        """清空封面缓存（换场景或主题时用）。"""
        with self._lock:
            self._cover_cache.clear()


async def _call_async(target: Any, name: str) -> Any:
    """调用 ``target`` 上的零参异步方法并等待结果；缺失或失败返回 ``None``。"""
    method = getattr(target, name, None)
    if method is None:
        return None
    try:
        return await _maybe_await(method())
    except Exception as exc:
        _record_error(f"调用 {name} 失败", exc)
        return None


_hub: MediaHub | None = None
_hub_lock = threading.Lock()


def get_hub(interval: float = DEFAULT_INTERVAL) -> MediaHub:
    """返回进程级 :class:`MediaHub` 单例并确保线程已启动（不可用时不会建线程）。"""
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = MediaHub(interval)
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
