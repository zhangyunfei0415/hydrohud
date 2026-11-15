"""Tests for :mod:`hydrohud.plugins` (plugin discovery + draw dispatch; no GUI)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from hydrohud import plugins

SIMPLE_PLUGIN = """
from PIL import ImageDraw


def draw(image, layer, ctx):
    ImageDraw.Draw(image).rectangle((0, 0, 10, 10), fill=layer.get("color", "#FF0000"))
    return True


PLUGIN = {
    "name": "stub",
    "label": "桩插件",
    "description": "测试用",
    "defaults": {"color": "#00FF00", "w": 0.5},
    "draw": draw,
}
"""


def _write(directory: Path, name: str, text: str) -> Path:
    """Write ``directory/name`` and return the path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_discover_registers_plugin_and_returns_count(tmp_path: Path) -> None:
    _write(tmp_path, "stub_plugin.py", SIMPLE_PLUGIN)
    registry = plugins.PluginRegistry(tmp_path)

    assert registry.discover() == 1
    spec = registry.get("stub")
    assert spec is not None
    assert spec.label == "桩插件"
    assert spec.description == "测试用"
    assert spec.defaults == {"color": "#00FF00", "w": 0.5}
    assert callable(spec.draw)
    assert spec.source.endswith("stub_plugin.py")
    assert registry.errors == []


def test_discover_without_directory_returns_zero(tmp_path: Path) -> None:
    registry = plugins.PluginRegistry(tmp_path / "missing")
    assert registry.discover() == 0
    assert registry.specs == {}
    assert registry.errors == []


def test_register_form_accepts_multiple_plugins(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "multi.py",
        """
PLUGIN = None


def register(reg):
    reg.add("first", lambda image, layer, ctx: True, label="一")
    reg.register("second", lambda image, layer, ctx: False, label="二", defaults={"n": 1})
""",
    )
    registry = plugins.PluginRegistry(tmp_path)

    assert registry.discover() == 2
    assert sorted(registry.specs) == ["first", "second"]
    assert registry.get("second").label == "二"
    assert registry.get("second").defaults == {"n": 1}


def test_broken_import_is_recorded_and_other_plugins_still_load(tmp_path: Path) -> None:
    _write(tmp_path, "good.py", SIMPLE_PLUGIN.replace('"stub"', '"good"'))
    _write(tmp_path, "broken_syntax.py", "def draw(:\n")
    _write(tmp_path, "boom.py", "raise RuntimeError('爆炸')\n")
    registry = plugins.PluginRegistry(tmp_path)

    assert registry.discover() == 1
    assert registry.get("good") is not None
    joined = "\n".join(registry.errors)
    assert "broken_syntax.py" in joined
    assert "导入失败" in joined
    assert "boom.py" in joined
    assert "RuntimeError" in joined


def test_plugin_without_plugin_or_register_is_recorded(tmp_path: Path) -> None:
    _write(tmp_path, "empty.py", "X = 1\n")
    registry = plugins.PluginRegistry(tmp_path)

    assert registry.discover() == 0
    assert any("缺少 PLUGIN" in error for error in registry.errors)


def test_duplicate_names_keep_first_and_record_error(tmp_path: Path) -> None:
    _write(tmp_path, "a_first.py", SIMPLE_PLUGIN)
    _write(tmp_path, "b_second.py", SIMPLE_PLUGIN.replace("桩插件", "第二个"))
    registry = plugins.PluginRegistry(tmp_path)

    assert registry.discover() == 1
    spec = registry.get("stub")
    assert spec.label == "桩插件"
    assert any("重名" in error for error in registry.errors)


def test_invalid_name_and_defaults_are_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "bad_name.py", SIMPLE_PLUGIN.replace('"stub"', '"has space"'))
    bad_defaults = SIMPLE_PLUGIN.replace(
        '    "defaults": {"color": "#00FF00", "w": 0.5},\n', '    "defaults": 3,\n'
    )
    _write(tmp_path, "bad_defaults.py", bad_defaults)
    registry = plugins.PluginRegistry(tmp_path)

    assert registry.discover() == 0
    joined = "\n".join(registry.errors)
    assert "插件名非法" in joined
    assert "defaults 必须是字典" in joined


def test_draw_must_be_callable(tmp_path: Path) -> None:
    _write(tmp_path, "bad_draw.py", SIMPLE_PLUGIN.replace('"draw": draw,', '"draw": 3,'))
    registry = plugins.PluginRegistry(tmp_path)

    assert registry.discover() == 0
    assert any("必须是函数" in error for error in registry.errors)


def test_specs_is_a_copy_and_get_missing_returns_none(tmp_path: Path) -> None:
    _write(tmp_path, "stub_plugin.py", SIMPLE_PLUGIN)
    registry = plugins.PluginRegistry(tmp_path)
    registry.discover()

    snapshot = registry.specs
    snapshot.clear()
    assert "stub" in registry.specs
    assert registry.get("nope") is None


def test_default_layer_uses_defaults_and_copies_them(tmp_path: Path) -> None:
    _write(tmp_path, "stub_plugin.py", SIMPLE_PLUGIN)
    registry = plugins.PluginRegistry(tmp_path)
    registry.discover()

    layer = registry.default_layer("stub")
    assert layer["type"] == "stub"
    assert layer["color"] == "#00FF00"
    layer["color"] = "#123456"
    assert registry.get("stub").defaults["color"] == "#00FF00"

    assert registry.default_layer()["type"] == "stub"
    assert registry.default_layer("unknown") is None
    assert plugins.PluginRegistry(tmp_path / "missing").default_layer() is None


def test_discover_clears_previous_specs(tmp_path: Path) -> None:
    path = _write(tmp_path, "stub_plugin.py", SIMPLE_PLUGIN)
    registry = plugins.PluginRegistry(tmp_path)
    assert registry.discover() == 1

    path.unlink()
    assert registry.discover() == 0
    assert registry.specs == {}


def test_reload_picks_up_edited_source(tmp_path: Path) -> None:
    path = _write(tmp_path, "stub_plugin.py", SIMPLE_PLUGIN)
    registry = plugins.PluginRegistry(tmp_path)
    registry.discover()

    # 同样的文件长度与秒级 mtime 下也必须读到新源码（不能命中 __pycache__）。
    path.write_text(SIMPLE_PLUGIN.replace('"#FF0000"', '"#0000FF"'), encoding="utf-8")
    assert registry.reload() == 1

    image = Image.new("RGB", (480, 480))
    assert registry.draw_layer(image, {"type": "stub"}, {}) is True
    assert image.getpixel((5, 5)) == (0, 0, 255)


def test_draw_layer_dispatches_only_to_registered_plugin(tmp_path: Path) -> None:
    _write(tmp_path, "stub_plugin.py", SIMPLE_PLUGIN)
    registry = plugins.PluginRegistry(tmp_path)
    registry.discover()

    image = Image.new("RGB", (480, 480), "#000000")
    assert registry.draw_layer(image, {"type": "stub", "color": "#00FF00"}, {}) is True
    assert image.getpixel((5, 5)) == (0, 255, 0)

    assert registry.draw_layer(image, {"type": "unknown"}, {}) is False
    assert registry.draw_layer(image, "not-a-layer", {}) is False
    assert registry.draw_layer(image, {}, {}) is False


def test_draw_layer_gives_plugin_the_context(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "ctx.py",
        """
def draw(image, layer, ctx):
    value = max(0, min(255, int(ctx["t"] * 100)))
    metrics = 1 if isinstance(ctx["metrics"], dict) else 0
    image.putpixel((0, 0), (value, metrics, 0 if "history" in ctx and "scene" in ctx else 9))
    return True


PLUGIN = {"name": "ctx", "draw": draw}
""",
    )
    registry = plugins.PluginRegistry(tmp_path)
    registry.discover()
    image = Image.new("RGB", (480, 480), "#000000")

    ctx = {"t": 1.5, "metrics": {"cpu": 1.0}, "history": {}, "scene": {}}
    assert registry.draw_layer(image, {"type": "ctx"}, ctx) is True
    assert image.getpixel((0, 0)) == (150, 1, 0)


def test_draw_layer_swallows_plugin_exceptions(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "explode.py",
        """
def draw(image, layer, ctx):
    raise RuntimeError("插件崩了")


PLUGIN = {"name": "explode", "draw": draw}
""",
    )
    _write(
        tmp_path,
        "none.py",
        'PLUGIN = {"name": "none", "draw": lambda image, layer, ctx: None}\n',
    )
    registry = plugins.PluginRegistry(tmp_path)
    registry.discover()
    image = Image.new("RGB", (480, 480))

    assert registry.draw_layer(image, {"type": "explode"}, {}) is False
    assert registry.draw_layer(image, {"type": "none"}, {}) is False


def test_module_level_api_discovers_default_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_dir = tmp_path / "HydroHUD" / "plugins"
    _write(plugin_dir, "stub_plugin.py", SIMPLE_PLUGIN)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(plugins, "_REGISTRY", None)

    assert plugins.plugin_dir() == plugin_dir
    try:
        assert plugins.discover() == 1
        assert plugins.registry().get("stub") is not None
        image = Image.new("RGB", (480, 480), "#000000")
        assert plugins.draw_layer(image, {"type": "stub", "color": "#00FF00"}, {}) is True
        assert image.getpixel((5, 5)) == (0, 255, 0)
    finally:
        monkeypatch.setattr(plugins, "_REGISTRY", None)


def test_example_sparkline_plugin_is_loadable(tmp_path: Path) -> None:
    example = Path(__file__).resolve().parents[1] / "examples" / "plugins" / "sparkline.py"
    assert example.is_file()
    _write(tmp_path, "sparkline.py", example.read_text(encoding="utf-8"))

    registry = plugins.PluginRegistry(tmp_path)
    assert registry.discover() == 1
    image = Image.new("RGB", (480, 480), "#000000")
    assert registry.draw_layer(image, registry.default_layer("sparkline"), {}) is True
    assert image.getpixel((240, 300)) != (0, 0, 0)


def test_plugin_that_returns_false_does_not_draw(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "skip.py",
        'PLUGIN = {"name": "skip", "draw": lambda image, layer, ctx: False}\n',
    )
    registry = plugins.PluginRegistry(tmp_path)
    registry.discover()
    image = Image.new("RGB", (480, 480), "#000000")

    assert registry.draw_layer(image, {"type": "skip"}, {}) is False


PARAM_PLUGIN = """
PLUGIN = {
    "name": "paramd",
    "params": [
        {"key": "thickness", "label": "线宽", "type": "int", "default": 3, "min": 1, "max": 12},
        {"key": "color", "label": "颜色", "type": "color", "default": "#38BDF8"},
        {"key": "shape", "type": "choice", "default": "sine",
         "options": [["sine", "正弦"], ["square", "方波"]]},
    ],
    "permissions": ["network"],
}
"""


def test_normalize_params_accepts_every_declared_type() -> None:
    specs = plugins.normalize_params(
        [
            {"key": "a", "type": "int", "default": 3, "min": 1, "max": 12},
            {"key": "b", "type": "float", "default": 0.25},
            {"key": "c", "type": "bool", "default": True},
            {"key": "d", "type": "text", "default": "hi"},
            {"key": "e", "type": "color", "default": "#abcdef"},
            {"key": "f", "type": "choice", "options": [["x", "X"], ["y", "Y"]]},
        ]
    )

    assert [spec.type for spec in specs] == list(plugins.PARAM_TYPES[:5]) + ["choice"]
    assert specs[0].default == 3
    assert specs[0].min == 1.0 and specs[0].max == 12.0
    assert specs[3].key == "d" and specs[3].default == "hi"
    assert specs[4].default == "#ABCDEF"
    assert specs[5].default == "x"


def test_normalize_params_drops_invalid_entries_and_duplicate_keys() -> None:
    specs = plugins.normalize_params(
        [
            {"key": "keep", "type": "int", "default": 2},
            {"key": "keep", "type": "int", "default": 9},
            {"type": "int"},
            {"key": "", "type": "int"},
            {"key": "bad type", "type": "int"},
            {"key": "nope", "type": "vector"},
            {"key": "badcolor", "type": "color", "default": "red"},
            {"key": "badchoice", "type": "choice", "options": [["only", "唯一"]]},
            {"key": "notadict"},
            "junk",
        ]
    )

    assert [spec.key for spec in specs] == ["keep"]
    assert specs[0].default == 2
    assert plugins.normalize_params(None) == ()
    assert plugins.normalize_params("junk") == ()


def test_normalize_params_clamps_defaults_into_declared_bounds() -> None:
    high, low = plugins.normalize_params(
        [
            {"key": "high", "type": "int", "default": 500, "min": 0, "max": 100},
            {"key": "low", "type": "float", "default": -3.0, "min": 0.0, "max": 1.0},
        ]
    )

    assert high.default == 100
    assert low.default == 0.0


def test_normalize_params_rejects_inverted_bounds() -> None:
    assert plugins.normalize_params([{"key": "x", "type": "int", "min": 10, "max": 1}]) == ()


def test_params_defaults_and_apply_params_fill_missing_keys() -> None:
    specs = plugins.normalize_params(
        [
            {"key": "thickness", "type": "int", "default": 3, "min": 1, "max": 12},
            {"key": "shape", "type": "choice", "options": [["sine", "正弦"], ["square", "方波"]]},
        ]
    )
    spec = plugins.PluginSpec(name="p", label="p", params=specs)
    layer = {"type": "p", "x": 0.1, "thickness": 99.6}

    applied = plugins.apply_params(spec, layer)

    assert applied["thickness"] == 12
    assert applied["shape"] == "sine"
    assert applied["x"] == 0.1
    assert layer["thickness"] == 99.6
    assert plugins.params_defaults(specs) == {"thickness": 3, "shape": "sine"}
    assert plugins.apply_params(spec, None)["thickness"] == 3


def test_coerce_param_falls_back_to_default_for_bad_values() -> None:
    specs = plugins.normalize_params(
        [
            {"key": "count", "type": "int", "default": 5, "min": 0, "max": 10},
            {"key": "ratio", "type": "float", "default": 0.5},
            {"key": "color", "type": "color", "default": "#112233"},
            {"key": "shape", "type": "choice", "options": [["a", "A"], ["b", "B"]]},
            {"key": "flag", "type": "bool", "default": True},
        ]
    )

    assert plugins.coerce_param(specs[0], "7.6") == 8
    assert plugins.coerce_param(specs[0], "abc") == 5
    assert plugins.coerce_param(specs[1], float("nan")) == 0.5
    assert plugins.coerce_param(specs[1], "0.75") == 0.75
    assert plugins.coerce_param(specs[2], "not-a-color") == "#112233"
    assert plugins.coerce_param(specs[3], "zzz") == "a"
    assert plugins.coerce_param(specs[4], "off") is False
    assert plugins.coerce_param(specs[4], "YES") is True


def test_plugin_dict_params_are_registered_and_merged_into_defaults(tmp_path: Path) -> None:
    _write(tmp_path, "paramd.py", PARAM_PLUGIN)
    registry = plugins.PluginRegistry(tmp_path)

    assert registry.discover() == 1
    spec = registry.get("paramd")
    assert spec is not None
    assert [param.key for param in spec.params] == ["thickness", "color", "shape"]
    assert spec.permissions == ("network",)
    assert spec.defaults["thickness"] == 3
    assert registry.params_for("paramd") == spec.params
    assert registry.params_for("missing") == ()
    assert registry.default_layer("paramd")["color"] == "#38BDF8"


def test_register_form_accepts_params_and_permissions(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "regform.py",
        "def register(reg):\n"
        '    reg.add("withparams", label="带参数",\n'
        '            params=[{"key": "speed", "type": "int", "default": 4}],\n'
        '            permissions="filesystem")\n',
    )
    registry = plugins.PluginRegistry(tmp_path)

    assert registry.discover() == 1
    spec = registry.get("withparams")
    assert spec is not None
    assert spec.label == "带参数"
    assert [param.key for param in spec.params] == ["speed"]
    assert spec.permissions == ("filesystem",)
    assert spec.defaults == {"speed": 4}


def test_failing_plugin_is_quarantined_after_limit(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "boom.py",
        'def boom(image, layer, ctx):\n    raise RuntimeError("boom")\n\n\n'
        'PLUGIN = {"name": "boom", "draw": boom}\n',
    )
    registry = plugins.PluginRegistry(tmp_path)
    registry.discover()
    image = Image.new("RGB", (480, 480), "#000000")
    layer = {"type": "boom"}
    limit = plugins.MAX_CONSECUTIVE_ERRORS
    calls: list[int] = []

    def counting(image, layer, ctx):
        calls.append(1)
        raise RuntimeError("boom")

    registry._specs["boom"] = plugins.PluginSpec(name="boom", label="boom", draw=counting)
    for _ in range(limit):
        assert registry.draw_layer(image, layer, {}) is False

    assert registry.is_quarantined("boom") is True
    assert len(calls) == limit
    assert registry.failures["boom"] == limit
    assert any("连续绘制失败" in message for message in registry.errors)
    assert registry.draw_layer(image, layer, {}) is False
    assert len(calls) == limit


def test_successful_draw_resets_failure_counter(tmp_path: Path) -> None:
    state = {"fail": True}

    def flaky(image, layer, ctx):
        if state["fail"]:
            raise RuntimeError("boom")
        return True

    registry = plugins.PluginRegistry(tmp_path)
    registry._specs["flaky"] = plugins.PluginSpec(name="flaky", label="flaky", draw=flaky)
    image = Image.new("RGB", (480, 480), "#000000")

    assert registry.draw_layer(image, {"type": "flaky"}, {}) is False
    assert registry.failures["flaky"] == 1
    state["fail"] = False
    assert registry.draw_layer(image, {"type": "flaky"}, {}) is True
    assert registry.failures["flaky"] == 0
    assert registry.is_quarantined("flaky") is False


def test_slow_draw_is_recorded_without_interrupting(tmp_path: Path) -> None:
    def slow(image, layer, ctx):
        deadline = plugins.time.perf_counter() + plugins.DRAW_TIME_LIMIT + 0.02
        while plugins.time.perf_counter() < deadline:
            pass
        return True

    registry = plugins.PluginRegistry(tmp_path)
    registry._specs["slow"] = plugins.PluginSpec(name="slow", label="slow", draw=slow)
    image = Image.new("RGB", (480, 480), "#000000")

    assert registry.draw_layer(image, {"type": "slow"}, {}) is True
    assert registry.slow["slow"] > plugins.DRAW_TIME_LIMIT
    assert registry.is_quarantined("slow") is False


def test_discover_clears_quarantine_state(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "boom.py",
        'def boom(image, layer, ctx):\n    raise RuntimeError("boom")\n\n\n'
        'PLUGIN = {"name": "boom", "draw": boom}\n',
    )
    registry = plugins.PluginRegistry(tmp_path)
    registry.discover()
    image = Image.new("RGB", (480, 480), "#000000")
    for _ in range(plugins.MAX_CONSECUTIVE_ERRORS):
        registry.draw_layer(image, {"type": "boom"}, {})
    assert registry.quarantined

    registry.discover()

    assert registry.quarantined == set()
    assert registry.failures == {}
    assert registry.errors == []


def test_draw_layer_rejects_non_mapping_layer(tmp_path: Path) -> None:
    registry = plugins.PluginRegistry(tmp_path)
    registry._specs["ok"] = plugins.PluginSpec(
        name="ok", label="ok", draw=lambda image, layer, ctx: True
    )
    image = Image.new("RGB", (480, 480), "#000000")

    assert registry.draw_layer(image, None, {}) is False
    assert registry.draw_layer(image, {"type": ""}, {}) is False
    assert registry.draw_layer(image, {"type": "unknown"}, {}) is False


def test_permissions_normalization_dedupes_and_lowercases() -> None:
    assert plugins._normalize_permissions(["Network", "network", " filesystem "]) == (
        "network",
        "filesystem",
    )
    assert plugins._normalize_permissions("network") == ("network",)
    assert plugins._normalize_permissions(None) == ()
    assert plugins._normalize_permissions(42) == ()
