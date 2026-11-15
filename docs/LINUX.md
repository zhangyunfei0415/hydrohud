# Linux 支持（v1.4）

HydroHUD 的协议实现分两层：`hydrohud/device.py` 负责帧格式、JPEG 编码和设备命令，
`hydrohud/transport.py` 负责把一条条 output report 写进设备。目前**传输层的 Linux 实现已经落地**，
`hydrohud/devices.py` 的跨型号设备表也已完成；但 `device.py` / `app.py` 打开设备时仍只走
Windows 的 `hid.dll`，还没有切到 `HidrawTransport`。也就是说：

- 已就位：设备表（型号 / PID / 报文长度 / 分包大小）、Linux hidraw 传输类、sysfs 枚举与解析测试。
- 尚未就位：Linux 上的端到端推流。把 `Screen` / `Control` 的打开路径接到 `HidrawTransport`
  是 v1.4 剩下的工作；在那之前 Linux 上可以启动界面、渲染预览，但画面到不了屏幕。

## 为什么 Linux 走 hidraw

Linux 的 HID 子系统为每个 HID collection 导出一个字符设备 `/dev/hidrawN`，可以直接
`open()`/`write()`/`read()`：

- **写**：一次 `write()` 写入的就是一条完整 output report，缓冲区首字节是 report id（本设备为 `0x00`），
  与 Windows `WriteFile` 到 hid 句柄的语义一致，所以帧构造（`device.build_packets()`）和 JPEG
  编码（`device.encode_jpeg()`）可以原样复用，不需要第二套协议实现。
- **读**：hidraw 把 report id 一并返回（控制口查询回 441 字节）；Windows 的 cython-hidapi 会剥掉
  report id（同样查询回 440 字节）。因此解析回复时必须对两种长度都做防御，不能硬编码偏移。
- **枚举**：不需要 `libusb`/`hidapi` 这类额外依赖，直接读 sysfs 即可。

`hydrohud/transport.py` 提供的接口：

| 名称 | 用途 |
| --- | --- |
| `Transport` | 协议：`path`、`is_open`、`open()`、`close()`、`write_report(data)`、`read_report(length, timeout=1.0)` |
| `HidrawTransport(path, report_len)` | 打开 `/dev/hidrawN`；非 Linux 平台 `open()` 抛 `RuntimeError` |
| `FakeTransport` | 内存队列，测试与抓包回放用 |
| `enumerate_hidraw(vendor_id, product_id, sysfs_root=None)` | 扫 `<root>/class/hidraw/*/device/uevent`，按 `HID_ID` 过滤，从 `HID_PHYS` 的 `MI_XX` 取接口号；`root` 可用 `HYDROHUD_SYSFS_ROOT` 覆盖，测试据此指向临时目录 |
| `report_len_for(path)` | 尽力而为；hidraw 不暴露解析后的报表长度，所以返回 `None`，长度一律从设备表取 |

设备表 `hydrohud/devices.py` 目前有两项：

| 型号 | VID:PID | 状态 | 图像口 | 控制口 | 屏幕 | 单帧上限 |
| --- | --- | --- | --- | --- | --- | --- |
| LG600 | `1a2c:1520` | 已验证 | 1025 B（payload 1020 B） | 441 B（chunk 436 B） | 480×480 JPEG | 46 包 × 1020 = 46920 B |
| LG700 | `1a2c:151c` | 未实测（协议推定与 LG600 相同） | 同上 | 同上 | 同上 | 同上 |

帧预算不要超：超过 46920 字节的帧没有实测过，而错误分辨率（例如 640×640）会让固件卡死并从
USB 总线掉线。屏幕还有约 10 秒看门狗，停止推流会熄屏，因此静态内容也要周期重发。

## 安装 udev 规则

普通用户默认只能读 hidraw 节点，需要一条 udev 规则放开写权限。仓库里已经带了规则
`packaging/linux/99-hydrohud-lg600.rules`（同时覆盖 `1a2c:1520` 与 `1a2c:151c`），两种安装方式：

```bash
# 方式一：手动复制
sudo cp packaging/linux/99-hydrohud-lg600.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger

# 方式二：用仓库自带的幂等脚本（root 检查 + 复制 + reload + trigger）
sudo bash packaging/linux/install-udev.sh
```

验证：

```bash
# 应看到 crw-rw---- root plugdev 的 hidraw 节点（带 uaccess ACL 时还会有 user:<你> 的 ACL 条目）
ls -l /dev/hidraw*

# 无图形界面的最小检查
python3 -c "from hydrohud.transport import enumerate_hidraw; print(enumerate_hidraw(0x1A2C, 0x1520))"
```

如果系统没有 systemd-logind 的 `uaccess`，规则里的 `MODE="0660"` + `GROUP="plugdev"` 会兜底：
把当前用户加入该组即可（`sudo usermod -aG plugdev "$USER"`，重新登录后生效）。

## 从源码运行

系统依赖：Python 3.10+、Tkinter（Debian/Ubuntu 上是 `python3-tk`，Fedora 上是 `python3-tkinter`）、
`python3-venv`/`pip`。Python 包即 `requirements.txt`：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m hydrohud
```

`requirements.txt` 里的 `pyinstaller` 只在打包 Windows 单文件 exe 时用到，Linux 上可以不管它；
`imageio` / `imageio-ffmpeg` 用于视频/GIF 背景，缺了只是对应图层不可用。无头环境可以用
`--smoke-shot out.png` 做一次渲染自检，但 Tk 仍需要一个显示（X11 或 Wayland）。

## 已知限制（如实）

- **还不能推流到屏幕**：见文首，`device.py` / `app.py` 尚未接 `HidrawTransport`。
- **托盘图标禁用**：`tray.available()` 依赖 `Shell_NotifyIconW`，Linux 上返回 `False`，托盘相关功能自动关闭。
- **部分传感器指标不可用**：CPU 温度/功耗/风扇只认 LibreHardwareMonitor / HWiNFO 的共享内存，
  GPU 指标走 NVML（`nvml.dll`），这些后端都是 Windows 路径，Linux 下这些指标为 `None`。
  `psutil` 是可选加速路径且不在 `requirements.txt` 里，装了也只能覆盖 CPU 占用、内存、磁盘、网络等基础项。
- **截屏图层需要显示服务**：`capture.available()` 在非 Windows/macOS 上要求 `DISPLAY` 或
  `WAYLAND_DISPLAY`；Wayland 下 `ImageGrab` 的实际行为未验证。
- **厂商软件互斥在 Linux 上是空操作**：`device.vendor_processes()` 在 Linux 返回 `[]`，
  `kill_vendor_software()` 不会杀任何进程（官方软件主要在 Windows 上）。
- **桌面入口**：`packaging/linux/hydrohud.desktop` 假设 `hydrohud` 已在 `PATH` 中；
  从源码运行时请改成 `Exec=/path/to/.venv/bin/python -m hydrohud`，图标 `Icon=hydrohud` 需要自行安装。

## 未验证项

以下都需要真机或真实桌面环境确认，仓库里没有任何 Linux 硬件测试：

1. Linux 上真正的端到端推流（等 `Screen`/`Control` 接线完成后）。
2. udev 规则在各发行版（有无 `uaccess`、`plugdev` 组是否存在）上的实际效果。
3. LG700（`1a2c:151c`）在 Linux 下的 PID 与接口布局——设备表里的数值是按 LG600 推定的，未实测。
4. Wayland 会话下的 Tk 截图与截屏图层。
5. 高帧率（30 fps 以上）下 hidraw 写入的稳定性与总线占用。

## 参与方式

```bash
python3 -m pytest tests -q                      # 纯软件测试，不碰硬件
python3 -m black --line-length 100 hydrohud tests tools
python3 -m ruff check hydrohud tests tools      # 配置在 pyproject.toml
```

协议细节写在 [docs/PROTOCOL.md](PROTOCOL.md)；新增面板请同时更新 `hydrohud/devices.py` 的设备表、
`packaging/linux/` 下的 udev 规则和本文档。Linux 相关的解析测试在
`tests/test_linux_packaging.py`，只读仓库文件，不依赖 Linux 运行时。
