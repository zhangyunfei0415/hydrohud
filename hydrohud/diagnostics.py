"""日志与崩溃诊断：按天写入文件，并接管未捕获异常。

- :func:`install` 安装一个带 ``_hydrohud_owned`` 标记的 ``FileHandler``，重复调用
  会先移除自己旧 handler 再挂新的，所以不会重复写行；
- :func:`log_path` 的日期在调用时求值，跨天自动换文件，不轮转、不清理；
- 未捕获异常仍然会继续交给原始 hook（保留 stderr 输出），只是先落一份日志；
- :func:`install_tk_handler` 只写日志，不弹任何对话框；
- :func:`record` 另行维护一份结构化 JSONL 事件日志（按大小轮转），与上述
  纯文本日志互不影响。
"""

from __future__ import annotations

import datetime as _datetime
import json
import logging
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
APP_DIR_NAME = "HydroHUD"
LOG_DIR_NAME = "logs"
FILE_PREFIX = "app-"
OWNED_ATTR = "_hydrohud_owned"

# 结构化事件日志（JSONL）：独立于上面的按天纯文本日志。
EVENT_LOG_FILE_NAME = "events.jsonl"
EVENT_LOG_MAX_BYTES = 512 * 1024
EVENT_LOG_KEEP = 3
EVENT_LEVELS = ("debug", "info", "warning", "error")

# 单条事件序列化后的字节上限；超过时截断最长的字符串字段。
EVENT_LINE_MAX_BYTES = 8 * 1024
# 被截断的字符串字段保留的字符数。
EVENT_FIELD_MAX_CHARS = 2000
EVENT_TRUNCATED_SUFFIX = "…(截断)"

# 事件文件的读、写、轮转、清理都走这把锁，保证并发写入不撕裂行。
_event_lock = threading.Lock()

# 首次 install 时记下的原始钩子，重复 install 不再嵌套包装。
_original_excepthook: Any = None
_original_threading_excepthook: Any = None
_excepthook_installed = False


def LOG_DIR() -> Path:
    """日志目录：``%LOCALAPPDATA%\\HydroHUD\\logs``。"""
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / APP_DIR_NAME / LOG_DIR_NAME


def log_path() -> Path:
    """当天日志文件：``logs\\app-YYYYMMDD.log``。"""
    stamp = _datetime.date.today().strftime("%Y%m%d")
    return LOG_DIR() / f"{FILE_PREFIX}{stamp}.log"


def get_logger(name: str = "hydrohud") -> logging.Logger:
    """取日志记录器；只返回对象，不挂 handler、不改全局配置。"""
    return logging.getLogger(name)


def _remove_owned_handlers(logger: logging.Logger) -> None:
    """移除本模块先前挂上的 handler，关闭其文件句柄。"""
    for handler in list(logger.handlers):
        if getattr(handler, OWNED_ATTR, False):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # pragma: no cover - 关闭失败不影响日志可用性
                pass


def install(excepthook: bool = True) -> Path:
    """安装文件日志（可选接管未捕获异常），返回当天日志文件路径。幂等。"""
    global _original_excepthook, _original_threading_excepthook, _excepthook_installed

    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = get_logger()
    logger.setLevel(logging.INFO)
    _remove_owned_handlers(logger)

    handler = logging.FileHandler(path, mode="a", encoding="utf-8", delay=False)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    setattr(handler, OWNED_ATTR, True)
    logger.addHandler(handler)

    if excepthook:
        if not _excepthook_installed:
            _original_excepthook = sys.excepthook
            _original_threading_excepthook = getattr(threading, "excepthook", None)
            _excepthook_installed = True
        sys.excepthook = _excepthook
        if hasattr(threading, "excepthook"):
            threading.excepthook = _threading_excepthook
    return path


def install_tk_handler(root: Any) -> None:
    """把 Tk 回调异常写入日志；保持原 ``report_callback_exception`` 的静默行为。"""

    def _handler(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
        log_exception(exc_value, "Tk 回调异常")
        if exc_tb is None:
            return
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        get_logger().debug("%s", detail)

    root.report_callback_exception = _handler


def log_exception(exc: BaseException | None, context: str = "") -> None:
    """把异常（含堆栈）写入日志；``exc`` 为空时只记上下文。"""
    logger = get_logger()
    prefix = f"{context}: " if context else ""
    if exc is None:
        logger.error("%s未知异常", prefix)
        return
    logger.error("%s%s", prefix, exc, exc_info=exc)


def tail(lines: int = 200) -> str:
    """返回当天日志最后 ``lines`` 行（不含结尾换行）；文件不存在返回空串。"""
    count = int(lines)
    if count <= 0:
        return ""
    path = log_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    rows = text.splitlines()
    return "\n".join(rows[-count:])


def _excepthook(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
    """未捕获异常：先落日志，再交给原始 hook（默认打印到 stderr）。"""
    try:
        log_exception(exc_value, "未捕获异常")
    except Exception:  # pragma: no cover - 日志失败时不能再套娃
        pass
    original = _original_excepthook
    if original is not None and original is not _excepthook:
        original(exc_type, exc_value, exc_tb)
    else:
        sys.__excepthook__(exc_type, exc_value, exc_tb)


def _threading_excepthook(args: Any) -> None:
    """线程未捕获异常：先落日志，再交给原始 hook。"""
    thread_name = getattr(getattr(args, "thread", None), "name", "?")
    try:
        log_exception(getattr(args, "exc_value", None), f"线程 {thread_name} 异常")
    except Exception:  # pragma: no cover - 日志失败时不能再套娃
        pass
    original = _original_threading_excepthook
    if original is not None and original is not _threading_excepthook:
        original(args)
    elif hasattr(threading, "__excepthook__"):  # pragma: no branch - 标准库总有
        threading.__excepthook__(args)


# --------------------------------------------------------------------------- #
# 结构化事件日志（JSONL）
# --------------------------------------------------------------------------- #
def event_log_path() -> Path:
    """事件日志主文件：``%LOCALAPPDATA%\\HydroHUD\\logs\\events.jsonl``。"""
    return LOG_DIR() / EVENT_LOG_FILE_NAME


def _coerce_event_value(value: Any) -> Any:
    """把任意值转成 JSON 安全值；不可序列化的对象退回 ``repr()`` 字符串。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_coerce_event_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _coerce_event_value(item) for key, item in value.items()}
    return repr(value)


def _encode_event_line(payload: dict) -> bytes:
    """把一条事件编码成一行 UTF-8 JSONL（含换行）。"""
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _longest_string_slot(node: Any) -> tuple[Any, Any] | None:
    """找出嵌套结构里最长的字符串值，返回 ``(容器, 键或下标)``。"""
    best: tuple[Any, Any] | None = None
    best_len = EVENT_FIELD_MAX_CHARS
    stack: list[Any] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if isinstance(value, str):
                    if len(value) > best_len:
                        best_len = len(value)
                        best = (current, key)
                else:
                    stack.append(value)
        elif isinstance(current, list):
            for index, value in enumerate(current):
                if isinstance(value, str):
                    if len(value) > best_len:
                        best_len = len(value)
                        best = (current, index)
                else:
                    stack.append(value)
    return best


def _truncate_event_fields(payload: dict) -> None:
    """反复截断最长字符串字段，直到整行不超过 ``EVENT_LINE_MAX_BYTES``。"""
    while len(_encode_event_line(payload)) > EVENT_LINE_MAX_BYTES:
        slot = _longest_string_slot(payload)
        if slot is None:
            return
        container, key = slot
        container[key] = container[key][:EVENT_FIELD_MAX_CHARS] + EVENT_TRUNCATED_SUFFIX


def _event_log_files() -> list[Path]:
    """主文件 + 所有轮转份（``.1`` 最新 … ``.KEEP`` 最旧）。"""
    base = event_log_path()
    return [base] + [
        base.with_name(f"{base.name}.{index}") for index in range(1, EVENT_LOG_KEEP + 1)
    ]


def _rotate_event_logs(path: Path) -> None:
    """轮转：删掉最旧份，``.N-1``→``.N``，主文件→``.1``。"""
    keep = EVENT_LOG_KEEP
    if keep <= 0:
        path.unlink(missing_ok=True)
        return
    oldest = path.with_name(f"{path.name}.{keep}")
    if oldest.exists():
        oldest.unlink()
    for index in range(keep - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(path.with_name(f"{path.name}.{index + 1}"))
    if path.exists():
        path.replace(path.with_name(f"{path.name}.1"))


def record(event: str, /, *, level: str = "info", **fields: Any) -> dict:
    """记录一条结构化事件，返回实际构造出的 dict。

    一行一条 JSON：``{"ts": ..., "level": ..., "event": ..., **fields}``。
    ``ts`` 为带毫秒的本地时区 ISO8601；``level`` 不在 :data:`EVENT_LEVELS`
    里时降级为 ``info``；值统一转成 JSON 安全类型，绝不因不可序列化而抛异常；
    单条超过 8 KB 时截断最长的字符串字段。写盘失败只记 debug，不抛。
    """
    safe_level = level if level in EVENT_LEVELS else "info"
    payload: dict[str, Any] = {
        "ts": _datetime.datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "level": safe_level,
        "event": event if isinstance(event, str) else str(event),
    }
    for key, value in fields.items():
        payload[str(key)] = _coerce_event_value(value)

    try:
        _truncate_event_fields(payload)
        line = _encode_event_line(payload)
    except Exception as exc:  # pragma: no cover - 序列化兜底，正常值不会到这里
        get_logger().debug("结构化事件序列化失败: %s", exc)
        return payload

    path = event_log_path()
    with _event_lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            get_logger().debug("结构化事件目录创建失败: %s", exc)
        try:
            size = path.stat().st_size if path.exists() else 0
        except OSError:
            size = 0
        if path.exists() and size + len(line) > EVENT_LOG_MAX_BYTES:
            try:
                _rotate_event_logs(path)
            except Exception as exc:
                get_logger().debug("结构化事件轮转失败: %s", exc)
        try:
            with path.open("ab") as handle:
                handle.write(line)
        except Exception as exc:
            get_logger().debug("结构化事件写入失败: %s", exc)
    return payload


def _read_event_file(path: Path) -> list[dict]:
    """读单个事件文件，跳过空行、坏 JSON 与非 dict 行。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows: list[dict] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def read_events(limit: int = 200, *, level: str | None = None) -> list[dict]:
    """读取最近的结构化事件，按时间顺序（旧→新）返回。

    为控制开销只读主文件与最新的 2 份轮转（``events.jsonl``、
    ``events.jsonl.1``、``events.jsonl.2``），更旧的 ``.3`` 不读；
    坏行跳过，不抛异常。``limit`` 上限 5000，``limit <= 0`` 返回空列表；
    ``level`` 给定时只保留该等级（精确匹配）。
    """
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 0
    if cap <= 0:
        return []
    cap = min(cap, 5000)

    base = event_log_path()
    collected: list[dict] = []
    # 旧→新：先最旧的轮转份，最后主文件。
    for index in range(min(2, EVENT_LOG_KEEP), -1, -1):
        path = base if index == 0 else base.with_name(f"{base.name}.{index}")
        collected.extend(_read_event_file(path))
    if level is not None:
        collected = [row for row in collected if row.get("level") == level]
    return collected[-cap:]


def clear_events() -> int:
    """删掉主文件与所有轮转份，返回成功删掉的文件数；幂等。"""
    removed = 0
    with _event_lock:
        for path in _event_log_files():
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                get_logger().debug("删除结构化事件文件失败: %s", exc)
                continue
            removed += 1
    return removed


def events_available() -> bool:
    """主文件或任一轮转份存在时返回 True。"""
    return any(path.exists() for path in _event_log_files())
