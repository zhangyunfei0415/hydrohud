"""HydroHUD 的极简国际化（i18n）基础设施：内置中英文词条 + 线程安全的语言切换。

设计约束：

- **零依赖**：只用标准库（``threading`` + 类型标注），不读外部 JSON，打包成单文件
  exe 后行为与源码运行完全一致。
- **语言状态是模块级全局变量**：Tk 主线程读界面文案，推流 / 采样等后台线程也可能
  调 :func:`t`，所以读写都走 :data:`_LOCK`。
- **缺词条不崩**：当前语言没有该 key 时回落中文；中文也没有时返回 key 本身，方便
  先写界面再补文案。``str.format`` 失败时返回未格式化的原文。

用法::

    from hydrohud import i18n

    i18n.set_language("en-US")                     # 归一成 "en"
    i18n.t("menu.file")                            # -> "File"
    i18n.t("header.chip_frames", frames=12, fps=29.8)
    # -> "已推 12 帧 · 29.8 fps"

:func:`add_catalog` 只影响当前进程，不落盘，供插件或测试追加词条。
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

LANGUAGES: tuple[str, ...] = ("zh", "en")
DEFAULT_LANGUAGE = "zh"
LANGUAGE_LABELS = {"zh": "简体中文", "en": "English"}

# --------------------------------------------------------------------------- #
# 内置词条：中文是基准，英文与中文字典的 key 集合必须完全一致
# --------------------------------------------------------------------------- #
_ZH: dict[str, str] = {
    # -- 菜单 ---------------------------------------------------------------- #
    "menu.file": "文件",
    "menu.file.new": "新建",
    "menu.file.open": "打开",
    "menu.file.save": "保存",
    "menu.file.save_as": "另存为",
    "menu.file.autostart": "开机自启",
    "menu.file.exit": "退出",
    "menu.playlist": "轮播",
    "menu.device": "设备",
    "menu.edit": "编辑",
    "menu.theme": "主题包",
    "menu.plugin": "插件",
    "menu.community": "社区",
    "menu.advanced": "高级",
    "menu.window": "窗口",
    "menu.help": "帮助",
    "menu.help.about": "关于",
    "menu.help.logs": "打开日志目录",
    "menu.help.lang_zh": "简体中文",
    "menu.help.lang_en": "English",
    # -- 左侧图标栏 ---------------------------------------------------------- #
    "rail.device": "设备",
    "rail.preset": "预设",
    "rail.background": "背景",
    "rail.layer": "图层",
    "rail.theme": "主题",
    "rail.about": "关于",
    # -- 顶栏 / 底栏 --------------------------------------------------------- #
    "header.subtitle": "TCOMAS LG600 水冷屏 · 开源控制台 · MIT License",
    "header.badge": "AURORA",
    "header.chip_frames": "已推 {frames} 帧 · {fps} fps",
    "header.chip_device_off": "● 设备未连接",
    "header.chip_device_on": "● 设备就绪",
    "bottom.stream_start": "▶  开始推送到屏幕",
    "bottom.stream_stop": "⏹  停止",
    "bottom.fps": "推流帧率",
    "bottom.status_ready": "就绪 · 未连接设备（不会自动连接）",
    # -- 卡片标题 ------------------------------------------------------------ #
    "card.metrics": "实时指标",
    "card.device": "设备",
    "card.preview": "实时预览",
    "card.editor": "内容与编辑",
    "card.presets": "预设场景",
    # -- 通用文案 ------------------------------------------------------------ #
    "common.ok": "确定",
    "common.cancel": "取消",
    "common.yes": "是",
    "common.no": "否",
    "common.close": "关闭",
    "common.loading": "加载中…",
    "common.failed": "失败",
    # -- 错误 ---------------------------------------------------------------- #
    "error.version": "HydroHUD {version}",
}

_EN: dict[str, str] = {
    # -- Menus --------------------------------------------------------------- #
    "menu.file": "File",
    "menu.file.new": "New",
    "menu.file.open": "Open",
    "menu.file.save": "Save",
    "menu.file.save_as": "Save As",
    "menu.file.autostart": "Launch at login",
    "menu.file.exit": "Exit",
    "menu.playlist": "Playlist",
    "menu.device": "Device",
    "menu.edit": "Edit",
    "menu.theme": "Theme pack",
    "menu.plugin": "Plugins",
    "menu.community": "Community",
    "menu.advanced": "Advanced",
    "menu.window": "Window",
    "menu.help": "Help",
    "menu.help.about": "About",
    "menu.help.logs": "Open log folder",
    "menu.help.lang_zh": "简体中文",
    "menu.help.lang_en": "English",
    # -- Icon rail ----------------------------------------------------------- #
    "rail.device": "Device",
    "rail.preset": "Presets",
    "rail.background": "Background",
    "rail.layer": "Layers",
    "rail.theme": "Themes",
    "rail.about": "About",
    # -- Header / bottom bar ------------------------------------------------- #
    "header.subtitle": "TCOMAS LG600 liquid-cooler display · Open-source console · MIT License",
    "header.badge": "AURORA",
    "header.chip_frames": "Pushed {frames} frames · {fps} fps",
    "header.chip_device_off": "● Device not connected",
    "header.chip_device_on": "● Device ready",
    "bottom.stream_start": "▶  Start streaming to screen",
    "bottom.stream_stop": "⏹  Stop",
    "bottom.fps": "Stream frame rate",
    "bottom.status_ready": "Ready · No device connected (won't auto-connect)",
    # -- Card titles --------------------------------------------------------- #
    "card.metrics": "Live metrics",
    "card.device": "Device",
    "card.preview": "Live preview",
    "card.editor": "Content & editing",
    "card.presets": "Preset scenes",
    # -- Shared strings ------------------------------------------------------ #
    "common.ok": "OK",
    "common.cancel": "Cancel",
    "common.yes": "Yes",
    "common.no": "No",
    "common.close": "Close",
    "common.loading": "Loading…",
    "common.failed": "Failed",
    # -- Errors -------------------------------------------------------------- #
    "error.version": "HydroHUD {version}",
}

_LOCK = threading.Lock()
_LANGUAGE = DEFAULT_LANGUAGE

# 运行时词条表（``add_catalog`` 改的就是这里）；测试可整体替换后恢复。
_CATALOGS: dict[str, dict[str, str]] = {
    "zh": dict(_ZH),
    "en": dict(_EN),
}


def _build_aliases() -> dict[str, str]:
    """构造语言别名表：``"en-US"`` / ``"English"`` / ``"英文"`` 都能落到 ``"en"``。"""
    aliases = {code: code for code in LANGUAGES}
    for code, label in LANGUAGE_LABELS.items():
        aliases[label.strip().lower()] = code
    aliases.update(
        {
            "cn": "zh",
            "zh-cn": "zh",
            "zh-hans": "zh",
            "chinese": "zh",
            "中文": "zh",
            "eng": "en",
            "en-us": "en",
            "en-gb": "en",
            "英文": "en",
            "英语": "en",
        }
    )
    return aliases


_ALIASES = _build_aliases()


def normalize_language(value: Any) -> str:
    """把任意输入归一成 :data:`LANGUAGES` 里的语言代码，认不出就回落默认语言。

    ``"en-US"`` / ``"EN"`` / ``"English"`` / ``b"en"`` 都得到 ``"en"``；
    ``"zh-CN"`` / ``"简体中文"`` 得到 ``"zh"``；``None``、``"fr"``、数字等未知值得到
    :data:`DEFAULT_LANGUAGE`。大小写、首尾空白、``-`` / ``_`` 分隔都不敏感。
    """
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return DEFAULT_LANGUAGE
    if not isinstance(value, str):
        return DEFAULT_LANGUAGE
    text = value.strip().lower().replace("_", "-")
    if not text:
        return DEFAULT_LANGUAGE
    if text in _ALIASES:
        return _ALIASES[text]
    return _ALIASES.get(text.split("-", 1)[0], DEFAULT_LANGUAGE)


def current_language() -> str:
    """返回当前界面语言代码（``"zh"`` 或 ``"en"``）。"""
    with _LOCK:
        return _LANGUAGE


def set_language(language: Any) -> str:
    """切换界面语言并返回归一后的代码；未知值回落 :data:`DEFAULT_LANGUAGE`。"""
    global _LANGUAGE
    code = normalize_language(language)
    with _LOCK:
        _LANGUAGE = code
    return code


def available_languages() -> tuple[tuple[str, str], ...]:
    """返回 ``(代码, 显示名)`` 元组，顺序与 :data:`LANGUAGES` 一致。"""
    return tuple((code, LANGUAGE_LABELS.get(code, code)) for code in LANGUAGES)


def _lookup(key: str) -> str | None:
    """按「当前语言 → 中文基准」的顺序查词条；都没命中返回 ``None``。"""
    with _LOCK:
        catalog = _CATALOGS.get(_LANGUAGE)
        if catalog is not None and key in catalog:
            return catalog[key]
        return _CATALOGS.get(DEFAULT_LANGUAGE, {}).get(key)


def t(key: str, /, **fields: Any) -> str:
    """翻译 ``key`` 并用 ``str.format(**fields)`` 填充占位符。

    找不到词条时返回 ``key`` 本身；``KeyError`` / ``IndexError`` / ``ValueError`` /
    ``TypeError``（字段缺失、位置参数缺失、格式符不匹配等）时返回未格式化的原文，
    绝不让界面因为一条文案抛异常。多余的 ``fields`` 会被忽略。
    """
    if not isinstance(key, str):
        return str(key)
    template = _lookup(key)
    if template is None:
        return key
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError, TypeError):
        return template


def has_key(key: str) -> bool:
    """当前语言（含中文回落）是否有 ``key`` 的译文。"""
    return isinstance(key, str) and _lookup(key) is not None


def add_catalog(language: str, mapping: Mapping[str, str]) -> None:
    """向运行时词条表追加 / 覆盖词条（插件或测试用），不落盘。

    ``language`` 先经 :func:`normalize_language` 归一，未知值写进中文基准。
    """
    code = normalize_language(language)
    if not mapping:
        return
    entries = {str(key): str(value) for key, value in mapping.items()}
    with _LOCK:
        _CATALOGS.setdefault(code, {}).update(entries)


def missing_keys(language: str) -> tuple[str, ...]:
    """返回相对中文基准缺失的 key（已排序），供 CI / 自检发现漏翻的词条。"""
    code = normalize_language(language)
    with _LOCK:
        base = _CATALOGS.get(DEFAULT_LANGUAGE, {})
        catalog = _CATALOGS.get(code, {})
        missing = set(base) - set(catalog)
    return tuple(sorted(missing))


__all__ = [
    "DEFAULT_LANGUAGE",
    "LANGUAGES",
    "LANGUAGE_LABELS",
    "add_catalog",
    "available_languages",
    "current_language",
    "has_key",
    "missing_keys",
    "normalize_language",
    "set_language",
    "t",
]
