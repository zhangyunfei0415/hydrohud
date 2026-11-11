"""推流工程播放列表：环形切换 + JSON 持久化。

``to_dict`` 会把当前下标一起存下来，``from_dict`` 恢复到同一位置，所以重启后从
上次播到的工程继续。文件读写都是原子的（同目录临时文件 + ``os.replace``），
列表文件损坏时 :meth:`Playlist.load` 返回空列表而不是抛异常。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

DEFAULT_INTERVAL = 30.0
APP_DIR_NAME = "LG600Studio"
FILE_NAME = "playlist.json"


def LIST_PATH() -> Path:
    """播放列表文件的默认位置：``%APPDATA%\\LG600Studio\\playlist.json``。"""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / APP_DIR_NAME / FILE_NAME


def _coerce_path(value: Any) -> Path | None:
    """把路径类输入统一成 ``Path``；非路径类型返回 ``None``。"""
    if isinstance(value, Path):
        return value.expanduser()
    if isinstance(value, str) and value:
        return Path(value).expanduser()
    return None


def _atomic_write(path: Path, text: str) -> None:
    """原子写入文本：先写同目录临时文件，再 ``os.replace`` 覆盖目标。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".playlist-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


class Playlist:
    """有序工程列表，支持环形前进和 JSON 往返。"""

    def __init__(
        self, items: Iterable[Any] | None = None, interval: float = DEFAULT_INTERVAL
    ) -> None:
        self._items: list[Path] = []
        for item in items or []:
            path = _coerce_path(item)
            if path is not None:
                self._items.append(path)
        self._index = 0
        self.interval = interval  # 走 setter 校验

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    @property
    def items(self) -> list[Path]:
        """当前列表副本（外部修改不会影响内部状态）。"""
        return list(self._items)

    @property
    def interval(self) -> float:
        """自动切换间隔，单位秒。"""
        return self._interval

    @interval.setter
    def interval(self, value: float) -> None:
        seconds = float(value)
        if seconds < 0:
            raise ValueError(f"切换间隔不能为负数：{value!r}")
        self._interval = seconds

    def add(self, path: Any) -> Path:
        """追加一个工程路径；非路径类型抛 ``TypeError``。"""
        converted = _coerce_path(path)
        if converted is None:
            raise TypeError(f"不是有效的工程路径：{path!r}")
        self._items.append(converted)
        return converted

    def remove(self, index: int) -> Path:
        """按下标删除一项并返回它；越界抛 ``IndexError``。"""
        position = int(index)
        if not 0 <= position < len(self._items):
            raise IndexError(f"播放列表下标越界：{index!r}")
        removed = self._items.pop(position)
        if self._index > position:
            self._index -= 1
        if self._items:
            self._index %= len(self._items)
        else:
            self._index = 0
        return removed

    def clear(self) -> None:
        """清空列表并把当前位置复位到 0。"""
        self._items.clear()
        self._index = 0

    def move(self, src: int, dst: int) -> None:
        """把 ``src`` 位置的工程移动到 ``dst``；越界抛 ``IndexError``。"""
        source = int(src)
        if not 0 <= source < len(self._items):
            raise IndexError(f"播放列表下标越界：{src!r}")
        item = self._items.pop(source)
        target = max(0, min(int(dst), len(self._items)))
        self._items.insert(target, item)
        self._index = target

    def current(self) -> Path | None:
        """当前工程；列表为空时返回 ``None``。"""
        if not self._items:
            return None
        return self._items[self._index]

    def advance(self) -> Path | None:
        """前进一项（末尾回到开头）；列表为空时返回 ``None``。"""
        if not self._items:
            return None
        self._index = (self._index + 1) % len(self._items)
        return self._items[self._index]

    def to_dict(self) -> dict:
        """导出为可 JSON 序列化的字典（路径存字符串，带当前位置）。"""
        return {
            "items": [str(item) for item in self._items],
            "interval": self._interval,
            "index": self._index,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Playlist":
        """由 :meth:`to_dict` 的输出还原；字段缺失时用默认值。"""
        if not isinstance(data, dict):
            raise TypeError(f"播放列表数据必须是字典：{type(data).__name__}")
        raw_items = data.get("items") or []
        if not isinstance(raw_items, list):
            raise TypeError("播放列表 items 必须是列表")
        instance = cls(raw_items, data.get("interval", DEFAULT_INTERVAL))
        index = data.get("index", 0)
        try:
            position = int(index)
        except (TypeError, ValueError):
            position = 0
        if instance._items:
            instance._index = position % len(instance._items)
        return instance

    def save(self, path: Path) -> None:
        """把当前列表原子写入 ``path``（自动建父目录）。"""
        target = Path(path).expanduser()
        text = json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"
        _atomic_write(target, text)

    @classmethod
    def load(cls, path: Path) -> "Playlist":
        """从 ``path`` 读取；文件不存在或内容损坏时返回空列表。"""
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return cls()
        try:
            return cls.from_dict(json.loads(text))
        except (ValueError, TypeError):
            return cls()
