"""v1.1 六个独立支撑模块的测试。

约定：不创建真实托盘图标、不注册全局热键、不写真实注册表；目录一律用
``tmp_path`` + ``monkeypatch.setenv`` 重定向，Win32 底层函数用替身注入。
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from hydrohud import autostart, diagnostics, hotkeys, playlist, settings, tray


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
@pytest.fixture()
def appdata(tmp_path, monkeypatch):
    """把 ``%APPDATA%`` 指到临时目录。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return tmp_path


def test_settings_path_uses_appdata(appdata):
    assert appdata in settings.settings_path().parents
    assert settings.settings_path().name == "settings.json"
    assert settings.settings_path().parent.name == settings.APP_DIR_NAME


def test_settings_get_set_roundtrip(appdata):
    assert settings.load() == {}
    assert settings.get("theme", "dark") == "dark"
    settings.set("theme", "light")
    settings.set("窗口宽度", 1280)
    assert settings.get("theme") == "light"
    assert settings.load() == {"theme": "light", "窗口宽度": 1280}


def test_settings_save_keeps_unicode_readable(appdata):
    settings.save({"标题": "中文标题", "路径": r"D:\推流\工程"})
    text = settings.settings_path().read_text(encoding="utf-8")
    assert "中文标题" in text
    assert "\\u4e2d" not in text
    assert text.endswith("\n")
    assert settings.from_json(text)["标题"] == "中文标题"


def test_settings_load_broken_json_returns_empty(appdata):
    settings.settings_path().write_text("{这不是 JSON", encoding="utf-8")
    assert settings.load() == {}
    assert settings.get("任意键", 42) == 42


def test_settings_to_json_handles_non_dict_text():
    assert settings.from_json("[]") == {}
    assert settings.from_json("") == {}


# --------------------------------------------------------------------------- #
# autostart
# --------------------------------------------------------------------------- #
class _FakeWinReg:
    """``winreg`` 的最小替身：只实现 autostart 用到的函数。"""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002
    REG_SZ = 1

    def __init__(self, stored=None, fail_open=False):
        self.stored = dict(stored or {})
        self.fail_open = fail_open
        self.calls = []

    def OpenKey(self, root, sub_key, reserved, access):
        self.calls.append(("OpenKey", root, sub_key, access))
        if self.fail_open:
            raise OSError(2, "找不到注册表键")
        return root, sub_key

    def QueryValueEx(self, key, name):
        self.calls.append(("QueryValueEx", name))
        try:
            return self.stored[name]
        except KeyError:
            raise OSError(2, "找不到注册表值") from None

    def SetValueEx(self, key, name, reserved, value_type, data):
        self.calls.append(("SetValueEx", name, value_type, data))
        self.stored[name] = (data, value_type)

    def DeleteValue(self, key, name):
        self.calls.append(("DeleteValue", name))
        if name not in self.stored:
            raise OSError(2, "找不到注册表值")
        del self.stored[name]

    def CloseKey(self, key):
        self.calls.append(("CloseKey",))


@pytest.fixture()
def fake_winreg(monkeypatch):
    """把 ``autostart.winreg`` 换成替身，绝不触碰真实注册表。"""
    fake = _FakeWinReg()
    monkeypatch.setattr(autostart, "winreg", fake)
    return fake


def test_build_command_frozen(monkeypatch):
    fake_sys = SimpleNamespace(frozen=True, executable=r"C:\Apps\HydroHUD.exe")
    monkeypatch.setattr(autostart, "sys", fake_sys)
    assert autostart.build_command() == r'"C:\Apps\HydroHUD.exe"'


def test_build_command_source_mode(monkeypatch):
    fake_sys = SimpleNamespace(frozen=False, executable=sys.executable)
    monkeypatch.setattr(autostart, "sys", fake_sys)
    command = autostart.build_command()
    assert command.endswith(f'"{autostart._launcher_path()}"')
    assert autostart.LAUNCHER_NAME in command


def test_build_command_autostream_and_project():
    target = Path(r"C:\Apps\HydroHUD.exe")
    command = autostart.build_command(
        target=target, autostream=True, project=Path(r"C:\projects\demo.hud")
    )
    assert command.startswith(r'"C:\Apps\HydroHUD.exe"')
    assert "--autostream" in command
    assert r'--project "C:\projects\demo.hud"' in command


def test_is_enabled_true_and_false(fake_winreg):
    assert autostart.is_enabled() is False
    fake_winreg.stored[autostart.RUN_VALUE] = (r'"C:\Apps\x.exe" --autostream', 1)
    assert autostart.is_enabled() is True
    assert autostart.read_command() == r'"C:\Apps\x.exe" --autostream'


def test_read_command_missing_key_returns_none(monkeypatch):
    monkeypatch.setattr(autostart, "winreg", _FakeWinReg(fail_open=True))
    assert autostart.read_command() is None


def test_enable_and_disable_write_fake_registry(fake_winreg):
    command = r'"C:\Apps\HydroHUD.exe" --autostream'
    autostart.enable(command)
    assert fake_winreg.stored[autostart.RUN_VALUE] == (command, fake_winreg.REG_SZ)
    assert ("SetValueEx", autostart.RUN_VALUE, fake_winreg.REG_SZ, command) in fake_winreg.calls
    autostart.disable()
    assert autostart.RUN_VALUE not in fake_winreg.stored
    autostart.disable()  # 再删一次也不该抛异常
    assert autostart.is_enabled() is False


def test_enable_rejects_empty_command(fake_winreg):
    with pytest.raises(ValueError):
        autostart.enable("   ")
    assert fake_winreg.calls == []


# --------------------------------------------------------------------------- #
# tray
# --------------------------------------------------------------------------- #
def test_tray_available_returns_bool():
    assert isinstance(tray.TrayIcon.available(), bool)


def test_tray_stop_is_idempotent():
    icon = tray.TrayIcon(None)
    icon.stop()
    icon.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="托盘只在 Windows 可用")
def test_tray_start_failure_returns_false(monkeypatch):
    notified = []

    def fake_shell_notify(flags, nid_pointer):
        notified.append(flags)
        return 0

    monkeypatch.setattr(tray.user32, "Shell_NotifyIconW", fake_shell_notify)
    # 跳过真实隐藏窗口，只验证失败路径的返回值与调用参数。
    monkeypatch.setattr(tray.TrayIcon, "_create_window", lambda self: True)
    icon = tray.TrayIcon(None)
    assert icon.available() is True
    assert icon.start() is False
    assert notified == [tray.NIM_ADD]
    icon.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="托盘只在 Windows 可用")
def test_tray_start_and_stop_with_fake_shell(monkeypatch):
    notified = []

    def fake_shell_notify(flags, nid_pointer):
        notified.append(flags)
        return 1

    def fake_create_window(self):
        self._hwnd = 1  # 假句柄，避免真的创建隐藏窗口
        return True

    monkeypatch.setattr(tray.user32, "Shell_NotifyIconW", fake_shell_notify)
    monkeypatch.setattr(tray.TrayIcon, "_create_window", fake_create_window)
    monkeypatch.setattr(tray.TrayIcon, "_destroy_window", lambda self: setattr(self, "_hwnd", None))
    icon = tray.TrayIcon(None, tooltip="HydroHUD")
    assert icon.start() is True
    assert icon.start() is True  # 重复 start 不重复注册
    assert notified == [tray.NIM_ADD]
    icon.set_tooltip("播放中")
    assert notified[-1] == tray.NIM_MODIFY
    icon.notify("提示", "测试消息")
    icon.stop()
    assert notified[-1] == tray.NIM_DELETE


# --------------------------------------------------------------------------- #
# hotkeys
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("ctrl+alt+s", (hotkeys.MOD_CONTROL | hotkeys.MOD_ALT, ord("S"))),
        ("Ctrl+Shift+2", (hotkeys.MOD_CONTROL | hotkeys.MOD_SHIFT, ord("2"))),
        ("win+e", (hotkeys.MOD_WIN, ord("E"))),
        ("alt+F5", (hotkeys.MOD_ALT, 0x70 + 4)),
        ("shift+space", (hotkeys.MOD_SHIFT, 0x20)),
    ],
)
def test_parse_spec_valid(spec, expected):
    assert hotkeys.parse_spec(spec) == expected


@pytest.mark.parametrize(
    "spec",
    ["foo+bar", "ctrl", "+a", "ctrl+ctrl+s", "ctrl+a+b", "ctrl+F25", "alt+unknown"],
)
def test_parse_spec_invalid(spec):
    assert hotkeys.parse_spec(spec) is None


def test_parse_spec_rejects_non_string():
    assert hotkeys.parse_spec(None) is None


@pytest.mark.skipif(sys.platform != "win32", reason="全局热键只在 Windows 可用")
def test_register_failure_returns_false(monkeypatch):
    def fake_register(hwnd, hotkey_id, modifiers, virtual_key):
        return 0

    monkeypatch.setattr(hotkeys.user32, "RegisterHotKey", fake_register)
    monkeypatch.setattr(hotkeys.HotkeyManager, "_ensure_window", lambda self: True)
    manager = hotkeys.HotkeyManager(None)
    manager._hwnd = 1
    assert manager.register("ctrl+alt+s", lambda: None) is False
    assert manager._callbacks == {}


def test_register_invalid_spec_returns_false():
    manager = hotkeys.HotkeyManager(None)
    assert manager.register("foo+bar", lambda: None) is False


def test_unregister_all_is_idempotent():
    manager = hotkeys.HotkeyManager(None)
    manager.unregister_all()
    manager.unregister_all()


# --------------------------------------------------------------------------- #
# playlist
# --------------------------------------------------------------------------- #
@pytest.fixture()
def playlist_appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return tmp_path


def _sample_items(tmp_path):
    return [tmp_path / "一.hud", tmp_path / "二.hud", tmp_path / "三.hud"]


def test_list_path_uses_appdata(playlist_appdata):
    path = playlist.LIST_PATH()
    assert path.name == "playlist.json"
    assert path.parent.name == "HydroHUD"
    assert playlist_appdata in path.parents


def test_playlist_advance_is_ring(tmp_path):
    items = _sample_items(tmp_path)
    queue = playlist.Playlist(items)
    assert queue.items == items
    assert queue.current() == items[0]
    assert queue.advance() == items[1]
    assert queue.advance() == items[2]
    assert queue.advance() == items[0]
    assert queue.current() == items[0]


def test_playlist_empty_advance_returns_none(tmp_path):
    queue = playlist.Playlist()
    assert queue.advance() is None
    assert queue.current() is None
    assert len(queue) == 0
    assert not queue


def test_playlist_add_remove_move(tmp_path):
    queue = playlist.Playlist()
    items = _sample_items(tmp_path)
    for item in items:
        queue.add(item)
    assert queue.remove(1) == items[1]
    assert queue.items == [items[0], items[2]]
    queue.add(items[1])
    queue.move(0, 2)
    assert queue.items == [items[2], items[1], items[0]]
    assert queue.current() == items[0]
    queue.clear()
    assert queue.items == []
    with pytest.raises(IndexError):
        queue.remove(0)


def test_playlist_negative_interval_raises(tmp_path):
    with pytest.raises(ValueError):
        playlist.Playlist(interval=-1)
    queue = playlist.Playlist()
    with pytest.raises(ValueError):
        queue.interval = -5
    queue.interval = 10.5
    assert queue.interval == 10.5


def test_playlist_to_dict_from_dict_roundtrip(tmp_path):
    queue = playlist.Playlist(_sample_items(tmp_path), interval=12.0)
    queue.advance()
    queue.advance()
    data = queue.to_dict()
    assert data["interval"] == 12.0
    restored = playlist.Playlist.from_dict(data)
    assert restored.items == queue.items
    assert restored.current() == queue.current()
    assert restored.interval == 12.0


def test_playlist_save_and_load_roundtrip(tmp_path):
    queue = playlist.Playlist(_sample_items(tmp_path), interval=15.0)
    queue.advance()
    target = tmp_path / "nested" / "playlist.json"
    queue.save(target)
    assert target.exists()
    loaded = playlist.Playlist.load(target)
    assert loaded.items == queue.items
    assert loaded.current() == queue.current()
    assert loaded.interval == 15.0


def test_playlist_load_broken_file_returns_empty(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{不是 JSON", encoding="utf-8")
    assert playlist.Playlist.load(broken).items == []
    assert playlist.Playlist.load(tmp_path / "missing.json").items == []


# --------------------------------------------------------------------------- #
# diagnostics
# --------------------------------------------------------------------------- #
@pytest.fixture()
def isolated_diagnostics(tmp_path, monkeypatch):
    """隔离日志目录、logger 与异常钩子，测试结束后还原。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    logger = diagnostics.get_logger()
    old_level = logger.level
    old_handlers = list(logger.handlers)
    old_excepthook = sys.excepthook
    old_thread_hook = getattr(threading, "excepthook", None)
    old_state = (
        diagnostics._original_excepthook,
        diagnostics._original_threading_excepthook,
        diagnostics._excepthook_installed,
    )
    yield tmp_path
    diagnostics._remove_owned_handlers(logger)
    for handler in old_handlers:
        if handler not in logger.handlers:
            logger.addHandler(handler)
    logger.setLevel(old_level)
    sys.excepthook = old_excepthook
    if old_thread_hook is not None:
        threading.excepthook = old_thread_hook
    (
        diagnostics._original_excepthook,
        diagnostics._original_threading_excepthook,
        diagnostics._excepthook_installed,
    ) = old_state


def test_log_path_under_localappdata(isolated_diagnostics):
    path = diagnostics.log_path()
    assert path.name.startswith("app-")
    assert path.name.endswith(".log")
    assert path.parent.name == "logs"
    assert path.parent.parent.name == diagnostics.APP_DIR_NAME
    assert isolated_diagnostics in path.parents


def test_install_returns_existing_file(isolated_diagnostics):
    path = diagnostics.install(excepthook=False)
    assert path.exists()
    assert path == diagnostics.log_path()


def test_install_is_idempotent(isolated_diagnostics):
    logger = diagnostics.get_logger()
    diagnostics.install(excepthook=False)
    diagnostics.install(excepthook=False)
    owned = [h for h in logger.handlers if getattr(h, diagnostics.OWNED_ATTR, False)]
    assert len(owned) == 1
    assert logger.level == logging.INFO


def test_log_exception_and_tail(isolated_diagnostics):
    diagnostics.install(excepthook=False)
    diagnostics.log_exception(ValueError("诊断测试异常"), "测试上下文")
    text = diagnostics.tail()
    assert "诊断测试异常" in text
    assert "测试上下文" in text
    assert diagnostics.tail(0) == ""


def test_tail_limits_lines(isolated_diagnostics):
    diagnostics.install(excepthook=False)
    for index in range(5):
        diagnostics.get_logger().info("第 %s 行", index)
    assert len(diagnostics.tail(2).splitlines()) == 2


def test_tail_missing_file_returns_empty(isolated_diagnostics):
    assert diagnostics.tail() == ""


def test_install_tk_handler_writes_log(isolated_diagnostics):
    diagnostics.install(excepthook=False)
    root = SimpleNamespace(report_callback_exception=None)
    diagnostics.install_tk_handler(root)
    root.report_callback_exception(RuntimeError, RuntimeError("Tk 异常"), None)
    assert "Tk 异常" in diagnostics.tail()
