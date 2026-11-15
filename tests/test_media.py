"""``hydrohud.media`` 的纯软件测试。

约定：不真的调用 Windows SMTC、不需要安装 ``winsdk``。测试通过
``media._sdk_loader`` 注入 :class:`FakeSdk`，它把 ``request_async`` /
``try_get_media_properties_async`` / ``try_get_thumbnail_async`` 都实现成直接返回
结果的协程，因此后台线程里跑的 ``asyncio.run`` 也不会碰系统接口。
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time

import pytest
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hydrohud import media  # noqa: E402


#: 内存里现做一张 300×200 的 PNG，用作封面。
def _png_bytes(size=(300, 200), color=(200, 40, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeStream:
    """``IInputStream`` 替身：只需要 ``size``。"""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.size = len(payload)
        self.opened = 0


class FakeReference:
    """``IRandomAccessStreamReference`` 替身。"""

    def __init__(self, payload: bytes | None) -> None:
        self.payload = payload
        self.opened = 0

    async def open_read_async(self) -> FakeStream:
        self.opened += 1
        return FakeStream(self.payload or b"")


class FakeReader:
    """``DataReader`` 替身：记录 ``load_async`` 的调用并返回原始字节。"""

    instances: list["FakeReader"] = []

    def __init__(self, stream: FakeStream) -> None:
        self.stream = stream
        self.loaded = 0
        self.reads = 0
        FakeReader.instances.append(self)

    async def load_async(self, size: int) -> None:
        self.loaded = size

    def read_buffer(self, size: int) -> bytes:
        self.reads += 1
        return self.stream.payload[:size]


class FakeProperties:
    """``GlobalSystemMediaTransportControlsSessionMediaProperties`` 替身。"""

    def __init__(
        self,
        title: str = "夜空中最亮的星",
        artist: str = "逃跑计划",
        album: str = "世界",
    ) -> None:
        self.title = title
        self.artist = artist
        self.album_title = album


class FakePlayback:
    """``GlobalSystemMediaTransportControlsSessionPlaybackInfo`` 替身。"""

    def __init__(self, status: int = media.STATUS_PLAYING) -> None:
        self.playback_status = status


class FakeSession:
    """当前 SMTC 会话替身。"""

    def __init__(
        self,
        properties: FakeProperties | None = None,
        playback: FakePlayback | None = None,
        thumbnail: FakeReference | None = None,
        source: str = "Spotify.exe",
    ) -> None:
        self.properties = properties
        self.playback = playback or FakePlayback()
        self.thumbnail = thumbnail
        self.source_app_user_model_id = source
        self.properties_calls = 0
        self.thumbnail_calls = 0

    async def try_get_media_properties_async(self) -> FakeProperties | None:
        self.properties_calls += 1
        return self.properties

    async def try_get_thumbnail_async(self) -> FakeReference | None:
        self.thumbnail_calls += 1
        return self.thumbnail

    def get_playback_info(self) -> FakePlayback:
        return self.playback


class FakeManager:
    """``GlobalSystemMediaTransportControlsSessionManager`` 替身。"""

    current: FakeSession | None = None
    requests = 0
    fail_request: BaseException | None = None

    def __init__(self) -> None:
        self.session_calls = 0

    @staticmethod
    async def request_async() -> "FakeManager":
        FakeManager.requests += 1
        if FakeManager.fail_request is not None:
            raise FakeManager.fail_request
        return FakeManager()

    def get_current_session(self) -> FakeSession | None:
        self.session_calls += 1
        return FakeManager.current


class FakeSdk:
    """把用到的 winsdk 成员收成命名空间的替身。"""

    def __init__(self) -> None:
        manager_class = type(
            "Manager", (), {"request_async": staticmethod(FakeManager.request_async)}
        )
        self.control = type(
            "control", (), {"GlobalSystemMediaTransportControlsSessionManager": manager_class}
        )
        self.streams = type("streams", (), {"DataReader": FakeReader})


@pytest.fixture(autouse=True)
def _reset_media(monkeypatch):
    """每个用例前重置 SDK 缓存、封面缓存与 Fake 状态。"""
    media._LAST_ERROR = None
    FakeManager.current = None
    FakeManager.requests = 0
    FakeManager.fail_request = None
    FakeReader.instances.clear()
    monkeypatch.setattr(media, "_sdk_loader", lambda: FakeSdk())
    media._reset_sdk()
    yield
    media._reset_sdk()
    media._LAST_ERROR = None


@pytest.fixture()
def sdk():
    """注入 FakeSdk 并返回它。"""
    fake = FakeSdk()
    media._reset_sdk(fake)
    return fake


@pytest.fixture()
def session():
    """一个默认的播放中会话（带封面）。"""
    made = FakeSession(
        properties=FakeProperties(),
        playback=FakePlayback(),
        thumbnail=FakeReference(_png_bytes()),
    )
    FakeManager.current = made
    return made


def _wait(predicate, timeout: float = 2.0) -> bool:
    """轮询等待条件成立（后台线程是异步的，不 sleep 会拿到旧快照）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------- #
# SDK 探测与降级
# --------------------------------------------------------------------------- #
def test_import_does_not_load_sdk():
    # 上一个用例会重置，这里独立确认导入期没有留下 SDK。
    media._reset_sdk()
    assert media._winsdk is None


def test_available_true_with_sdk(sdk):
    assert media.available() is True


def test_available_false_without_sdk(monkeypatch):
    monkeypatch.setattr(media, "_sdk_loader", lambda: None)
    media._reset_sdk()
    assert media.available() is False


def test_available_false_off_windows(monkeypatch, sdk):
    monkeypatch.setattr(media.sys, "platform", "linux")
    assert media.available() is False


def test_available_swallows_loader_error(monkeypatch):
    def boom():
        raise RuntimeError("加载失败")

    monkeypatch.setattr(media, "_sdk_loader", boom)
    media._reset_sdk()
    assert media.available() is False
    assert "加载 winsdk 失败" in (media.last_error() or "")


def test_snapshot_empty_without_sdk(monkeypatch):
    monkeypatch.setattr(media, "_sdk_loader", lambda: None)
    media._reset_sdk()
    hub = media.MediaHub()
    assert hub.snapshot() == {}
    assert hub.refresh() is False
    assert hub.snapshot() == {}


def test_start_noop_without_sdk(monkeypatch):
    monkeypatch.setattr(media, "_sdk_loader", lambda: None)
    media._reset_sdk()
    hub = media.MediaHub()
    hub.start()
    assert not hub.running()
    assert hub.snapshot() == {}
    hub.close()


# --------------------------------------------------------------------------- #
# 字段映射
# --------------------------------------------------------------------------- #
def test_refresh_maps_fields(sdk, session):
    hub = media.MediaHub()
    assert hub.refresh() is True
    data = hub.snapshot()
    assert data["title"] == "夜空中最亮的星"
    assert data["artist"] == "逃跑计划"
    assert data["album"] == "世界"
    assert data["status"] == "playing"
    assert data["source"] == "Spotify.exe"
    assert data["cover"] is not None
    assert isinstance(data["cover"], Image.Image)


def test_snapshot_is_copy(sdk, session):
    hub = media.MediaHub()
    hub.refresh()
    first = hub.snapshot()
    first["title"] = "改掉"
    assert hub.snapshot()["title"] == "夜空中最亮的星"


@pytest.mark.parametrize(
    ("status", "name"),
    [
        (media.STATUS_PLAYING, "playing"),
        (media.STATUS_PAUSED, "paused"),
        (media.STATUS_STOPPED, "stopped"),
        (media.STATUS_CLOSED, "closed"),
        (media.STATUS_OPENED, "opened"),
        (media.STATUS_CHANGING, "changing"),
    ],
)
def test_playback_status_mapping(sdk, session, status, name):
    session.playback = FakePlayback(status)
    hub = media.MediaHub()
    hub.refresh()
    assert hub.snapshot()["status"] == name


def test_unknown_playback_status(sdk, session):
    session.playback = FakePlayback(99)
    hub = media.MediaHub()
    hub.refresh()
    assert hub.snapshot()["status"] == "unknown"


def test_status_text_covers_all_names():
    for name in ("playing", "paused", "stopped", "closed", "opened", "changing", "unknown"):
        assert media.STATUS_TEXT[name]


def test_no_session_returns_empty(sdk):
    FakeManager.current = None
    hub = media.MediaHub()
    assert hub.refresh() is True
    assert hub.snapshot() == {}


def test_empty_properties_returns_empty(sdk, session):
    session.properties = FakeProperties(title="", artist="")
    hub = media.MediaHub()
    hub.refresh()
    assert hub.snapshot() == {}


def test_session_gone_after_being_present(sdk, session):
    hub = media.MediaHub()
    hub.refresh()
    assert hub.snapshot()
    FakeManager.current = None
    hub.refresh()
    assert hub.snapshot() == {}


# --------------------------------------------------------------------------- #
# 封面
# --------------------------------------------------------------------------- #
def test_cover_decoded_and_scaled(sdk, session):
    hub = media.MediaHub()
    hub.refresh()
    cover = hub.snapshot()["cover"]
    assert cover.size[0] <= media.COVER_MAX_SIZE
    assert cover.size[1] <= media.COVER_MAX_SIZE
    assert cover.mode == "RGB"
    # 300x200 → 缩到 96 宽、64 高（等比，不裁不拉伸）
    assert cover.size == (96, 64)


def test_cover_reused_for_same_title(sdk, session):
    hub = media.MediaHub()
    hub.refresh()
    hub.refresh()
    assert session.thumbnail_calls == 1
    assert session.properties_calls == 2


def test_cover_refetched_on_title_change(sdk, session):
    hub = media.MediaHub()
    hub.refresh()
    session.properties = FakeProperties(title="另一首歌")
    hub.refresh()
    assert session.thumbnail_calls == 2
    assert hub.snapshot()["title"] == "另一首歌"


def test_cover_cache_is_bounded(sdk):
    session = FakeSession()
    FakeManager.current = session
    hub = media.MediaHub()
    for index in range(media.COVER_CACHE_SIZE + 4):
        session.properties = FakeProperties(title=f"歌曲 {index}")
        hub.refresh()
    assert len(hub._cover_cache) <= media.COVER_CACHE_SIZE


def test_missing_thumbnail_leaves_cover_none(sdk, session):
    session.thumbnail = None
    hub = media.MediaHub()
    hub.refresh()
    assert hub.snapshot()["cover"] is None


def test_broken_thumbnail_bytes_do_not_raise(sdk, session):
    session.thumbnail = FakeReference(b"not an image")
    hub = media.MediaHub()
    hub.refresh()
    data = hub.snapshot()
    assert data["cover"] is None
    assert data["title"] == "夜空中最亮的星"
    assert "解码封面图片失败" in (media.last_error() or "")


def test_broken_reader_does_not_raise(sdk, session, monkeypatch):
    class BrokenReader:
        def __init__(self, stream):
            raise RuntimeError("reader 坏了")

    sdk.streams = type("streams", (), {"DataReader": BrokenReader})
    hub = media.MediaHub()
    hub.refresh()
    assert hub.snapshot()["cover"] is None
    assert "解码封面缓冲失败" in (media.last_error() or "")


def test_clear_cover_cache(sdk, session):
    hub = media.MediaHub()
    hub.refresh()
    assert hub._cover_cache
    hub.clear_cover_cache()
    assert not hub._cover_cache


def test_decode_cover_rejects_empty():
    assert media._decode_cover(b"") is None
    assert media._decode_cover(None) is None


# --------------------------------------------------------------------------- #
# 异常路径
# --------------------------------------------------------------------------- #
def test_request_failure_recorded(sdk):
    FakeManager.fail_request = RuntimeError("SMTC 挂了")
    hub = media.MediaHub()
    assert hub.refresh() is False
    assert "轮询 SMTC 失败" in (media.last_error() or "")
    assert hub.snapshot() == {}


def test_properties_failure_recorded(sdk, session):
    async def boom():
        raise RuntimeError("属性读取失败")

    session.try_get_media_properties_async = boom
    hub = media.MediaHub()
    assert hub.refresh() is True
    assert hub.snapshot() == {}
    assert "try_get_media_properties_async" in (media.last_error() or "")


def test_refresh_inside_event_loop_is_swallowed(sdk, session):
    import asyncio

    hub = media.MediaHub()

    async def scenario():
        return hub.refresh()

    assert asyncio.run(scenario()) is False
    assert "事件循环" in (media.last_error() or "")


# --------------------------------------------------------------------------- #
# 线程与单例
# --------------------------------------------------------------------------- #
def test_background_thread_updates_snapshot(sdk, session):
    hub = media.MediaHub(interval=0.05)
    hub.start()
    assert hub.running()
    assert _wait(lambda: hub.snapshot().get("title"))
    hub.close()
    assert not hub.running()
    assert hub.snapshot()["title"] == "夜空中最亮的星"


def test_close_is_idempotent(sdk, session):
    hub = media.MediaHub(interval=0.05)
    hub.start()
    hub.close()
    hub.close()
    assert not hub.running()


def test_close_without_start(sdk):
    hub = media.MediaHub()
    hub.close()
    assert not hub.running()


def test_start_is_idempotent(sdk, session):
    hub = media.MediaHub(interval=0.05)
    hub.start()
    first = hub._thread
    hub.start()
    assert hub._thread is first
    hub.close()


def test_thread_survives_poll_failure(sdk):
    FakeManager.fail_request = RuntimeError("临时失败")
    hub = media.MediaHub(interval=0.02)
    hub.start()
    assert _wait(lambda: "轮询 SMTC 失败" in (media.last_error() or ""))
    assert hub.running()
    hub.close()


def test_interval_is_floored(sdk):
    assert media.MediaHub(interval=0.0).interval == 0.1
    assert media.MediaHub(interval=-5).interval == 0.1
    assert media.MediaHub(interval=2.5).interval == 2.5


def test_describe_variants(sdk, session):
    hub = media.MediaHub()
    assert "无活动会话" in hub.describe()
    hub.refresh()
    text = hub.describe()
    assert "夜空中最亮的星" in text
    assert "播放中" in text


def test_describe_when_unavailable(monkeypatch):
    monkeypatch.setattr(media, "_sdk_loader", lambda: None)
    media._reset_sdk()
    assert "不可用" in media.MediaHub().describe()


def test_get_hub_singleton(monkeypatch, sdk):
    monkeypatch.setattr(media.MediaHub, "start", lambda self: None)
    first = media.get_hub()
    assert media.get_hub() is first
    media.close_hub()
    assert media.get_hub() is not first
    media.close_hub()


def test_no_thread_named_media_after_tests():
    assert not [t for t in threading.enumerate() if t.name == "hydrohud-media"]
