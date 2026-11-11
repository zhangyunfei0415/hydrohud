"""全局热键：message-only 隐藏窗口 + ``RegisterHotKey``。

设计要点：

- import 阶段不创建窗口、不注册任何热键；
- :meth:`HotkeyManager.register` 才懒创建隐藏窗口并注册热键，返回 False 表示
  组合键非法或已被其它程序占用，不抛异常；
- ``WM_HOTKEY`` 由窗口过程分发到对应回调，:meth:`HotkeyManager.poll` 用
  ``PeekMessageW`` + ``DispatchMessageW`` 在主线程泵消息，需由
  ``root.after(60, hotkeys.poll)`` 驱动；
- :meth:`HotkeyManager.unregister_all` 注销全部热键并销毁窗口，可重复调用。
"""

from __future__ import annotations

import ctypes
import logging
from typing import Any, Callable

logger = logging.getLogger("hydrohud.hotkeys")

try:  # 非 Windows 平台仍允许 import，只是不可用
    from ctypes import wintypes

    user32: Any = ctypes.WinDLL("user32", use_last_error=True)
    _WIN32 = True
except (AttributeError, OSError):  # pragma: no cover - 非 Windows 平台
    wintypes = None  # type: ignore[assignment]
    user32 = None
    _WIN32 = False

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

WM_HOTKEY = 0x0312
GWLP_WNDPROC = -4
HWND_MESSAGE = -3
PM_REMOVE = 0x0001

_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
    "meta": MOD_WIN,
    "super": MOD_WIN,
}

# 具名键 -> 虚拟键码（字母、数字键直接在 :func:`_key_code` 里按 ASCII 处理）。
_KEY_NAMES = {
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "bksp": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "ins": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "pause": 0x13,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
}

if _WIN32:
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT, ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t
    )

    user32.CreateWindowExW.argtypes = (
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    user32.CreateWindowExW.restype = ctypes.c_void_p

    user32.DestroyWindow.argtypes = (ctypes.c_void_p,)
    user32.DestroyWindow.restype = wintypes.BOOL

    user32.DefWindowProcW.argtypes = (
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
    )
    user32.DefWindowProcW.restype = LRESULT

    SetWindowLongPtrW = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    SetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
    SetWindowLongPtrW.restype = ctypes.c_void_p

    user32.RegisterHotKey.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        wintypes.UINT,
        wintypes.UINT,
    )
    user32.RegisterHotKey.restype = wintypes.BOOL

    user32.UnregisterHotKey.argtypes = (ctypes.c_void_p, ctypes.c_int)
    user32.UnregisterHotKey.restype = wintypes.BOOL

    user32.PeekMessageW.argtypes = (
        ctypes.POINTER(wintypes.MSG),
        ctypes.c_void_p,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    )
    user32.PeekMessageW.restype = wintypes.BOOL

    user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
    user32.TranslateMessage.restype = wintypes.BOOL

    user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
    user32.DispatchMessageW.restype = LRESULT


def _last_error() -> int:
    """取最近一次 Win32 调用的错误码（不抛异常）。"""
    try:
        return ctypes.get_last_error()
    except (AttributeError, OSError):  # pragma: no cover - 理论不可达
        return 0


def _key_code(token: str) -> int | None:
    """把单个按键 token 转成虚拟键码；无法识别返回 ``None``。"""
    if len(token) == 1 and token.isascii() and token.isalnum():
        return ord(token.upper())
    if token in _KEY_NAMES:
        return _KEY_NAMES[token]
    if token.startswith("f") and token[1:].isdigit():
        number = int(token[1:])
        if 1 <= number <= 24:
            return 0x70 + number - 1
    return None


def parse_spec(spec: str) -> tuple[int, int] | None:
    """解析 ``ctrl+alt+s`` 形式的组合键为 ``(修饰键掩码, 虚拟键码)``。

    大小写不敏感；``win`` / ``meta`` / ``super`` 都表示 Win 键；空串、未知键名、
    多个主键、重复修饰键都返回 ``None``。
    """
    if not isinstance(spec, str):
        return None
    tokens = [token.strip().lower() for token in spec.split("+")]
    if not tokens or any(not token for token in tokens):
        return None
    modifiers = 0
    virtual_key: int | None = None
    seen: set[int] = set()
    for token in tokens:
        if token in _MODIFIERS:
            bit = _MODIFIERS[token]
            if bit in seen:
                return None
            seen.add(bit)
            modifiers |= bit
            continue
        if virtual_key is not None:
            return None
        virtual_key = _key_code(token)
        if virtual_key is None:
            return None
    if virtual_key is None:
        return None
    return modifiers, virtual_key


def _win32_ready() -> bool:
    """探测注册热键所需的 Win32 API 是否可用（不抛异常）。"""
    try:
        return bool(_WIN32 and user32 is not None and user32.RegisterHotKey)
    except (AttributeError, OSError):
        return False


class HotkeyManager:
    """注册全局热键，并在 Tk 主线程的 :meth:`poll` 里分发回调。"""

    def __init__(self, root: Any) -> None:
        self._root = root
        self._hwnd: int | None = None
        self._old_wndproc: int | None = None
        self._wndproc: Any = None
        self._next_id = 1
        self._callbacks: dict[int, Callable[[], Any]] = {}

    def register(self, spec: str, callback: Callable[[], Any]) -> bool:
        """注册热键；``spec`` 非法或已被占用时返回 False，不抛异常。"""
        parsed = parse_spec(spec)
        if parsed is None:
            logger.warning("无法解析热键：%r", spec)
            return False
        if not _win32_ready():
            logger.warning("全局热键不可用：缺少 RegisterHotKey")
            return False
        try:
            if not self._ensure_window():
                return False
            modifiers, virtual_key = parsed
            hotkey_id = self._next_id
            if not user32.RegisterHotKey(self._hwnd, hotkey_id, modifiers, virtual_key):
                logger.warning("热键 %s 注册失败（可能已被占用），错误码 %s", spec, _last_error())
                return False
            self._next_id += 1
            self._callbacks[hotkey_id] = callback
            return True
        except Exception:
            logger.exception("注册热键 %s 时发生异常", spec)
            return False

    def unregister_all(self) -> None:
        """注销全部热键并销毁隐藏窗口；可重复调用，不抛异常。"""
        hwnd = self._hwnd
        for hotkey_id in list(self._callbacks):
            if hwnd is not None:
                try:
                    user32.UnregisterHotKey(hwnd, hotkey_id)
                except Exception:
                    logger.exception("注销热键 %s 失败", hotkey_id)
        self._callbacks.clear()
        try:
            self._destroy_window()
        except Exception:  # pragma: no cover - 防御性兜底
            logger.exception("销毁热键隐藏窗口失败")

    def poll(self) -> None:
        """泵出热键窗口消息；由 ``root.after(60, hotkeys.poll)`` 周期调用。"""
        if self._hwnd is None:
            return
        try:
            message = wintypes.MSG()
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception:
            logger.exception("热键消息轮询失败")

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    def _ensure_window(self) -> bool:
        """懒创建 message-only 隐藏窗口并子类化窗口过程。"""
        if self._hwnd is not None:
            return True
        hwnd = user32.CreateWindowExW(
            0,
            "STATIC",
            "",
            0,
            0,
            0,
            0,
            0,
            ctypes.c_void_p(HWND_MESSAGE),
            None,
            None,
            None,
        )
        if not hwnd:
            logger.warning("创建热键隐藏窗口失败：错误码 %s", _last_error())
            return False
        self._hwnd = int(hwnd)
        self._wndproc = WNDPROC(self._window_proc)  # 必须持有引用，否则回调会被 GC
        old = SetWindowLongPtrW(
            self._hwnd, GWLP_WNDPROC, ctypes.cast(self._wndproc, ctypes.c_void_p)
        )
        if not old:
            logger.warning("子类化热键窗口失败：错误码 %s", _last_error())
            self._destroy_window()
            return False
        self._old_wndproc = int(old)
        return True

    def _destroy_window(self) -> None:
        """恢复旧窗口过程并销毁隐藏窗口；可重复调用。"""
        hwnd = self._hwnd
        self._hwnd = None
        if hwnd is None:
            return
        try:
            if self._old_wndproc:
                SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.c_void_p(self._old_wndproc))
        except Exception:
            logger.exception("恢复热键窗口过程失败")
        finally:
            self._old_wndproc = None
            try:
                user32.DestroyWindow(hwnd)
            except Exception:  # pragma: no cover - 防御性兜底
                logger.exception("销毁热键隐藏窗口失败")
            self._wndproc = None

    def _window_proc(self, hwnd, message, wparam, lparam):  # noqa: D401 - Win32 回调
        """窗口过程：``WM_HOTKEY`` 按 id 分发回调，其余交给默认过程。"""
        try:
            if message == WM_HOTKEY:
                callback = self._callbacks.get(int(wparam))
                if callback is not None:
                    try:
                        callback()
                    except Exception:
                        logger.exception("热键回调执行失败")
                return 0
        except Exception:
            logger.exception("处理热键消息失败")
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)
