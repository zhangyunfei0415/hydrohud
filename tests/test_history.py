"""``hydrohud.history`` 撤销/重做历史栈的测试。

全部纯软件：用注入的 ``time_fn`` 控制合并窗口，不依赖 Tk、不写文件。
运行：``python -m pytest tests/test_history.py -q``。
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hydrohud.history import DEFAULT_COALESCE_WINDOW, DEFAULT_LIMIT, History  # noqa: E402


class FakeClock:
    """可手动推进的测试时钟。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _scene(label: str) -> dict:
    return {"name": label, "layers": [{"type": "text", "text": label}]}


# --------------------------------------------------------------------------- #
# 基本状态
# --------------------------------------------------------------------------- #
def test_defaults_match_documented_constants() -> None:
    history = History()
    assert history.limit == DEFAULT_LIMIT
    assert history.coalesce_window == pytest.approx(DEFAULT_COALESCE_WINDOW)
    assert history.position == -1
    assert history.current is None
    assert history.can_undo is False
    assert history.can_redo is False
    assert history.is_dirty is False
    assert history.labels == []


def test_push_records_states_and_position() -> None:
    history = History()
    history.push(_scene("a"), "第一")
    history.push(_scene("b"), "第二")
    assert len(history) == 2
    assert history.position == 1
    assert history.labels == ["第一", "第二"]
    assert history.current == _scene("b")


def test_push_deep_copies_the_state() -> None:
    history = History()
    scene = _scene("original")
    history.push(scene, "初始")
    scene["name"] = "被调用方改了"
    scene["layers"][0]["text"] = "也改了"
    assert history.current == _scene("original")


def test_current_and_undo_return_deep_copies() -> None:
    history = History()
    history.push(_scene("a"))
    history.push(_scene("b"))
    snapshot = history.current
    assert snapshot is not None
    snapshot["name"] = "外部修改"
    history.undo()
    restored = history.current
    assert restored is not None
    restored["name"] = "外部再改"
    assert history.current == _scene("a")


# --------------------------------------------------------------------------- #
# 撤销 / 重做
# --------------------------------------------------------------------------- #
def test_undo_and_redo_walk_the_stack() -> None:
    history = History()
    for name in ("a", "b", "c"):
        history.push(_scene(name))
    assert history.undo() == _scene("b")
    assert history.position == 1
    assert history.undo() == _scene("a")
    assert history.position == 0
    assert history.can_undo is False
    assert history.undo() is None
    assert history.redo() == _scene("b")
    assert history.redo() == _scene("c")
    assert history.can_redo is False
    assert history.redo() is None


def test_push_after_undo_truncates_redo_branch() -> None:
    history = History()
    for name in ("a", "b", "c"):
        history.push(_scene(name))
    history.undo()
    history.undo()
    assert history.position == 0
    history.push(_scene("d"), "新分支")
    assert len(history) == 2
    assert history.labels == ["", "", "新分支"][-2:]
    assert history.can_redo is False
    assert history.current == _scene("d")


def test_undo_clear_redo_after_push() -> None:
    history = History()
    history.push(_scene("a"))
    history.push(_scene("b"))
    history.undo()
    assert history.can_redo is True
    history.push(_scene("c"))
    assert history.can_redo is False


# --------------------------------------------------------------------------- #
# 合并（coalesce）
# --------------------------------------------------------------------------- #
def test_coalesce_merges_within_window() -> None:
    clock = FakeClock()
    history = History(coalesce_window=0.5, time_fn=clock)
    history.push(_scene("a"), "起点")
    history.push({"name": "拖拽 1", "layers": []}, "拖拽", coalesce_key="drag:0")
    clock.advance(0.2)
    history.push({"name": "拖拽 2", "layers": []}, "拖拽", coalesce_key="drag:0")
    clock.advance(0.2)
    history.push({"name": "拖拽 3", "layers": []}, "拖拽", coalesce_key="drag:0")
    assert len(history) == 2
    assert history.current == {"name": "拖拽 3", "layers": []}


def test_coalesce_splits_after_window_expires() -> None:
    clock = FakeClock()
    history = History(coalesce_window=0.5, time_fn=clock)
    history.push({"name": "拖拽 1", "layers": []}, "拖拽", coalesce_key="drag:0")
    clock.advance(0.6)
    history.push({"name": "拖拽 2", "layers": []}, "拖拽", coalesce_key="drag:0")
    assert len(history) == 2


def test_coalesce_requires_same_key() -> None:
    clock = FakeClock()
    history = History(coalesce_window=5.0, time_fn=clock)
    history.push(_scene("a"), "亮度", coalesce_key="field:0:brightness")
    history.push(_scene("b"), "字号", coalesce_key="field:0:size")
    assert len(history) == 2


def test_coalesce_without_key_never_merges() -> None:
    clock = FakeClock()
    history = History(coalesce_window=5.0, time_fn=clock)
    history.push(_scene("a"))
    history.push(_scene("b"))
    assert len(history) == 2


def test_coalesce_does_not_apply_after_undo() -> None:
    clock = FakeClock()
    history = History(coalesce_window=5.0, time_fn=clock)
    history.push(_scene("a"))
    history.push(_scene("b"), "拖拽", coalesce_key="drag")
    history.undo()
    history.push(_scene("c"), "拖拽", coalesce_key="drag")
    # undo 已经把游标移离栈顶，且重做分支被截断，因此不会合并进旧项
    assert len(history) == 2
    assert history.current == _scene("c")


def test_redo_clears_pending_coalesce_key() -> None:
    clock = FakeClock()
    history = History(coalesce_window=5.0, time_fn=clock)
    history.push(_scene("a"))
    history.push(_scene("b"), "拖拽", coalesce_key="drag")
    history.undo()
    history.redo()  # 回到栈顶，若合并没有被清掉，下一步会并进 "拖拽" 项
    history.push(_scene("c"), "拖拽", coalesce_key="drag")
    assert len(history) == 3
    assert history.current == _scene("c")


# --------------------------------------------------------------------------- #
# 数量上限
# --------------------------------------------------------------------------- #
def test_limit_trims_oldest_entries() -> None:
    history = History(limit=3)
    for name in ("a", "b", "c", "d", "e"):
        history.push(_scene(name))
    assert len(history) == 3
    assert history.labels == ["", "", ""]
    assert history.current == _scene("e")
    assert history.position == 2
    assert history.undo() == _scene("d")
    assert history.undo() == _scene("c")
    assert history.can_undo is False


def test_limit_setter_trims_immediately() -> None:
    history = History(limit=10)
    for index in range(6):
        history.push(_scene(str(index)))
    history.limit = 2
    assert len(history) == 2
    assert history.current == _scene("5")
    assert history.position == 1


def test_limit_setter_is_idempotent_and_floors_at_one() -> None:
    history = History(limit=2)
    history.push(_scene("a"))
    history.push(_scene("b"))
    history.limit = 2
    assert len(history) == 2
    history.limit = 0
    assert history.limit == 1
    assert len(history) == 1
    assert history.current == _scene("b")


def test_trim_keeps_saved_position_in_sync() -> None:
    history = History(limit=3)
    history.push(_scene("a"))
    history.push(_scene("b"))
    history.push(_scene("c"))
    history.mark_saved()
    assert history.is_dirty is False
    history.push(_scene("d"))  # 触发 trim，把 "a" 挤掉
    assert history.is_dirty is True


def test_trim_drops_saved_position_when_it_falls_off() -> None:
    history = History(limit=2)
    history.push(_scene("a"))
    assert len(history) == 1
    history.mark_saved()
    assert history.is_dirty is False
    history.push(_scene("b"))
    history.push(_scene("c"))  # "a"（已保存项）被挤掉
    assert history.is_dirty is True


# --------------------------------------------------------------------------- #
# 脏标记
# --------------------------------------------------------------------------- #
def test_is_dirty_tracks_mark_saved() -> None:
    history = History()
    assert history.is_dirty is False
    history.push(_scene("a"))
    assert history.is_dirty is True
    history.mark_saved()
    assert history.is_dirty is False
    history.push(_scene("b"))
    assert history.is_dirty is True
    history.undo()
    assert history.is_dirty is False
    history.redo()
    assert history.is_dirty is True


def test_clear_resets_everything() -> None:
    history = History()
    history.push(_scene("a"))
    history.push(_scene("b"))
    history.mark_saved()
    history.clear()
    assert len(history) == 0
    assert history.position == -1
    assert history.current is None
    assert history.labels == []
    assert history.can_undo is False
    assert history.can_redo is False
    assert history.is_dirty is False


def test_mark_saved_clears_pending_coalesce() -> None:
    clock = FakeClock()
    history = History(coalesce_window=5.0, time_fn=clock)
    history.push(_scene("a"), "拖拽", coalesce_key="drag")
    history.mark_saved()
    history.push(_scene("b"), "拖拽", coalesce_key="drag")
    assert len(history) == 2


# --------------------------------------------------------------------------- #
# 防御性输入
# --------------------------------------------------------------------------- #
def test_push_after_clear_starts_fresh() -> None:
    history = History()
    history.push(_scene("a"))
    history.clear()
    history.push(_scene("b"))
    assert len(history) == 1
    assert history.position == 0
    assert history.can_undo is False


def test_repr_is_informative() -> None:
    history = History(limit=5)
    history.push(_scene("a"))
    text = repr(history)
    assert "History(" in text
    assert "limit=5" in text
    assert "position=0" in text
