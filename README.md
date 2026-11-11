# LG600 Studio

**TCOMAS LG600（1A2C:1520）水冷头 480×480 屏幕的开源控制软件。** 图形界面、可自由导入图片 / GIF / 视频、可自己拼装画面（文字、时钟、系统监控、进度条），一键推流到屏幕。MIT 许可。

官方 TCOMAS 软件只有少量预设素材；这个项目把屏幕协议完全解密后重写了一份，想显示什么就显示什么。

![界面截图](assets/screenshot.png)

<details>
<summary>预设场景预览（点击展开）</summary>

![预设场景](preview/contact-sheet.png)

</details>

> 协议逆向记录见 [docs/PROTOCOL.md](docs/PROTOCOL.md)；同生态的另外两条路子：命令行版 [lg600.py](../lg600/lg600.py) 与 [liquidctl 驱动适配](../lg600/notes/liquidctl-adapter.md)。

---

## 功能

| 模块 | 能力 |
| --- | --- |
| **画面来源** | 纯色背景；图片（JPG/PNG/WebP/BMP）；GIF / 动态 WebP；视频（MP4/AVI/MKV/WebM，需 imageio-ffmpeg） |
| **可叠加图层** | 文字（自定义内容/字号/字重/颜色/描边阴影/位置）、时钟（时分秒 + 日期）、系统监控（CPU / 内存）、进度条、图片贴图 |
| **实时预览** | 480×480 所见即所得预览，动态内容 10–15 fps 刷新，显示当前单帧 JPEG 体积（超限标红） |
| **推流** | 10 / 20 / 30 / 60 fps 可选，静态画面自动按 2 s 间隔重推（固件看门狗要求），支持停止与热更新（边改边播） |
| **设备控制** | 亮度 0–100 滑块、开/关屏幕、方向设置、读取设备状态（旋转象限） |
| **工程文件** | 画面保存为 `.lg6.json`，可分享、可版本管理 |

## 快速开始

### 方式一：直接跑 exe（推荐给普通用户）

`dist\LG600Studio.exe`（单文件，约 72 MB）双击运行。**不需要安装 Python**，视频 / GIF 解码用的 ffmpeg 已内置。

> 使用前请先退出官方 `TCOMAS` 软件（任务管理器里结束 `DeviceDriver.exe`），HID 设备同一时间只能被一个程序占用。

### 方式二：源码运行

```powershell
cd lg600-studio
C:\Python314\python.exe -m pip install -r requirements.txt
C:\Python314\python.exe -m lg6studio
```

Python 3.10+；Windows 10/11（协议基于 hid.dll，暂无 Linux/macOS 版本）。

### 命令行参数（开机自启常驻）

```powershell
# 打开项目并立刻开始推流——适合放进「启动」文件夹，开机即显示画面
LG600Studio.exe --autostream --project "D:\我的场景.lg6.json"

python -m lg6studio --autostream --project "D:\我的场景.lg6.json"
```

| 参数 | 说明 |
| --- | --- |
| `--project PATH` | 启动时打开 `.lg6.json` 项目 |
| `--autostream` | 启动后自动开始推流（默认不自动连设备） |
| `--smoke-shot PNG` | 启动 1.2 s 后把预览区截图存 PNG，再 0.4 s 自动退出（用于无人值守验证，退出码 0） |

## 界面说明

```
┌───────────────┬──────────────────────┬───────────────────────┐
│  设备          │      预览 480×480     │  内容与编辑            │
│  ─────────    │                      │  ─────────            │
│  连接状态      │   ┌──────────────┐   │  预设场景列表          │
│  亮度滑块      │   │              │   │  背景：纯色/图片/GIF/视频│
│  开/关屏幕     │   │   实时画面    │   │  图层：＋文字 ＋时钟    │
│  方向下拉      │   │              │   │       ＋监控 ＋进度条  │
│  读取设备状态  │   └──────────────┘   │       ＋图片           │
│               │  FPS · 帧体积  字节   │  项目：新建/打开/保存   │
├───────────────┴──────────────────────┴───────────────────────┤
│  FPS [30 ▾]   [▶ 开始推送到屏幕]  [⏹ 停止]   状态：……          │
└──────────────────────────────────────────────────────────────┘
```

1. **左栏**先点「检测设备」，确认看到 `TCOMAS LG600`。亮度、开关屏按钮会立即作用到硬件。
2. **右栏**挑一个预设，或自己搭：选背景 → 加图层 → 拖动位置滑块微调。
3. **底部**选 FPS，点「▶ 开始推送到屏幕」。改任何设置都会实时反映到屏幕。
4. 「⏹ 停止」后屏幕大约 10 秒内会变黑（固件看门狗），这是正常现象；要长期显示就保持推流。想恢复官方画面，重新打开 TCOMAS 软件即可。

## 命令行 / 自动化

GUI 之外还有两条路子：

- `lg600.py`（纯标准库 + Pillow，单文件）：`python lg600.py push photo.jpg --hold 0` 常驻显示一张图。
- `liquidctl` 驱动：`liquidctl set lcd screen static photo.png --keepalive 0`，可和别的 AIO 设备统一管理。

## 更多玩法

- **开机常驻系统监控**：右栏选「系统监控」预设 → 保存为 `监控.lg6.json` → 把
  `LG600Studio.exe --autostream --project "D:\监控.lg6.json"` 的快捷方式丢进
  `shell:startup`（Win+R 输入）即可开机自动上屏。
- **放自己的视频/GIF**：右栏「背景与叠加」→ 类型选 `video`/`gif` → 选文件，`fit` 用
  `crop` 铺满。单帧上限 15300 字节，程序会自动降 JPEG 质量塞进去。
- **自己拼界面**：「图层编辑」里任意叠文字、时钟、CPU/内存条、贴图，全部所见即所得，
  边改边播。
- **做成桌面挂件**：把窗口拖到副屏/角落，点开始推流后最小化即可（推流在后台线程）。

## 打包 exe

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1 -Clean
# 产物：dist\LG600Studio.exe（单文件，约 72 MB，含 Tkinter / Pillow / imageio + ffmpeg）
```

无 Python 环境也能运行。首次启动会比源码方式慢 1–2 秒（单文件解包）。

## 场景文件格式（`.lg6.json`）

```json
{
  "name": "我的桌面",
  "background": { "type": "solid", "color": "#0B0F14" },
  "layers": [
    { "type": "clock",  "x": 0.5, "y": 0.28, "size": 120, "color": "#E2E8F0" },
    { "type": "text",   "x": 0.5, "y": 0.62, "text": "水冷 42°C", "size": 48, "color": "#38BDF8" },
    { "type": "sysinfo","x": 0.5, "y": 0.82, "size": 36, "color": "#94A3B8" }
  ]
}
```

`background.type` 可为 `solid` / `image` / `gif` / `video`（后三者带 `path` 与 `fit`：`crop` 裁剪填满、`fit` 留黑边、`stretch` 拉伸）。坐标 `x`/`y` 为 0–1 归一化，`0.5` 表示居中。

## 技术要点

- 传输：裸 Win32 HID（`hid.dll` + `setupapi.dll`，ctypes 直调，无第三方 HID 库）。图像走 1025 字节输出报文的接口（MI_01），控制走 441 字节的接口（MI_00）。
- 一帧固定 15 包 × 1020 字节 = **15300 字节上限**（不足 15 包时补 `0x00`，与官方软件一致）；JPEG 质量自 95 自适应下调直到装进去，极端复杂的图会降到很低质量但不会失败。超过 15 包固件会花屏。
- **看门狗**：屏幕不锁存单帧，停流数秒即黑屏/回落官方画面，所以静态图也必须持续重推。
- 官方软件的硬件旋转命令只是存配置、不转画面，所以本项目的旋转在 Pillow 里转像素。
- 与官方软件互斥：同一 HID 接口不能同时被两个进程占用。

## 目录结构

```
lg600-studio/
├── lg6studio/
│   ├── device.py     USB HID 传输 + JPEG 编码 + 报文分帧（无 GUI 依赖）
│   ├── render.py     场景 → 480×480 图像（图层、GIF/视频背景、缓存）
│   ├── scenes.py     场景 schema、8 个预设、工程文件读写
│   ├── sysmon.py     CPU/内存采样（ctypes，无第三方库）
│   ├── app.py        Tkinter 界面
│   └── __main__.py   python -m lg6studio 入口
├── docs/PROTOCOL.md  屏幕 HID 协议逆向记录
├── tests/            pytest 用例（52 项：渲染、场景、报文分帧、JPEG 编码）
├── preview/          预设渲染预览图
├── assets/           图标、界面截图
├── tools/            预览图 / 测试素材生成脚本
├── launcher.py       PyInstaller 入口
├── make_icon.py      生成图标
├── build.ps1         一键打包
└── requirements.txt
```

## 已知限制

- 仅 Windows；仅 LG600（PID 1520）。LG700（1A2C:151C）走另一套 USB 实现，未验证。
- 单帧 15300 字节上限意味着极复杂画面会被压缩到质量较低（画面依旧正常，只是细节少）。
- 视频需要 ffmpeg：`pip install imageio imageio-ffmpeg`，打包版已内置。
- 官方软件开着时无法连接设备，界面会提示退出官方软件。

## 许可

MIT，见 [LICENSE](LICENSE)。协议为互操作性目的逆向自自有硬件；TCOMAS 与 LG600 为其各自所有者的商标，本项目与 TCOMAS 无关联。
