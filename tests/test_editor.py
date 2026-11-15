"""``hydrohud.editor`` 画布编辑几何的测试。

只测模块级纯函数（包围盒、命中、吸附、拖拽、缩放、微移），不创建 Tk 窗口：
``CanvasEditor`` 需要真实 Tk 主循环，属于 GUI 集成自检（``tools/gui_check.py``）
覆盖的范围。运行：``python -m pytest tests/test_editor.py -q``。
"""

from __future__ import annotations

import datetime as _dt
import os
import sys

import pytest
from PIL import Image

pytest.importorskip("tkinter")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hydrohud import editor  # noqa: E402
from hydrohud.scenes import normalize_scene  # noqa: E402

SCREEN_W = editor.SCREEN_W
SCREEN_H = editor.SCREEN_H
GRID = editor.GRID_STEP
TOLERANCE = editor.SNAP_TOLERANCE

FIXED_TIME = _dt.datetime(2026, 1, 2, 3, 4, 5)


def _layer(**kwargs) -> dict:
    scene = normalize_scene({"layers": [dict(kwargs)]})
    return scene["layers"][0]


def _scene(*layers: dict) -> dict:
    return normalize_scene({"layers": list(layers)})


def _png(path, size=(120, 60)) -> str:
    Image.new("RGB", size, (30, 60, 90)).save(path)
    return str(path)


# --------------------------------------------------------------------------- #
# 锚点与夹取
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("anchor", "expected"),
    [
        ("center", ("center", "center")),
        ("top-left", ("top", "left")),
        ("top-right", ("top", "right")),
        ("bottom-center", ("bottom", "center")),
        ("center-left", ("center", "left")),
    ],
)
def test_anchor_parts(anchor, expected) -> None:
    assert editor.anchor_parts(anchor) == expected


@pytest.mark.parametrize("anchor", [None, "", "不存在的锚点", 123])
def test_anchor_parts_falls_back_to_center(anchor) -> None:
    assert editor.anchor_parts(anchor) == ("center", "center")


@pytest.mark.parametrize(
    ("anchor", "expected"),
    [
        ("center", (0.5, 0.5)),
        ("top-left", (0.0, 0.0)),
        ("bottom-right", (1.0, 1.0)),
        ("center-right", (1.0, 0.5)),
    ],
)
def test_anchor_factors(anchor, expected) -> None:
    assert editor.anchor_factors(anchor) == pytest.approx(expected)


def test_clamp_bounds_value() -> None:
    assert editor.clamp(5.0, 0.0, 3.0) == pytest.approx(3.0)
    assert editor.clamp(-2.0, 0.0, 3.0) == pytest.approx(0.0)
    assert editor.clamp(1.5, 0.0, 3.0) == pytest.approx(1.5)


def test_clamp_norm_keeps_normalized_range() -> None:
    assert editor.clamp_norm(-0.4) == pytest.approx(0.0)
    assert editor.clamp_norm(0.4) == pytest.approx(0.4)
    assert editor.clamp_norm(9.0) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# 尺寸度量
# --------------------------------------------------------------------------- #
def test_measure_text_grows_with_size() -> None:
    small = editor.measure_text("HydroHUD", 20)
    large = editor.measure_text("HydroHUD", 60)
    assert small[0] > 0 and small[1] > 0
    assert large[0] > small[0]
    assert large[1] > small[1]


def test_measure_block_handles_empty_input() -> None:
    assert editor.measure_block([]) == (0.0, 0.0)


def test_measure_block_stacks_lines_vertically() -> None:
    font = editor.render.load_font(24)
    one = editor.measure_block([("一行", font)])
    two = editor.measure_block([("一行", font), ("第二行", font)])
    assert two[1] > one[1]


def test_text_lines_for_returns_none_for_non_text_layers() -> None:
    assert editor.text_lines_for(_layer(type="bars"), FIXED_TIME) is None
    assert editor.text_lines_for(_layer(type="gauge"), FIXED_TIME) is None


def test_text_lines_for_clock_uses_sample_time() -> None:
    layer = _layer(type="clock", format="%H:%M", show_date=True)
    lines = editor.text_lines_for(layer, FIXED_TIME)
    assert lines is not None
    assert lines[0][0] == "03:04"
    assert lines[1][0] == "2026-01-02"


def test_text_lines_for_text_layer_splits_newlines() -> None:
    lines = editor.text_lines_for(_layer(type="text", text="第一行\n第二行"))
    assert lines is not None
    assert [text for text, _font in lines] == ["第一行", "第二行"]


def test_image_layer_size_uses_scale_and_aspect(tmp_path) -> None:
    path = _png(tmp_path / "wide.png", (120, 60))
    layer = _layer(type="image", path=path, scale=0.5)
    width, height = editor.image_layer_size(layer)
    assert width == pytest.approx(0.5 * SCREEN_W, abs=1.0)
    assert height == pytest.approx(width / 2.0, abs=2.0)


def test_image_layer_size_falls_back_for_missing_file(tmp_path) -> None:
    layer = _layer(type="image", path=str(tmp_path / "nope.png"), scale=0.5)
    assert editor.image_layer_size(layer) == (150.0, 80.0)


# --------------------------------------------------------------------------- #
# 包围盒
# --------------------------------------------------------------------------- #
def test_layer_extent_covers_every_visible_kind(tmp_path) -> None:
    cases = [
        _layer(type="bars"),
        _layer(type="chart"),
        _layer(type="gauge"),
        _layer(type="image", path=_png(tmp_path / "a.png")),
        _layer(type="thermometer"),
        _layer(type="sequence", paths=[_png(tmp_path / "b.png")]),
        _layer(type="qrcode", data="HydroHUD"),
        _layer(type="text", text="文字"),
        _layer(type="clock", format="%H:%M"),
        _layer(type="sysinfo"),
        _layer(type="countdown"),
        _layer(type="ticker", text="滚动"),
        _layer(type="weather"),
        _layer(type="nowplaying", show_cover=False),
    ]
    for layer in cases:
        extent = editor.layer_extent(layer, FIXED_TIME)
        assert extent is not None, layer["type"]
        assert extent[0] > 0 and extent[1] > 0, layer["type"]


def test_layer_extent_returns_none_for_unknown_type() -> None:
    assert editor.layer_extent({"type": "不存在的图层"}) is None


def test_bars_bounds_treat_xy_as_top_left() -> None:
    layer = _layer(type="bars", x=0.1, y=0.2, w=0.5, h=0.1)
    bounds = editor.layer_bounds(layer)
    assert bounds is not None
    assert bounds[0] == pytest.approx(0.1 * SCREEN_W, abs=1.0)
    assert bounds[1] == pytest.approx(0.2 * SCREEN_H, abs=1.0)
    assert bounds[2] - bounds[0] == pytest.approx(0.5 * SCREEN_W, abs=1.0)


def test_centered_text_bounds_are_symmetric_around_xy() -> None:
    layer = _layer(type="text", text="居中文字", x=0.5, y=0.5, anchor="center")
    bounds = editor.layer_bounds(layer)
    assert bounds is not None
    center_x = (bounds[0] + bounds[2]) / 2.0
    center_y = (bounds[1] + bounds[3]) / 2.0
    assert center_x == pytest.approx(0.5 * SCREEN_W, abs=1.0)
    assert center_y == pytest.approx(0.5 * SCREEN_H, abs=1.0)


def test_top_left_anchor_puts_xy_at_bounds_corner() -> None:
    layer = _layer(type="text", text="左上", x=0.1, y=0.15, anchor="top-left")
    bounds = editor.layer_bounds(layer)
    assert bounds is not None
    assert bounds[0] == pytest.approx(0.1 * SCREEN_W, abs=1.0)
    assert bounds[1] == pytest.approx(0.15 * SCREEN_H, abs=1.0)


def test_gauge_bounds_are_square() -> None:
    layer = _layer(type="gauge", diameter=0.4, x=0.5, y=0.5)
    bounds = editor.layer_bounds(layer)
    assert bounds is not None
    assert (bounds[2] - bounds[0]) == pytest.approx(bounds[3] - bounds[1], abs=0.5)


def test_qrcode_bounds_are_square() -> None:
    layer = _layer(type="qrcode", data="HydroHUD", size=0.4)
    bounds = editor.layer_bounds(layer)
    assert bounds is not None
    assert (bounds[2] - bounds[0]) == pytest.approx(bounds[3] - bounds[1], abs=0.5)


def test_layer_bounds_returns_none_for_unknown_type() -> None:
    assert editor.layer_bounds({"type": "未知"}) is None


# --------------------------------------------------------------------------- #
# 命中测试
# --------------------------------------------------------------------------- #
def test_hit_test_prefers_topmost_layer() -> None:
    bottom = _layer(type="text", text="底层", x=0.5, y=0.5, size=80)
    top = _layer(type="text", text="顶层", x=0.5, y=0.5, size=80)
    assert editor.hit_test([bottom, top], 0.5 * SCREEN_W, 0.5 * SCREEN_H) == 1


def test_hit_test_returns_none_outside_any_layer() -> None:
    layer = _layer(type="text", text="角落", x=0.1, y=0.1, size=24)
    assert editor.hit_test([layer], 0.9 * SCREEN_W, 0.9 * SCREEN_H) is None


def test_hit_test_skips_undrawable_layers() -> None:
    layer = _layer(type="text", text="可见", x=0.5, y=0.5, size=80)
    assert editor.hit_test([{"type": "未知"}, layer], 0.5 * SCREEN_W, 0.5 * SCREEN_H) == 1


def test_selection_bounds_is_the_union() -> None:
    left = _layer(type="text", text="左", x=0.2, y=0.5, size=30)
    right = _layer(type="text", text="右", x=0.8, y=0.5, size=30)
    union = editor.selection_bounds([left, right], [0, 1])
    assert union is not None
    left_box = editor.layer_bounds(left)
    right_box = editor.layer_bounds(right)
    assert left_box is not None and right_box is not None
    assert union[0] == pytest.approx(left_box[0])
    assert union[2] == pytest.approx(right_box[2])


def test_selection_bounds_ignores_invalid_indices() -> None:
    layer = _layer(type="text", text="唯一", x=0.5, y=0.5)
    union = editor.selection_bounds([layer], [0, 5, -3])
    assert union is not None
    assert union == editor.layer_bounds(layer)


def test_selection_bounds_none_when_nothing_drawable() -> None:
    assert editor.selection_bounds([{"type": "未知"}], [0]) is None
    assert editor.selection_bounds([], []) is None


# --------------------------------------------------------------------------- #
# 吸附
# --------------------------------------------------------------------------- #
def test_snap_offset_snaps_within_tolerance() -> None:
    result = editor.snap_offset([100.0], [100.0 + TOLERANCE / 2.0, 300.0])
    assert result is not None
    assert result[0] == pytest.approx(TOLERANCE / 2.0)
    assert result[1] == pytest.approx(100.0 + TOLERANCE / 2.0)


def test_snap_offset_ignores_targets_outside_tolerance() -> None:
    assert editor.snap_offset([0.0], [TOLERANCE * 3.0]) is None


def test_snap_offset_picks_nearest_target() -> None:
    result = editor.snap_offset([100.0], [102.0, 103.0], tolerance=5.0)
    assert result is not None
    assert result[1] == pytest.approx(102.0)


def test_snap_targets_include_grid_center_and_edges() -> None:
    xs, ys = editor.snap_targets([])
    assert 0.0 in xs and pytest.approx(SCREEN_W / 2.0) in xs
    assert SCREEN_W in xs
    assert 0.0 in ys and SCREEN_H in ys
    assert pytest.approx(GRID) in xs


def test_snap_targets_include_other_layer_edges() -> None:
    layer = _layer(type="text", text="参照", x=0.5, y=0.5, size=40)
    bounds = editor.layer_bounds(layer)
    assert bounds is not None
    xs, _ys = editor.snap_targets([layer])
    assert pytest.approx(bounds[0]) in xs


def test_snap_targets_skip_excluded_layers() -> None:
    layer = _layer(type="text", text="被排除", x=0.13, y=0.13, size=40)
    bounds = editor.layer_bounds(layer)
    assert bounds is not None
    xs_all, _ = editor.snap_targets([layer])
    xs_none, _ = editor.snap_targets([layer], exclude=(0,))
    assert pytest.approx(bounds[0]) in xs_all
    assert pytest.approx(bounds[0]) not in xs_none


# --------------------------------------------------------------------------- #
# 手柄
# --------------------------------------------------------------------------- #
def test_handle_points_returns_eight_handles() -> None:
    handles = editor.handle_points((0.0, 0.0, 100.0, 50.0))
    assert set(handles) == {"nw", "n", "ne", "e", "se", "s", "sw", "w"}
    assert handles["nw"] == (0.0, 0.0)
    assert handles["se"] == (100.0, 50.0)
    assert handles["n"] == (50.0, 0.0)


def test_hit_handle_finds_corner_within_tolerance() -> None:
    bounds = (10.0, 20.0, 110.0, 120.0)
    assert editor.hit_handle(bounds, 10.0 + editor.HANDLE_SIZE, 20.0) == "nw"
    assert editor.hit_handle(bounds, 500.0, 500.0) is None


def test_can_resize_matches_resizable_kinds() -> None:
    for kind in ("bars", "chart", "gauge", "image", "qrcode", "sequence", "thermometer"):
        assert editor.can_resize({"type": kind}) is True, kind
    for kind in ("text", "clock", "sysinfo", "countdown", "ticker", "weather", "nowplaying"):
        assert editor.can_resize({"type": kind}) is False, kind


def test_can_resize_false_for_unknown_type() -> None:
    assert editor.can_resize({"type": "未知"}) is False


# --------------------------------------------------------------------------- #
# 拖拽
# --------------------------------------------------------------------------- #
def test_drag_selection_moves_layer_by_pixels() -> None:
    scene = _scene(_layer(type="text", text="拖动", x=0.5, y=0.5, size=40))
    moved, _gx, _gy = editor.drag_selection(scene, [0], 50.0, 25.0, snap=False)
    layer = moved["layers"][0]
    assert layer["x"] == pytest.approx(0.5 + 50.0 / SCREEN_W)
    assert layer["y"] == pytest.approx(0.5 + 25.0 / SCREEN_H)


def test_drag_selection_does_not_mutate_input_scene() -> None:
    scene = _scene(_layer(type="text", text="原样", x=0.5, y=0.5))
    editor.drag_selection(scene, [0], 40.0, 40.0, snap=False)
    assert scene["layers"][0]["x"] == pytest.approx(0.5)


def test_drag_selection_clamps_inside_screen() -> None:
    layer = _layer(type="text", text="边界", x=0.1, y=0.1, size=40)
    scene = _scene(layer)
    moved, _gx, _gy = editor.drag_selection(scene, [0], -5000.0, -5000.0, snap=False)
    bounds = editor.layer_bounds(moved["layers"][0])
    assert bounds is not None
    # 夹取按包围盒做：拖到极端位置时图层左/上边界贴在屏幕上，不会整体飞出屏幕
    assert bounds[0] >= -0.5
    assert bounds[1] >= -0.5
    assert bounds[0] < editor.layer_bounds(layer)[0]


def test_drag_selection_moves_whole_group() -> None:
    scene = _scene(
        _layer(type="text", text="甲", x=0.3, y=0.5, size=30),
        _layer(type="text", text="乙", x=0.7, y=0.5, size=30),
    )
    moved, _gx, _gy = editor.drag_selection(scene, [0, 1], 20.0, 0.0, snap=False)
    assert moved["layers"][0]["x"] == pytest.approx(0.3 + 20.0 / SCREEN_W)
    assert moved["layers"][1]["x"] == pytest.approx(0.7 + 20.0 / SCREEN_W)


def test_drag_selection_axis_lock_keeps_dominant_axis() -> None:
    scene = _scene(_layer(type="text", text="锁轴", x=0.5, y=0.5, size=30))
    moved, _gx, _gy = editor.drag_selection(scene, [0], 30.0, 4.0, snap=False, axis_lock=True)
    layer = moved["layers"][0]
    assert layer["x"] != pytest.approx(0.5)
    assert layer["y"] == pytest.approx(0.5)


def test_drag_selection_snaps_to_center_line() -> None:
    # 宽 0.2（96px）的图层放在 x=0.399：中心≈239.5，距屏幕中心线 240 只有 0.5px，
    # 左右边界（≈191 / 287）离网格线都很远，所以只可能吸附到中心线。
    layer = _layer(type="bars", x=0.399, y=0.1, w=0.2, h=0.1)
    scene = _scene(layer)
    bounds = editor.layer_bounds(layer)
    assert bounds is not None
    assert (bounds[0] + bounds[2]) / 2.0 == pytest.approx(editor.SCREEN_W / 2.0, abs=TOLERANCE)
    moved, guides_x, _guides_y = editor.drag_selection(scene, [0], 0.0, 0.0, snap=True)
    assert guides_x, "中心线附近拖拽时应当产生竖直吸附线"
    assert guides_x[0] == pytest.approx(editor.SCREEN_W / 2.0, abs=0.01)
    moved_bounds = editor.layer_bounds(moved["layers"][0])
    assert moved_bounds is not None
    assert (moved_bounds[0] + moved_bounds[2]) / 2.0 == pytest.approx(
        editor.SCREEN_W / 2.0, abs=0.51
    )


def test_drag_selection_returns_empty_guides_without_snap() -> None:
    scene = _scene(_layer(type="text", text="不吸附", x=0.5, y=0.5))
    _moved, guides_x, guides_y = editor.drag_selection(scene, [0], 3.0, 3.0, snap=False)
    assert guides_x == [] and guides_y == []


def test_drag_selection_with_no_valid_indices_is_a_noop() -> None:
    scene = _scene(_layer(type="text", text="空选", x=0.5, y=0.5))
    moved, guides_x, guides_y = editor.drag_selection(scene, [], 10.0, 10.0)
    assert moved["layers"][0]["x"] == pytest.approx(0.5)
    assert guides_x == [] and guides_y == []


def test_nudge_selection_uses_no_snapping() -> None:
    scene = _scene(_layer(type="text", text="微移", x=0.5, y=0.5))
    moved = editor.nudge_selection(scene, [0], 4.0, 0.0)
    assert moved["layers"][0]["x"] == pytest.approx(0.5 + 4.0 / SCREEN_W)


# --------------------------------------------------------------------------- #
# 缩放
# --------------------------------------------------------------------------- #
def test_resize_bars_sets_normalized_w_and_h() -> None:
    layer = _layer(type="bars", x=0.1, y=0.1, w=0.4, h=0.1)
    bounds = editor.layer_bounds(layer)
    assert bounds is not None
    resized = editor.resize_layer(layer, "se", bounds[2] + 20.0, bounds[3] + 10.0)
    assert resized["w"] > layer["w"]
    assert resized["h"] > layer["h"]


def test_resize_returns_a_copy_and_keeps_input_unchanged() -> None:
    layer = _layer(type="bars", x=0.1, y=0.1, w=0.4, h=0.1)
    bounds = editor.layer_bounds(layer)
    assert bounds is not None
    editor.resize_layer(layer, "se", bounds[2] + 40.0, bounds[3] + 20.0)
    assert layer["w"] == pytest.approx(0.4)


def test_resize_qrcode_keeps_square_and_updates_size() -> None:
    layer = _layer(type="qrcode", data="HydroHUD", size=0.3, x=0.5, y=0.5)
    bounds = editor.layer_bounds(layer)
    assert bounds is not None
    resized = editor.resize_layer(layer, "e", bounds[2] + 60.0, bounds[3])
    assert resized["size"] > layer["size"]
    assert 0.05 <= resized["size"] <= 1.0


def test_resize_gauge_uses_diameter() -> None:
    layer = _layer(type="gauge", diameter=0.3, x=0.5, y=0.5)
    bounds = editor.layer_bounds(layer)
    assert bounds is not None
    center_x = (bounds[0] + bounds[2]) / 2.0
    center_y = (bounds[1] + bounds[3]) / 2.0
    resized = editor.resize_layer(layer, "se", center_x + 150.0, center_y + 150.0)
    assert resized["diameter"] > layer["diameter"]
    assert "w" not in resized or resized.get("w") == layer.get("w")


def test_resize_image_uses_scale(tmp_path) -> None:
    layer = _layer(type="image", path=_png(tmp_path / "img.png"), scale=0.3, x=0.5, y=0.5)
    bounds = editor.layer_bounds(layer)
    assert bounds is not None
    resized = editor.resize_layer(layer, "e", bounds[2] + 80.0, bounds[3])
    assert resized["scale"] != pytest.approx(0.3)


def test_resize_thermometer_sets_w_and_h() -> None:
    layer = _layer(type="thermometer", w=0.14, h=0.62, x=0.4, y=0.1)
    bounds = editor.layer_bounds(layer)
    assert bounds is not None
    resized = editor.resize_layer(layer, "se", bounds[2] + 30.0, bounds[3] - 40.0)
    assert resized["w"] > layer["w"]
    assert resized["h"] < layer["h"]


def test_resize_non_resizable_layer_is_a_copy() -> None:
    layer = _layer(type="text", text="不可缩放", x=0.5, y=0.5)
    resized = editor.resize_layer(layer, "se", 400.0, 400.0)
    assert resized == layer
    assert resized is not layer


def test_resize_respects_minimum_size() -> None:
    layer = _layer(type="bars", x=0.1, y=0.1, w=0.4, h=0.2)
    bounds = editor.layer_bounds(layer)
    assert bounds is not None
    resized = editor.resize_layer(layer, "se", bounds[0] - 100.0, bounds[1] - 100.0)
    assert resized["w"] >= 0.01
    assert resized["h"] >= 0.01
