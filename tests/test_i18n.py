"""``hydrohud.i18n`` 极简国际化基础设施的测试。

全部纯软件：不依赖 Tk、不读写磁盘。语言是模块级全局状态，所以每个用例前都通过
``reset_language`` fixture 归位到中文，并用 ``restore_catalogs`` 还原被
``add_catalog`` 改过的运行时词条表，保证用例之间互不影响。
运行：``python -m pytest tests/test_i18n.py -q``。
"""

from __future__ import annotations

import os
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hydrohud import i18n  # noqa: E402

# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def reset_language():
    """每个用例开始 / 结束都把界面语言复位到默认值。"""
    i18n.set_language(i18n.DEFAULT_LANGUAGE)
    yield
    i18n.set_language(i18n.DEFAULT_LANGUAGE)


@pytest.fixture
def restore_catalogs():
    """快照运行时词条表，用例结束后逐表还原（undo ``add_catalog``）。"""
    snapshot = {code: dict(entries) for code, entries in i18n._CATALOGS.items()}
    yield
    with i18n._LOCK:
        i18n._CATALOGS.clear()
        i18n._CATALOGS.update({code: dict(entries) for code, entries in snapshot.items()})


# --------------------------------------------------------------------------- #
# 语言代码归一
# --------------------------------------------------------------------------- #


def test_normalize_language_known_codes():
    assert i18n.normalize_language("zh") == "zh"
    assert i18n.normalize_language("en") == "en"


def test_normalize_language_regional_and_case_variants():
    assert i18n.normalize_language("en-US") == "en"
    assert i18n.normalize_language("EN") == "en"
    assert i18n.normalize_language("en_GB") == "en"
    assert i18n.normalize_language("English") == "en"
    assert i18n.normalize_language("  English  ") == "en"


def test_normalize_language_chinese_variants():
    assert i18n.normalize_language("zh-CN") == "zh"
    assert i18n.normalize_language("简体中文") == "zh"


def test_normalize_language_unknown_falls_back_to_default():
    for value in (None, "", "fr", "de-DE", 42, 3.5, object(), b"\xff\xfe"):
        assert i18n.normalize_language(value) == i18n.DEFAULT_LANGUAGE


def test_normalize_language_accepts_bytes():
    assert i18n.normalize_language(b"en") == "en"


def test_normalize_language_default_is_valid():
    assert i18n.DEFAULT_LANGUAGE in i18n.LANGUAGES


# --------------------------------------------------------------------------- #
# 语言状态
# --------------------------------------------------------------------------- #


def test_set_language_returns_normalized_code_and_updates_state():
    assert i18n.set_language("en-US") == "en"
    assert i18n.current_language() == "en"
    assert i18n.set_language("中文") == "zh"
    assert i18n.current_language() == "zh"


def test_set_language_unknown_value_resets_to_default():
    i18n.set_language("en")
    assert i18n.set_language("klingon") == i18n.DEFAULT_LANGUAGE
    assert i18n.current_language() == i18n.DEFAULT_LANGUAGE


def test_available_languages_matches_languages_and_labels():
    available = i18n.available_languages()
    assert [code for code, _label in available] == list(i18n.LANGUAGES)
    assert available == (("zh", "简体中文"), ("en", "English"))
    assert set(i18n.LANGUAGE_LABELS) == set(i18n.LANGUAGES)
    for code, label in available:
        assert label == i18n.LANGUAGE_LABELS[code]
        assert label


# --------------------------------------------------------------------------- #
# 翻译
# --------------------------------------------------------------------------- #


def test_t_translates_between_languages():
    i18n.set_language("zh")
    assert i18n.t("menu.file") == "文件"
    assert i18n.t("card.preview") == "实时预览"
    i18n.set_language("en")
    assert i18n.t("menu.file") == "File"
    assert i18n.t("card.preview") == "Live preview"


def test_t_formats_named_fields():
    i18n.set_language("zh")
    assert i18n.t("header.chip_frames", frames=42, fps="29.9") == "已推 42 帧 · 29.9 fps"
    i18n.set_language("en")
    assert i18n.t("header.chip_frames", frames=42, fps="29.9") == "Pushed 42 frames · 29.9 fps"


def test_t_version_string_uses_error_version_key():
    i18n.set_language("en")
    assert i18n.t("error.version", version="1.6.0") == "HydroHUD 1.6.0"


def test_t_missing_key_returns_key():
    assert i18n.t("no.such.key") == "no.such.key"
    assert i18n.t("") == ""


def test_t_format_failure_returns_raw_template(restore_catalogs):
    i18n.set_language("zh")
    assert i18n.t("header.chip_frames") == "已推 {frames} 帧 · {fps} fps"
    # 多余的字段被忽略，不会因为多传而抛异常
    assert i18n.t("menu.file", frames=1) == "文件"
    # 花括号不是合法格式串时也返回原文
    i18n.add_catalog("zh", {"test.bad_format": "坏掉的 {模板"})
    assert i18n.t("test.bad_format") == "坏掉的 {模板"


def test_t_falls_back_to_chinese_when_english_missing(restore_catalogs):
    i18n.add_catalog("zh", {"test.zh_only": "只有中文"})
    i18n.set_language("en")
    assert i18n.t("test.zh_only") == "只有中文"


def test_has_key_reports_catalog_membership():
    assert i18n.has_key("menu.file") is True
    assert i18n.has_key("no.such.key") is False
    assert i18n.has_key(123) is False


def test_add_catalog_adds_and_overrides(restore_catalogs):
    i18n.set_language("en")
    i18n.add_catalog("en", {"card.custom": "Custom card"})
    assert i18n.t("card.custom") == "Custom card"
    i18n.add_catalog("en", {"card.custom": "Overridden"})
    assert i18n.t("card.custom") == "Overridden"
    # 只加英文不改变中文基准，所以相对中文基准仍然「不缺」
    assert "card.custom" not in i18n.missing_keys("en")
    # 反过来只加中文，就会成为英文的缺词条
    i18n.add_catalog("zh", {"card.zh_only": "仅中文"})
    assert "card.zh_only" in i18n.missing_keys("en")


def test_add_catalog_normalizes_language(restore_catalogs):
    i18n.add_catalog("EN-us", {"test.alias": "Aliased"})
    i18n.set_language("en")
    assert i18n.t("test.alias") == "Aliased"


# --------------------------------------------------------------------------- #
# 词条完整性
# --------------------------------------------------------------------------- #


def test_missing_keys_english_is_empty():
    assert set(i18n.missing_keys("en")) == set()


def test_missing_keys_chinese_is_empty():
    assert set(i18n.missing_keys("zh")) == set()


def test_required_keys_are_present_in_both_catalogs():
    required = [
        "menu.file",
        "menu.file.new",
        "menu.file.open",
        "menu.file.save",
        "menu.file.save_as",
        "menu.file.autostart",
        "menu.file.exit",
        "menu.playlist",
        "menu.device",
        "menu.edit",
        "menu.theme",
        "menu.plugin",
        "menu.community",
        "menu.advanced",
        "menu.window",
        "menu.help",
        "menu.help.about",
        "menu.help.logs",
        "menu.help.lang_zh",
        "menu.help.lang_en",
        "rail.device",
        "rail.preset",
        "rail.background",
        "rail.layer",
        "rail.theme",
        "rail.about",
        "header.subtitle",
        "header.badge",
        "header.chip_frames",
        "header.chip_device_off",
        "header.chip_device_on",
        "bottom.stream_start",
        "bottom.stream_stop",
        "bottom.fps",
        "bottom.status_ready",
        "card.metrics",
        "card.device",
        "card.preview",
        "card.editor",
        "card.presets",
        "common.ok",
        "common.cancel",
        "common.yes",
        "common.no",
        "common.close",
        "common.loading",
        "common.failed",
        "error.version",
    ]
    for key in required:
        assert key in i18n._ZH, f"中文基准缺少 {key}"
        assert key in i18n._EN, f"英文词条缺少 {key}"


def test_catalog_values_are_non_empty_strings():
    for code in i18n.LANGUAGES:
        catalog = i18n._CATALOGS[code]
        assert catalog, f"{code} 词条表为空"
        for key, value in catalog.items():
            assert isinstance(key, str) and key
            assert isinstance(value, str) and value, f"{code}.{key} 的译文为空"


# --------------------------------------------------------------------------- #
# 线程安全
# --------------------------------------------------------------------------- #


def test_t_is_thread_safe_under_concurrent_switching():
    errors: list[str] = []
    stop = threading.Event()

    def flipper() -> None:
        try:
            for _ in range(100):
                i18n.set_language("en")
                i18n.set_language("zh")
        except Exception as exc:  # pragma: no cover - 仅在实现出错时触发
            errors.append(f"flipper: {exc!r}")
        finally:
            stop.set()

    def reader() -> None:
        try:
            for _ in range(100):
                value = i18n.t("menu.file")
                assert value in ("文件", "File"), value
                assert i18n.current_language() in i18n.LANGUAGES
        except Exception as exc:  # pragma: no cover - 仅在实现出错时触发
            errors.append(f"reader: {exc!r}")

    workers = [threading.Thread(target=flipper), threading.Thread(target=reader)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)

    assert not errors, errors
    assert stop.is_set()
    assert i18n.current_language() in i18n.LANGUAGES
