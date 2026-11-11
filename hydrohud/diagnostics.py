"""日志与崩溃诊断：按天写入文件，并接管未捕获异常。

- :func:`install` 安装一个带 ``_hydrohud_owned`` 标记的 ``FileHandler``，重复调用
  会先移除自己旧 handler 再挂新的，所以不会重复写行；
- :func:`log_path` 的日期在调用时求值，跨天自动换文件，不轮转、不清理；
- 未捕获异常仍然会继续交给原始 hook（保留 stderr 输出），只是先落一份日志；
- :func:`install_tk_handler` 只写日志，不弹任何对话框。
"""

from __future__ import annotations

import datetime as _datetime
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
