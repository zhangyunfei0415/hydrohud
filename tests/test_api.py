"""Tests for :mod:`hydrohud.api` (real loopback, fake host, no hardware)."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from hydrohud import api, plugins, scenes

TOKEN = "test-token"


class FakeHost:
    """记录调用并返回可控数据的最小 Host 实现。"""

    def __init__(self) -> None:
        self.scenes: list[dict[str, Any]] = []
        self.brightness: list[int] = []
        self.power: list[bool] = []
        self.streaming: list[bool] = []
        self.fail_status = False
        self._scene = scenes.normalize_scene({})

    def status(self) -> dict[str, Any]:
        if self.fail_status:
            raise RuntimeError("状态读取失败")
        return {"ok": True, "device": "TCOMAS LG600", "streaming": False, "fps": 30}

    def apply_scene(self, scene: dict[str, Any]) -> str:
        self.scenes.append(scene)
        self._scene = scene
        return "已应用场景"

    def get_scene(self) -> dict[str, Any]:
        return self._scene

    def set_brightness(self, value: int) -> None:
        self.brightness.append(value)

    def set_power(self, on: bool) -> None:
        self.power.append(on)

    def set_stream(self, on: bool) -> None:
        self.streaming.append(on)

    def metrics(self) -> dict[str, Any]:
        return {"cpu": 12.5, "gpu_temp": 61, "memory": 42}


@pytest.fixture()
def server() -> Any:
    """启动一个监听系统随机端口的真实服务器，测试结束停止。"""
    instance = api.ApiServer(FakeHost(), port=0, token=TOKEN)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def _call(
    server: api.ApiServer,
    method: str,
    path: str,
    payload: Any = None,
    *,
    token: str | None = TOKEN,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any, dict[str, str]]:
    """发一次真实 HTTP 请求，返回 ``(状态码, 解析后的 JSON, 响应头)``。"""
    data = None
    merged_headers = dict(headers or {})
    if token is not None:
        merged_headers["X-HydroHUD-Token"] = token
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.port}{path}", data=data, headers=merged_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            body = response.read()
            return response.status, json.loads(body or b"{}"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            parsed = json.loads(body or b"{}")
        except ValueError:
            parsed = {"raw": body.decode("utf-8", "replace")}
        return exc.code, parsed, dict(exc.headers)


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #
def test_constructing_does_not_bind_and_start_reports_real_port() -> None:
    instance = api.ApiServer(FakeHost(), port=0, token="x")
    assert instance.running is False
    assert instance.port == 0
    instance.start()
    try:
        assert instance.running is True
        assert instance.port > 0
        with pytest.raises(RuntimeError, match="已经启动"):
            instance.start()
    finally:
        instance.stop()
    assert instance.running is False
    instance.stop()  # 幂等


def test_stop_releases_the_port() -> None:
    instance = api.ApiServer(FakeHost(), port=0, token="x")
    instance.start()
    port = instance.port
    instance.stop()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        assert probe.connect_ex(("127.0.0.1", port)) != 0


def test_invalid_port_is_rejected() -> None:
    with pytest.raises(ValueError):
        api.ApiServer(FakeHost(), port=-1)
    with pytest.raises(ValueError):
        api.ApiServer(FakeHost(), port=70000)


def test_context_manager_starts_and_stops() -> None:
    with api.ApiServer(FakeHost(), port=0, token="x") as instance:
        assert instance.running is True
    assert instance.running is False


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #
def test_missing_and_wrong_token_are_rejected(server: api.ApiServer) -> None:
    status, body, _ = _call(server, "GET", "/status", token=None)
    assert status == 401
    assert "token" in body["error"]
    assert body["ok"] is False

    status, _, _ = _call(server, "GET", "/status", token="nope")
    assert status == 401


def test_token_can_come_from_query_parameter(server: api.ApiServer) -> None:
    status, body, _ = _call(server, "GET", f"/status?token={TOKEN}", token=None)
    assert status == 200
    assert body["device"] == "TCOMAS LG600"


def test_empty_token_disables_auth() -> None:
    instance = api.ApiServer(FakeHost(), port=0, token="")
    instance.start()
    try:
        status, body, _ = _call(instance, "GET", "/status", token=None)
        assert status == 200
        assert body["ok"] is True
    finally:
        instance.stop()


def test_ensure_token_reuses_and_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "HydroHUD" / "api-token.txt"
    first = api.ensure_token(target)
    assert len(first) == 32
    assert target.read_text(encoding="utf-8").strip() == first
    assert api.ensure_token(target) == first


def test_token_path_honours_appdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert api.token_path() == tmp_path / "HydroHUD" / "api-token.txt"


# --------------------------------------------------------------------------- #
# GET 路由
# --------------------------------------------------------------------------- #
def test_status_scene_and_metrics(server: api.ApiServer) -> None:
    status, body, _ = _call(server, "GET", "/status")
    assert status == 200 and body["fps"] == 30

    status, body, _ = _call(server, "GET", "/scene")
    assert status == 200
    assert body["version"] == scenes.SCENE_VERSION

    status, body, _ = _call(server, "GET", "/metrics")
    assert status == 200 and body["cpu"] == 12.5


def test_unknown_path_and_method(server: api.ApiServer) -> None:
    status, body, _ = _call(server, "GET", "/nope")
    assert status == 404
    assert "未知路径" in body["error"]

    status, _, headers = _call(server, "POST", "/metrics", {})
    assert status == 405
    assert headers.get("Allow") == "GET"


def test_host_exception_becomes_json_500(server: api.ApiServer) -> None:
    server.host.fail_status = True  # type: ignore[attr-defined]
    status, body, _ = _call(server, "GET", "/status")
    assert status == 500
    assert "服务器内部错误" in body["error"]


# --------------------------------------------------------------------------- #
# POST /scene
# --------------------------------------------------------------------------- #
def _layer_scene(*layers: dict[str, Any]) -> dict[str, Any]:
    """构造一个带图层的场景 dict。"""
    return {"version": scenes.SCENE_VERSION, "name": "测试", "layers": list(layers)}


def test_post_scene_wrapped_normalizes_and_returns_scene(server: api.ApiServer) -> None:
    scene = _layer_scene({"type": "clock", "format": "%H:%M"})
    status, body, _ = _call(server, "POST", "/scene", {"scene": scene})
    assert status == 200
    assert body["ok"] is True
    assert body["message"] == "已应用场景"
    assert body["scene"]["layers"][0]["type"] == "clock"
    applied = server.host.scenes[-1]  # type: ignore[attr-defined]
    assert applied["layers"][0]["type"] == "clock"


def test_post_scene_accepts_bare_scene(server: api.ApiServer) -> None:
    status, body, _ = _call(server, "POST", "/scene", _layer_scene({"type": "text", "text": "hi"}))
    assert status == 200
    assert body["scene"]["layers"][0]["text"] == "hi"


def test_post_scene_rejects_unknown_layer_type(server: api.ApiServer) -> None:
    status, body, _ = _call(server, "POST", "/scene", _layer_scene({"type": "nope"}))
    assert status == 400
    assert "图层类型未知" in body["error"]
    assert server.host.scenes == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "payload",
    [
        {"scene": {"layers": "oops"}},
        {"scene": {"layers": [1]}},
        {"scene": {"layers": [{"type": ""}]}},
        {"scene": {"layers": [{"nope": 1}]}},
    ],
)
def test_post_scene_rejects_malformed_layers(server: api.ApiServer, payload: dict) -> None:
    status, body, _ = _call(server, "POST", "/scene", payload)
    assert status == 400
    assert body["ok"] is False


def test_post_scene_requires_object_body(server: api.ApiServer) -> None:
    status, body, _ = _call(server, "POST", "/scene", [])
    assert status == 400


def test_plugin_layers_survive_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "spark.py").write_text(
        'PLUGIN = {"name": "spark", "draw": lambda image, layer, ctx: True}\n', encoding="utf-8"
    )
    registry = plugins.PluginRegistry(tmp_path)
    registry.discover()
    monkeypatch.setattr(plugins, "_REGISTRY", registry)

    instance = api.ApiServer(FakeHost(), port=0, token=TOKEN)
    instance.start()
    try:
        scene = _layer_scene(
            {"type": "clock"},
            {"type": "spark", "color": "#FFFFFF"},
            {"type": "text", "text": "hello"},
        )
        status, body, _ = _call(instance, "POST", "/scene", {"scene": scene})
        assert status == 200
        kinds = [layer["type"] for layer in body["scene"]["layers"]]
        assert kinds == ["clock", "spark", "text"]
    finally:
        instance.stop()
        monkeypatch.setattr(plugins, "_REGISTRY", None)


# --------------------------------------------------------------------------- #
# POST 控制命令
# --------------------------------------------------------------------------- #
def test_brightness_accepts_valid_range(server: api.ApiServer) -> None:
    status, body, _ = _call(server, "POST", "/brightness", {"value": 60})
    assert status == 200 and body["value"] == 60
    assert server.host.brightness == [60]  # type: ignore[attr-defined]


@pytest.mark.parametrize("value", [101, -1, "60", True, 3.5])
def test_brightness_rejects_invalid_values(server: api.ApiServer, value: Any) -> None:
    status, body, _ = _call(server, "POST", "/brightness", {"value": value})
    assert status == 400
    assert body["ok"] is False
    assert server.host.brightness == []  # type: ignore[attr-defined]


def test_power_and_stream_accept_booleans(server: api.ApiServer) -> None:
    status, _, _ = _call(server, "POST", "/power", {"on": False})
    assert status == 200
    status, _, _ = _call(server, "POST", "/stream", {"on": True})
    assert status == 200
    assert server.host.power == [False]  # type: ignore[attr-defined]
    assert server.host.streaming == [True]  # type: ignore[attr-defined]


@pytest.mark.parametrize("path", ["/power", "/stream"])
def test_power_and_stream_reject_non_booleans(server: api.ApiServer, path: str) -> None:
    status, body, _ = _call(server, "POST", path, {"on": 1})
    assert status == 400
    assert "true 或 false" in body["error"]


def test_post_requires_json_body(server: api.ApiServer) -> None:
    status, body, _ = _call(server, "POST", "/brightness", None)
    assert status == 400
    assert "请求体为空" in body["error"]


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #
def test_events_streams_limit_lines(server: api.ApiServer) -> None:
    with urllib.request.urlopen(
        urllib.request.Request(
            f"http://127.0.0.1:{server.port}/events?limit=2",
            headers={"X-HydroHUD-Token": TOKEN},
        ),
        timeout=5.0,
    ) as response:
        body = response.read().decode("utf-8")

    lines = [line for line in body.splitlines() if line.startswith("data:")]
    assert len(lines) == 2
    first = json.loads(lines[0][len("data:") :])
    assert first["device"] == "TCOMAS LG600"


def test_events_rejects_negative_limit(server: api.ApiServer) -> None:
    status, body, _ = _call(server, "GET", "/events?limit=-1")
    assert status == 400
    assert "limit" in body["error"]


def test_events_rejects_non_integer_limit(server: api.ApiServer) -> None:
    status, _, _ = _call(server, "GET", "/events?limit=abc")
    assert status == 400


def test_events_requires_token(server: api.ApiServer) -> None:
    status, body, _ = _call(server, "GET", "/events?limit=1", token=None)
    assert status == 401


# --------------------------------------------------------------------------- #
# Host 协议
# --------------------------------------------------------------------------- #
def test_fake_host_satisfies_host_protocol() -> None:
    assert isinstance(FakeHost(), api.Host)
