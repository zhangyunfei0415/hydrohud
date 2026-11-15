"""v1.3 新增图层（温度计 / 倒计时 / 滚动文字 / 图片序列 / 二维码 / 天气 / 正在播放）
与屏幕捕获背景的渲染测试。

全部纯软件：传感器、天气、媒体、截屏、插件五个可选数据源都通过
``render._sensor_hub`` / ``render._weather_hub`` / ``render._media_hub`` /
``render._capture`` / ``render._plugins`` 注入替身，因此不碰真实硬件、网络、
系统截屏或 GUI。运行：``python -m pytest tests/test_render_v13.py -q``。
"""

from __future__ import annotations

import os
import sys
import time

import pytest
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hydrohud import render  # noqa: E402
from hydrohud.render import SCREEN_H, SCREEN_W, clear_cache, render_scene  # noqa: E402
from hydrohud.scenes import normalize_scene  # noqa: E402

BLACK = (0, 0, 0)
NEW_LAYER_TYPES = (
    "thermometer",
    "countdown",
    "ticker",
    "sequence",
    "qrcode",
    "weather",
    "nowplaying",
)


@pytest.fixture(autouse=True)
def _reset_state():
    """每个用例前后清掉渲染缓存与注入的替身，避免用例之间互相污染。"""
    clear_cache()
    render._sensor_hub = None
    render._weather_hub = None
    render._media_hub = None
    yield
    clear_cache()
    render._sensor_hub = None
    render._weather_hub = None
    render._media_hub = None


# --------------------------------------------------------------------------- #
# 替身
# --------------------------------------------------------------------------- #
class FakeSensorHub:
    """固定指标值的假 SensorHub。"""

    def __init__(self, values: dict | None = None) -> None:
        self.values = dict(values or {})

    def sample(self, force: bool = False) -> dict:
        return dict(self.values)

    def value(self, metric: str):
        return self.values.get(metric)

    def history(self, metric: str, limit: int = 0) -> list[float]:
        return [self.values[metric]] if metric in self.values else []


class FakeSnapshotHub:
    """只有一个 ``snapshot()`` 的假 hub（天气 / 媒体共用）。"""

    def __init__(self, snapshot: dict) -> None:
        self._snapshot = dict(snapshot)
        self.calls = 0

    def snapshot(self) -> dict:
        self.calls += 1
        return dict(self._snapshot)


class FakeCapture:
    """按调用次数返回不同纯色图的假截屏模块。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def grab(self, region=(0.0, 0.0, 1.0, 1.0), monitor: int = 0, size: int = 480) -> Image.Image:
        self.calls.append((tuple(region), monitor, size))
        color = (40 + (len(self.calls) * 17) % 200, 60, 90)
        return Image.new("RGB", (size, size), color)


class FakePlugins:
    """把整块画布涂成给定的纯色，返回 ``result``。"""

    def __init__(self, result: bool = True, color=(0, 200, 120)) -> None:
        self.result = result
        self.color = color
        self.calls = 0

    def draw_layer(self, image: Image.Image, layer: dict, ctx: dict) -> bool:
        self.calls += 1
        if not self.result:
            return False
        image.paste(Image.new("RGBA", image.size, self.color + (255,)), (0, 0))
        return True


def _layer(**kwargs) -> dict:
    """构造一个经 normalize 的单个图层。"""
    scene = normalize_scene({"layers": [dict(kwargs)]})
    return scene["layers"][0]


def _scene(*layers: dict, background: dict | None = None) -> dict:
    payload = {"layers": list(layers)}
    if background is not None:
        payload["background"] = background
    return normalize_scene(payload)


def _opaque_pixels(image: Image.Image) -> int:
    """统计 RGBA 画布里 alpha > 0 的像素数（用直方图，避免逐像素遍历）。"""
    histogram = image.getchannel("A").histogram()
    return sum(histogram[1:])


def _render_layer(layer: dict, t: float = 0.0, ctx: dict | None = None) -> Image.Image:
    return render._render_layer(layer, t, ctx if ctx is not None else {})


# --------------------------------------------------------------------------- #
# 温度计
# --------------------------------------------------------------------------- #
def test_thermometer_draws_with_metric() -> None:
    render._sensor_hub = FakeSensorHub({"cpu_temp": 55.0})
    overlay = _render_layer(_layer(type="thermometer", metric="cpu_temp"))
    assert overlay.size == (SCREEN_W, SCREEN_H)
    assert _opaque_pixels(overlay) > 200


def test_thermometer_value_changes_pixels() -> None:
    render._sensor_hub = FakeSensorHub({"cpu_temp": 5.0})
    cold = _render_layer(_layer(type="thermometer", metric="cpu_temp"))
    clear_cache()
    render._sensor_hub = FakeSensorHub({"cpu_temp": 100.0})
    hot = _render_layer(_layer(type="thermometer", metric="cpu_temp"))
    assert cold.tobytes() != hot.tobytes()


def test_thermometer_survives_missing_metric() -> None:
    render._sensor_hub = FakeSensorHub({})
    overlay = _render_layer(_layer(type="thermometer", metric="cpu_temp"))
    assert _opaque_pixels(overlay) > 100


def test_thermometer_accepts_empty_layer() -> None:
    render._sensor_hub = FakeSensorHub({"cpu": 30.0})
    overlay = _render_layer(_layer(type="thermometer"))
    assert overlay.size == (SCREEN_W, SCREEN_H)


# --------------------------------------------------------------------------- #
# 倒计时
# --------------------------------------------------------------------------- #
def test_countdown_without_target_shows_placeholder() -> None:
    overlay = _render_layer(_layer(type="countdown"))
    assert _opaque_pixels(overlay) > 100


def test_countdown_future_target_renders_and_differs_from_placeholder() -> None:
    placeholder = _render_layer(_layer(type="countdown"))
    future = _render_layer(_layer(type="countdown", target="2099-12-31", title="发射"))
    assert _opaque_pixels(future) > 100
    assert placeholder.tobytes() != future.tobytes()


def test_countdown_parts_parse_and_count_down() -> None:
    import datetime as _dt

    layer = _layer(type="countdown", target="2000-01-02 00:00:00", title="过期")
    title, main, state = render._countdown_parts(layer, now=_dt.datetime(2000, 1, 1, 0, 0, 0))
    assert title == "过期"
    assert state == "running"
    assert main.startswith("1 天")


def test_countdown_past_target_reports_done() -> None:
    import datetime as _dt

    layer = _layer(type="countdown", target="2000-01-01", done_text="结束啦")
    _title, main, state = render._countdown_parts(layer, now=_dt.datetime(2030, 1, 1))
    assert state == "done"
    assert main == "结束啦"


def test_countdown_invalid_target_reports_placeholder() -> None:
    layer = _layer(type="countdown", target="不是时间")
    _title, _main, state = render._countdown_parts(layer)
    assert state == "placeholder"


# --------------------------------------------------------------------------- #
# 滚动文字
# --------------------------------------------------------------------------- #
def test_ticker_draws_and_scrolls() -> None:
    layer = _layer(type="ticker", text="HydroHUD 滚动字幕测试")
    first = _render_layer(layer, t=0.0)
    later = _render_layer(layer, t=1.0)
    assert _opaque_pixels(first) > 100
    assert first.tobytes() != later.tobytes()


def test_ticker_is_stable_within_same_time() -> None:
    layer = _layer(type="ticker", text="稳定性")
    clear_cache()
    first = _render_layer(layer, t=2.5)
    clear_cache()
    second = _render_layer(layer, t=2.5)
    assert first.tobytes() == second.tobytes()


def test_ticker_empty_text_draws_nothing() -> None:
    overlay = _render_layer(_layer(type="ticker", text=""))
    assert _opaque_pixels(overlay) == 0


def test_ticker_metrics_cycle_is_at_least_minimum() -> None:
    layer = _layer(type="ticker", text="短", gap=0.0, speed=600.0)
    phase, width, cycle = render._ticker_metrics(layer, 1.0)
    assert width > 0
    assert cycle >= render._TICKER_CYCLE_MIN
    assert 0.0 <= phase < cycle


# --------------------------------------------------------------------------- #
# 图片序列
# --------------------------------------------------------------------------- #
def _make_png(path, color) -> str:
    Image.new("RGB", (60, 40), color).save(path)
    return str(path)


def test_sequence_frame_path_cycles(tmp_path) -> None:
    layer = _layer(
        type="sequence",
        paths=[_make_png(tmp_path / f"{index}.png", (index * 40, 10, 10)) for index in range(3)],
        fps=2.0,
    )
    path, index = render._sequence_frame_path(layer, 0.0)
    assert index == 0 and path.endswith("0.png")
    path, index = render._sequence_frame_path(layer, 1.0)
    assert index == 2 and path.endswith("2.png")
    path, index = render._sequence_frame_path(layer, 2.0)
    assert index == 1


def test_sequence_frame_path_clamps_when_loop_off(tmp_path) -> None:
    layer = _layer(
        type="sequence",
        paths=[_make_png(tmp_path / f"{index}.png", (10, 10, index * 40)) for index in range(2)],
        fps=4.0,
        loop=False,
    )
    _path, index = render._sequence_frame_path(layer, 100.0)
    assert index == 1


def test_sequence_draws_and_caches_frame(tmp_path) -> None:
    paths = [_make_png(tmp_path / f"{index}.png", (index * 60, 30, 200)) for index in range(2)]
    layer = _layer(type="sequence", paths=paths, fps=1.0)
    assert _opaque_pixels(_render_layer(layer, t=0.0)) > 100
    assert len(render._sequence_cache) >= 1
    before = len(render._sequence_cache)
    _render_layer(layer, t=0.0)
    assert len(render._sequence_cache) == before


def test_sequence_missing_paths_shows_placeholder() -> None:
    overlay = _render_layer(_layer(type="sequence", paths=[]))
    assert _opaque_pixels(overlay) > 100


def test_sequence_missing_file_shows_placeholder(tmp_path) -> None:
    layer = _layer(type="sequence", paths=[str(tmp_path / "nope.png")])
    overlay = _render_layer(layer)
    assert _opaque_pixels(overlay) > 100


# --------------------------------------------------------------------------- #
# 二维码
# --------------------------------------------------------------------------- #
def test_qrcode_uses_qrmini_matrix() -> None:
    assert render._qrmini is not None
    layer = _layer(type="qrcode", data="https://github.com/zhangyunfei0415/hydrohud")
    matrix = render._qr_matrix(layer, "https://github.com/zhangyunfei0415/hydrohud")
    assert matrix is not None
    assert len(matrix) >= 21
    assert len({len(row) for row in matrix}) == 1


def test_qrcode_draws_visible_square() -> None:
    layer = _layer(type="qrcode", data="HydroHUD")
    overlay = _render_layer(layer)
    assert _opaque_pixels(overlay) > 10000
    assert len(render._qr_image_cache) >= 1


def test_qrcode_empty_data_shows_placeholder() -> None:
    overlay = _render_layer(_layer(type="qrcode", data=""))
    assert _opaque_pixels(overlay) > 100
    assert not render._qr_image_cache


def test_qrcode_oversized_data_degrades_without_raising() -> None:
    layer = _layer(type="qrcode", data="x" * 5000, error="H")
    overlay = _render_layer(layer)
    assert overlay.size == (SCREEN_W, SCREEN_H)


def test_qrcode_resolves_metric_placeholder() -> None:
    render._sensor_hub = FakeSensorHub({"cpu": 42.0})
    layer = _layer(type="qrcode", data="CPU {cpu}")
    resolved = render._qr_data(layer, {"values": {"cpu": 42.0}, "metrics": None, "history": None})
    assert "42" in resolved


# --------------------------------------------------------------------------- #
# 天气
# --------------------------------------------------------------------------- #
def test_weather_uses_injected_snapshot() -> None:
    render._weather_hub = FakeSnapshotHub(
        {"temp": 25.3, "desc": "晴", "code": 0, "humidity": 61.0, "wind": 8.0, "place": "北京"}
    )
    layer = _layer(type="weather", fields=["temp", "desc"], place="北京")
    overlay = _render_layer(layer)
    assert _opaque_pixels(overlay) > 100
    snapshot = render._weather_snapshot({}, layer)
    assert snapshot["temp"] == pytest.approx(25.3)
    text = render._weather_text(layer, snapshot)
    assert "北京" in text
    assert "25°" in text
    assert "晴" in text


def test_weather_without_data_shows_placeholder_text() -> None:
    render._weather = None
    render._weather_hub = None
    layer = _layer(type="weather")
    snapshot = render._weather_snapshot({}, layer)
    assert snapshot == {}
    assert render._weather_text(layer, snapshot) == render._WEATHER_PLACEHOLDER_TEXT


def test_weather_snapshot_shared_within_one_render() -> None:
    hub = FakeSnapshotHub({"temp": 10.0, "desc": "阴"})
    render._weather_hub = hub
    layer = _layer(type="weather")
    ctx: dict = {}
    render._weather_snapshot(ctx, layer)
    render._weather_snapshot(ctx, layer)
    assert hub.calls == 1


def test_weather_icon_kind_maps_code() -> None:
    assert render._weather_icon_kind({"code": 0}) == "sun"
    assert render._weather_icon_kind({"code": 61}) == "rain"
    assert render._weather_icon_kind({"code": 95}) == "storm"


# --------------------------------------------------------------------------- #
# 正在播放
# --------------------------------------------------------------------------- #
def test_nowplaying_renders_title_and_artist() -> None:
    render._media_hub = FakeSnapshotHub(
        {"title": "夜曲", "artist": "周杰伦", "album": "十一月的萧邦", "status": "playing"}
    )
    layer = _layer(type="nowplaying", fields=["title", "artist"], show_cover=False)
    overlay = _render_layer(layer)
    assert _opaque_pixels(overlay) > 100
    lines = render._nowplaying_lines(layer, render._media_snapshot({}))
    assert lines[0][1] == "夜曲"
    assert "周杰伦" in lines[1][1]


def test_nowplaying_without_media_shows_placeholder() -> None:
    render._media = None
    render._media_hub = None
    layer = _layer(type="nowplaying")
    assert render._media_snapshot({}) == {}
    overlay = _render_layer(layer)
    assert _opaque_pixels(overlay) > 100


def test_nowplaying_cover_image_is_cropped_square() -> None:
    cover = Image.new("RGB", (120, 80), (200, 30, 30))
    render._media_hub = FakeSnapshotHub({"title": "封面测试", "cover": cover})
    layer = _layer(type="nowplaying", show_cover=True, cover_size=0.2)
    overlay = _render_layer(layer)
    assert _opaque_pixels(overlay) > 100
    assert len(render._cover_cache) == 1


# --------------------------------------------------------------------------- #
# 动态图层判定与场景组装
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", [kind for kind in NEW_LAYER_TYPES if kind != "qrcode"])
def test_new_layer_types_are_dynamic(kind) -> None:
    assert render._is_dynamic_layer(_layer(type=kind)) is True


def test_qrcode_is_static_unless_it_has_a_placeholder() -> None:
    """二维码内容固定时是可烘焙的静态图层，只有带模板占位符才逐帧重算。"""
    assert render._is_dynamic_layer(_layer(type="qrcode", data="固定内容")) is False
    assert render._is_dynamic_layer(_layer(type="qrcode", data="看板 {cpu}")) is True


def test_static_text_is_not_dynamic_but_template_is() -> None:
    assert render._is_dynamic_layer(_layer(type="text", text="固定文案")) is False
    assert render._is_dynamic_layer(_layer(type="text", text="CPU {cpu}")) is True


def test_qrcode_with_placeholder_is_dynamic() -> None:
    assert render._is_dynamic_layer(_layer(type="qrcode", data="{cpu}")) is True
    assert render._is_dynamic_layer(_layer(type="qrcode", data="固定内容")) is False


def test_scene_with_new_layers_renders_once_for_all() -> None:
    render._sensor_hub = FakeSensorHub({"cpu": 30.0, "cpu_temp": 50.0})
    render._weather_hub = FakeSnapshotHub({"temp": 20.0, "desc": "多云"})
    render._media_hub = FakeSnapshotHub({"title": "歌", "artist": "手"})
    scene = _scene(
        _layer(type="thermometer", metric="cpu_temp", x=0.15, y=0.1),
        _layer(type="countdown", target="2099-01-01", x=0.5, y=0.15),
        _layer(type="ticker", text="跑马灯", x=0.5, y=0.3),
        _layer(type="qrcode", data="HydroHUD", x=0.8, y=0.1),
        _layer(type="weather", x=0.3, y=0.8),
        _layer(type="nowplaying", x=0.7, y=0.8, show_cover=False),
    )
    image = render_scene(scene, 0.5)
    assert image.size == (SCREEN_W, SCREEN_H)
    assert image.mode == "RGB"
    assert image.getbbox() is not None
    # 场景里含动态图层，不能走"静态烘焙"快路径
    entry = render._lru_touch(render._scene_cache, render._scene_key(scene))
    assert entry is not None and entry["dynamic"] is True


def test_scene_with_only_static_layers_uses_baked_image(monkeypatch) -> None:
    scene = _scene(_layer(type="text", text="静态"))
    first = render_scene(scene, 0.0)
    entry = render._lru_touch(render._scene_cache, render._scene_key(scene))
    assert entry is not None and entry["dynamic"] is False
    second = render_scene(scene, 99.0)
    assert first.tobytes() == second.tobytes()


# --------------------------------------------------------------------------- #
# 屏幕捕获背景
# --------------------------------------------------------------------------- #
def test_capture_background_uses_injected_grabber() -> None:
    fake = FakeCapture()
    render._capture = fake
    scene = _scene(
        _layer(type="text", text="捕获背景"),
        background={
            "type": "capture",
            "region": [0.25, 0.25, 0.5, 0.5],
            "monitor": 1,
            "fps": 5.0,
        },
    )
    image = render_scene(scene, 0.0)
    assert image.size == (SCREEN_W, SCREEN_H)
    assert len(fake.calls) >= 1
    region, monitor, size = fake.calls[0]
    assert region == pytest.approx((0.25, 0.25, 0.5, 0.5))
    assert monitor == 1
    assert size == SCREEN_W


def test_capture_background_caches_within_one_fps_slot() -> None:
    fake = FakeCapture()
    render._capture = fake
    scene = _scene(background={"type": "capture", "fps": 5.0})
    render_scene(scene, 0.0)
    calls = len(fake.calls)
    render_scene(scene, 0.05)  # 同一 0.2 秒槽位 → 复用缓存
    assert len(fake.calls) == calls
    render_scene(scene, 0.25)  # 下一个槽位 → 重新截屏
    assert len(fake.calls) == calls + 1


def test_capture_background_is_dynamic() -> None:
    scene = _scene(background={"type": "capture"})
    entry = render._build_entry(scene)
    assert entry["dynamic"] is True


def test_capture_background_degrades_when_module_missing() -> None:
    render._capture = None
    scene = _scene(background={"type": "capture"})
    image = render_scene(scene, 0.0)
    assert image.size == (SCREEN_W, SCREEN_H)
    assert image.getpixel((0, 0)) == render._rgb("#0B0F14")


def test_capture_frame_tolerates_broken_grabber() -> None:
    class Broken:
        def grab(self, region, monitor, size):
            raise RuntimeError("截屏后端崩了")

    render._capture = Broken()
    frame = render._capture_frame({"type": "capture"}, 0.0)
    assert frame.size == (SCREEN_W, SCREEN_H)
    assert frame.getpixel((0, 0)) == render._rgb("#0B0F14")


def test_capture_frame_normalizes_odd_size_image() -> None:
    class OddSize:
        def grab(self, region, monitor, size):
            return Image.new("RGB", (120, 200), (10, 20, 30))

    render._capture = OddSize()
    frame = render._capture_frame({"type": "capture"}, 0.0)
    assert frame.size == (SCREEN_W, SCREEN_H)


def test_capture_frame_clamps_invalid_region_and_fps() -> None:
    fake = FakeCapture()
    render._capture = fake
    frame = render._capture_frame(
        {"type": "capture", "region": [2.0, -1.0, 0.0, 5.0], "fps": 999.0, "monitor": 99},
        0.0,
    )
    assert frame.size == (SCREEN_W, SCREEN_H)
    region, monitor, _size = fake.calls[0]
    assert region == (1.0, 0.0, 0.0, 1.0) or region[0] == pytest.approx(1.0)
    assert 0 <= monitor <= 8


# --------------------------------------------------------------------------- #
# 插件图层分发
# --------------------------------------------------------------------------- #
def test_plugin_layer_dispatches_unknown_type() -> None:
    fake = FakePlugins(result=True)
    render._plugins = fake
    overlay = _render_layer({"type": "sparkline", "x": 0.5, "y": 0.5})
    assert fake.calls == 1
    assert _opaque_pixels(overlay) > 10000


def test_plugin_layer_false_result_draws_nothing() -> None:
    fake = FakePlugins(result=False)
    render._plugins = fake
    overlay = _render_layer({"type": "sparkline"})
    assert fake.calls == 1
    assert _opaque_pixels(overlay) == 0


def test_plugin_layer_exception_is_swallowed() -> None:
    class Boom:
        def draw_layer(self, image, layer, ctx):
            raise RuntimeError("插件炸了")

    render._plugins = Boom()
    overlay = _render_layer({"type": "sparkline"})
    assert _opaque_pixels(overlay) == 0


def test_unknown_layer_without_plugins_is_ignored() -> None:
    render._plugins = None
    overlay = _render_layer({"type": "不存在的类型"})
    assert _opaque_pixels(overlay) == 0


# --------------------------------------------------------------------------- #
# 性能预算与缓存清理
# --------------------------------------------------------------------------- #
def test_all_new_layers_stay_within_frame_budget() -> None:
    render._sensor_hub = FakeSensorHub({"cpu": 40.0, "cpu_temp": 55.0})
    render._weather_hub = FakeSnapshotHub({"temp": 20.0, "desc": "晴"})
    render._media_hub = FakeSnapshotHub({"title": "歌", "artist": "手"})
    layers = [
        _layer(type="thermometer", metric="cpu_temp"),
        _layer(type="countdown", target="2099-01-01"),
        _layer(type="ticker", text="性能预算"),
        _layer(type="qrcode", data="HydroHUD"),
        _layer(type="weather"),
        _layer(type="nowplaying", show_cover=False),
    ]
    scene = _scene(*layers)
    render_scene(scene, 0.0)  # 预热字体与二维码缓存
    started = time.perf_counter()
    for step in range(10):
        render_scene(scene, step * 0.1)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"10 帧耗时 {elapsed:.3f}s，超出一帧预算"


def test_clear_cache_drops_new_layer_caches() -> None:
    render._sensor_hub = FakeSensorHub({"cpu": 30.0})
    _render_layer(_layer(type="qrcode", data="清理缓存"))
    assert render._qr_matrix_cache
    clear_cache()
    for cache in (
        render._qr_matrix_cache,
        render._qr_image_cache,
        render._sequence_cache,
        render._cover_cache,
        render._nowplaying_text_cache,
        render._capture_cache,
    ):
        assert len(cache) == 0
