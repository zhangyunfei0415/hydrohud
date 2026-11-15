"""Tests for :mod:`hydrohud.hudctl` (argparse wiring + output; fake or loopback HTTP)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hydrohud import api, hudctl, scenes


class FakeHost:
    """``hudctl`` 回环测试用的最小宿主。"""

    def status(self) -> dict[str, Any]:
        return {"ok": True, "streaming": True, "fps": 30, "brightness": 60}

    def apply_scene(self, scene: dict[str, Any]) -> str:
        return "已应用场景"

    def get_scene(self) -> dict[str, Any]:
        return scenes.normalize_scene({})

    def set_brightness(self, value: int) -> None:
        pass

    def set_power(self, on: bool) -> None:
        pass

    def set_stream(self, on: bool) -> None:
        pass

    def metrics(self) -> dict[str, Any]:
        return {"cpu": 12.5}


class FakeTransport:
    """替代 ``hudctl._request``，记录调用并可注入返回值/异常。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response: dict[str, Any] = {"ok": True}
        self.error: Exception | None = None

    def __call__(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        host: str,
        port: int,
        token: str,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "payload": payload,
                "host": host,
                "port": port,
                "token": token,
            }
        )
        if self.error is not None:
            raise self.error
        return dict(self.response)


@pytest.fixture()
def transport(monkeypatch: pytest.MonkeyPatch) -> FakeTransport:
    fake = FakeTransport()
    monkeypatch.setattr(hudctl, "_request", fake)
    monkeypatch.delenv("HYDROHUD_TOKEN", raising=False)
    return fake


# --------------------------------------------------------------------------- #
# token 解析
# --------------------------------------------------------------------------- #
def test_resolve_token_prefers_explicit_then_env_then_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert hudctl.resolve_token("explicit") == "explicit"

    monkeypatch.setenv("HYDROHUD_TOKEN", "from-env")
    assert hudctl.resolve_token(None) == "from-env"

    monkeypatch.delenv("HYDROHUD_TOKEN")
    token_file = tmp_path / "HydroHUD" / "api-token.txt"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("from-file\n", encoding="utf-8")
    assert hudctl.resolve_token(None) == "from-file"

    token_file.unlink()
    assert hudctl.resolve_token(None) == ""


# --------------------------------------------------------------------------- #
# 公共选项
# --------------------------------------------------------------------------- #
def test_global_options_before_and_after_subcommand(transport: FakeTransport) -> None:
    assert hudctl.main(["--port", "5001", "--host", "10.0.0.5", "status"]) == 0
    assert transport.calls[-1]["port"] == 5001
    assert transport.calls[-1]["host"] == "10.0.0.5"

    assert hudctl.main(["status", "--port", "5002"]) == 0
    assert transport.calls[-1]["port"] == 5002
    assert transport.calls[-1]["host"] == "127.0.0.1"


def test_token_option_is_passed_through(transport: FakeTransport) -> None:
    assert hudctl.main(["--token", "abc", "status"]) == 0
    assert transport.calls[-1]["token"] == "abc"


# --------------------------------------------------------------------------- #
# status / metrics / get-scene
# --------------------------------------------------------------------------- #
def test_status_human_output(transport: FakeTransport, capsys: pytest.CaptureFixture[str]) -> None:
    transport.response = {"ok": True, "streaming": False, "fps": 30}
    assert hudctl.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "streaming: 关" in output
    assert "fps: 30" in output


def test_json_output_is_single_parseable_line(
    transport: FakeTransport, capsys: pytest.CaptureFixture[str]
) -> None:
    transport.response = {"cpu": 12.5, "gpu_temp": 61}
    assert hudctl.main(["--json", "metrics"]) == 0
    output = capsys.readouterr().out
    assert len(output.strip().splitlines()) == 1
    assert json.loads(output) == {"cpu": 12.5, "gpu_temp": 61}


def test_metrics_human_output(transport: FakeTransport, capsys: pytest.CaptureFixture[str]) -> None:
    transport.response = {"cpu": 12.5, "gpu_temp": 61}
    assert hudctl.main(["metrics"]) == 0
    assert capsys.readouterr().out.splitlines() == ["cpu 12.5", "gpu_temp 61"]


def test_get_scene_prints_pretty_json(
    transport: FakeTransport, capsys: pytest.CaptureFixture[str]
) -> None:
    transport.response = {"version": 1, "name": "测试", "layers": []}
    assert hudctl.main(["get-scene"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["name"] == "测试"


# --------------------------------------------------------------------------- #
# scene / brightness / power / stream
# --------------------------------------------------------------------------- #
def test_scene_loads_file_and_posts_scene(
    transport: FakeTransport, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "demo.hud.json"
    scenes.save_project(scenes.normalize_scene({"name": "演示"}), str(path))
    transport.response = {"ok": True, "message": "已应用场景"}
    assert hudctl.main(["scene", str(path)]) == 0

    call = transport.calls[-1]
    assert call["method"] == "POST" and call["path"] == "/scene"
    assert call["payload"]["scene"]["name"] == "演示"
    assert "已应用场景" in capsys.readouterr().out


def test_missing_scene_file_exits_one(
    transport: FakeTransport, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert hudctl.main(["scene", str(tmp_path / "nope.hud.json")]) == 1
    assert "不存在" in capsys.readouterr().err


def test_invalid_scene_file_exits_one(
    transport: FakeTransport, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bad.hud.json"
    path.write_text("not json", encoding="utf-8")
    assert hudctl.main(["scene", str(path)]) == 1
    assert capsys.readouterr().err


def test_brightness_posts_value(
    transport: FakeTransport, capsys: pytest.CaptureFixture[str]
) -> None:
    transport.response = {"ok": True, "value": 60}
    assert hudctl.main(["brightness", "60"]) == 0
    assert transport.calls[-1]["payload"] == {"value": 60}
    assert "亮度已设为 60" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["-1", "101"])
def test_brightness_out_of_range_is_a_usage_error(transport: FakeTransport, value: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        hudctl.main(["brightness", value])
    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    ("state", "expected", "message"), [("on", True, "已开始推流"), ("off", False, "已停止推流")]
)
def test_stream_maps_on_off_to_bool(
    transport: FakeTransport,
    state: str,
    expected: bool,
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert hudctl.main(["stream", state]) == 0
    assert transport.calls[-1]["payload"] == {"on": expected}
    assert message in capsys.readouterr().out


@pytest.mark.parametrize(("state", "expected"), [("on", True), ("off", False)])
def test_power_maps_on_off_to_bool(
    transport: FakeTransport, state: str, expected: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    assert hudctl.main(["power", state]) == 0
    assert transport.calls[-1]["payload"] == {"on": expected}
    capsys.readouterr()


# --------------------------------------------------------------------------- #
# 错误处理
# --------------------------------------------------------------------------- #
def test_cli_error_is_printed_and_exits_one(
    transport: FakeTransport, capsys: pytest.CaptureFixture[str]
) -> None:
    transport.error = hudctl.CliError("无法连接 http://127.0.0.1:4780：拒绝连接")
    assert hudctl.main(["status"]) == 1
    assert "错误：" in capsys.readouterr().err


def test_unknown_command_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        hudctl.main(["nonsense"])
    assert excinfo.value.code == 2


def test_missing_argument_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        hudctl.main(["brightness"])
    assert excinfo.value.code == 2


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
def test_events_prints_streamed_lines(
    transport: FakeTransport, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str] = []

    def fake_stream(path: str, *, host: str, port: int, token: str):
        seen.append(path)
        yield {"fps": 30, "streaming": True}
        yield {"fps": 29, "streaming": True}

    monkeypatch.setattr(hudctl, "_stream_events", fake_stream)
    assert hudctl.main(["events", "--limit", "2"]) == 0
    assert seen == ["/events?limit=2"]
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert "fps: 30" in lines[0]


def test_events_json_output(
    transport: FakeTransport, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        hudctl, "_stream_events", lambda path, *, host, port, token: iter([{"fps": 30}])
    )
    assert hudctl.main(["--json", "events"]) == 0
    assert json.loads(capsys.readouterr().out) == {"fps": 30}


# --------------------------------------------------------------------------- #
# 真回环
# --------------------------------------------------------------------------- #
def test_loopback_status_and_missing_token_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("HYDROHUD_TOKEN", raising=False)
    server = api.ApiServer(FakeHost(), port=0, token="secret")
    server.start()
    try:
        assert hudctl.main(["--token", "secret", "--port", str(server.port), "status"]) == 0
        assert "fps: 30" in capsys.readouterr().out

        assert hudctl.main(["--port", str(server.port), "status"]) == 1
        err = capsys.readouterr().err
        assert "401" in err
        assert "token" in err
    finally:
        server.stop()
