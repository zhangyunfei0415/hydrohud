"""Tests for the WebSocket endpoint and the Home Assistant example package.

No hardware, no third-party deps: the WebSocket tests use a real loopback socket
and a fake :class:`hydrohud.api.Host`.
"""

from __future__ import annotations

import contextlib
import json
import socket
import struct
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hydrohud import api

TOKEN = "ws-token"
KEY = "dGhlIHNhbXBsZSBub25jZQ=="
ACCEPT = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
WS_GUID_SAMPLE = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

ROOT = Path(__file__).resolve().parent.parent
HA_DIR = ROOT / "examples" / "homeassistant"
HA_YAML = HA_DIR / "hydrohud.yaml"
HA_README = HA_DIR / "README.md"


class FakeHost:
    """记录调用次数的最小 Host 实现。"""

    def __init__(self) -> None:
        self.status_calls = 0
        self.fail_status = False
        self._scene: dict[str, Any] = {"version": 1, "name": "空", "layers": []}

    def status(self) -> dict[str, Any]:
        self.status_calls += 1
        if self.fail_status:
            raise RuntimeError("采样失败")
        return {
            "ok": True,
            "device": "TCOMAS LG600",
            "streaming": True,
            "fps": 30,
            "frames": 120,
            "brightness": 80,
        }

    def apply_scene(self, scene: dict[str, Any]) -> str:
        self._scene = scene
        return "已应用场景"

    def get_scene(self) -> dict[str, Any]:
        return self._scene

    def set_brightness(self, value: int) -> None:
        return None

    def set_power(self, on: bool) -> None:
        return None

    def set_stream(self, on: bool) -> None:
        return None

    def metrics(self) -> dict[str, Any]:
        return {"cpu": 12.5}


@pytest.fixture()
def server() -> Any:
    """启动监听随机端口的真实 API 服务，测试结束停止。"""
    instance = api.ApiServer(FakeHost(), port=0, token=TOKEN)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


# --------------------------------------------------------------------------- #
# 握手与帧编码（纯函数）
# --------------------------------------------------------------------------- #
def test_handshake_accept_matches_rfc6455_sample() -> None:
    assert api.handshake_accept(KEY) == ACCEPT


def test_handshake_accept_strips_surrounding_whitespace() -> None:
    assert api.handshake_accept(f"  {KEY}\r\n") == ACCEPT


def test_handshake_accept_uses_the_rfc_guid() -> None:
    assert WS_GUID_SAMPLE.startswith("258EAFA5")
    assert api.handshake_accept(KEY) != api.handshake_accept("YW5vdGhlciBrZXk=")


def test_encode_text_frame_short_payload_uses_seven_bit_length() -> None:
    frame = api.encode_text_frame(b"hi")
    assert frame[0] == 0x81
    assert frame[1] == 2
    assert frame[2:] == b"hi"


def test_encode_text_frame_medium_payload_uses_sixteen_bit_length() -> None:
    payload = b"a" * 126
    frame = api.encode_text_frame(payload)
    assert frame[:2] == bytes((0x81, 126))
    assert struct.unpack(">H", frame[2:4])[0] == 126
    assert frame[4:] == payload


def test_encode_text_frame_long_payload_uses_sixty_four_bit_length() -> None:
    payload = b"b" * 65536
    frame = api.encode_text_frame(payload)
    assert frame[:2] == bytes((0x81, 127))
    assert struct.unpack(">Q", frame[2:10])[0] == 65536
    assert frame[10:] == payload


def test_encode_text_frame_preserves_utf8_bytes() -> None:
    payload = "亮度 80%".encode()
    frame = api.encode_text_frame(payload)
    assert frame[2:] == payload


def test_encode_close_frame_default_code_is_1000() -> None:
    assert api.encode_close_frame() == bytes((0x88, 2)) + struct.pack(">H", 1000)


def test_encode_close_frame_custom_code() -> None:
    assert api.encode_close_frame(1001) == bytes((0x88, 2)) + struct.pack(">H", 1001)


def test_ws_path_constant() -> None:
    assert api.WS_PATH == "/ws"
    assert api.WS_PATH in api._ROUTES


# --------------------------------------------------------------------------- #
# 真回环 WebSocket
# --------------------------------------------------------------------------- #
def _recv_until(sock: socket.socket, marker: bytes, buffer: bytes = b"") -> tuple[bytes, bytes]:
    """读到 ``marker`` 为止，返回 ``(marker 之前的内容, marker 之后剩余字节)``。"""
    data = buffer
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError("连接在响应头之前关闭")
        data += chunk
    head, _, rest = data.partition(marker)
    return head, rest


def _read_exact(sock: socket.socket, count: int, buffer: bytes) -> tuple[bytes, bytes]:
    """从 ``buffer`` + socket 读满 ``count`` 字节。"""
    data = buffer
    while len(data) < count:
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError("连接提前关闭")
        data += chunk
    return data[:count], data[count:]


def _read_frame(sock: socket.socket, buffer: bytes = b"") -> tuple[int, bytes, bytes]:
    """读一个服务端帧（服务端不加掩码），返回 ``(opcode, payload, 剩余字节)``。"""
    head, buffer = _read_exact(sock, 2, buffer)
    opcode = head[0] & 0x0F
    length = head[1] & 0x7F
    if length == 126:
        extra, buffer = _read_exact(sock, 2, buffer)
        length = struct.unpack(">H", extra)[0]
    elif length == 127:
        extra, buffer = _read_exact(sock, 8, buffer)
        length = struct.unpack(">Q", extra)[0]
    payload, buffer = _read_exact(sock, length, buffer)
    return opcode, payload, buffer


def _open_ws(
    server: api.ApiServer,
    *,
    path: str = "/ws",
    token: str | None = TOKEN,
    upgrade: bool = True,
    include_key: bool = True,
) -> tuple[socket.socket, str, dict[str, str], bytes]:
    """建立 TCP 连接并发 WebSocket 握手，返回 (socket, 状态行, 响应头, 剩余字节)。"""
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=5.0)
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: 127.0.0.1:{server.port}",
        "Connection: Upgrade",
        "Sec-WebSocket-Version: 13",
    ]
    if upgrade:
        lines.append("Upgrade: websocket")
    if include_key:
        lines.append(f"Sec-WebSocket-Key: {KEY}")
    if token is not None:
        lines.append(f"X-HydroHUD-Token: {token}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))

    head, rest = _recv_until(sock, b"\r\n\r\n")
    text = head.decode("latin-1")
    status_line, _, header_blob = text.partition("\r\n")
    headers: dict[str, str] = {}
    for line in header_blob.split("\r\n"):
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return sock, status_line, headers, rest


@contextlib.contextmanager
def _ws(
    server: api.ApiServer, *, path: str = "/ws", token: str | None = TOKEN
) -> Iterator[tuple[socket.socket, str, dict[str, str], bytes]]:
    sock, status_line, headers, rest = _open_ws(server, path=path, token=token)
    try:
        yield sock, status_line, headers, rest
    finally:
        sock.close()


def test_ws_handshake_returns_101_and_accept_header(server: api.ApiServer) -> None:
    with _ws(server, path="/ws?limit=1") as (sock, status_line, headers, rest):
        assert status_line.startswith("HTTP/1.1 101")
        assert headers["upgrade"].lower() == "websocket"
        assert headers["connection"].lower() == "upgrade"
        assert headers["sec-websocket-accept"] == ACCEPT

        opcode, payload, _ = _read_frame(sock, rest)
        assert opcode == 0x1
        data = json.loads(payload.decode("utf-8"))
        assert data["device"] == "TCOMAS LG600"
        assert data["fps"] == 30
        assert server.host.status_calls >= 1


def test_ws_limit_sends_close_frame_after_last_text_frame(server: api.ApiServer) -> None:
    with _ws(server, path="/ws?limit=2") as (sock, status_line, _, rest):
        assert status_line.startswith("HTTP/1.1 101")
        first_opcode, first_payload, rest = _read_frame(sock, rest)
        second_opcode, second_payload, rest = _read_frame(sock, rest)
        assert first_opcode == second_opcode == 0x1
        assert json.loads(first_payload)["fps"] == 30
        assert json.loads(second_payload)["fps"] == 30

        close_opcode, close_payload, _ = _read_frame(sock, rest)
        assert close_opcode == 0x8
        assert struct.unpack(">H", close_payload[:2])[0] == 1000


def test_ws_status_error_becomes_error_event(server: api.ApiServer) -> None:
    server.host.fail_status = True  # type: ignore[attr-defined]
    with _ws(server, path="/ws?limit=1") as (sock, status_line, _, rest):
        assert status_line.startswith("HTTP/1.1 101")
        opcode, payload, _ = _read_frame(sock, rest)
        assert opcode == 0x1
        data = json.loads(payload.decode("utf-8"))
        assert data["ok"] is False
        assert "采样失败" in data["error"]


def test_ws_requires_valid_token(server: api.ApiServer) -> None:
    sock, status_line, _, rest = _open_ws(server, token=None)
    try:
        assert " 401 " in status_line
        body = rest + sock.recv(4096)
        assert b'"ok": false' in body
        assert "token" in body.decode("utf-8")
    finally:
        sock.close()


def test_ws_rejects_missing_upgrade_header(server: api.ApiServer) -> None:
    sock, status_line, _, _ = _open_ws(server, upgrade=False)
    try:
        assert " 400 " in status_line
    finally:
        sock.close()


def test_ws_rejects_missing_sec_websocket_key(server: api.ApiServer) -> None:
    sock, status_line, _, rest = _open_ws(server, include_key=False)
    try:
        assert " 400 " in status_line
        body = rest + sock.recv(4096)
        assert "Sec-WebSocket-Key" in body.decode("utf-8")
    finally:
        sock.close()


def test_ws_rejects_non_get_method(server: api.ApiServer) -> None:
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=5.0)
    try:
        request = (
            f"POST /ws HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
            f"X-HydroHUD-Token: {TOKEN}\r\nContent-Length: 0\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        head, _ = _recv_until(sock, b"\r\n\r\n")
        assert " 405 " in head.decode("latin-1")
    finally:
        sock.close()


def test_ws_survives_client_disconnect(server: api.ApiServer) -> None:
    sock, status_line, _, rest = _open_ws(server)
    try:
        assert status_line.startswith("HTTP/1.1 101")
        opcode, _, _ = _read_frame(sock, rest)
        assert opcode == 0x1
    finally:
        sock.close()

    time.sleep(0.2)
    assert server.running
    with _ws(server, path="/ws?limit=1") as (sock2, status_line2, _, rest2):
        assert status_line2.startswith("HTTP/1.1 101")
        opcode2, payload2, _ = _read_frame(sock2, rest2)
        assert opcode2 == 0x1
        assert json.loads(payload2)["fps"] == 30


# --------------------------------------------------------------------------- #
# Home Assistant 集成包
# --------------------------------------------------------------------------- #
def _load_yaml() -> Any:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(HA_YAML.read_text(encoding="utf-8"))


def test_ha_yaml_parses() -> None:
    document = _load_yaml()
    assert isinstance(document, dict)
    assert set(document) >= {"rest", "rest_command", "template"}


def test_ha_yaml_monitors_status_and_metrics() -> None:
    document = _load_yaml()
    resources = [item["resource"] for item in document["rest"]]
    assert any(item.endswith("/status") for item in resources)
    assert any(item.endswith("/metrics") for item in resources)

    status_sensor = document["rest"][0]["sensor"][0]
    assert {"fps", "brightness", "streaming"} <= set(status_sensor["json_attributes"])


def test_ha_yaml_commands_match_api_routes() -> None:
    document = _load_yaml()
    commands = document["rest_command"]
    urls = {name: command["url"] for name, command in commands.items()}
    assert urls["hydrohud_brightness"].endswith("/brightness")
    assert urls["hydrohud_power_on"].endswith("/power")
    assert urls["hydrohud_power_off"].endswith("/power")
    assert urls["hydrohud_stream_on"].endswith("/stream")
    assert urls["hydrohud_stream_off"].endswith("/stream")
    assert urls["hydrohud_scene"].endswith("/scene")
    for url in urls.values():
        assert f"127.0.0.1:{api.DEFAULT_PORT}" in url


def test_ha_yaml_mentions_token_file_and_summary_template() -> None:
    text = HA_YAML.read_text(encoding="utf-8")
    assert f"127.0.0.1:{api.DEFAULT_PORT}" in text
    assert "api-token.txt" in text
    document = _load_yaml()
    summary = document["template"][0]["sensor"][0]
    assert "HydroHUD" in summary["name"]
    assert "fps" in summary["state"]


def test_ha_readme_is_short_and_covers_token() -> None:
    text = HA_README.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) <= 80
    assert "api-token.txt" in text
    assert str(api.DEFAULT_PORT) in text
    assert "401" in text
    assert api.WS_PATH in text or "WebSocket" in text
