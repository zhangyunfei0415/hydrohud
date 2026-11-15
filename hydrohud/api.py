"""本地 HTTP 控制面：让外部程序（Home Assistant / Stream Deck / 脚本 / hudctl）控制 HydroHUD。

设计要点
--------
- 只监听回环地址（默认 ``127.0.0.1:4780``），用 token 鉴权，不向局域网暴露；
- :class:`Host` 是宿主抽象，真正的动作（推场景、调亮度、开关屏幕）由 GUI 实现；
- 本模块只依赖标准库 + :mod:`hydrohud.scenes`（场景校验），导入时不监听端口、
  不起线程、不读 token 文件（token 文件在构造 :class:`ApiServer` 时读取/生成）；
- 关闭用 :meth:`ApiServer.stop`，可安全重复调用；所有服务线程均为 daemon。

线程契约（必须遵守）
--------------------
HTTP 请求在 ``ThreadingHTTPServer`` 的工作线程里处理。**这些线程绝不直接操作 Tk**：
:class:`Host` 的每个方法都可能从任意服务线程被调用，宿主实现（``app.py``）必须把
操作封送回 UI 线程——通常是向 ``queue.Queue`` 投递一个闭包，再用 ``root.after``
在 UI 线程执行，服务线程用 ``threading.Event`` 等结果并返回。直接在工作线程里
调 ``tkinter`` 会造成随机崩溃或界面卡死。

路由
----
===== =========================== ================================================
方法  路径                        说明
===== =========================== ================================================
GET   ``/status``                 ``host.status()``
GET   ``/scene``                  ``host.get_scene()``
POST  ``/scene``                  ``{"scene": {...}}`` 或裸场景 → 校验后 ``apply_scene``
POST  ``/brightness``             ``{"value": 0-100}``
POST  ``/power``                  ``{"on": true|false}``
POST  ``/stream``                 ``{"on": true|false}``
GET   ``/metrics``                ``host.metrics()``
GET   ``/events``                 SSE，每秒一行 ``data: {status}``，``?limit=N`` 发 N 条后关闭
===== =========================== ================================================

鉴权：请求头 ``X-HydroHUD-Token`` 或查询参数 ``?token=``，使用
:func:`hmac.compare_digest` 常量时间比较；不匹配返回 401。``token=""`` 表示关闭鉴权
（仅建议在完全可信的本机环境使用）。
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlsplit

from . import plugins, scenes

__all__ = [
    "DEFAULT_PORT",
    "MAX_BODY_BYTES",
    "TOKEN_FILE_NAME",
    "Host",
    "ApiServer",
    "token_path",
    "ensure_token",
]

DEFAULT_PORT = 4780
MAX_BODY_BYTES = 1024 * 1024
TOKEN_FILE_NAME = "api-token.txt"
EVENT_INTERVAL_SECONDS = 1.0


@runtime_checkable
class Host(Protocol):
    """控制面看到的宿主接口；实现方（``app.py``）负责封送到 UI 线程。

    所有方法都可能从 HTTP 工作线程调用，实现里不要直接碰 Tk。抛出的异常会变成
    ``500`` JSON 响应（``{"ok": false, "error": "..."}``），不会弄崩服务。
    """

    def status(self) -> dict[str, Any]:
        """返回状态快照：设备名、是否连接、是否推流、fps、亮度、当前工程等。"""

    def apply_scene(self, scene: dict[str, Any]) -> str:
        """应用一个已规范化的场景，返回给客户端的提示消息。"""

    def get_scene(self) -> dict[str, Any]:
        """返回当前场景（应已是 :func:`scenes.normalize_scene` 的结果）。"""

    def set_brightness(self, value: int) -> None:
        """设置背光亮度，``value`` 已保证在 0–100 之间。"""

    def set_power(self, on: bool) -> None:
        """打开/关闭屏幕。"""

    def set_stream(self, on: bool) -> None:
        """开始/停止推流。"""

    def metrics(self) -> dict[str, Any]:
        """返回最新指标采样值（CPU/GPU/内存等）。"""


def token_path() -> Path:
    """返回 token 文件路径 ``%APPDATA%\\HydroHUD\\api-token.txt``。"""
    base = Path.home() / "AppData" / "Roaming"
    import os

    env = os.environ.get("APPDATA")
    if env:
        base = Path(env)
    return base / "HydroHUD" / TOKEN_FILE_NAME


def ensure_token(path: Path | None = None) -> str:
    """读取 token 文件；不存在或为空时生成 32 位 hex token 并写回。

    目录不可写时退化为内存 token（不抛异常），保证 GUI 仍能启动。
    """
    target = Path(path) if path is not None else token_path()
    try:
        existing = target.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    token = secrets.token_hex(16)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(token + "\n", encoding="utf-8")
    except OSError:
        pass
    return token


class _ApiError(Exception):
    """带 HTTP 状态码与可选响应头的内部异常。"""

    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = dict(headers or {})


_ROUTES: dict[str, tuple[str, ...]] = {
    "/status": ("GET",),
    "/scene": ("GET", "POST"),
    "/brightness": ("POST",),
    "/power": ("POST",),
    "/stream": ("POST",),
    "/metrics": ("GET",),
    "/events": ("GET",),
}


def _json_bytes(payload: Any) -> bytes:
    """把对象序列化成 UTF-8 JSON（中文不转义）。"""
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _normalize_scene(raw: Any) -> dict[str, Any]:
    """校验并规范化客户端提交的场景；非法时抛 :class:`_ApiError`（400）。

    ``scenes.normalize_scene`` 对未知图层类型会丢弃，因此这里先做严格检查：内置
    类型之外只放行已注册的插件（:mod:`hydrohud.plugins`），由 ``normalize_scene``
    在保持层叠顺序的前提下保留插件图层。
    """
    if not isinstance(raw, dict):
        raise _ApiError(400, "scene 必须是 JSON 对象")
    raw_layers = raw.get("layers")
    if raw_layers is not None:
        if not isinstance(raw_layers, list):
            raise _ApiError(400, "layers 必须是数组")
        known = set(getattr(scenes, "LAYER_TYPES", ()))
        plugin_specs = plugins.registry().specs
        for index, layer in enumerate(raw_layers):
            if not isinstance(layer, dict):
                raise _ApiError(400, f"第 {index + 1} 个图层必须是 JSON 对象")
            kind = str(layer.get("type") or "").strip()
            if not kind:
                raise _ApiError(400, f"第 {index + 1} 个图层缺少 type")
            if kind not in known and kind not in plugin_specs:
                raise _ApiError(400, f"第 {index + 1} 个图层类型未知：{kind}")
    try:
        return scenes.normalize_scene(raw)
    except Exception as exc:  # noqa: BLE001 - 规范化理论上不抛，防御第三方篡改
        raise _ApiError(400, f"场景校验失败：{exc}") from None


def _coerce_int(value: Any, field: str, low: int, high: int) -> int:
    """把 JSON 数值转成闭区间整数；越界/非整数抛 400。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _ApiError(400, f"{field} 必须是整数")
    number = float(value)
    if not number.is_integer():
        raise _ApiError(400, f"{field} 必须是整数")
    if not low <= int(number) <= high:
        raise _ApiError(400, f"{field} 必须在 {low}–{high} 之间")
    return int(number)


def _coerce_bool(value: Any, field: str) -> bool:
    """把 JSON 布尔值取出；非布尔抛 400。"""
    if not isinstance(value, bool):
        raise _ApiError(400, f"{field} 必须是 true 或 false")
    return value


class _RequestHandler(BaseHTTPRequestHandler):
    """单个 HTTP 请求的处理；路由与鉴权都在这里完成。"""

    server_version = "HydroHUDApi/1.0"
    protocol_version = "HTTP/1.1"

    # -- 基础工具 --------------------------------------------------------- #
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 基类签名
        """静默默认的 stderr 访问日志（GUI 不应被请求日志刷屏）。"""

    def _send_json(
        self, payload: Any, status: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        """发送 JSON 响应并带上 Content-Length。"""
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, error: _ApiError) -> None:
        """把 :class:`_ApiError` 转成统一错误 JSON。"""
        self._send_json({"ok": False, "error": error.message}, error.status, headers=error.headers)

    def _read_json(self) -> dict[str, Any]:
        """读取并解析 JSON 请求体；空/过大/非对象都抛 400/413。"""
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header or 0)
        except ValueError:
            raise _ApiError(400, "Content-Length 无效") from None
        if length <= 0:
            raise _ApiError(400, "请求体为空")
        if length > MAX_BODY_BYTES:
            raise _ApiError(413, "请求体过大")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise _ApiError(400, "请求体不是合法 JSON") from None
        if not isinstance(payload, dict):
            raise _ApiError(400, "请求体必须是 JSON 对象")
        return payload

    # -- 分发 ------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802 - 基类约定
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802 - 基类约定
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802 - 基类约定
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802 - 基类约定
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802 - 基类约定
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802 - 基类约定
        self._dispatch()

    def do_OPTIONS(self) -> None:  # noqa: N802 - 基类约定
        self._dispatch()

    def _dispatch(self) -> None:
        """鉴权 → 路由 → 执行；所有异常都转成 JSON 错误。"""
        api: ApiServer = self.server.api  # type: ignore[attr-defined]
        try:
            split = urlsplit(self.path)
            path = split.path.rstrip("/") or "/"
            query = parse_qs(split.query)
            api._check_token(self, query)
            self._route(path)
        except _ApiError as error:
            self._send_error(error)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001 - 宿主实现可能抛任何异常
            self._send_error(_ApiError(500, f"服务器内部错误：{type(exc).__name__}: {exc}"))

    def _route(self, path: str) -> None:
        """按方法和路径选择处理函数；未知路径 404，方法不符 405。"""
        methods = _ROUTES.get(path)
        if methods is None:
            raise _ApiError(404, f"未知路径：{path}")
        if self.command not in methods:
            allow = ", ".join(methods)
            raise _ApiError(405, f"方法不允许：{self.command} {path}", headers={"Allow": allow})
        if path == "/events":
            self._handle_events()
            return
        handler = {
            ("GET", "/status"): self._handle_status,
            ("GET", "/scene"): self._handle_get_scene,
            ("POST", "/scene"): self._handle_post_scene,
            ("POST", "/brightness"): self._handle_brightness,
            ("POST", "/power"): self._handle_power,
            ("POST", "/stream"): self._handle_stream,
            ("GET", "/metrics"): self._handle_metrics,
        }[(self.command, path)]
        handler()

    # -- 路由实现 --------------------------------------------------------- #
    def _handle_status(self) -> None:
        self._send_json(self.server.api.host.status())  # type: ignore[attr-defined]

    def _handle_get_scene(self) -> None:
        self._send_json(self.server.api.host.get_scene())  # type: ignore[attr-defined]

    def _handle_post_scene(self) -> None:
        payload = self._read_json()
        if "scene" in payload:
            raw = payload["scene"]
        else:
            raw = payload
        scene = _normalize_scene(raw)
        message = self.server.api.host.apply_scene(scene)  # type: ignore[attr-defined]
        self._send_json({"ok": True, "message": str(message or ""), "scene": scene})

    def _handle_brightness(self) -> None:
        payload = self._read_json()
        value = _coerce_int(payload.get("value"), "value", 0, 100)
        self.server.api.host.set_brightness(value)  # type: ignore[attr-defined]
        self._send_json({"ok": True, "value": value})

    def _handle_power(self) -> None:
        payload = self._read_json()
        on = _coerce_bool(payload.get("on"), "on")
        self.server.api.host.set_power(on)  # type: ignore[attr-defined]
        self._send_json({"ok": True, "on": on})

    def _handle_stream(self) -> None:
        payload = self._read_json()
        on = _coerce_bool(payload.get("on"), "on")
        self.server.api.host.set_stream(on)  # type: ignore[attr-defined]
        self._send_json({"ok": True, "on": on})

    def _handle_metrics(self) -> None:
        self._send_json(self.server.api.host.metrics())  # type: ignore[attr-defined]

    def _handle_events(self) -> None:
        """SSE 状态流：立刻发第一条，之后每 1 s 一条；``limit`` 到达即关闭。"""
        api: ApiServer = self.server.api  # type: ignore[attr-defined]
        split = urlsplit(self.path)
        query = parse_qs(split.query)
        limit_raw = (query.get("limit") or ["0"])[0].strip()
        try:
            limit = int(limit_raw or 0)
        except ValueError:
            raise _ApiError(400, "limit 必须是整数") from None
        if limit < 0:
            raise _ApiError(400, "limit 不能为负数")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        sent = 0
        while not api._stop_event.is_set():
            try:
                status = api.host.status()
            except Exception as exc:  # noqa: BLE001 - 单次采样失败不应断开整个流
                status = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            line = f"data: {json.dumps(status, ensure_ascii=False)}\n\n"
            try:
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            sent += 1
            if limit and sent >= limit:
                return
            if api._stop_event.wait(EVENT_INTERVAL_SECONDS):
                return


class ApiServer:
    """本地控制面服务器；构造只准备 token，:meth:`start` 才绑定端口并起线程。"""

    def __init__(
        self,
        host: Host,
        port: int = DEFAULT_PORT,
        token: str | None = None,
        bind: str = "127.0.0.1",
    ) -> None:
        self.host = host
        self.bind = str(bind)
        self._requested_port = int(port)
        if self._requested_port < 0 or self._requested_port > 65535:
            raise ValueError("port 必须在 0–65535 之间")
        self._token = ensure_token() if token is None else str(token)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # -- 只读属性 --------------------------------------------------------- #
    @property
    def token(self) -> str:
        """当前使用的 token（空串表示鉴权关闭）。"""
        return self._token

    @property
    def port(self) -> int:
        """实际监听端口；``port=0`` 时在 :meth:`start` 后返回系统分配的端口。"""
        httpd = self._httpd
        if httpd is None:
            return self._requested_port
        return int(httpd.server_address[1])

    @property
    def running(self) -> bool:
        """服务器是否已经启动且未停止。"""
        return self._httpd is not None

    # -- 生命周期 --------------------------------------------------------- #
    def start(self) -> "ApiServer":
        """绑定端口并在 daemon 线程里开始服务；重复调用抛 :class:`RuntimeError`。"""
        if self._httpd is not None:
            raise RuntimeError("API 服务已经启动")
        try:
            httpd = ThreadingHTTPServer((self.bind, self._requested_port), _RequestHandler)
        except OSError as exc:
            raise RuntimeError(f"无法监听 {self.bind}:{self._requested_port}：{exc}") from None
        httpd.daemon_threads = True
        httpd.api = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="hydrohud-api",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """停止服务并释放端口；可重复调用。"""
        httpd = self._httpd
        if httpd is None:
            return
        self._stop_event.set()
        try:
            httpd.shutdown()
        finally:
            httpd.server_close()
            self._httpd = None
            thread = self._thread
            self._thread = None
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)

    def __enter__(self) -> "ApiServer":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- 内部 ------------------------------------------------------------- #
    def _check_token(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        """校验 ``X-HydroHUD-Token`` 头或 ``?token=``；不匹配抛 401。"""
        if not self._token:
            return
        header = (handler.headers.get("X-HydroHUD-Token") or "").strip()
        query_token = (query.get("token") or [""])[0].strip()
        supplied = header or query_token
        if not supplied or not hmac.compare_digest(
            supplied.encode("utf-8", "replace"), self._token.encode("utf-8", "replace")
        ):
            raise _ApiError(401, "token 无效或缺失")
