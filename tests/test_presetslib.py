"""``hydrohud.presetslib`` 的纯软件测试。

约定：**绝不在测试里联网**。所有 HTTP 访问都 monkeypatch 掉
``urllib.request.urlopen``（返回 :class:`io.BytesIO` 里包着的 JSON），
``APPDATA`` 一律指向 ``tmp_path``；安装产物再用 ``scenes.load_project`` 读回验证。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import urllib.error

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hydrohud import presetslib, scenes  # noqa: E402

#: 一份最小但合法的场景（render 能直接画）。
SCENE = {
    "name": "测试场景",
    "background": {"type": "solid", "color": "#101820"},
    "layers": [
        {
            "type": "text",
            "text": "hello",
            "x": 0.5,
            "y": 0.5,
            "size": 32,
            "color": "#FFFFFF",
        }
    ],
}

#: 两条合法预设。
ENTRIES = [
    {
        "name": "极简时钟",
        "author": "someone",
        "description": "只有一个大时钟",
        "tags": ["clock", "minimal", "clock"],
        "version": "1.0",
        "scene": SCENE,
        "unknown_field": "会被丢掉",
    },
    {"name": "性能曲线", "scene": SCENE},
]

INDEX_PAYLOAD = {"version": 1, "presets": ENTRIES}


def _json_bytes(payload: object) -> io.BytesIO:
    """把对象包成 urlopen 返回值的样子（支持 with / read）。"""
    return io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


class FakeResponse:
    """``urlopen`` 返回值的替身：支持 ``with`` 与 ``read()``。"""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw
        self.closed = False

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        self.closed = True
        return False


class UrlSpy:
    """替换 ``urlopen``：记录 URL，按队列返回响应或抛异常。"""

    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, float | None]] = []

    def __call__(self, url: str, timeout: float | None = None, **kwargs: object) -> FakeResponse:
        self.calls.append((str(url), timeout))
        if not self.results:
            raise AssertionError(f"没有预设响应了，却请求了 {url}")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, bytes):
            return FakeResponse(result)
        return FakeResponse(json.dumps(result, ensure_ascii=False).encode("utf-8"))


@pytest.fixture(autouse=True)
def appdata(tmp_path, monkeypatch):
    """把 ``APPDATA`` 指到临时目录，每个用例都从"无缓存"开始。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    presetslib._LAST_ERROR = None
    yield tmp_path
    presetslib._LAST_ERROR = None


@pytest.fixture()
def spy(monkeypatch):
    """替换 ``urllib.request.urlopen`` 并返回录制器。"""
    recorder = UrlSpy()
    monkeypatch.setattr(presetslib.urllib.request, "urlopen", recorder)
    return recorder


# --------------------------------------------------------------------------- #
# 常量与路径
# --------------------------------------------------------------------------- #
def test_constants_match_contract():
    assert presetslib.DEFAULT_INDEX_URL.startswith("https://")
    assert presetslib.DEFAULT_INDEX_URL.endswith("/index.json")
    assert presetslib.CACHE_FILE_NAME == "presets-index.json"
    assert presetslib.TIMEOUT_SECONDS == 8.0


def test_cache_path_is_under_appdata(appdata):
    path = presetslib.cache_path()
    assert path == appdata / "HydroHUD" / "presets-index.json"
    assert not path.parent.exists()  # 只算路径，不创建目录


def test_presets_dir_is_under_appdata(appdata):
    assert presetslib.presets_dir() == appdata / "HydroHUD" / "presets"


# --------------------------------------------------------------------------- #
# validate_entry
# --------------------------------------------------------------------------- #
def test_validate_entry_accepts_full_entry():
    cleaned = presetslib.validate_entry(ENTRIES[0])
    assert cleaned["name"] == "极简时钟"
    assert cleaned["author"] == "someone"
    assert cleaned["description"] == "只有一个大时钟"
    assert cleaned["tags"] == ["clock", "minimal"]
    assert cleaned["version"] == "1.0"
    assert cleaned["scene"] is ENTRIES[0]["scene"]
    assert "unknown_field" not in cleaned
    assert set(cleaned) == {
        "name",
        "scene",
        "author",
        "description",
        "tags",
        "version",
        "thumbnail",
        "rating",
        "downloads",
    }
    assert cleaned["thumbnail"] is None
    assert cleaned["rating"] is None
    assert cleaned["downloads"] is None


def test_validate_entry_fills_optional_defaults():
    cleaned = presetslib.validate_entry({"name": "x", "scene": {}})
    assert cleaned["author"] == ""
    assert cleaned["description"] == ""
    assert cleaned["tags"] == []
    assert cleaned["version"] == ""
    assert cleaned["thumbnail"] is None
    assert cleaned["rating"] is None
    assert cleaned["downloads"] is None


def test_validate_entry_trims_name():
    assert presetslib.validate_entry({"name": "  带空格  ", "scene": {}})["name"] == "带空格"


@pytest.mark.parametrize("raw", [None, [], "text", 42])
def test_validate_entry_rejects_non_dict(raw):
    with pytest.raises(presetslib.PresetError):
        presetslib.validate_entry(raw)


@pytest.mark.parametrize("entry", [{"scene": {}}, {"name": "", "scene": {}}, {"name": "   "}])
def test_validate_entry_rejects_missing_name(entry):
    with pytest.raises(presetslib.PresetError):
        presetslib.validate_entry(entry)


def test_validate_entry_rejects_too_long_name():
    with pytest.raises(presetslib.PresetError):
        presetslib.validate_entry({"name": "x" * 81, "scene": {}})


@pytest.mark.parametrize(
    "entry", [{"name": "n"}, {"name": "n", "scene": []}, {"name": "n", "scene": "x"}]
)
def test_validate_entry_rejects_bad_scene(entry):
    with pytest.raises(presetslib.PresetError):
        presetslib.validate_entry(entry)


def test_validate_entry_coerces_optional_types():
    cleaned = presetslib.validate_entry(
        {"name": "n", "scene": {}, "author": 42, "tags": "not-a-list", "version": None}
    )
    assert cleaned["author"] == "42"
    assert cleaned["tags"] == []
    assert cleaned["version"] == ""


def test_validate_entry_dedupes_and_strips_tags():
    cleaned = presetslib.validate_entry(
        {"name": "n", "scene": {}, "tags": [" a ", "a", "", None, "b"]}
    )
    assert cleaned["tags"] == ["a", "b"]


# --------------------------------------------------------------------------- #
# slugify
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("极简时钟", "极简时钟"),
        ("Minimal Clock", "Minimal-Clock"),
        ("a/b\\c", "a-b-c"),
        ("a:b*c?d", "a-b-c-d"),
        ("  spaced  name  ", "spaced-name"),
        ("a___b", "a-b"),
        ("a - _ b", "a-b"),
    ],
)
def test_slugify_is_safe(name, slug):
    assert presetslib.slugify(name) == slug


@pytest.mark.parametrize("bad", ["/", "\\", "..", "...", "   ", "", "-", "_", "///"])
def test_slugify_never_empty_or_traversing(bad):
    slug = presetslib.slugify(bad)
    assert slug
    assert "/" not in slug
    assert "\\" not in slug
    assert ".." not in slug


def test_slugify_handles_reserved_device_names():
    assert presetslib.slugify("con") == "con_preset"
    assert presetslib.slugify("NUL") == "NUL_preset"
    assert presetslib.slugify("com1") == "com1_preset"


def test_slugify_truncates_long_names():
    slug = presetslib.slugify("长" * 200)
    assert len(slug) <= presetslib.MAX_SLUG_LENGTH


# --------------------------------------------------------------------------- #
# fetch_index
# --------------------------------------------------------------------------- #
def test_fetch_index_returns_entries(spy):
    spy.results = [INDEX_PAYLOAD]
    entries = presetslib.fetch_index()
    assert [entry["name"] for entry in entries] == ["极简时钟", "性能曲线"]
    assert entries[0]["tags"] == ["clock", "minimal"]
    assert spy.calls[0][0] == presetslib.DEFAULT_INDEX_URL
    assert spy.calls[0][1] == presetslib.TIMEOUT_SECONDS


def test_fetch_index_writes_cache(spy):
    spy.results = [INDEX_PAYLOAD]
    presetslib.fetch_index()
    path = presetslib.cache_path()
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert [entry["name"] for entry in payload["presets"]] == ["极简时钟", "性能曲线"]
    assert payload["fetched"] > 0


def test_fetch_index_accepts_bare_list(spy):
    spy.results = [ENTRIES]
    entries = presetslib.fetch_index()
    assert len(entries) == 2


def test_fetch_index_custom_url_and_timeout(spy):
    spy.results = [INDEX_PAYLOAD]
    presetslib.fetch_index("https://example.invalid/x.json", timeout=1.5)
    assert spy.calls[0] == ("https://example.invalid/x.json", 1.5)


def test_fetch_index_skips_bad_entries(spy):
    spy.results = [{"presets": [ENTRIES[0], {"name": ""}, "junk", ENTRIES[1]]}]
    entries = presetslib.fetch_index()
    assert [entry["name"] for entry in entries] == ["极简时钟", "性能曲线"]


def test_fetch_index_network_error_raises(spy):
    spy.results = [urllib.error.URLError("offline")]
    with pytest.raises(presetslib.PresetError):
        presetslib.fetch_index()
    assert "下载预设索引失败" in (presetslib.last_error() or "")


def test_fetch_index_network_error_keeps_old_cache(spy):
    spy.results = [INDEX_PAYLOAD]
    presetslib.fetch_index()
    before = presetslib.cache_path().read_bytes()
    spy.results = [urllib.error.URLError("offline")]
    with pytest.raises(presetslib.PresetError):
        presetslib.fetch_index()
    assert presetslib.cache_path().read_bytes() == before
    assert len(presetslib.cached_index()) == 2


@pytest.mark.parametrize("payload", [b"not json", b"", b"[1,2"])
def test_fetch_index_bad_json_raises(spy, payload):
    spy.results = [payload]
    with pytest.raises(presetslib.PresetError):
        presetslib.fetch_index()


@pytest.mark.parametrize("payload", [{"version": 1}, {"presets": "x"}, 42, "text"])
def test_fetch_index_bad_shape_raises(spy, payload):
    spy.results = [payload]
    with pytest.raises(presetslib.PresetError):
        presetslib.fetch_index()


# --------------------------------------------------------------------------- #
# cached_index
# --------------------------------------------------------------------------- #
def test_cached_index_without_cache_returns_empty():
    assert presetslib.cached_index() == []
    assert presetslib.last_error() is None


def test_cached_index_reads_cache(spy):
    spy.results = [INDEX_PAYLOAD]
    presetslib.fetch_index()
    entries = presetslib.cached_index()
    assert [entry["name"] for entry in entries] == ["极简时钟", "性能曲线"]


@pytest.mark.parametrize("text", ["{", "[]x", '"text"', "null"])
def test_cached_index_tolerates_corrupt_cache(text):
    path = presetslib.cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    assert presetslib.cached_index() == []
    assert presetslib.last_error()


def test_cached_index_tolerates_bad_entries(spy):
    path = presetslib.cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"presets": [{}, ENTRIES[0]]}), encoding="utf-8")
    assert [entry["name"] for entry in presetslib.cached_index()] == ["极简时钟"]


# --------------------------------------------------------------------------- #
# index_or_cache
# --------------------------------------------------------------------------- #
def test_index_or_cache_prefers_network(spy):
    spy.results = [INDEX_PAYLOAD]
    entries, from_network = presetslib.index_or_cache()
    assert from_network is True
    assert len(entries) == 2
    assert len(spy.calls) == 1


def test_index_or_cache_falls_back_to_cache(spy):
    spy.results = [INDEX_PAYLOAD]
    presetslib.fetch_index()
    spy.results = [urllib.error.URLError("offline")]
    entries, from_network = presetslib.index_or_cache()
    assert from_network is False
    assert [entry["name"] for entry in entries] == ["极简时钟", "性能曲线"]
    assert "下载预设索引失败" in (presetslib.last_error() or "")


def test_index_or_cache_returns_empty_when_both_fail(spy):
    spy.results = [urllib.error.URLError("offline")]
    assert presetslib.index_or_cache() == ([], False)
    assert presetslib.last_error()


def test_index_or_cache_passes_timeout(spy):
    spy.results = [INDEX_PAYLOAD]
    presetslib.index_or_cache(timeout=3.0)
    assert spy.calls[0][1] == 3.0


# --------------------------------------------------------------------------- #
# install_preset
# --------------------------------------------------------------------------- #
def test_install_preset_writes_loadable_project(tmp_path):
    target = presetslib.install_preset(ENTRIES[0], tmp_path / "out")
    assert target.exists()
    assert target.name == "极简时钟.hud.json"
    assert target.parent == tmp_path / "out"
    loaded = scenes.load_project(str(target))
    assert loaded["layers"]
    assert loaded["background"]["type"] == "solid"


def test_install_preset_creates_default_dir():
    target = presetslib.install_preset(ENTRIES[1])
    assert target.parent == presetslib.presets_dir()
    assert target.parent.is_dir()
    assert scenes.load_project(str(target))["layers"]


def test_install_preset_dedupes_names(tmp_path):
    first = presetslib.install_preset(ENTRIES[0], tmp_path)
    second = presetslib.install_preset(ENTRIES[0], tmp_path)
    third = presetslib.install_preset(ENTRIES[0], tmp_path)
    assert [first.name, second.name, third.name] == [
        "极简时钟.hud.json",
        "极简时钟-2.hud.json",
        "极简时钟-3.hud.json",
    ]
    assert len(list(tmp_path.glob("*.hud.json"))) == 3


def test_install_preset_reuses_existing_file_from_disk(tmp_path):
    (tmp_path / "极简时钟.hud.json").write_text("{}", encoding="utf-8")
    target = presetslib.install_preset(ENTRIES[0], tmp_path)
    assert target.name == "极简时钟-2.hud.json"


def test_install_preset_normalizes_scene(tmp_path):
    entry = {"name": "脏场景", "scene": {"layers": [{"type": "nope"}], "background": {"type": "?"}}}
    target = presetslib.install_preset(entry, tmp_path)
    loaded = scenes.load_project(str(target))
    assert loaded["background"]["type"] == "solid"
    assert loaded["layers"] == []


@pytest.mark.parametrize("entry", [None, {}, {"name": "n"}, {"scene": {}}])
def test_install_preset_rejects_invalid_entry(entry, tmp_path):
    with pytest.raises(presetslib.PresetError):
        presetslib.install_preset(entry, tmp_path)


def test_install_preset_wraps_save_failure(tmp_path, monkeypatch):
    def boom(scene, path):
        raise OSError("磁盘满了")

    monkeypatch.setattr(presetslib.scenes, "save_project", boom)
    with pytest.raises(presetslib.PresetError):
        presetslib.install_preset(ENTRIES[0], tmp_path)


def test_install_preset_reports_unwritable_dir(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(presetslib.PresetError):
        presetslib.install_preset(ENTRIES[0], blocker / "sub")


# --------------------------------------------------------------------------- #
# clear_cache / last_error
# --------------------------------------------------------------------------- #
def test_clear_cache_removes_file(spy):
    spy.results = [INDEX_PAYLOAD]
    presetslib.fetch_index()
    assert presetslib.clear_cache() is True
    assert not presetslib.cache_path().exists()
    assert presetslib.cached_index() == []


def test_clear_cache_without_file_returns_false():
    assert presetslib.clear_cache() is False


def test_last_error_starts_none():
    assert presetslib.last_error() is None


def test_fetch_index_clears_previous_error(spy):
    spy.results = [urllib.error.URLError("offline")]
    with pytest.raises(presetslib.PresetError):
        presetslib.fetch_index()
    assert presetslib.last_error()
    spy.results = [INDEX_PAYLOAD]
    presetslib.fetch_index()
    assert presetslib.last_error() is None


# --------------------------------------------------------------------------- #
# validate_entry：thumbnail / rating / downloads
# --------------------------------------------------------------------------- #
def test_entry_keys_include_display_fields():
    for field in ("thumbnail", "rating", "downloads"):
        assert field in presetslib.ENTRY_KEYS


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a.png",
        "http://example.com/a.jpg",
        "https://cdn.example.com/x/y/z.webp",
    ],
)
def test_validate_entry_accepts_http_thumbnail(url):
    cleaned = presetslib.validate_entry({"name": "n", "scene": {}, "thumbnail": url})
    assert cleaned["thumbnail"] == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/explorer.exe",
        "javascript:alert(1)",
        "//example.com/a.png",
        "assets/a.png",
        "./a.png",
        "ftp://example.com/a.png",
        "data:image/png;base64,AAAA",
        "https://" + "a" * 500,
        "",
        None,
        42,
    ],
)
def test_validate_entry_rejects_bad_thumbnail(url):
    cleaned = presetslib.validate_entry({"name": "n", "scene": {}, "thumbnail": url})
    assert cleaned["thumbnail"] is None


def test_validate_entry_keeps_thumbnail_at_length_limit():
    prefix = "https://example.com/"
    url = prefix + "a" * (presetslib.THUMBNAIL_MAX_URL_LENGTH - len(prefix))
    assert len(url) == presetslib.THUMBNAIL_MAX_URL_LENGTH
    cleaned = presetslib.validate_entry({"name": "n", "scene": {}, "thumbnail": url})
    assert cleaned["thumbnail"] == url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(4.6, 4.6), (0, 0.0), (5, 5.0), (4.567, 4.6), (9.9, 5.0), (-3, 0.0)],
)
def test_validate_entry_clamps_rating(raw, expected):
    assert (
        presetslib.validate_entry({"name": "n", "scene": {}, "rating": raw})["rating"] == expected
    )


@pytest.mark.parametrize("raw", ["4.6", None, [], {}, True, False, "5", object()])
def test_validate_entry_drops_non_numeric_rating(raw):
    assert presetslib.validate_entry({"name": "n", "scene": {}, "rating": raw})["rating"] is None


@pytest.mark.parametrize(("raw", "expected"), [(0, 0), (7, 7), (12000, 12000), (-5, 0)])
def test_validate_entry_cleans_downloads(raw, expected):
    cleaned = presetslib.validate_entry({"name": "n", "scene": {}, "downloads": raw})
    assert cleaned["downloads"] == expected


@pytest.mark.parametrize("raw", ["12", 1.5, None, [], True, float("nan")])
def test_validate_entry_drops_non_integer_downloads(raw):
    assert (
        presetslib.validate_entry({"name": "n", "scene": {}, "downloads": raw})["downloads"] is None
    )


def test_validate_entry_mixed_display_fields():
    cleaned = presetslib.validate_entry(
        {
            "name": "极光",
            "scene": {},
            "thumbnail": "https://example.com/aurora.png",
            "rating": 4.64,
            "downloads": 12345,
        }
    )
    assert cleaned["thumbnail"] == "https://example.com/aurora.png"
    assert cleaned["rating"] == 4.6
    assert cleaned["downloads"] == 12345


def test_old_index_without_display_fields_parses_to_none(spy):
    spy.results = [INDEX_PAYLOAD]
    entries = presetslib.fetch_index()
    assert all(entry["thumbnail"] is None for entry in entries)
    assert all(entry["rating"] is None for entry in entries)
    assert all(entry["downloads"] is None for entry in entries)


def test_old_index_install_preset_unchanged(tmp_path):
    old_entry = {"name": "老索引预设", "scene": SCENE}
    target = presetslib.install_preset(old_entry, tmp_path)
    assert target.name == "老索引预设.hud.json"
    assert scenes.load_project(str(target))["layers"]


def test_cached_index_roundtrips_display_fields(spy):
    spy.results = [
        {
            "version": 1,
            "presets": [
                {
                    "name": "极光",
                    "scene": SCENE,
                    "thumbnail": "https://example.com/aurora.png",
                    "rating": 4.6,
                    "downloads": 12000,
                }
            ],
        }
    ]
    presetslib.fetch_index()
    entry = presetslib.cached_index()[0]
    assert entry["thumbnail"] == "https://example.com/aurora.png"
    assert entry["rating"] == 4.6
    assert entry["downloads"] == 12000


# --------------------------------------------------------------------------- #
# 缩略图缓存
# --------------------------------------------------------------------------- #
THUMB_URL = "https://example.com/thumb/aurora.png"
THUMB_BYTES = b"\x89PNG\r\n\x1a\nfake-png-payload"


def _thumb_entry(url: str = THUMB_URL) -> dict:
    return {"name": "极光", "scene": {}, "thumbnail": url}


class BlobResponse:
    """``urlopen`` 返回值的替身：按请求的字节数返回内容并记录调用。"""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw
        self.closed = False
        self.read_sizes: list[int | None] = []

    def read(self, size: int | None = None) -> bytes:
        self.read_sizes.append(size)
        if size is None:
            return self._raw
        return self._raw[:size]

    def __enter__(self) -> BlobResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        self.closed = True
        return False


class ThumbSpy:
    """替换 ``urlopen``：记录请求并按队列返回响应或抛异常。"""

    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.requests: list[object] = []

    def __call__(self, request: object, timeout: float | None = None, **kwargs: object):
        self.requests.append(request)
        if not self.results:
            raise AssertionError(f"没有预设响应了，却请求了 {request}")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, bytes):
            return BlobResponse(result)
        return result


@pytest.fixture()
def thumb_spy(monkeypatch):
    """替换 ``urllib.request.urlopen`` 供缩略图下载使用。"""
    recorder = ThumbSpy()
    monkeypatch.setattr(presetslib.urllib.request, "urlopen", recorder)
    return recorder


def test_thumbnail_constants_match_contract():
    assert presetslib.THUMBNAIL_DIR_NAME == "thumbnails"
    assert presetslib.THUMBNAIL_MAX_BYTES == 1024 * 1024
    assert presetslib.THUMBNAIL_TIMEOUT_SECONDS == 10.0
    assert presetslib.THUMBNAIL_SUFFIXES == (".png", ".jpg", ".jpeg", ".webp")


def test_thumbnails_dir_is_under_appdata(appdata):
    path = presetslib.thumbnails_dir()
    assert path == appdata / "HydroHUD" / "thumbnails"
    assert not path.exists()


def test_thumbnail_path_without_cache_returns_none_and_creates_nothing(appdata):
    assert presetslib.thumbnail_path(_thumb_entry()) is None
    assert not presetslib.thumbnails_dir().exists()


def test_thumbnail_path_without_thumbnail_field(appdata):
    assert presetslib.thumbnail_path({"name": "n", "scene": {}}) is None
    assert presetslib.thumbnail_path({"name": "n", "thumbnail": "file:///x.png"}) is None


def test_thumbnail_path_uses_sha256_prefix_and_url_suffix(appdata):
    presetslib.thumbnails_dir().mkdir(parents=True)
    expected_name = hashlib.sha256(THUMB_URL.encode()).hexdigest()[:16] + ".png"
    (presetslib.thumbnails_dir() / expected_name).write_bytes(THUMB_BYTES)
    assert presetslib.thumbnail_path(_thumb_entry()) == (
        presetslib.thumbnails_dir() / expected_name
    )


@pytest.mark.parametrize(
    ("url", "suffix"),
    [
        ("https://example.com/a.png", ".png"),
        ("https://example.com/a.JPG", ".jpg"),
        ("https://example.com/a.jpeg", ".jpeg"),
        ("https://example.com/a.webp", ".webp"),
        ("https://example.com/a.gif", ".img"),
        ("https://example.com/a", ".img"),
        ("https://example.com/a.png?x=1&y=2", ".png"),
    ],
)
def test_thumbnail_suffix_comes_from_url_only(appdata, url, suffix):
    presetslib.thumbnails_dir().mkdir(parents=True)
    name = hashlib.sha256(url.encode()).hexdigest()[:16] + suffix
    (presetslib.thumbnails_dir() / name).write_bytes(THUMB_BYTES)
    assert presetslib.thumbnail_path({"name": "n", "thumbnail": url}) is not None


def test_download_thumbnail_success(thumb_spy):
    thumb_spy.results = [THUMB_BYTES]
    path = presetslib.download_thumbnail(_thumb_entry())
    assert path is not None
    assert path.exists()
    assert path.read_bytes() == THUMB_BYTES
    assert path.parent == presetslib.thumbnails_dir()
    assert path.suffix == ".png"
    assert presetslib.last_error() is None
    assert len(thumb_spy.requests) == 1
    assert not list(path.parent.glob(".thumb-*"))  # 临时文件已清掉


def test_download_thumbnail_sends_user_agent_and_timeout(thumb_spy):
    thumb_spy.results = [THUMB_BYTES]
    presetslib.download_thumbnail(_thumb_entry(), timeout=2.5)
    request = thumb_spy.requests[0]
    assert request.get_header("User-agent") == "HydroHUD-thumbnail"
    assert request.full_url == THUMB_URL


def test_download_thumbnail_uses_cache_on_second_call(thumb_spy):
    thumb_spy.results = [THUMB_BYTES]
    first = presetslib.download_thumbnail(_thumb_entry())
    second = presetslib.download_thumbnail(_thumb_entry())
    assert first == second
    assert len(thumb_spy.requests) == 1  # 第二次没联网


def test_download_thumbnail_rejects_oversized_without_residue(thumb_spy):
    oversized = b"x" * (presetslib.THUMBNAIL_MAX_BYTES + 1)
    thumb_spy.results = [BlobResponse(oversized)]
    assert presetslib.download_thumbnail(_thumb_entry()) is None
    assert presetslib.last_error() is not None
    directory = presetslib.thumbnails_dir()
    assert directory.is_dir()
    assert list(directory.iterdir()) == []  # 超限不落盘，也不留 .part


def test_download_thumbnail_accepts_exact_limit(thumb_spy):
    payload = b"y" * presetslib.THUMBNAIL_MAX_BYTES
    thumb_spy.results = [payload]
    path = presetslib.download_thumbnail(_thumb_entry())
    assert path is not None
    assert path.stat().st_size == presetslib.THUMBNAIL_MAX_BYTES


def test_download_thumbnail_network_error_returns_none(thumb_spy):
    thumb_spy.results = [urllib.error.URLError("offline")]
    assert presetslib.download_thumbnail(_thumb_entry()) is None
    assert "下载缩略图失败" in (presetslib.last_error() or "")


def test_download_thumbnail_without_url_returns_none(thumb_spy):
    assert presetslib.download_thumbnail({"name": "n", "thumbnail": "javascript:alert(1)"}) is None
    assert presetslib.download_thumbnail({"name": "n"}) is None
    assert thumb_spy.requests == []


def test_download_thumbnail_records_write_failure(thumb_spy, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("磁盘满了")

    monkeypatch.setattr(presetslib.tempfile, "mkstemp", boom)
    thumb_spy.results = [THUMB_BYTES]
    assert presetslib.download_thumbnail(_thumb_entry()) is None
    assert presetslib.last_error()


def test_clear_thumbnails_counts_and_is_idempotent(thumb_spy):
    thumb_spy.results = [THUMB_BYTES, THUMB_BYTES]
    presetslib.download_thumbnail(_thumb_entry("https://example.com/a.png"))
    presetslib.download_thumbnail(_thumb_entry("https://example.com/b.jpg"))
    assert presetslib.clear_thumbnails() == 2
    assert presetslib.clear_thumbnails() == 0
    assert presetslib.thumbnails_dir().is_dir()


def test_clear_thumbnails_without_dir_returns_zero(appdata):
    assert presetslib.clear_thumbnails() == 0
    assert not presetslib.thumbnails_dir().exists()


# --------------------------------------------------------------------------- #
# sort_entries
# --------------------------------------------------------------------------- #
def test_sort_keys_contract():
    assert presetslib.SORT_KEYS == ("rating", "downloads", "name")


def test_sort_entries_by_rating_desc_with_missing_last():
    entries = [
        {"name": "低分", "rating": 2.0},
        {"name": "无评分"},
        {"name": "高分", "rating": 4.9},
        {"name": "中分", "rating": 3.5},
    ]
    ordered = presetslib.sort_entries(entries, "rating")
    assert [entry["name"] for entry in ordered] == ["高分", "中分", "低分", "无评分"]


def test_sort_entries_by_downloads_desc_with_missing_last():
    entries = [
        {"name": "少", "downloads": 10},
        {"name": "无下载数"},
        {"name": "多", "downloads": 90000},
        {"name": "中", "downloads": 500},
    ]
    ordered = presetslib.sort_entries(entries, "downloads")
    assert [entry["name"] for entry in ordered] == ["多", "中", "少", "无下载数"]


def test_sort_entries_by_name_ascending_casefold():
    entries = [{"name": "beta"}, {"name": "Alpha"}, {"name": "极光"}, {"name": "gamma"}]
    ordered = presetslib.sort_entries(entries, "name")
    assert [entry["name"] for entry in ordered] == ["Alpha", "beta", "gamma", "极光"]


def test_sort_entries_is_stable_for_ties():
    entries = [
        {"name": "甲", "rating": 4.5},
        {"name": "乙", "rating": 4.5},
        {"name": "丙", "rating": 5.0},
        {"name": "丁", "rating": 4.5},
    ]
    ordered = presetslib.sort_entries(entries, "rating")
    assert [entry["name"] for entry in ordered] == ["丙", "甲", "乙", "丁"]


def test_sort_entries_default_key_is_rating():
    entries = [{"name": "a", "rating": 1.0}, {"name": "b", "rating": 5.0}]
    assert [entry["name"] for entry in presetslib.sort_entries(entries)] == ["b", "a"]


def test_sort_entries_does_not_mutate_input():
    entries = [{"name": "a", "rating": 1.0}, {"name": "b", "rating": 5.0}]
    ordered = presetslib.sort_entries(entries, "rating")
    assert entries[0]["name"] == "a"
    assert ordered is not entries


def test_sort_entries_handles_empty_and_single():
    assert presetslib.sort_entries([], "rating") == []
    assert [entry["name"] for entry in presetslib.sort_entries([{"name": "only"}], "name")] == [
        "only"
    ]


def test_sort_entries_unknown_key_raises():
    with pytest.raises(ValueError):
        presetslib.sort_entries([], "author")


# --------------------------------------------------------------------------- #
# describe_entry
# --------------------------------------------------------------------------- #
def test_describe_entry_full():
    assert (
        presetslib.describe_entry({"name": "极光", "rating": 4.6, "downloads": 12000})
        == "极光 · 4.6 分 · 1.2 万下载"
    )


def test_describe_entry_name_only():
    assert presetslib.describe_entry({"name": "极光"}) == "极光"


def test_describe_entry_rating_only():
    assert presetslib.describe_entry({"name": "极光", "rating": 4.6}) == "极光 · 4.6 分"


def test_describe_entry_downloads_only():
    assert presetslib.describe_entry({"name": "极光", "downloads": 987}) == "极光 · 987下载"


def test_describe_entry_unnamed():
    assert presetslib.describe_entry({}) == "未命名预设"


@pytest.mark.parametrize(
    ("downloads", "text"),
    [
        (0, "极光 · 0下载"),
        (9999, "极光 · 9999下载"),
        (10000, "极光 · 1 万下载"),
        (12345, "极光 · 1.2 万下载"),
        (100000000, "极光 · 1 亿下载"),
        (123456789, "极光 · 1.2 亿下载"),
    ],
)
def test_describe_entry_count_formatting(downloads, text):
    assert presetslib.describe_entry({"name": "极光", "downloads": downloads}) == text


def test_describe_entry_integer_rating_has_one_decimal():
    assert presetslib.describe_entry({"name": "极光", "rating": 5}) == "极光 · 5 分"
