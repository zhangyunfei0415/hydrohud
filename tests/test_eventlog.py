"""结构化事件日志（JSONL + 按大小轮转 + 读取）的测试。

目录一律用 ``tmp_path`` + ``monkeypatch.setenv("LOCALAPPDATA", ...)`` 隔离，
绝不触碰真实的 ``%LOCALAPPDATA%\\HydroHUD\\logs``。
"""

from __future__ import annotations

import datetime as _datetime
import json
import threading

import pytest

from hydrohud import diagnostics


@pytest.fixture()
def event_dir(tmp_path, monkeypatch):
    """把 ``%LOCALAPPDATA%`` 指到临时目录，返回事件日志目录。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return diagnostics.LOG_DIR()


def _lines(path):
    """读文件的原始行（去掉空行）。"""
    if not path.exists():
        return []
    return [row for row in path.read_text(encoding="utf-8").splitlines() if row.strip()]


def _parsed(path):
    return [json.loads(row) for row in _lines(path)]


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def test_event_log_path_under_localappdata(tmp_path, event_dir):
    path = diagnostics.event_log_path()
    assert path == event_dir / "events.jsonl"
    assert path.name == diagnostics.EVENT_LOG_FILE_NAME
    assert path.parent.name == "logs"
    assert path.parent.parent.name == diagnostics.APP_DIR_NAME
    assert tmp_path in path.parents


# --------------------------------------------------------------------------- #
# record：基本写入
# --------------------------------------------------------------------------- #
def test_record_writes_one_json_line(event_dir):
    diagnostics.record("stream.start", project="演示.hud", fps=30)
    rows = _lines(diagnostics.event_log_path())
    assert len(rows) == 1
    payload = json.loads(rows[0])
    assert payload["event"] == "stream.start"
    assert payload["level"] == "info"
    assert payload["project"] == "演示.hud"
    assert payload["fps"] == 30
    assert set(payload) >= {"ts", "level", "event"}


def test_record_timestamp_is_iso8601_with_millis(event_dir):
    payload = diagnostics.record("tick")
    stamp = payload["ts"]
    assert stamp[10] == "T"
    assert "." in stamp.split("T")[1]
    parsed = _datetime.datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None


def test_record_return_value_matches_disk(event_dir):
    returned = diagnostics.record("layer.add", level="warning", kind="text", n=2)
    on_disk = _parsed(diagnostics.event_log_path())[-1]
    assert returned == on_disk


def test_record_appends_multiple_lines(event_dir):
    for index in range(3):
        diagnostics.record("step", index=index)
    rows = _parsed(diagnostics.event_log_path())
    assert [row["index"] for row in rows] == [0, 1, 2]


def test_record_creates_missing_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    target = tmp_path / "尚未" / "创建"
    monkeypatch.setattr(diagnostics, "LOG_DIR", lambda: target)
    diagnostics.record("cold.start")
    assert (target / diagnostics.EVENT_LOG_FILE_NAME).exists()


# --------------------------------------------------------------------------- #
# record：等级
# --------------------------------------------------------------------------- #
def test_record_invalid_level_falls_back_to_info(event_dir):
    payload = diagnostics.record("weird", level="trace")
    assert payload["level"] == "info"
    assert _parsed(diagnostics.event_log_path())[-1]["level"] == "info"


@pytest.mark.parametrize("level", diagnostics.EVENT_LEVELS)
def test_record_accepts_each_known_level(event_dir, level):
    assert diagnostics.record("known", level=level)["level"] == level


# --------------------------------------------------------------------------- #
# record：值转义
# --------------------------------------------------------------------------- #
def test_record_unserializable_values_become_strings(event_dir):
    marker = object()
    payload = diagnostics.record(
        "bad.values",
        marker=marker,
        when=_datetime.datetime(2026, 11, 18, 15, 0, 0),
        tags={"a", "b"},
    )
    assert payload["marker"] == repr(marker)
    assert isinstance(payload["when"], str)
    assert isinstance(payload["tags"], str)
    json.loads(_lines(diagnostics.event_log_path())[0])  # 仍然是一行合法 JSON


def test_record_keeps_json_safe_values(event_dir):
    payload = diagnostics.record(
        "safe",
        text="中文",
        num=1,
        ratio=0.5,
        flag=True,
        nothing=None,
        items=[1, "二", None],
        nested={"k": [1, 2]},
    )
    assert payload["text"] == "中文"
    assert payload["ratio"] == 0.5
    assert payload["nothing"] is None
    assert payload["items"] == [1, "二", None]
    assert payload["nested"] == {"k": [1, 2]}


def test_record_stringifies_non_string_keys(event_dir):
    payload = diagnostics.record("keys", mapping={1: "一", None: "空"}, **{"正常": 1})
    assert payload["mapping"]["1"] == "一"
    assert payload["mapping"]["None"] == "空"
    assert payload["正常"] == 1


def test_record_handles_non_string_event_name(event_dir):
    payload = diagnostics.record(123)
    assert payload["event"] == "123"


# --------------------------------------------------------------------------- #
# record：超长截断
# --------------------------------------------------------------------------- #
def test_record_truncates_oversized_field(event_dir):
    payload = diagnostics.record("huge", blob="x" * 20000)
    assert payload["blob"].endswith(diagnostics.EVENT_TRUNCATED_SUFFIX)
    assert len(payload["blob"]) <= diagnostics.EVENT_FIELD_MAX_CHARS + len(
        diagnostics.EVENT_TRUNCATED_SUFFIX
    )


def test_record_line_stays_under_limit(event_dir):
    diagnostics.record("huge", blob="y" * 50000, other="z" * 30000)
    raw = _lines(diagnostics.event_log_path())[0]
    assert len(raw.encode("utf-8")) <= diagnostics.EVENT_LINE_MAX_BYTES
    json.loads(raw)


def test_record_short_field_is_not_truncated(event_dir):
    payload = diagnostics.record("small", blob="x" * 100)
    assert payload["blob"] == "x" * 100


# --------------------------------------------------------------------------- #
# record：写失败不抛
# --------------------------------------------------------------------------- #
def test_record_write_failure_does_not_raise(event_dir):
    event_dir.parent.mkdir(parents=True, exist_ok=True)
    event_dir.write_text("目录被换成了文件", encoding="utf-8")
    payload = diagnostics.record("broken", reason="只读")
    assert payload["event"] == "broken"
    assert payload["reason"] == "只读"


def test_record_open_failure_does_not_raise(event_dir, monkeypatch):
    def boom(self, *args, **kwargs):
        raise PermissionError("权限不足")

    monkeypatch.setattr(diagnostics.Path, "open", boom, raising=True)
    payload = diagnostics.record("denied")
    assert payload["event"] == "denied"


# --------------------------------------------------------------------------- #
# record：并发
# --------------------------------------------------------------------------- #
def test_record_concurrent_threads_write_whole_lines(event_dir):
    threads = 20
    per_thread = 50

    def worker(index):
        for seq in range(per_thread):
            diagnostics.record("concurrent", thread=index, seq=seq)

    workers = [threading.Thread(target=worker, args=(index,)) for index in range(threads)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(timeout=30)

    rows = _lines(diagnostics.event_log_path())
    assert len(rows) == threads * per_thread
    assert all(json.loads(row)["event"] == "concurrent" for row in rows)


# --------------------------------------------------------------------------- #
# 轮转
# --------------------------------------------------------------------------- #
def test_rotation_creates_backup(event_dir, monkeypatch):
    monkeypatch.setattr(diagnostics, "EVENT_LOG_MAX_BYTES", 400)
    for index in range(20):
        diagnostics.record("rotate", index=index)
    assert diagnostics.event_log_path().exists()
    assert diagnostics.event_log_path().with_name("events.jsonl.1").exists()


def test_rotation_keeps_at_most_keep_files(event_dir, monkeypatch):
    monkeypatch.setattr(diagnostics, "EVENT_LOG_MAX_BYTES", 400)
    for index in range(80):
        diagnostics.record("rotate", index=index)
    base = diagnostics.event_log_path()
    assert not base.with_name(f"events.jsonl.{diagnostics.EVENT_LOG_KEEP + 1}").exists()
    for index in range(1, diagnostics.EVENT_LOG_KEEP + 1):
        assert base.with_name(f"events.jsonl.{index}").exists()


def test_rotation_preserves_order_and_valid_lines(event_dir, monkeypatch):
    monkeypatch.setattr(diagnostics, "EVENT_LOG_MAX_BYTES", 400)
    for index in range(40):
        diagnostics.record("rotate", index=index)
    base = diagnostics.event_log_path()
    ordered = []
    for index in range(diagnostics.EVENT_LOG_KEEP, 0, -1):
        ordered.extend(_parsed(base.with_name(f"{base.name}.{index}")))
    ordered.extend(_parsed(base))
    assert ordered, "轮转后应当还留有事件"
    seen = [row["index"] for row in ordered]
    assert seen == sorted(seen)
    assert len(set(seen)) == len(seen)
    assert seen[-1] == 39


def test_rotation_failure_does_not_raise(event_dir, monkeypatch):
    monkeypatch.setattr(diagnostics, "EVENT_LOG_MAX_BYTES", 1)
    diagnostics.record("first")

    def boom(path):
        raise PermissionError("轮转被拒绝")

    monkeypatch.setattr(diagnostics, "_rotate_event_logs", boom)
    payload = diagnostics.record("second")
    assert payload["event"] == "second"


# --------------------------------------------------------------------------- #
# read_events
# --------------------------------------------------------------------------- #
def test_read_events_returns_oldest_to_newest(event_dir):
    for index in range(5):
        diagnostics.record("seq", index=index)
    rows = diagnostics.read_events()
    assert [row["index"] for row in rows] == [0, 1, 2, 3, 4]


def test_read_events_respects_limit(event_dir):
    for index in range(10):
        diagnostics.record("seq", index=index)
    rows = diagnostics.read_events(3)
    assert [row["index"] for row in rows] == [7, 8, 9]


def test_read_events_filters_level(event_dir):
    diagnostics.record("a", level="info")
    diagnostics.record("b", level="error")
    diagnostics.record("c", level="debug")
    rows = diagnostics.read_events(level="error")
    assert [row["event"] for row in rows] == ["b"]


def test_read_events_non_positive_limit_returns_empty(event_dir):
    diagnostics.record("a")
    assert diagnostics.read_events(0) == []
    assert diagnostics.read_events(-5) == []


def test_read_events_caps_limit_at_5000(event_dir):
    for index in range(5005):
        diagnostics.record("bulk", index=index)
    rows = diagnostics.read_events(99999)
    assert len(rows) == 5000
    assert rows[-1]["index"] == 5004


def test_read_events_missing_file_returns_empty(event_dir):
    assert diagnostics.read_events() == []


def test_read_events_skips_bad_lines(event_dir):
    diagnostics.record("good", index=0)
    path = diagnostics.event_log_path()
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{这不是 JSON\n")
        handle.write("[1, 2, 3]\n")
        handle.write("null\n")
        handle.write("\n")
    diagnostics.record("good", index=1)
    rows = diagnostics.read_events()
    assert [row["index"] for row in rows] == [0, 1]


def test_read_events_sees_rotated_files(event_dir, monkeypatch):
    monkeypatch.setattr(diagnostics, "EVENT_LOG_MAX_BYTES", 400)
    for index in range(40):
        diagnostics.record("rotate", index=index)
    rows = diagnostics.read_events(5000)
    assert len(rows) > len(_parsed(diagnostics.event_log_path()))
    seen = [row["index"] for row in rows]
    assert seen == sorted(seen)


# --------------------------------------------------------------------------- #
# clear_events / events_available
# --------------------------------------------------------------------------- #
def test_events_available_reflects_files(event_dir):
    assert diagnostics.events_available() is False
    diagnostics.record("a")
    assert diagnostics.events_available() is True


def test_clear_events_counts_files_and_is_idempotent(event_dir, monkeypatch):
    monkeypatch.setattr(diagnostics, "EVENT_LOG_MAX_BYTES", 400)
    for index in range(40):
        diagnostics.record("rotate", index=index)
    base = diagnostics.event_log_path()
    expected = len(
        [p for p in [base] + [base.with_name(f"events.jsonl.{i}") for i in (1, 2, 3)] if p.exists()]
    )
    assert expected >= 2
    assert diagnostics.clear_events() == expected
    assert diagnostics.clear_events() == 0
    assert diagnostics.events_available() is False
    assert diagnostics.read_events() == []


def test_clear_events_with_no_files_returns_zero(event_dir):
    assert diagnostics.clear_events() == 0
