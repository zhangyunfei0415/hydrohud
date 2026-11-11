"""开机自启：读写 ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``。

只操作当前用户的 Run 键，值类型固定为 ``REG_SZ``；读不到时返回 False/None，
绝不因注册表异常中断界面启动。enable/disable 的实际写操作只发生在
:func:`enable` / :func:`disable` 里，测试可整体替换 :data:`winreg` 替身。
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover - 非 Windows 平台
    winreg = None  # type: ignore[assignment]

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "LG600Studio"
LAUNCHER_NAME = "launcher.py"


def _quote(path: Path) -> str:
    """把路径包成命令行参数（始终加双引号，兼容含空格路径）。"""
    return f'"{path}"'


def _launcher_path() -> Path:
    """源码运行时 ``launcher.py`` 的路径（本文件位于 ``lg6studio/`` 下）。"""
    return Path(__file__).resolve().parent.parent / LAUNCHER_NAME


def _source_program() -> Path:
    """源码运行时优先用 ``pythonw.exe``（无控制台窗口），缺失则退回当前解释器。"""
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    return pythonw if pythonw.exists() else executable


def _program(target: Path | None) -> str:
    """返回可执行程序部分（必要时带上 ``launcher.py``）。"""
    if target is not None:
        return _quote(Path(target).expanduser())
    if getattr(sys, "frozen", False):
        return _quote(Path(sys.executable))
    return f"{_quote(_source_program())} {_quote(_launcher_path())}"


def _project_of_argv() -> Path | None:
    """从当前进程命令行里找 ``--project <path>`` / ``--project=<path>``。"""
    argv = list(sys.argv[1:])
    for index, argument in enumerate(argv):
        if argument == "--project" and index + 1 < len(argv):
            return Path(argv[index + 1]).expanduser()
        if argument.startswith("--project="):
            return Path(argument.partition("=")[2]).expanduser()
    return None


def build_command(
    target: Path | None = None,
    autostream: bool = False,
    project: Path | None = None,
) -> str:
    """拼出注册表里要写的启动命令行。

    - ``target`` 给定时直接作为可执行文件（通常是打包后的 ``LG600Studio.exe``）；
    - frozen 模式（PyInstaller）默认用 ``sys.executable``；
    - 源码模式用 ``pythonw.exe``（或当前解释器）+ 仓库根目录的 ``launcher.py``；
    - ``autostream=True`` 追加 ``--autostream``，``project`` 追加 ``--project <path>``。
    """
    parts = [_program(target)]
    if autostream:
        parts.append("--autostream")
    if project is not None:
        parts.append(f"--project {_quote(Path(project).expanduser())}")
    return " ".join(parts)


def current_command() -> str:
    """当前进程的启动命令行（沿用这次的 ``--autostream`` / ``--project`` 参数）。"""
    autostream = "--autostream" in sys.argv[1:]
    return build_command(autostream=autostream, project=_project_of_argv())


def read_command() -> str | None:
    """读 Run 项的值；键或值不存在（或平台不支持）时返回 ``None``。"""
    if winreg is None:
        return None
    key = None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
        value, _value_type = winreg.QueryValueEx(key, RUN_VALUE)
    except OSError:
        return None
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except OSError:
                pass
    text = str(value).strip()
    return text or None


def is_enabled() -> bool:
    """是否已设置开机自启；注册表读不到时返回 False，不抛异常。"""
    return read_command() is not None


def enable(command: str) -> None:
    """把命令行写进 Run 键（``REG_SZ``）。"""
    if winreg is None:
        raise RuntimeError("当前平台不支持写 Windows 注册表")
    if not command.strip():
        raise ValueError("自启命令行不能为空")
    key = None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except OSError:
                pass


def disable() -> None:
    """删除 Run 项；本来就不存在时静默返回，可重复调用。"""
    if winreg is None:
        return
    key = None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, RUN_VALUE)
    except OSError:
        pass
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except OSError:
                pass
