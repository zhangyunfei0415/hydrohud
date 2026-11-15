"""场景编辑的撤销/重做历史栈（纯标准库，不依赖 Tk）。

设计要点：

- 每次 :meth:`History.push` 都做 :func:`copy.deepcopy`，调用方之后修改传入的
  dict 不会污染历史；:meth:`undo` / :meth:`redo` 同样返回深拷贝；
- ``coalesce_key`` 让连续的同类微操作（例如拖拽中的每一帧）合并成一个历史项，
  只在 ``coalesce_window`` 秒内有效，超时后重新开一项；
- ``limit`` 只保留最近的 N 项，裁掉最旧项时同步修正当前位置和已保存位置；
- :meth:`mark_saved` / :attr:`is_dirty` 供窗口标题和关闭确认使用。
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from typing import Any

#: 默认保留的历史项上限。
DEFAULT_LIMIT = 100

#: 默认的合并窗口（秒）。
DEFAULT_COALESCE_WINDOW = 0.6


class History:
    """线性撤销/重做历史栈。

    参数 ``limit`` 为最多保留的历史项数；``coalesce_window`` 为同类操作
    自动合并的时间窗口；``time_fn`` 可注入测试用时钟（默认
    :func:`time.monotonic`）。
    """

    def __init__(
        self,
        limit: int = DEFAULT_LIMIT,
        coalesce_window: float = DEFAULT_COALESCE_WINDOW,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._time_fn = time_fn
        self.coalesce_window = max(0.0, float(coalesce_window))
        self._limit = max(1, int(limit))
        self._states: list[dict] = []
        self._labels: list[str] = []
        self._position = -1
        self._saved_position: int | None = None
        self._coalesce_key: str | None = None
        self._last_push_time = 0.0

    # ------------------------------------------------------------ 只读属性
    @property
    def can_undo(self) -> bool:
        """是否还有可撤销的历史项。"""
        return self._position > 0

    @property
    def can_redo(self) -> bool:
        """是否还有可重做的历史项。"""
        return self._position < len(self._states) - 1

    @property
    def current(self) -> dict | None:
        """当前历史项（深拷贝）；栈为空时返回 ``None``。"""
        if 0 <= self._position < len(self._states):
            return copy.deepcopy(self._states[self._position])
        return None

    @property
    def labels(self) -> list[str]:
        """各项的标签列表（与历史顺序一致），供 UI 菜单使用。"""
        return list(self._labels)

    @property
    def position(self) -> int:
        """当前历史项下标；空栈时为 ``-1``。"""
        return self._position

    @property
    def is_dirty(self) -> bool:
        """当前状态是否与最近一次 :meth:`mark_saved` 的状态不同。"""
        if self._saved_position is None:
            return bool(self._states)
        return self._position != self._saved_position

    @property
    def limit(self) -> int:
        """历史项数量上限。"""
        return self._limit

    @limit.setter
    def limit(self, value: int) -> None:
        new_limit = max(1, int(value))
        if new_limit == self._limit:
            return
        self._limit = new_limit
        if len(self._states) > new_limit:
            self._trim()

    # ------------------------------------------------------------ 写入操作
    def push(self, state: dict, label: str = "", coalesce_key: str | None = None) -> None:
        """记录一个新状态。

        ``coalesce_key`` 非空且与上一次 push 的 key 相同、间隔不超过
        ``coalesce_window`` 时，替换栈顶状态而不是新增一项。
        """
        now = self._time_fn()
        snapshot = copy.deepcopy(state)
        if (
            coalesce_key is not None
            and coalesce_key == self._coalesce_key
            and self._states
            and self._position == len(self._states) - 1
            and now - self._last_push_time <= self.coalesce_window
        ):
            self._states[self._position] = snapshot
            self._labels[self._position] = label
            self._last_push_time = now
            return
        del self._states[self._position + 1 :]
        del self._labels[self._position + 1 :]
        self._states.append(snapshot)
        self._labels.append(label)
        self._position += 1
        self._coalesce_key = coalesce_key
        self._last_push_time = now
        if len(self._states) > self._limit:
            self._trim()

    def mark_saved(self) -> None:
        """把当前历史位置标记为“已保存”，之后 :attr:`is_dirty` 为假。"""
        self._saved_position = self._position
        self._coalesce_key = None

    def clear(self) -> None:
        """清空全部历史，并把“已保存”位置也复位。"""
        self._states.clear()
        self._labels.clear()
        self._position = -1
        self._saved_position = None
        self._coalesce_key = None
        self._last_push_time = 0.0

    # ------------------------------------------------------------ 移动游标
    def undo(self) -> dict | None:
        """退回上一个状态并返回其深拷贝；没有可撤销项时返回 ``None``。"""
        if not self.can_undo:
            return None
        self._position -= 1
        self._coalesce_key = None
        return copy.deepcopy(self._states[self._position])

    def redo(self) -> dict | None:
        """前进到下一个状态并返回其深拷贝；没有可重做项时返回 ``None``。"""
        if not self.can_redo:
            return None
        self._position += 1
        self._coalesce_key = None
        return copy.deepcopy(self._states[self._position])

    # ------------------------------------------------------------ 内部工具
    def _trim(self) -> None:
        """裁掉最旧的历史项，并修正当前位置与已保存位置。"""
        excess = len(self._states) - self._limit
        if excess <= 0:
            return
        del self._states[:excess]
        del self._labels[:excess]
        self._position = max(-1, self._position - excess)
        if self._saved_position is not None:
            if self._saved_position < excess:
                self._saved_position = None
            else:
                self._saved_position -= excess

    def __len__(self) -> int:
        return len(self._states)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"History(limit={self._limit}, position={self._position}, "
            f"len={len(self._states)}, dirty={self.is_dirty})"
        )


__all__: list[Any] = ["DEFAULT_COALESCE_WINDOW", "DEFAULT_LIMIT", "History"]
