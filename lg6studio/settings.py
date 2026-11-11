"""应用配置读写：``%APPDATA%\\LG600Studio\\settings.json``。

设计要点：

- 目录在每次调用时按环境变量解析，测试可用 ``APPDATA`` 重定向；
- :func:`load` / :func:`from_json` 遇到缺失、损坏或非 dict 的 JSON 一律返回
  ``{}``，不抛异常，保证界面启动不被坏配置挡住；
- :func:`save` 先写同目录临时文件再 ``os.replace`` 原子替换，断电不会留下半截 JSON。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

APP_DIR_NAME = "LG600Studio"
FILE_NAME = "settings.json"
_TMP_PREFIX = ".settings-"


def app_data_dir() -> Path:
    """返回 ``%APPDATA%\\LG600Studio``；目录不存在时创建。"""
    base = os.environ.get("APPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Roaming")
    directory = Path(base) / APP_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def settings_path() -> Path:
    """返回配置文件路径（位于 :func:`app_data_dir` 下）。"""
    return app_data_dir() / FILE_NAME


def to_json(data: dict) -> str:
    """把配置 dict 序列化为 JSON 文本（中文不转义、缩进 2）。"""
    return json.dumps(data, ensure_ascii=False, indent=2)


def from_json(text: str) -> dict:
    """解析 JSON 文本；任何异常或非 dict 结果都返回 ``{}``。"""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return value


def load() -> dict:
    """读取配置；文件不存在或损坏时返回 ``{}``。"""
    try:
        text = settings_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    return from_json(text)


def save(data: dict) -> None:
    """原子保存配置：先写临时文件，再用 :func:`os.replace` 覆盖目标文件。"""
    directory = app_data_dir()
    target = directory / FILE_NAME
    text = to_json(data) + "\n"
    tmp_path: str | None = None
    try:
        handle_fd, tmp_path = tempfile.mkstemp(
            prefix=_TMP_PREFIX, suffix=".tmp", dir=str(directory)
        )
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp_path, target)
    except OSError:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def get(key: str, default: Any = None) -> Any:
    """读取单个配置项，缺失时返回 ``default``。"""
    return load().get(key, default)


def set(key: str, value: Any) -> None:
    """读-改-写单个配置项。"""
    data = load()
    data[key] = value
    save(data)
