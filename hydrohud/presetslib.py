"""社区预设库：远程索引拉取、离线缓存与一键安装。

预设库是一份静态 JSON 索引（默认托管在 GitHub raw 上），结构为::

    {"version": 1, "presets": [{"name": "...", "author": "...", "scene": {...}}, ...]}

为方便手工维护，也接受裸 list。索引里每条预设把**场景 dict 内嵌**在 ``scene``
字段，所以本模块只处理数据、不下载也不执行任何代码。

工作方式：

- :func:`fetch_index` 联网拉取并原子写入 ``%APPDATA%\\HydroHUD\\presets-index.json``，
  失败抛 :class:`PresetError`（且不会破坏已有缓存）；
- :func:`cached_index` 只读缓存，供离线启动使用；
- :func:`index_or_cache` 是 GUI 用的组合入口：先网络后缓存，永不抛异常；
- :func:`install_preset` 把条目里的场景经 :func:`hydrohud.scenes.normalize_scene`
  归一化后写成 ``.hud.json``，文件名由预设名清洗而来，重名自动加序号。

导入本模块不联网、不建目录、不读文件：所有 I/O 都在函数被调用时才发生。
"""

# MIT License
#
# Copyright (c) 2026 HydroHUD contributors
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

from hydrohud import scenes

__all__ = [
    "CACHE_FILE_NAME",
    "DEFAULT_INDEX_URL",
    "INDEX_VERSION",
    "MAX_NAME_LENGTH",
    "PRESETS_DIR_NAME",
    "PROJECT_SUFFIX",
    "TIMEOUT_SECONDS",
    "PresetError",
    "cache_path",
    "cached_index",
    "clear_cache",
    "fetch_index",
    "index_or_cache",
    "install_preset",
    "last_error",
    "presets_dir",
    "slugify",
    "validate_entry",
]

#: 默认索引地址（社区预设库）。
DEFAULT_INDEX_URL = (
    "https://raw.githubusercontent.com/zhangyunfei0415/hydrohud-presets/main/index.json"
)

#: 缓存文件名，位于 :func:`cache_path` 下。
CACHE_FILE_NAME = "presets-index.json"

#: 单次 HTTP 请求超时（秒）。
TIMEOUT_SECONDS = 8.0

#: 索引 JSON 的期望版本号（缺省或缺失时不强制校验）。
INDEX_VERSION = 1

#: 预设名的最大长度。
MAX_NAME_LENGTH = 80

#: 安装目录名（位于 ``%APPDATA%\\HydroHUD`` 下）。
PRESETS_DIR_NAME = "presets"

#: 安装出的工程文件后缀。
PROJECT_SUFFIX = ".hud.json"

#: 清洗后的文件名最大长度（字节级安全，给 ``-2`` 之类后缀留余量）。
MAX_SLUG_LENGTH = 60

#: 文件名清洗时替换掉的字符：Windows 非法字符、路径分隔符与控制字符。
_ILLEGAL_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')

#: 连续空白与连字符压成一个（避免出现 ``a---b``）。
_DASH_RUNS = re.compile(r"[\s\-_]+")

#: Windows 保留设备名；直接用作文件名会失败。
_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)

#: 重名时尝试的后缀上限（``name-2`` … ``name-999``）。
_MAX_DUPLICATES = 999

#: 单条预设允许的字段；其余字段在 :func:`validate_entry` 里被丢掉。
ENTRY_KEYS = ("name", "scene", "author", "description", "tags", "version")

_LAST_ERROR: str | None = None


def last_error() -> str | None:
    """返回最近一次内部错误的中文描述；没有任何错误时返回 ``None``。"""
    return _LAST_ERROR


def _record_error(message: str) -> str:
    """记录内部错误并返回描述文本。"""
    global _LAST_ERROR
    _LAST_ERROR = message
    return message


def _clear_error() -> None:
    """清掉上一次的错误（成功路径调用）。"""
    global _LAST_ERROR
    _LAST_ERROR = None


def _app_data_dir() -> Path:
    """返回 ``%APPDATA%\\HydroHUD``；没有 ``APPDATA`` 时退回用户主目录。"""
    app_data = os.environ.get("APPDATA")
    if app_data:
        return Path(app_data) / "HydroHUD"
    return Path.home() / "AppData" / "Roaming" / "HydroHUD"


def cache_path() -> Path:
    """返回缓存文件路径 ``%APPDATA%\\HydroHUD\\presets-index.json``（不创建目录）。"""
    return _app_data_dir() / CACHE_FILE_NAME


def presets_dir() -> Path:
    """返回预设安装目录 ``%APPDATA%\\HydroHUD\\presets``（不创建目录）。"""
    return _app_data_dir() / PRESETS_DIR_NAME


class PresetError(Exception):
    """预设库的任何可展示错误（网络、索引格式、条目非法）。"""


# --------------------------------------------------------------------------- #
# 单条预设的校验与清洗
# --------------------------------------------------------------------------- #
def _clean_text(value: Any, limit: int) -> str:
    """把任意值转成去首尾空白的 str，并截断到 ``limit`` 个字符。"""
    if value is None:
        return ""
    try:
        text = str(value).strip()
    except Exception:  # pragma: no cover - 只可能来自坏掉的 __str__
        return ""
    return text[:limit]


def _clean_tags(value: Any) -> list[str]:
    """把 ``tags`` 清洗成去重后的非空字符串列表；非 list 视为空。"""
    if not isinstance(value, (list, tuple)):
        return []
    tags: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean_text(item, 40)
        if not text or text in seen:
            continue
        seen.add(text)
        tags.append(text)
    return tags


def validate_entry(raw: Any) -> dict:
    """校验并清洗单条预设，返回只含已知字段的 dict。

    :param raw: 索引里的一个条目（任意类型）。
    :raises PresetError: 不是对象、缺 ``name``、``name`` 为空或超长、缺 ``scene``
        或 ``scene`` 不是对象。

    ``scene`` 原样保留（安装时才归一化），这样 GUI 可以按需展示作者、标签等信息。
    """
    if not isinstance(raw, dict):
        raise PresetError("预设条目必须是 JSON 对象")
    name = _clean_text(raw.get("name"), MAX_NAME_LENGTH + 1)
    if not name:
        raise PresetError("预设缺少 name")
    if len(name) > MAX_NAME_LENGTH:
        raise PresetError(f"预设 name 过长（最多 {MAX_NAME_LENGTH} 个字符）")
    scene = raw.get("scene")
    if not isinstance(scene, dict):
        raise PresetError(f"预设「{name}」缺少 scene 对象")
    return {
        "name": name,
        "scene": scene,
        "author": _clean_text(raw.get("author"), MAX_NAME_LENGTH),
        "description": _clean_text(raw.get("description"), 500),
        "tags": _clean_tags(raw.get("tags")),
        "version": _clean_text(raw.get("version"), 32),
    }


def _validate_entries(items: Any) -> list[dict]:
    """校验条目列表；单条非法只跳过它，整体不是 list 时抛 :class:`PresetError`。"""
    if not isinstance(items, list):
        raise PresetError("索引里的 presets 必须是数组")
    entries: list[dict] = []
    for raw in items:
        try:
            entries.append(validate_entry(raw))
        except PresetError:
            continue  # 坏条目跳过，不拖垮整份索引
    return entries


def _parse_index(payload: Any) -> list[dict]:
    """解析索引 JSON：接受 ``{"presets": [...]}`` 与裸 list 两种形状。"""
    if isinstance(payload, list):
        return _validate_entries(payload)
    if not isinstance(payload, dict):
        raise PresetError("索引 JSON 必须是对象或数组")
    presets = payload.get("presets")
    if presets is None:
        raise PresetError("索引缺少 presets 字段")
    return _validate_entries(presets)


def _read_json_file(path: Path) -> Any:
    """读 JSON 文件；不存在或损坏返回 ``None``（损坏会记录错误）。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except OSError as exc:
        _record_error(f"读取预设缓存失败：{exc}")
        return None
    try:
        data = json.loads(text)
    except ValueError as exc:
        _record_error(f"预设缓存不是合法 JSON：{exc}")
        return None
    if data is None:
        _record_error("预设缓存格式错误：内容为空")
    return data


def _write_cache(entries: list[dict], version: int = INDEX_VERSION) -> None:
    """原子写入缓存：先写同目录临时文件，再 ``os.replace``。"""
    path = cache_path()
    payload = {"version": version, "fetched": time.time(), "presets": entries}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".presets-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except OSError as exc:
        _record_error(f"写入预设缓存失败：{exc}")


# --------------------------------------------------------------------------- #
# 网络与缓存
# --------------------------------------------------------------------------- #
def _download(url: str, timeout: float) -> Any:
    """下载并解析索引 JSON；任何失败都转成 :class:`PresetError`。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read()
    except PresetError:
        raise
    except Exception as exc:
        raise PresetError(f"下载预设索引失败：{exc}") from exc
    try:
        text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else str(raw)
    except UnicodeDecodeError as exc:
        raise PresetError(f"预设索引编码错误：{exc}") from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise PresetError(f"预设索引不是合法 JSON：{exc}") from exc


def fetch_index(url: str | None = None, timeout: float = TIMEOUT_SECONDS) -> list[dict]:
    """联网拉取索引，成功后写缓存并返回清洗过的条目列表。

    :param url: 索引地址；``None`` 时用 :data:`DEFAULT_INDEX_URL`。
    :param timeout: 请求超时（秒）。
    :raises PresetError: 网络失败、JSON 非法或索引形状不对；此时旧缓存保持原样。

    单条非法只会被跳过（不会让整份索引失败），只有"整体不可用"才抛异常。
    """
    target = url or DEFAULT_INDEX_URL
    try:
        payload = _download(target, timeout)
        entries = _parse_index(payload)
    except PresetError as exc:
        raise PresetError(_record_error(str(exc))) from exc
    version = payload.get("version") if isinstance(payload, dict) else INDEX_VERSION
    _write_cache(entries, version if isinstance(version, int) else INDEX_VERSION)
    _clear_error()
    return entries


def cached_index() -> list[dict]:
    """只读缓存并返回条目列表；没有缓存或缓存损坏时返回 ``[]``，不抛异常。"""
    payload = _read_json_file(cache_path())
    if payload is None:
        return []
    try:
        return _parse_index(payload)
    except PresetError as exc:
        _record_error(f"预设缓存格式错误：{exc}")
        return []


def index_or_cache(
    url: str | None = None, timeout: float = TIMEOUT_SECONDS
) -> tuple[list[dict], bool]:
    """先联网再回落缓存，返回 ``(条目, 是否来自网络)``；两步都失败返回 ``([], False)``。

    GUI 刷新按钮直接用这个函数：它永不抛异常，失败原因记录在 :func:`last_error`。
    """
    try:
        return fetch_index(url, timeout), True
    except PresetError as exc:
        _record_error(str(exc))
    cached = cached_index()
    return (cached, False) if cached else ([], False)


def clear_cache() -> bool:
    """删除缓存文件；真的删掉了返回 ``True``（本来就不存在返回 ``False``）。"""
    path = cache_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        _record_error(f"删除预设缓存失败：{exc}")
        return False
    return True


# --------------------------------------------------------------------------- #
# 安装
# --------------------------------------------------------------------------- #
def slugify(name: str) -> str:
    """把预设名清洗成安全文件名（不含扩展名）。

    保留中文与字母数字，把空白、路径分隔符和 Windows 非法字符压成 ``-``；
    截断到 :data:`MAX_SLUG_LENGTH`，避免 Windows 保留设备名；空结果回退 ``preset``。
    """
    text = _ILLEGAL_CHARS.sub("-", str(name or "").strip())
    text = _DASH_RUNS.sub("-", text).strip("-. ")
    if len(text) > MAX_SLUG_LENGTH:
        text = text[:MAX_SLUG_LENGTH].strip("-. ")
    if not text:
        return "preset"
    if text.lower() in _RESERVED_NAMES:
        return f"{text}_preset"
    return text


def _unique_destination(directory: Path, slug: str) -> Path:
    """在 ``directory`` 下找一个未被占用的文件名（重名加 ``-2`` … ``-999``）。"""
    candidate = directory / f"{slug}{PROJECT_SUFFIX}"
    if not candidate.exists():
        return candidate
    for index in range(2, _MAX_DUPLICATES + 1):
        candidate = directory / f"{slug}-{index}{PROJECT_SUFFIX}"
        if not candidate.exists():
            return candidate
    raise PresetError(f"预设「{slug}」重名过多，无法写入")


def install_preset(
    entry: Any,
    dest_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """把一条预设安装成 ``.hud.json`` 工程文件，返回写入路径。

    :param entry: 索引里的一条预设（会先经 :func:`validate_entry` 校验）。
    :param dest_dir: 目标目录；``None`` 时用 :func:`presets_dir`（会自动创建）。
    :raises PresetError: 条目非法、目录不可写或场景无法归一化/保存。

    场景先经 :func:`hydrohud.scenes.normalize_scene` 补默认值，再用
    :func:`hydrohud.scenes.save_project` 原子写入，所以产物可被
    :func:`hydrohud.scenes.load_project` 直接读回。
    """
    cleaned = validate_entry(entry)
    directory = Path(dest_dir) if dest_dir is not None else presets_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PresetError(f"无法创建预设目录 {directory}：{exc}") from exc
    target = _unique_destination(directory, slugify(cleaned["name"]))
    try:
        normalized = scenes.normalize_scene(cleaned["scene"])
        scenes.save_project(normalized, str(target))
    except PresetError:
        raise
    except Exception as exc:
        raise PresetError(f"安装预设「{cleaned['name']}」失败：{exc}") from exc
    _clear_error()
    return target
