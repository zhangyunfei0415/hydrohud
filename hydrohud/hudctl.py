"""``hudctl``：HydroHUD 本地控制面的命令行客户端（无第三方依赖）。

用法示例::

    hudctl status
    hudctl --json metrics
    hudctl brightness 60
    hudctl power off
    hudctl scene examples\\性能曲线.hud.json
    hudctl get-scene > my.hud.json
    hudctl events --limit 5

选项既可写在子命令前，也可写在子命令后；``--host``/``--port`` 默认
``127.0.0.1:4780``，``--token`` 缺省依次取环境变量 ``HYDROHUD_TOKEN`` 和
``%APPDATA%\\HydroHUD\\api-token.txt``。所有请求超时 5 秒；HTTP 错误打印状态码与
响应体并以退出码 1 结束，成功返回 0，参数错误由 :mod:`argparse` 以退出码 2 结束。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from . import api, scenes

__all__ = ["main", "build_parser", "resolve_token", "TIMEOUT_SECONDS"]

TIMEOUT_SECONDS = 5.0


class CliError(Exception):
    """命令执行失败（网络/HTTP/文件问题），消息直接展示给用户。"""


def resolve_token(explicit: str | None) -> str:
    """确定 token：优先 ``--token``，其次 ``HYDROHUD_TOKEN``，最后 token 文件。

    都找不到时返回空串（服务器会以 401 提示），不抛异常。
    """
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("HYDROHUD_TOKEN", "").strip()
    if env:
        return env
    try:
        return api.token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _url(host: str, port: int, path: str) -> str:
    """拼接 ``http://host:port/path``。"""
    return f"http://{host}:{port}{path}"


def _build_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    host: str,
    port: int,
    token: str,
    accept: str = "application/json",
) -> urllib.request.Request:
    """构造带鉴权头的请求（JSON 体用 UTF-8 编码）。"""
    data = None
    headers = {"Accept": accept}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["X-HydroHUD-Token"] = token
    return urllib.request.Request(
        _url(host, port, path), data=data, headers=headers, method=method.upper()
    )


def _http_error_message(status: int, detail: str) -> str:
    """把 HTTP 错误转成一句中文，401 时附带 token 提示。"""
    detail = (detail or "").strip()
    message = f"HTTP {status}"
    if detail:
        message += f"：{detail}"
    if status == 401:
        message += (
            "（请用 --token 或环境变量 HYDROHUD_TOKEN 提供与 %APPDATA%\\HydroHUD\\api-token.txt "
            "一致的 token）"
        )
    return message


def _request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    host: str,
    port: int,
    token: str,
) -> dict[str, Any]:
    """发送一次 JSON 请求并返回解析后的响应 dict；失败抛 :class:`CliError`。

    测试可以 monkeypatch 本函数来避免真实网络。
    """
    request = _build_request(method, path, payload, host, port, token)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")
        except OSError:
            detail = ""
        raise CliError(_http_error_message(exc.code, detail)) from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise CliError(f"无法连接 http://{host}:{port}：{reason}") from None
    except OSError as exc:
        raise CliError(f"无法连接 http://{host}:{port}：{exc}") from None
    text = raw.decode("utf-8", "replace")
    if not text.strip():
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        raise CliError(f"响应不是合法 JSON：{text[:200]}") from None
    if isinstance(parsed, dict):
        return parsed
    return {"value": parsed}


def _stream_events(path: str, *, host: str, port: int, token: str) -> Iterator[dict[str, Any]]:
    """连接 SSE 接口，逐条产出 ``data:`` 行解析出的 dict。"""
    request = _build_request("GET", path, None, host, port, token, accept="text/event-stream")
    try:
        response = urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")
        except OSError:
            detail = ""
        raise CliError(_http_error_message(exc.code, detail)) from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise CliError(f"无法连接 http://{host}:{port}：{reason}") from None
    except OSError as exc:
        raise CliError(f"无法连接 http://{host}:{port}：{exc}") from None
    with response:
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            body = line[len("data:") :].strip()
            if not body:
                continue
            try:
                parsed = json.loads(body)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                yield parsed


def _emit_json(payload: dict[str, Any]) -> None:
    """紧凑 JSON 输出（单行、中文不转义，可被 json.loads 解析）。"""
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _format_value(value: Any) -> str:
    """人类可读输出里把值转成短文本。"""
    if isinstance(value, bool):
        return "开" if value else "关"
    if value is None:
        return "-"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _emit_fields(payload: dict[str, Any]) -> None:
    """按键值逐行输出（status 等）。"""
    for key, value in payload.items():
        print(f"{key}: {_format_value(value)}")


def _emit_metrics(payload: dict[str, Any]) -> None:
    """每行一个指标：``指标 值``。"""
    for key, value in payload.items():
        print(f"{key} {_format_value(value)}")


def _emit_message(payload: dict[str, Any], fallback: str) -> None:
    """打印服务端返回的 message（缺省用 fallback）。"""
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        print(message.strip())
    else:
        print(fallback)


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #
def _cmd_status(args: argparse.Namespace, token: str) -> int:
    payload = _request("GET", "/status", None, host=args.host, port=args.port, token=token)
    if args.json:
        _emit_json(payload)
    else:
        _emit_fields(payload)
    return 0


def _cmd_scene(args: argparse.Namespace, token: str) -> int:
    try:
        scene = scenes.load_project(args.file)
    except FileNotFoundError:
        raise CliError(f"场景文件不存在：{args.file}") from None
    except ValueError as exc:
        raise CliError(str(exc)) from None
    payload = _request(
        "POST", "/scene", {"scene": scene}, host=args.host, port=args.port, token=token
    )
    if args.json:
        _emit_json(payload)
    else:
        _emit_message(payload, f"已应用场景：{args.file}")
    return 0


def _cmd_get_scene(args: argparse.Namespace, token: str) -> int:
    payload = _request("GET", "/scene", None, host=args.host, port=args.port, token=token)
    if args.json:
        _emit_json(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_brightness(args: argparse.Namespace, token: str) -> int:
    payload = _request(
        "POST", "/brightness", {"value": args.value}, host=args.host, port=args.port, token=token
    )
    if args.json:
        _emit_json(payload)
    else:
        print(f"亮度已设为 {args.value}")
    return 0


def _cmd_power(args: argparse.Namespace, token: str) -> int:
    on = args.state == "on"
    payload = _request("POST", "/power", {"on": on}, host=args.host, port=args.port, token=token)
    if args.json:
        _emit_json(payload)
    else:
        print("屏幕已打开" if on else "屏幕已关闭")
    return 0


def _cmd_stream(args: argparse.Namespace, token: str) -> int:
    on = args.state == "on"
    payload = _request("POST", "/stream", {"on": on}, host=args.host, port=args.port, token=token)
    if args.json:
        _emit_json(payload)
    else:
        print("已开始推流" if on else "已停止推流")
    return 0


def _cmd_metrics(args: argparse.Namespace, token: str) -> int:
    payload = _request("GET", "/metrics", None, host=args.host, port=args.port, token=token)
    if args.json:
        _emit_json(payload)
    else:
        _emit_metrics(payload)
    return 0


def _cmd_events(args: argparse.Namespace, token: str) -> int:
    limit = int(getattr(args, "limit", 0) or 0)
    path = f"/events?limit={limit}" if limit else "/events"
    for event in _stream_events(path, host=args.host, port=args.port, token=token):
        if args.json:
            _emit_json(event)
        else:
            print("  ".join(f"{key}: {_format_value(value)}" for key, value in event.items()))
    return 0


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def _add_common_options(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """添加 --host/--port/--token/--json；子命令里用 SUPPRESS 免得覆盖全局值。"""
    host_default = argparse.SUPPRESS if suppress else "127.0.0.1"
    port_default = argparse.SUPPRESS if suppress else api.DEFAULT_PORT
    token_default = argparse.SUPPRESS if suppress else None
    json_default = argparse.SUPPRESS if suppress else False
    parser.add_argument("--host", default=host_default, help="服务地址（默认 127.0.0.1）")
    parser.add_argument(
        "--port", type=int, default=port_default, help=f"服务端口（默认 {api.DEFAULT_PORT}）"
    )
    parser.add_argument(
        "--token",
        default=token_default,
        help="API token（缺省用 HYDROHUD_TOKEN 或 api-token.txt）",
    )
    parser.add_argument("--json", action="store_true", default=json_default, help="紧凑 JSON 输出")


def _add_subcommand(subparsers: Any, name: str, help_text: str) -> argparse.ArgumentParser:
    """创建子命令解析器并附上通用选项（默认值 SUPPRESS）。"""
    sub = subparsers.add_parser(name, help=help_text, description=help_text)
    _add_common_options(sub, suppress=True)
    return sub


def build_parser() -> argparse.ArgumentParser:
    """构造 ``hudctl`` 的参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="hudctl",
        description="HydroHUD 本地控制面命令行客户端",
        epilog="示例：hudctl status / hudctl brightness 60 / hudctl events --limit 5",
    )
    _add_common_options(parser, suppress=False)
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_subcommand(subparsers, "status", "查询运行状态（设备、推流、fps、亮度）")

    scene = _add_subcommand(subparsers, "scene", "载入并应用一个 .hud.json 场景文件")
    scene.add_argument("file", help="场景文件路径（.hud.json）")

    _add_subcommand(subparsers, "get-scene", "输出当前场景 JSON")

    brightness = _add_subcommand(subparsers, "brightness", "设置背光亮度 0-100")
    brightness.add_argument("value", type=int, help="亮度 0-100")

    power = _add_subcommand(subparsers, "power", "打开或关闭屏幕")
    power.add_argument("state", choices=("on", "off"), help="on 打开 / off 关闭")

    stream = _add_subcommand(subparsers, "stream", "开始或停止推流")
    stream.add_argument("state", choices=("on", "off"), help="on 开始 / off 停止")

    _add_subcommand(subparsers, "metrics", "输出最新指标采样值")

    events = _add_subcommand(subparsers, "events", "订阅状态流（SSE），Ctrl+C 退出")
    events.add_argument("--limit", type=int, default=0, help="收到 N 条后退出（0 表示一直订阅）")
    return parser


def _safe_stdio() -> None:
    """让控制台在不支持中文的编码下也不会因打印而崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    """``hudctl`` 入口；返回进程退出码（0 成功 / 1 失败 / 2 参数错误）。"""
    _safe_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "brightness" and not 0 <= args.value <= 100:
        parser.error("brightness 必须在 0 到 100 之间")
    token = resolve_token(args.token)
    handlers = {
        "status": _cmd_status,
        "scene": _cmd_scene,
        "get-scene": _cmd_get_scene,
        "brightness": _cmd_brightness,
        "power": _cmd_power,
        "stream": _cmd_stream,
        "metrics": _cmd_metrics,
        "events": _cmd_events,
    }
    try:
        return handlers[args.command](args, token)
    except CliError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover - 方便 python -m hydrohud.hudctl
    raise SystemExit(main())
