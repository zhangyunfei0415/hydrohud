"""``hydrohud.presetslib`` 的纯软件测试。

约定：**绝不在测试里联网**。所有 HTTP 访问都 monkeypatch 掉
``urllib.request.urlopen``（返回 :class:`io.BytesIO` 里包着的 JSON），
``APPDATA`` 一律指向 ``tmp_path``；安装产物再用 ``scenes.load_project`` 读回验证。
"""

from __future__ import annotations

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
    assert set(cleaned) == {"name", "scene", "author", "description", "tags", "version"}


def test_validate_entry_fills_optional_defaults():
    cleaned = presetslib.validate_entry({"name": "x", "scene": {}})
    assert cleaned["author"] == ""
    assert cleaned["description"] == ""
    assert cleaned["tags"] == []
    assert cleaned["version"] == ""


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
