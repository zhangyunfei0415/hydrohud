"""``hydrohud.weather`` 的纯软件测试。

约定：所有网络请求都 monkeypatch 掉 ``urllib.request.urlopen``，缓存目录用
``APPDATA`` 重定向到 ``tmp_path``；不会真的访问 Open-Meteo，也不会启动常驻线程
（只在个别用例里显式 start 并立刻 close）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hydrohud import weather  # noqa: E402

#: 一份典型的 Open-Meteo 响应。
PAYLOAD = {
    "latitude": 39.875,
    "longitude": 116.375,
    "timezone": "Asia/Shanghai",
    "current": {
        "time": "2026-11-15T20:00",
        "temperature_2m": 7.4,
        "apparent_temperature": 5.1,
        "relative_humidity_2m": 46,
        "wind_speed_10m": 11.2,
        "weather_code": 3,
    },
}


class FakeResponse:
    """最小的 ``urlopen`` 返回值替身：支持 with 与 read()。"""

    def __init__(self, payload: object, status: int = 200) -> None:
        if isinstance(payload, bytes):
            self._raw = payload
        else:
            self._raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.status = status
        self.closed = False

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        self.closed = True
        return False


class UrlSpy:
    """记录每次请求的 URL，并按顺序返回预设响应（或抛预设异常）。"""

    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.calls: list[str] = []
        self.timeouts: list[float | None] = []

    def __call__(self, url: str, timeout: float | None = None, **kwargs: object) -> FakeResponse:
        self.calls.append(str(url))
        self.timeouts.append(timeout)
        if not self.results:
            return FakeResponse(PAYLOAD)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return FakeResponse(result)


@pytest.fixture(autouse=True)
def _reset_last_error():
    """每个用例前清掉模块级 last_error，避免用例之间互相污染。"""
    weather._LAST_ERROR = None
    yield
    weather._LAST_ERROR = None


@pytest.fixture()
def appdata(tmp_path, monkeypatch):
    """把 ``%APPDATA%`` 指到临时目录，避免碰真实用户配置。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    weather.close_hub()
    yield tmp_path
    weather.close_hub()


@pytest.fixture()
def spy(monkeypatch):
    """替换 ``urllib.request.urlopen``；返回录制器。"""
    recorder = UrlSpy()
    monkeypatch.setattr(weather.urllib.request, "urlopen", recorder)
    return recorder


@pytest.fixture()
def hub(appdata):
    """默认坐标、默认间隔的 hub（不启动线程）。"""
    return weather.WeatherHub(weather.DEFAULT_LAT, weather.DEFAULT_LON)


# --------------------------------------------------------------------------- #
# 天气代码表与图标
# --------------------------------------------------------------------------- #
def test_code_text_covers_documented_ranges():
    assert weather.code_text(0) == "晴"
    assert weather.code_text(1) == "大部晴朗"
    assert weather.code_text(3) == "阴"
    assert weather.code_text(45) == "雾"
    assert weather.code_text(53) == "毛毛雨"
    assert weather.code_text(63) == "中雨"
    assert weather.code_text(75) == "大雪"
    assert weather.code_text(81) == "阵雨"
    assert weather.code_text(86) == "强阵雪"
    assert weather.code_text(95) == "雷暴"


@pytest.mark.parametrize(
    "code", [0, 1, 2, 3, 45, 48, 51, 55, 57, 61, 65, 67, 71, 77, 80, 82, 85, 95, 99]
)
def test_every_documented_code_has_text(code):
    assert code in weather.WEATHER_CODE_TEXT
    assert weather.WEATHER_CODE_TEXT[code]
    assert weather.code_text(code) == weather.WEATHER_CODE_TEXT[code]


def test_code_text_unknown_values():
    assert weather.code_text(None) == weather.UNKNOWN_TEXT
    assert weather.code_text(1234) == weather.UNKNOWN_TEXT


@pytest.mark.parametrize(
    ("code", "icon"),
    [
        (0, "sun"),
        (1, "cloud"),
        (2, "cloud"),
        (3, "cloud"),
        (45, "fog"),
        (48, "fog"),
        (51, "rain"),
        (57, "rain"),
        (61, "rain"),
        (67, "rain"),
        (71, "snow"),
        (77, "snow"),
        (80, "rain"),
        (82, "rain"),
        (85, "snow"),
        (86, "snow"),
        (95, "storm"),
        (99, "storm"),
    ],
)
def test_code_icon_mapping(code, icon):
    assert weather.code_icon(code) == icon
    assert weather.code_icon(code) in weather.ICON_NAMES


def test_code_icon_fallback():
    assert weather.code_icon(None) == "cloud"
    assert weather.code_icon(-1) == "cloud"
    assert weather.code_icon(4242) == "cloud"


# --------------------------------------------------------------------------- #
# URL 与解析
# --------------------------------------------------------------------------- #
def test_url_contains_all_fields(hub):
    url = hub.url()
    assert url.startswith(weather.API_URL + "?")
    for field in weather.CURRENT_FIELDS:
        assert field in url
    assert "latitude=39.9042" in url
    assert "longitude=116.4074" in url
    assert "timezone=auto" in url


def test_parse_payload_maps_current_fields():
    parsed = weather.parse_payload(PAYLOAD)
    assert parsed == {
        "code": 3,
        "temp": 7.4,
        "feels": 5.1,
        "humidity": 46.0,
        "wind": 11.2,
    }


def test_parse_payload_tolerates_null_numbers():
    payload = {"current": {"weather_code": 0, "temperature_2m": None}}
    parsed = weather.parse_payload(payload)
    assert parsed["code"] == 0
    assert parsed["temp"] is None
    assert parsed["feels"] is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "text",
        {},
        {"current": None},
        {"current": {}},
        {"current": {"temperature_2m": 1.0}},
    ],
)
def test_parse_payload_rejects_missing_code(payload):
    with pytest.raises(ValueError):
        weather.parse_payload(payload)


def test_parse_payload_rejects_nan_and_inf():
    parsed = weather.parse_payload(
        {"current": {"weather_code": 1, "temperature_2m": float("nan"), "wind_speed_10m": "x"}}
    )
    assert parsed["temp"] is None
    assert parsed["wind"] is None


# --------------------------------------------------------------------------- #
# refresh：成功、超时、坏 JSON
# --------------------------------------------------------------------------- #
def test_refresh_success_updates_snapshot(hub, spy):
    assert hub.snapshot() == {}
    assert hub.refresh(force=True) is True
    data = hub.snapshot()
    assert data["place"] == "Asia/Shanghai"
    assert data["code"] == 3
    assert data["temp"] == 7.4
    assert data["feels"] == 5.1
    assert data["humidity"] == 46.0
    assert data["wind"] == 11.2
    assert data["desc"] == "阴"
    assert data["icon"] == "cloud"
    assert data["fetched"] > 0
    assert spy.timeouts == [weather.TIMEOUT]


def test_refresh_snapshot_is_copy(hub, spy):
    hub.refresh(force=True)
    first = hub.snapshot()
    first["temp"] = -100.0
    assert hub.snapshot()["temp"] == 7.4


def test_refresh_failure_records_last_error(hub, spy):
    spy.results = [urllib.error.URLError("offline")]
    assert hub.refresh(force=True) is False
    assert hub.snapshot() == {}
    error = weather.last_error()
    assert error is not None
    assert "天气刷新失败" in error


def test_refresh_bad_json_records_error(hub, spy):
    spy.results = [b"not json"]
    assert hub.refresh(force=True) is False
    assert "天气刷新失败" in (weather.last_error() or "")


def test_refresh_skips_when_fresh(hub, spy):
    hub.refresh(force=True)
    assert hub.refresh() is False
    assert len(spy.calls) == 1
    assert hub.refresh(force=True) is True
    assert len(spy.calls) == 2


def test_refresh_always_runs_when_interval_zero(appdata, spy):
    hub = weather.WeatherHub(1.0, 2.0, interval=0.0)
    assert hub.refresh() is True
    assert hub.refresh() is True
    assert len(spy.calls) == 2


# --------------------------------------------------------------------------- #
# 缓存读写
# --------------------------------------------------------------------------- #
def test_cache_written_to_appdata(hub, spy, appdata):
    hub.refresh(force=True)
    path = weather.settings.app_data_dir() / weather.CACHE_FILE_NAME
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == set(weather.CACHE_KEYS)
    assert payload["code"] == 3
    assert payload["temp"] == 7.4
    assert "desc" not in payload


def test_snapshot_backfills_from_cache_offline(hub, spy, appdata):
    hub.refresh(force=True)
    offline = weather.WeatherHub(hub.lat, hub.lon, cache_path=hub.cache_path)
    spy.results = [urllib.error.URLError("offline")]
    data = offline.snapshot()
    assert data["code"] == 3
    assert data["desc"] == "阴"
    assert data["icon"] == "cloud"
    assert offline.refresh(force=True) is False
    assert offline.snapshot()["temp"] == 7.4


def test_custom_cache_path(appdata, spy, tmp_path):
    path = tmp_path / "custom" / "cache.json"
    hub = weather.WeatherHub(1.0, 2.0, cache_path=path)
    hub.refresh(force=True)
    assert path.exists()
    assert hub.cache_path == path


@pytest.mark.parametrize("text", ["{", "[]", "null", '"text"'])
def test_bad_cache_is_ignored(hub, appdata, text):
    path = weather.settings.app_data_dir() / weather.CACHE_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    assert hub.snapshot() == {}
    assert weather.last_error()


def test_cache_without_code_degrades(hub, appdata):
    path = weather.settings.app_data_dir() / weather.CACHE_FILE_NAME
    path.write_text(json.dumps({"temp": 1.0, "fetched": 1}), encoding="utf-8")
    data = hub.snapshot()
    assert data["code"] is None
    assert data["desc"] == weather.UNKNOWN_TEXT
    assert data["temp"] == 1.0


def test_cache_dir_unwritable_is_swallowed(appdata, spy, tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")  # 同名文件，mkdir 必失败
    hub = weather.WeatherHub(1.0, 2.0, cache_path=blocker / "cache.json")
    assert hub.refresh(force=True) is True
    assert hub.snapshot()["code"] == 3
    assert "写入天气缓存失败" in (weather.last_error() or "")


# --------------------------------------------------------------------------- #
# describe / 线程
# --------------------------------------------------------------------------- #
def test_describe_without_data(hub):
    text = hub.describe()
    assert "暂无数据" in text


def test_describe_with_data(hub, spy):
    hub.refresh(force=True)
    text = hub.describe()
    assert "Asia/Shanghai" in text
    assert "阴" in text
    assert "7.4" in text


def test_start_and_close_thread(hub, spy):
    hub.start()
    assert hub.running()
    deadline = time.monotonic() + 2.0
    while hub.snapshot() == {} and time.monotonic() < deadline:
        time.sleep(0.01)
    hub.close()
    assert not hub.running()
    assert hub.snapshot()["code"] == 3
    hub.close()  # 幂等


def test_start_is_idempotent(hub, spy):
    hub.start()
    first = hub._thread
    hub.start()
    assert hub._thread is first
    hub.close()


def test_close_without_start(hub):
    hub.close()  # 不抛异常
    assert not hub.running()


def test_thread_records_unexpected_failure(appdata, monkeypatch):
    hub = weather.WeatherHub(1.0, 2.0, interval=0.01)

    def boom(*args: object, **kwargs: object):
        raise RuntimeError("线程里炸了")

    monkeypatch.setattr(hub, "refresh", boom)
    hub.start()
    deadline = time.monotonic() + 2.0
    while weather.last_error() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    hub.close()
    assert "天气线程异常" in (weather.last_error() or "")


# --------------------------------------------------------------------------- #
# 单例
# --------------------------------------------------------------------------- #
def test_get_hub_returns_singleton(appdata, monkeypatch):
    monkeypatch.setattr(weather.WeatherHub, "start", lambda self: None)
    first = weather.get_hub()
    second = weather.get_hub()
    assert first is second
    assert (first.lat, first.lon) == (weather.DEFAULT_LAT, weather.DEFAULT_LON)


def test_get_hub_rebuilds_on_coordinate_change(appdata, monkeypatch):
    monkeypatch.setattr(weather.WeatherHub, "start", lambda self: None)
    first = weather.get_hub(31.2304, 121.4737)
    second = weather.get_hub(31.2304, 121.4737)
    assert first is second
    third = weather.get_hub(22.5431, 114.0579)
    assert third is not first
    assert (third.lat, third.lon) == (22.5431, 114.0579)


def test_get_hub_reuses_same_coordinates(appdata, monkeypatch):
    monkeypatch.setattr(weather.WeatherHub, "start", lambda self: None)
    hub = weather.get_hub(1.5, 2.5)
    assert weather.get_hub(1.5, 2.5) is hub


def test_close_hub_clears_singleton(appdata, monkeypatch):
    monkeypatch.setattr(weather.WeatherHub, "start", lambda self: None)
    hub = weather.get_hub()
    weather.close_hub()
    assert weather.get_hub() is not hub


def test_last_error_starts_none():
    weather._LAST_ERROR = None
    assert weather.last_error() is None


def test_no_threads_started_on_import():
    # 导入本模块不会留下任何以 hydrohud-weather 命名的线程。
    assert not [t for t in threading.enumerate() if t.name == "hydrohud-weather"]
