"""``hydrohud.capture`` 的纯软件测试。

约定：不真的截屏、不碰真实多显示器。``capture.ImageGrab`` 与 ``capture.user32``
都被替换成替身：前者返回固定尺寸的合成图并记录 bbox，后者按字典返回
``GetSystemMetrics`` 的值，所以坐标换算可以在任意平台断言。
"""

from __future__ import annotations

import os
import sys

import pytest
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hydrohud import capture  # noqa: E402

#: 测试统一使用的虚拟桌面：左上角 (-1920, 0)，总尺寸 3840x1080（双屏）。
BOUNDS = (-1920, 0, 3840, 1080)


class FakeUser32:
    """``user32`` 替身：按索引返回 ``GetSystemMetrics`` 的值。"""

    def __init__(self, values: dict[int, int] | None = None) -> None:
        self.values = dict(values or {})
        self.calls: list[int] = []
        self.GetSystemMetrics = self._metric

    def _metric(self, index: int) -> int:
        self.calls.append(index)
        if index in self.values:
            return self.values[index]
        return {
            capture.SM_XVIRTUALSCREEN: BOUNDS[0],
            capture.SM_YVIRTUALSCREEN: BOUNDS[1],
            capture.SM_CXVIRTUALSCREEN: BOUNDS[2],
            capture.SM_CYVIRTUALSCREEN: BOUNDS[3],
        }.get(index, 0)


class BoomingUser32:
    """``GetSystemMetrics`` 抛异常的替身。"""

    def GetSystemMetrics(self, index: int) -> int:
        raise OSError("user32 调用失败")


class FakeImageGrab:
    """``PIL.ImageGrab`` 替身：返回固定大小的图并记录每次 bbox。"""

    def __init__(self, size=(800, 600), color=(10, 20, 30), fail: BaseException | None = None):
        self.size = size
        self.color = color
        self.fail = fail
        self.calls: list[dict] = []

    def grab(self, bbox=None, all_screens=False):
        self.calls.append({"bbox": bbox, "all_screens": all_screens})
        if self.fail is not None:
            raise self.fail
        return Image.new("RGB", self.size, self.color)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """每个用例前清空缓存、last_error，并注入两个替身。"""
    capture._LAST_ERROR = None
    capture.reset_cache()
    monkeypatch.setattr(capture, "user32", FakeUser32())
    monkeypatch.setattr(capture, "ImageGrab", FakeImageGrab())
    yield
    capture.reset_cache()
    capture._LAST_ERROR = None


def _grab() -> FakeImageGrab:
    """取当前注入的 ImageGrab 替身。"""
    return capture.ImageGrab


# --------------------------------------------------------------------------- #
# virtual_bounds
# --------------------------------------------------------------------------- #
def test_virtual_bounds_reads_four_metrics():
    assert capture.virtual_bounds() == BOUNDS
    indices = capture.user32.calls
    assert indices == [
        capture.SM_XVIRTUALSCREEN,
        capture.SM_YVIRTUALSCREEN,
        capture.SM_CXVIRTUALSCREEN,
        capture.SM_CYVIRTUALSCREEN,
    ]


def test_virtual_bounds_negative_origin():
    capture.user32 = FakeUser32(
        {
            capture.SM_XVIRTUALSCREEN: -1280,
            capture.SM_YVIRTUALSCREEN: -200,
            capture.SM_CXVIRTUALSCREEN: 3200,
            capture.SM_CYVIRTUALSCREEN: 1440,
        }
    )
    assert capture.virtual_bounds() == (-1280, -200, 3200, 1440)


@pytest.mark.parametrize(
    "values",
    [
        {capture.SM_CXVIRTUALSCREEN: 0},
        {capture.SM_CYVIRTUALSCREEN: 0},
        {capture.SM_CXVIRTUALSCREEN: -5},
        {capture.SM_CYVIRTUALSCREEN: -5},
        {capture.SM_CXVIRTUALSCREEN: 0, capture.SM_CYVIRTUALSCREEN: 0},
    ],
)
def test_virtual_bounds_rejects_non_positive_size(values):
    capture.user32 = FakeUser32(values)
    assert capture.virtual_bounds() == capture.FALLBACK_BOUNDS
    assert capture.last_error()


def test_virtual_bounds_falls_back_when_user32_raises():
    capture.user32 = BoomingUser32()
    assert capture.virtual_bounds() == capture.FALLBACK_BOUNDS
    assert "读取虚拟桌面尺寸失败" in (capture.last_error() or "")


def test_virtual_bounds_falls_back_when_dll_missing(monkeypatch):
    def boom():
        raise OSError("没有 user32")

    monkeypatch.setattr(capture, "user32", None)
    monkeypatch.setattr(capture, "_user32", boom)
    assert capture.virtual_bounds() == capture.FALLBACK_BOUNDS
    assert "加载 user32 失败" in (capture.last_error() or "")


def test_fallback_bounds_shape():
    assert len(capture.FALLBACK_BOUNDS) == 4
    assert capture.FALLBACK_BOUNDS[2] > 0
    assert capture.FALLBACK_BOUNDS[3] > 0


# --------------------------------------------------------------------------- #
# 坐标换算
# --------------------------------------------------------------------------- #
def test_absolute_bbox_full_desktop():
    assert capture._absolute_bbox((0.0, 0.0, 1.0, 1.0)) == (-1920, 0, 1920, 1080)


def test_absolute_bbox_quarter_region():
    # 0.25/0.25/0.5/0.5 → 偏移 (-1920+960, 0+270)，宽高各 1920/540
    assert capture._absolute_bbox((0.25, 0.25, 0.5, 0.5)) == (-960, 270, 960, 810)


def test_absolute_bbox_tiny_region_has_positive_size():
    left, top, right, bottom = capture._absolute_bbox((0.5, 0.5, 0.0001, 0.0001))
    assert right > left
    assert bottom > top


def test_absolute_bbox_clamps_region():
    assert capture._absolute_bbox((-1.0, -1.0, 2.0, 2.0)) == capture._absolute_bbox(
        (0.0, 0.0, 1.0, 1.0)
    )


@pytest.mark.parametrize("region", [(0.0, 0.0, 1.0), (0.0, 0.0, 1.0, 1.0, 2.0), "abcd"])
def test_absolute_bbox_rejects_bad_shape(region):
    with pytest.raises(ValueError):
        capture._absolute_bbox(region)


@pytest.mark.parametrize("region", [(0.0, 0.0, 0.0, 1.0), (0.1, 0.1, 1.0, 0.0)])
def test_absolute_bbox_rejects_zero_size(region):
    with pytest.raises(ValueError):
        capture._absolute_bbox(region)


def test_absolute_bbox_rejects_nan():
    with pytest.raises(ValueError):
        capture._absolute_bbox((float("nan"), 0.0, 1.0, 1.0))


@pytest.mark.parametrize("monitor", [0, 1, 4, 8])
def test_monitor_range_is_accepted(monitor):
    assert capture._clean_monitor(monitor) == monitor


@pytest.mark.parametrize("monitor", [-1, 9, 100])
def test_monitor_out_of_range_rejected(monitor):
    with pytest.raises(ValueError):
        capture._clean_monitor(monitor)


# --------------------------------------------------------------------------- #
# grab
# --------------------------------------------------------------------------- #
def test_grab_passes_bbox_and_all_screens():
    image = capture.grab((0.25, 0.25, 0.5, 0.5), size=100)
    assert _grab().calls == [{"bbox": (-960, 270, 960, 810), "all_screens": True}]
    assert image.size == (100, 100)
    assert image.mode == "RGB"


def test_grab_uses_default_region():
    image = capture.grab()
    assert _grab().calls[0]["bbox"] == (-1920, 0, 1920, 1080)
    assert image.size == (480, 480)


def test_grab_center_crops_to_square():
    capture.ImageGrab = FakeImageGrab(size=(800, 600))
    image = capture.grab(size=64)
    assert image.size == (64, 64)


def test_grab_output_is_rgb():
    capture.ImageGrab = FakeImageGrab()
    original = Image.new("L", (800, 600), 128)
    capture.ImageGrab.grab = lambda bbox=None, all_screens=False: original
    image = capture.grab(size=32)
    assert image.mode == "RGB"


@pytest.mark.parametrize("size", [0, -10, 1])
def test_grab_clamps_small_size(size):
    image = capture.grab(size=size)
    assert image.size == (capture.MIN_SIZE, capture.MIN_SIZE)


def test_grab_clamps_large_size():
    image = capture.grab(size=10_000)
    assert image.size == (capture.MAX_SIZE, capture.MAX_SIZE)


def test_grab_rejects_non_integer_size():
    with pytest.raises(ValueError):
        capture.grab(size="big")


def test_grab_failure_returns_blank_image():
    capture.ImageGrab = FakeImageGrab(fail=OSError("没有桌面"))
    image = capture.grab(size=48)
    assert image.size == (48, 48)
    assert image.getpixel((0, 0)) == capture.FALLBACK_COLOR
    assert "截屏失败" in (capture.last_error() or "")


def test_grab_none_result_returns_blank_image():
    capture.ImageGrab = FakeImageGrab()
    capture.ImageGrab.grab = lambda bbox=None, all_screens=False: None
    image = capture.grab(size=40)
    assert image.getpixel((0, 0)) == capture.FALLBACK_COLOR
    assert "空结果" in (capture.last_error() or "")


def test_grab_resize_failure_returns_blank_image():
    capture.ImageGrab = FakeImageGrab()
    capture.ImageGrab.grab = lambda bbox=None, all_screens=False: object()
    image = capture.grab(size=40)
    assert image.size == (40, 40)
    assert "缩放截图失败" in (capture.last_error() or "")


# --------------------------------------------------------------------------- #
# grab_cached 节流
# --------------------------------------------------------------------------- #
def test_grab_cached_hits_same_slot():
    first = capture.grab_cached(size=32, fps=5.0, now=100.00)
    second = capture.grab_cached(size=32, fps=5.0, now=100.10)
    assert first is second
    assert len(_grab().calls) == 1


def test_grab_cached_misses_next_slot():
    first = capture.grab_cached(size=32, fps=5.0, now=100.00)
    second = capture.grab_cached(size=32, fps=5.0, now=100.25)
    assert first is not second
    assert len(_grab().calls) == 2


def test_grab_cached_separates_regions():
    capture.grab_cached((0.0, 0.0, 0.5, 0.5), size=32, fps=5.0, now=1.0)
    capture.grab_cached((0.5, 0.5, 0.5, 0.5), size=32, fps=5.0, now=1.0)
    assert len(_grab().calls) == 2
    assert _grab().calls[0]["bbox"] != _grab().calls[1]["bbox"]


def test_grab_cached_separates_size_and_monitor():
    capture.grab_cached(size=32, fps=5.0, now=1.0)
    capture.grab_cached(size=48, fps=5.0, now=1.0)
    capture.grab_cached(size=32, monitor=1, fps=5.0, now=1.0)
    assert len(_grab().calls) == 3


def test_grab_cached_lru_eviction_keeps_four():
    for index in range(6):
        capture.grab_cached((0.0, 0.0, 0.1 * (index + 1), 1.0), size=32, fps=5.0, now=float(index))
    assert len(capture._cache) == capture.CACHE_SIZE


def test_grab_cached_lru_refreshes_recent_entry():
    capture.grab_cached((0.0, 0.0, 0.5, 1.0), size=32, fps=5.0, now=0.0)
    capture.grab_cached((0.5, 0.0, 0.5, 1.0), size=32, fps=5.0, now=0.0)
    capture.grab_cached((0.0, 0.0, 0.5, 1.0), size=32, fps=5.0, now=0.0)  # 命中，移到最新
    capture.grab_cached((0.0, 0.0, 0.25, 1.0), size=32, fps=5.0, now=0.0)
    capture.grab_cached((0.0, 0.0, 0.75, 1.0), size=32, fps=5.0, now=0.0)
    keys = list(capture._cache)
    assert (0, 0.0, 0.0, 0.5, 1.0, 0, 32) in keys
    assert len(keys) == capture.CACHE_SIZE


def test_grab_cached_zero_fps_always_grabs():
    capture.grab_cached(size=32, fps=0.0, now=1.0)
    capture.grab_cached(size=32, fps=0.0, now=1.0)
    assert len(_grab().calls) == 2
    assert not capture._cache


def test_grab_cached_negative_fps_always_grabs():
    capture.grab_cached(size=32, fps=-3.0, now=1.0)
    capture.grab_cached(size=32, fps=-3.0, now=1.0)
    assert len(_grab().calls) == 2


def test_grab_cached_uses_injected_clock(monkeypatch):
    monkeypatch.setattr(capture, "time_now", lambda: 12.34)
    capture.grab_cached(size=32, fps=5.0)
    assert list(capture._cache)[0][0] == int(12.34 * 5.0)


def test_grab_cached_clamps_region_before_caching():
    capture.grab_cached((-1.0, -1.0, 2.0, 2.0), size=32, fps=5.0, now=1.0)
    capture.grab_cached((0.0, 0.0, 1.0, 1.0), size=32, fps=5.0, now=1.0)
    assert len(_grab().calls) == 1


def test_grab_cached_returns_blank_on_failure():
    capture.ImageGrab = FakeImageGrab(fail=OSError("失败"))
    image = capture.grab_cached(size=40, fps=5.0, now=1.0)
    assert image.getpixel((0, 0)) == capture.FALLBACK_COLOR


# --------------------------------------------------------------------------- #
# available / 缓存清理
# --------------------------------------------------------------------------- #
def test_available_true_with_imagegrab(monkeypatch):
    monkeypatch.setattr(capture.sys, "platform", "win32")
    assert capture.available() is True


def test_available_false_without_grab(monkeypatch):
    class NoGrab:
        pass

    monkeypatch.setattr(capture, "ImageGrab", NoGrab())
    assert capture.available() is False
    assert "缺少 grab" in (capture.last_error() or "")


def test_available_false_when_import_fails(monkeypatch):
    def boom():
        raise ImportError("没有 ImageGrab")

    monkeypatch.setattr(capture, "ImageGrab", None)
    monkeypatch.setattr(capture, "_imagegrab", boom)
    assert capture.available() is False
    assert "加载 ImageGrab 失败" in (capture.last_error() or "")


def test_available_requires_display_on_linux(monkeypatch):
    monkeypatch.setattr(capture.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert capture.available() is False
    monkeypatch.setenv("DISPLAY", ":0")
    assert capture.available() is True


def test_reset_cache_clears_entries():
    capture.grab_cached(size=32, fps=5.0, now=1.0)
    assert capture._cache
    capture.reset_cache()
    assert not capture._cache


def test_center_square_keeps_smaller_side():
    image = Image.new("RGB", (800, 300), (1, 2, 3))
    square = capture._center_square(image)
    assert square.size == (300, 300)


def test_fit_square_no_resize_when_already_square_of_size():
    image = Image.new("RGB", (64, 64), (9, 9, 9))
    assert capture._fit_square(image, 64).size == (64, 64)


def test_last_error_starts_none(monkeypatch):
    monkeypatch.setattr(capture, "_LAST_ERROR", None)
    assert capture.last_error() is None
