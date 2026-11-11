"""Windows 系统托盘图标：message-only 隐藏窗口 + ``Shell_NotifyIconW``。

设计要点：

- import 阶段只加载 DLL、设置 ``argtypes/restype``，不创建任何窗口；
- :meth:`TrayIcon.start` 才创建 message-only 窗口并注册图标；任何失败只记日志并
  返回 False，绝不向上抛异常（资源管理器未就绪时调用方可稍后重试）；
- 鼠标消息经 ``WM_APP+1`` 回调进入窗口过程，由 :meth:`TrayIcon.poll` 用
  ``PeekMessageW`` + ``DispatchMessageW`` 在主线程泵出，所以必须由 Tk 主线程用
  ``root.after(50, tray.poll)`` 驱动；
- 64 位下所有句柄按 ``c_void_p`` 传参，子类化用 ``SetWindowLongPtrW``（32 位回退
  ``SetWindowLongW``），并在 :meth:`TrayIcon.stop` 里恢复旧窗口过程。
"""

from __future__ import annotations

import ctypes
import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("hydrohud.tray")

try:  # 非 Windows 平台仍允许 import，只是不可用
    from ctypes import wintypes

    user32: Any = ctypes.WinDLL("user32", use_last_error=True)
    shell32: Any = ctypes.WinDLL("shell32", use_last_error=True)
    _WIN32 = True
except (AttributeError, OSError):  # pragma: no cover - 非 Windows 平台
    wintypes = None  # type: ignore[assignment]
    user32 = None
    shell32 = None
    _WIN32 = False

HWND_MESSAGE = -3
WM_NULL = 0x0000
WM_APP = 0x8000
WM_TRAY_CALLBACK = WM_APP + 1
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
GWLP_WNDPROC = -4

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_RIGHTBUTTON = 0x0002
TPM_LEFTALIGN = 0x0000
TPM_RETURNCMD = 0x0100
PM_REMOVE = 0x0001

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
IDI_APPLICATION = 32512

MENU_SHOW = 1
MENU_TOGGLE = 2
MENU_ROTATE = 3
MENU_QUIT = 4

if _WIN32:
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT, ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t
    )

    class NOTIFYICONDATAW(ctypes.Structure):
        """``Shell_NotifyIconW`` 的数据结构（完整 v3 尺寸）。"""

        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", ctypes.c_void_p),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", ctypes.c_void_p),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", ctypes.c_void_p),
        ]

    # Shell_NotifyIconW 实际导出在 shell32.dll；按任务约定挂在 user32 命名空间上，
    # 这样测试可以统一用 ``tray.user32.Shell_NotifyIconW`` 注入失败。
    user32.Shell_NotifyIconW = shell32.Shell_NotifyIconW

    user32.Shell_NotifyIconW.argtypes = (wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
    user32.Shell_NotifyIconW.restype = wintypes.BOOL

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

    user32.LoadIconW.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    user32.LoadIconW.restype = ctypes.c_void_p

    user32.LoadImageW.argtypes = (
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    )
    user32.LoadImageW.restype = ctypes.c_void_p

    user32.CreatePopupMenu.argtypes = ()
    user32.CreatePopupMenu.restype = ctypes.c_void_p

    user32.AppendMenuW.argtypes = (
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.c_size_t,
        wintypes.LPCWSTR,
    )
    user32.AppendMenuW.restype = wintypes.BOOL

    user32.DestroyMenu.argtypes = (ctypes.c_void_p,)
    user32.DestroyMenu.restype = wintypes.BOOL

    user32.TrackPopupMenu.argtypes = (
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    user32.TrackPopupMenu.restype = ctypes.c_int

    user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
    user32.GetCursorPos.restype = wintypes.BOOL

    user32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
    user32.SetForegroundWindow.restype = wintypes.BOOL

    user32.PostMessageW.argtypes = (
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
    )
    user32.PostMessageW.restype = wintypes.BOOL

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


def _clip(text: Any, limit: int) -> str:
    """截断文本到 Win32 结构体允许的宽字符数（不含结尾 NUL）。"""
    return str(text)[:limit]


def _last_error() -> int:
    """取最近一次 Win32 调用的错误码（不抛异常）。"""
    try:
        return ctypes.get_last_error()
    except (AttributeError, OSError):  # pragma: no cover - 理论不可达
        return 0


class TrayIcon:
    """系统托盘图标；所有回调都在 Tk 主线程的 :meth:`poll` 中执行。"""

    def __init__(
        self,
        root: Any,
        icon_path: Path | None = None,
        tooltip: str = "HydroHUD",
        on_show: Callable[[], Any] | None = None,
        on_toggle: Callable[[], Any] | None = None,
        on_quit: Callable[[], Any] | None = None,
        on_rotate: Callable[[], Any] | None = None,
    ) -> None:
        self._root = root
        self._icon_path = Path(icon_path).expanduser() if icon_path is not None else None
        self._tooltip = _clip(tooltip, 127)
        self._on_show = on_show
        self._on_toggle = on_toggle
        self._on_quit = on_quit
        self._on_rotate = on_rotate
        self._hwnd: int | None = None
        self._icon: int | None = None
        self._old_wndproc: int | None = None
        self._wndproc: Any = None
        self._started = False

    @classmethod
    def available(cls) -> bool:
        """探测托盘 API 是否可用；只查符号，不创建窗口，不抛异常。"""
        try:
            return bool(_WIN32 and user32 is not None and user32.Shell_NotifyIconW)
        except (AttributeError, OSError):
            return False

    def start(self) -> bool:
        """创建隐藏窗口并注册托盘图标；成功返回 True，失败记日志并返回 False。"""
        if self._started:
            return True
        if not self.available():
            logger.warning("托盘图标不可用：缺少 Shell_NotifyIconW")
            return False
        try:
            if not self._create_window():
                return False
            self._icon = self._load_icon()
            if not self._notify(NIM_ADD, NIF_MESSAGE | NIF_ICON | NIF_TIP):
                logger.warning("注册托盘图标失败，稍后可重试")
                self._destroy_window()
                return False
        except Exception:  # 原生调用可能抛 ctypes.ArgumentError / OSError
            logger.exception("创建托盘图标时发生异常")
            self._destroy_window()
            return False
        self._started = True
        return True

    def stop(self) -> None:
        """删除托盘图标并销毁隐藏窗口；可重复调用，不抛异常。"""
        if self._started and self._hwnd:
            try:
                self._notify(NIM_DELETE, NIF_MESSAGE | NIF_ICON | NIF_TIP)
            except Exception:
                logger.exception("删除托盘图标失败")
        self._started = False
        try:
            self._destroy_window()
        except Exception:  # pragma: no cover - 防御性兜底
            logger.exception("销毁托盘隐藏窗口失败")

    def poll(self) -> None:
        """泵出托盘窗口消息；由 ``root.after(50, tray.poll)`` 周期调用。"""
        if self._hwnd is None:
            return
        try:
            message = wintypes.MSG()
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception:
            logger.exception("托盘消息轮询失败")

    def set_tooltip(self, text: str) -> None:
        """更新悬停提示；未启动时只记下来，启动后立即生效。"""
        self._tooltip = _clip(text, 127)
        if not self._started:
            return
        try:
            self._notify(NIM_MODIFY, NIF_TIP)
        except Exception:
            logger.exception("更新托盘提示失败")

    def notify(self, title: str, message: str) -> None:
        """弹出气泡通知；未启动时静默返回。"""
        if not self._started:
            logger.debug("托盘未启动，忽略通知：%s", title)
            return
        try:
            nid = self._build_nid(NIF_INFO)
            nid.szInfoTitle = _clip(title, 63)
            nid.szInfo = _clip(message, 255)
            nid.dwInfoFlags = NIIF_INFO
            user32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            logger.exception("发送托盘通知失败")

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    def _build_nid(self, nif_flags: int) -> "NOTIFYICONDATAW":
        """按当前状态填一个 ``NOTIFYICONDATAW``。"""
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = ctypes.c_void_p(self._hwnd or 0)
        nid.uID = 1
        nid.uFlags = nif_flags
        nid.uCallbackMessage = WM_TRAY_CALLBACK
        nid.hIcon = ctypes.c_void_p(self._icon or 0)
        nid.szTip = self._tooltip
        return nid

    def _notify(self, flags: int, nif_flags: int) -> bool:
        """调用 ``Shell_NotifyIconW`` 并返回是否成功。"""
        nid = self._build_nid(nif_flags)
        return bool(user32.Shell_NotifyIconW(flags, ctypes.byref(nid)))

    def _create_window(self) -> bool:
        """创建 message-only 隐藏窗口并子类化窗口过程。"""
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
            logger.warning("创建托盘隐藏窗口失败：错误码 %s", _last_error())
            return False
        self._hwnd = int(hwnd)
        self._wndproc = WNDPROC(self._window_proc)  # 必须持有引用，否则回调会被 GC
        old = SetWindowLongPtrW(
            self._hwnd, GWLP_WNDPROC, ctypes.cast(self._wndproc, ctypes.c_void_p)
        )
        if not old:
            logger.warning("子类化托盘窗口失败：错误码 %s", _last_error())
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
            logger.exception("恢复托盘窗口过程失败")
        finally:
            self._old_wndproc = None
            try:
                user32.DestroyWindow(hwnd)
            except Exception:  # pragma: no cover - 防御性兜底
                logger.exception("销毁托盘隐藏窗口失败")
            self._wndproc = None

    def _load_icon(self) -> int | None:
        """加载图标：优先 ``icon_path``，失败或未提供时用系统默认图标。"""
        if self._icon_path is not None:
            try:
                icon = user32.LoadImageW(
                    None, str(self._icon_path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE
                )
            except Exception:
                logger.exception("加载图标文件失败：%s", self._icon_path)
                icon = None
            if icon:
                return int(icon)
        try:
            return int(user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION)) or 0) or None
        except Exception:  # pragma: no cover - 防御性兜底
            logger.exception("加载系统默认图标失败")
            return None

    def _window_proc(self, hwnd, message, wparam, lparam):  # noqa: D401 - Win32 回调
        """窗口过程：把托盘回调消息转给 Python 回调，其余交给默认过程。"""
        try:
            if message == WM_TRAY_CALLBACK:
                self._handle_mouse_message(int(lparam))
                return 0
        except Exception:
            logger.exception("处理托盘鼠标消息失败")
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _handle_mouse_message(self, mouse_message: int) -> None:
        """按鼠标消息类型分发：双击显示窗口，右键弹菜单。"""
        if mouse_message == WM_LBUTTONDBLCLK:
            self._call(self._on_show)
        elif mouse_message == WM_RBUTTONUP:
            self._show_menu()

    def _show_menu(self) -> None:
        """在鼠标位置弹出托盘菜单并分发命令。"""
        menu = user32.CreatePopupMenu()
        if not menu:
            logger.warning("创建托盘菜单失败")
            return
        try:
            user32.AppendMenuW(menu, MF_STRING, MENU_SHOW, "显示窗口")
            user32.AppendMenuW(menu, MF_STRING, MENU_TOGGLE, "开始或停止推流")
            if self._on_rotate is not None:
                user32.AppendMenuW(menu, MF_STRING, MENU_ROTATE, "下一个工程")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, MENU_QUIT, "退出")
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(self._hwnd)
            command = user32.TrackPopupMenu(
                menu,
                TPM_RIGHTBUTTON | TPM_LEFTALIGN | TPM_RETURNCMD,
                point.x,
                point.y,
                0,
                self._hwnd,
                None,
            )
            # 让菜单在点击别处时正常收起（经典托盘菜单写法）。
            user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
            if command:
                self._dispatch_menu(int(command))
        except Exception:
            logger.exception("弹出托盘菜单失败")
        finally:
            user32.DestroyMenu(menu)

    def _dispatch_menu(self, command: int) -> None:
        """把菜单命令映射到构造时传入的回调。"""
        callbacks = {
            MENU_SHOW: self._on_show,
            MENU_TOGGLE: self._on_toggle,
            MENU_ROTATE: self._on_rotate,
            MENU_QUIT: self._on_quit,
        }
        self._call(callbacks.get(command))

    @staticmethod
    def _call(callback: Callable[[], Any] | None) -> None:
        """安全执行用户回调：异常只记日志，不破坏窗口过程。"""
        if callback is None:
            return
        try:
            callback()
        except Exception:
            logger.exception("托盘回调执行失败")
