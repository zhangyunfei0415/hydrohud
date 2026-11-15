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
