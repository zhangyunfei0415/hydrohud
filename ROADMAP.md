# HydroHUD 路线图

v1.2.0 补上了「系统状态」这条主线（23 项指标、折线图/仪表盘），v1.3.0 补上了另一条更根本的主线：**「自己做画面」真的做得出来了**——可视化画布编辑器、撤销重做、7 类新图层、主题包、插件、本地 API。v1.4.0 继续往「平台化」推：插件参数声明与编辑器自动表单、插件隔离与权限声明、WebSocket 事件流与 Home Assistant 集成、社区预设库、Linux 打包资源、`ruff` 门禁。v1.5.0 转向「一眼看懂」：性能仪表盘图层（CPU / GPU 温度与占用率常驻、功耗频率可开关）、全套 Aurora 界面换肤、ACPI 温度兜底。这份文档规划之后往哪儿长：哪些是顺手就能做的小改进，哪些要依赖协议层再做实验，哪些属于长期生态建设。

工作量单位是**人日**（含测试与真机验证，AI 辅助开发的保守估计）。

---

## 0. 基线：v1.5.0 有什么

| 能力 | 现状 |
| --- | --- |
| 传输 | 裸 Win32 HID（ctypes），图像 1025 字节报文 / 控制 441 字节报文，零第三方 HID 库；`transport.py` 抽象 + `FakeTransport`，Linux 走 `/dev/hidrawN`（`Screen` / `Control` 按平台分派，未真机验证） |
| 帧格式 | 每帧 1020 字节分包，预算 **46 包 = 46920 字节**（v1.0.0 沿用官方软件的 15 包习惯；E1 实测后放开），JPEG 自适应质量（100→5） |
| 渲染 | **15 类图层**：文字（含指标模板）、时钟、状态文字、进度条、折线图、环形仪表盘、贴图、温度计、倒计时、滚动文字、图片序列、二维码、天气、正在播放、**性能仪表盘**；所有图层支持 `visible` 单独隐藏；背景纯色 / 图片 / GIF / 视频 / **屏幕捕获**；插件图层兜底 |
| 系统状态（v1.2 / v1.5） | 23 项指标：CPU/内存/GPU/磁盘/网络/温度/功耗/风扇/频率/运行时间；GPU 走 NVML，CPU 温度走 LHM/HWiNFO 共享内存、缺失时回落 ACPI 热区；`SensorHub` 后台 0.5 s 采样 + 120 点历史，稳态 3–4 ms |
| 编辑（v1.3） | 画布直接拖动 / 8 手柄缩放 / 网格与中心线吸附 / Ctrl 多选 / 方向键微移；撤销重做（拖动合并为一步）；15 类图层的参数面板，性能仪表盘可在面板里逐个勾选要显示的附加指标 |
| 预览 | 480×480，约 16 fps；顶部显示渲染耗时与帧体积，下方常驻三格（渲染耗时 / 实际帧率 / 累计帧数） |
| 界面（v1.5） | Aurora 换肤：左侧图标栏、顶栏 AURORA 徽章 + 版本 / 帧数 / 设备 chip、卡片青紫渐变顶线、左栏「实时指标」卡（CPU / GPU 进度条 + sparkline + 双温度 + 附加指标开关） |
| 推流 | 10/20/30/60 fps，绝对截止时刻调度，实测 **29.8–30.0 fps**；静态场景按 2 s 重推对抗看门狗；后台线程，窗口可最小化 |
| 设备控制 | 亮度、开/关屏、读状态（441 字节回复）、方向设置（只存配置）、一键结束官方软件；设备表 `devices.py`（LG600 已验证 / LG700 待验证）；`Control.upload_file()` 实现 flash 上传（真机未验证） |
| 工程 | `.hud.json` 工程文件；**12 个中文预设**（含 Aurora 仪表盘）；`.hudpack` 主题包（工程 + 素材 zip）；官方素材扫描导入 |
| 自动化（v1.3） | 本地 HTTP API（`127.0.0.1:4780`，token + SSE `/events` + **WebSocket `/ws`**，附 `examples/homeassistant/`）+ `hudctl` CLI；图层插件（`%APPDATA%\HydroHUD\plugins`，**参数声明 + 自动表单 + 故障隔离**）；**社区预设库**（`presetslib.py`：远程 JSON 索引 + 离线缓存 + 一键安装） |
| 常驻能力（v1.1） | 系统托盘、全局热键、工程轮播、拖放导入、界面状态记忆、开机自启开关、崩溃日志、休眠唤醒自动重连 |
| 分发 | PyInstaller 单文件 exe（约 73 MB）；**982 项 pytest**；`tools/check_device.py` 硬件自检、`tools/verify_v11.py` / `verify_v13.py` 真机帧率回归、`tools/gui_check.py` 无人值守 GUI 自检（45 项） |
| 协议文档 | 仓库内 `docs/PROTOCOL.md`、`docs/LAYERS.md`、`docs/AUTOMATION.md`、`docs/PLUGINS.md`、`docs/LINUX.md` |

已知没做的：flash 上传的真机验证、`0x18`/`0x1A`/`0x1E` 三个设置命令、LG700 型号、Linux 真机验证、多屏、代码签名、国际化。

---

## 1. 硬约束（先划掉不可能的事）

| 约束 | 影响 |
| --- | --- |
| ~~单帧 15300 字节上限~~ → **已解除** | E1 实测定案：固件能完整渲染 46 包（46920 B），品牌软件只发 15 包是它的习惯而非固件限制。46 包/帧按 30 fps 推 0 帧迟到（1.4 MB/s），自由跑 175 fps，所以画质上限已提到质量 100 照片原样上屏 |
| 画面必须恒为 480×480 | E5 实测定案：640×640 JPEG 不是被缩放，而是十几帧后 `WriteFile error 31/433`、整块屏从 USB 掉线。超采样提升清晰度的路不通，只能靠高质量 JPEG |
| 固件看门狗 | 任何"停在那儿显示"的方案都必须持续推流；静态画面靠重复同一段 JPEG，成本很低（30 fps 占 6% 带宽） |
| 面板 30 Hz | 推超过 30 fps 没有视觉收益，只增加 CPU/USB 占用 |
| 与官方软件互斥 | 需要引导用户退出 `DeviceDriver.exe`；可通过检测自动提示（已做），无法共存 |
| 仅 Windows（当前实现） | Linux 需要 hidraw 重写传输层（结构化开销小，见 v1.4） |
| 无音频、无触摸、无亮度传感器 | 视频只能静音；不能做交互式 UI |

---

## 2. 规划原则

1. **先补「日常顺手」，再堆「炫技功能」**：托盘、热键、多工程轮播这类小功能对留存的影响比新图层类型大。
2. **每个功能都要能验证**：渲染类进 `pytest` + 预览图；设备类必须有 `tools/check_device.py` 式真机脚本；协议类先做实验再动代码。
3. **不引入重量级依赖**：保持"单文件 exe、双击即用"的体验。新依赖必须能在 PyInstaller 里稳定工作，且体积可控（当前 72 MB，目标上限 100 MB）。
4. **协议结论一律写进 `docs/PROTOCOL.md`**，不留在聊天记录里。

---

## 3. 版本路线

### v1.1「顺手」— 日常可用性（✅ 已完成，v1.1.0）

目标：让它成为"真的会一直开着"的软件，而不是一个 demo。

| 功能 | 落地做法 | 状态 |
| --- | --- | --- |
| 系统托盘 | `hydrohud/tray.py`：message-only 窗口 + `Shell_NotifyIconW`（不引入 pystray），关窗进托盘，菜单：显示窗口/开始·停止推流/下一个工程/退出 | ✅ |
| 工程轮播（播放列表） | `hydrohud/playlist.py` 环形列表 + `App._playlist_tick`；切换时不停流，列表与间隔持久化 | ✅ |
| 全局热键 | `hydrohud/hotkeys.py`：`RegisterHotKey` + `PeekMessageW` 泵；`Ctrl+Alt+S` 开关推流、`Ctrl+Alt+N` 下一个工程、`Ctrl+Alt+L` 显示窗口 | ✅ |
| 拖放导入 | `WM_DROPFILES` 子类化窗口过程：`.hud.json` 打开工程，图片/GIF/视频直接设为背景 | ✅ |
| 界面记忆 | `hydrohud/settings.py` 原子写 `%APPDATA%\HydroHUD\settings.json`（窗口几何、工程、FPS、亮度、轮播间隔） | ✅ |
| 一键开机自启 | `hydrohud/autostart.py` 写 `HKCU\...\Run`（比 `shell:startup` 快捷方式更可控、可读回校验），命令行带 `--autostream --project` | ✅ |
| 官方软件检测与一键结束 | `device.vendor_processes()/kill_vendor_software()` + 「设备 → 结束官方 TCOMAS 软件」 | ✅ |
| 崩溃/异常日志 | `hydrohud/diagnostics.py`：按天日志、接管 `sys.excepthook`/`threading.excepthook`/Tk 回调异常，「帮助 → 打开日志目录」 | ✅ |
| 休眠唤醒重连 | 推流线程捕获 `OSError` → `Screen.reopen()`（重新枚举接口，路径变化也能接上），最多重试 3 次 | ✅ |
| 帧率打满 30 | 帧调度改为**绝对截止时刻**累加（逐帧 `wait(1/fps)` 的实际开销会把 30 fps 拖到 20 fps）；实测 29.8–30.0 fps | ✅ |

遗留（不阻塞 v1.1）：`Ctrl+Alt+L` 在本机被别的软件占用（`RegisterHotKey` 返回 1409），程序会在标题栏只列出注册成功的热键，不会报错。

### v1.2「内容」— 想显示什么就显示什么（✅ 已完成，v1.2.0 + v1.3.0）

目标：从"能放图"变成"能表达信息"，这是用户最初提的"更多玩法"的核心。

本轮已完成（用户重点要求的「系统状态」）：

| 功能 | 落地做法 | 状态 |
| --- | --- | --- |
| 硬件传感器接入 | `hydrohud/sensors.py`：23 项指标。GPU 直调 `nvml.dll`（占用/温度/显存/功耗/风扇/频率），CPU/内存/磁盘/网络走 ctypes（`GetSystemTimes`/`GlobalMemoryStatusEx`/`GetDiskFreeSpaceExW`/`GetIfTable2`，有 psutil 则优先），CPU 温度/功耗/风扇从 LHM/HWiNFO 共享内存读、缺失显示「—」；`SensorHub` 0.5 s 节流采样 + 120 点环形历史 | ✅ |
| 折线图图层 | `chart`：指标历史曲线，圆角背景 + 网格 + 渐变面积 + 平滑折线 + 左上角标题；可配指标/宽高/颜色/填充/背景/网格/点数 | ✅ |
| 环形仪表盘图层 | `gauge`：270° 弧 + 2× 超采样抗锯齿 + 圆心大数字 + 上方标签；自动按指标量程归一化 | ✅ |
| 状态文字支持任意指标 | `sysinfo`/`bars` 从固定 cpu/ram 改为任意 23 项指标（最多 8 项/图层），编辑界面用勾选对话框选 | ✅ |
| 文字模板变量 | `text` 支持 `{cpu}` `{gpu_temp}` `{time}` `{date}` `{uptime}`，含指标占位符自动判定为动态图层 | ✅ |
| 新预设 | 硬件温度（CPU/GPU 双仪表 + 功耗风扇条）、性能曲线（双折线图）、极客面板（大号 CPU 仪表 + 多指标），预设 8 → 11 | ✅ |
| 编辑界面适配 | 「添加图层」5 → 7 按钮；指标选择对话框；chart/gauge 参数面板（指标下拉 + 尺寸滑块 + 三色） | ✅ |

剩余项已在 v1.3.0 一并做完（原本列在这里作为"下一轮继续"）：

| 功能 | 落地做法 | 状态 |
| --- | --- | --- |
| 可视化图层编辑器 | `hydrohud/editor.py`（几何部分为纯函数：`layer_bounds`/`hit_test`/`snap_offset`/`drag_selection`/`resize_layer`）+ `App._toggle_canvas_edit`；8 手柄、1/8 网格与中心线/相邻图层吸附、Ctrl 多选、方向键微移 | ✅ |
| 撤销/重做 | `hydrohud/history.py`：深拷贝快照栈、`coalesce_key` 窗口内合并（拖动滑块不刷历史）、脏标记；`Ctrl+Z` / `Ctrl+Y` | ✅ |
| 更多图层类型 | 温度计 `thermometer`、倒计时 `countdown`、滚动文字 `ticker`、图片序列 `sequence`、二维码 `qrcode`（纯 Python `qrmini.py`，无第三方依赖） | ✅ |
| 正在播放 | `hydrohud/media.py`：Windows SMTC，`winsdk` 为可选依赖，未装则图层显示占位文案 | ✅ |
| 天气 | `hydrohud/weather.py`：Open-Meteo（免 key），10 分钟缓存 + 原子写 + 离线降级 | ✅ |
| 屏幕区域捕获 | `hydrohud/capture.py` + 背景类型 `capture`：`ImageGrab.grab(region)` 按 0.5–30 fps（默认 5）缩到 480，带 4 槽缓存；渲染层标记为动态背景 | ✅ |
| 主题包导入导出 | `hydrohud/themepack.py`：`.hudpack` = 场景 JSON + 引用素材的 zip，`FORMAT="hydrohud.theme/1"` | ✅ |
| 官方素材导入 | 菜单「主题包 → 导入官方素材到素材库」，扫描 `C:\Program Files (x86)\TcomasSoftware\led*/` 复制进素材库（只读官方文件） | ✅ |

### v1.3「编辑与平台化」— 自己做画面 + 给程序用（✅ 已完成，v1.3.0）

目标：v1.2 解决"能显示什么"，v1.3 解决"做起来有多顺手"和"别的程序能不能控制它"。

| 项目 | 内容 | 状态 |
| --- | --- | --- |
| 图层插件 API | `hydrohud/plugins.py`：`PluginSpec(name/label/description/defaults/draw/source)`，插件放 `%APPDATA%\HydroHUD\plugins`，支持 `PLUGIN = dict(...)` 与 `register(reg)` 两种写法；未知图层类型自动走插件；坏插件只进 `errors`；GUI 生成按钮 + 一键重载 | ✅ |
| 本地 API | `hydrohud/api.py`：`127.0.0.1:4780`，token（`X-HydroHUD-Token` 或 `?token=`），路由 `/status` `/scene` `/metrics` `/events`(SSE) + `POST /scene` `/brightness` `/power` `/stream`；`Host` Protocol 由 app.py 实现，所有回调封送到 Tk 主线程 | ✅ |
| `hudctl` CLI | `hydrohud/hudctl.py`：status / scene / get-scene / brightness / power / stream / metrics / events，`--json` 输出 | ✅ |
| 设备表 | `hydrohud/devices.py`：`DeviceSpec`（型号、VID/PID、两端接口报文长度、分包预算、是否已验证），`device.detected_spec()` / `describe_panel()` 走表查询；LG700 已在表中标注"未验证" | ✅ |
| 传输抽象 + Linux 后端 | `hydrohud/transport.py`：`Transport` 协议、`FakeTransport`（测试用）、`HidrawTransport` + sysfs 枚举（`list_hidraw`，接口号回落读 `bInterfaceNumber`）；`device.py` 按平台分派，复用 `build_packets` | ✅ 代码就位，未在 Linux 真机验证 |
| flash 上传（开机图） | `device.Control.upload_file()`：`0x0A` 声明 + `0x0B` 12 字节块头分块，含进度回调；协议见 `docs/PROTOCOL.md` §3.1 | ✅ 代码 + 桩测试，**真机未验证** |
| 折叠式刷新 / 异步 HID 写 | — | ⬜ 未做：46 包已实测 30.0 fps / 0 迟到，收益不足 |
| 未知命令扫盲（E3） | `0x18`/`0x1A`/`0x1E` | ⬜ 未测 |
| 自适应画质 | — | ⬜ 未做：JPEG 质量阶梯 + 帧预算已经够用 |

### v1.4「跨平台与多型号」（约 10 人日）

v1.4.0 先落地了「平台化」里不依赖硬件的那一半：插件参数声明 + 编辑器自动表单、插件故障隔离与 `permissions` 声明、WebSocket `/ws` 与 Home Assistant 集成包、社区预设库（`presetslib.py`）、Linux udev 规则与打包资源、`ruff` 与 CI lint 门禁。真正需要硬件的部分（Linux 端到端推流、LG700 实测、多屏、macOS）仍在下面表里。

| 项目 | 内容 | 工作量 | 状态 |
| --- | --- | --- | --- |
| Linux 支持 | `hydrohud/transport.py` 的 `HidrawTransport` + sysfs 枚举 + `FakeTransport`（复用 `build_packets`）；`device.Screen` / `Control` 已按平台分派到 `/dev/hidrawN`（`enumerate_collections` / `_HidDevice.backend`）；udev 规则、桌面入口、安装脚本在 `packaging/linux/`（见 `docs/LINUX.md`）；剩余：X11/Wayland 下 Tk 验证、真机推流回归 | 3 | 🟡 端到端已接线，待真机验证 |
| LG700 适配 | `1A2C:151C` 已进设备表（`devices.py`，标 `verified=False`）；剩余：接硬件实测报文长度与命令差异 | 1 | 🟡 表就位，待硬件 |
| 设备表 / 自动探测 | ✅ v1.3 完成：`devices.py` 表 + `device.detected_spec()` / `describe_panel()`，新屏幕加一行即可 | 1.5 | ✅ |
| 多屏支持 | 同时驱动两块屏（不同工程），UI 做设备选择器 | 2 | ⬜ |
| macOS（可选） | IOHIDManager 传输层；优先级最低，视社区需求 | 2.5 | ⬜ |

### v1.5「Aurora」— 一眼看懂的硬件面板（✅ 已完成，v1.5.0）

目标：把"能显示什么"变成"一眼看懂机器在干什么"。CPU / GPU 的温度与占用率是常驻信息，功耗、频率这类想加再加。
这版同时把桌面端的视觉语言统一了一遍：屏幕上的仪表盘和程序界面用同一套青紫渐变。

| 项目 | 内容 | 状态 |
| --- | --- | --- |
| 性能仪表盘图层 | 第 15 类图层 `dashboard`：标题栏（标题 + 时钟）、CPU / GPU 双英雄卡（大号占用率 + 温度 + 底部历史折线）、附加指标行（标签 / 迷你进度条 / 数值）；2× 超采样绘制，文字在贴图之后落笔；热态单帧约 1 ms | ✅ |
| 指标可开关 | 英雄卡常驻；`extras` 列表驱动附加行，图层面板里逐个勾选、左栏一个选择框整体收起；本机采不到值的指标在选择器里置灰并标「本机不可用」 | ✅ |
| 图层可见性 | 全部 15 类图层通用 `visible` 字段，关闭后 `render._layer_overlay` 直接返回透明 overlay，编辑器和工程文件保留 | ✅ |
| Aurora 界面 | 左侧图标栏、顶栏 AURORA 徽章 + 版本 / 帧数 / 设备三个 chip、卡片青紫渐变顶线、左栏「实时指标」卡（进度条 + sparkline + 双温度 + 附加指标开关）、预览卡渲染耗时 / 实际帧率 / 累计帧数三格 | ✅ |
| ACPI 温度兜底 | `sensors._acpi_thermal_celsius()`：LHM / HWiNFO 都读不到时回落到 `MSAcpi_ThermalZoneTemperature`，后台线程 + 5 s TTL，标注为主板传感器 | ✅ |
| 渲染统计三格 | 预览卡下方常驻显示渲染耗时、实际帧率、累计帧数 | ✅ |
| 预设 | 第 12 个预设「Aurora 仪表盘」 | ✅ |

### v2.0「平台化」— 社区与生态（约 20 人日）

| 项目 | 内容 | 工作量 / 状态 |
| --- | --- | --- |
| ~~图层插件 API~~ | ✅ v1.3 已完成基础版（`plugins.py`：`PluginSpec` + `draw_layer` + 自动按钮）；**v1.4 补完参数声明**：`plugins.ParamSpec` / `PARAM_TYPES` / `normalize_params` / `coerce_param` / `apply_params`，编辑器按声明自动生成控件，示例见 `examples/plugins/sparkline.py` | ✅ 完成 |
| 插件沙箱与验收 | 已做：单次耗时软预算 `DRAW_TIME_LIMIT=0.25s`（记入 `slow`）、连续异常 `MAX_CONSECUTIVE_ERRORS=3` 次后隔离、`permissions` 声明字段；未做：真沙箱（子进程/限权）与安装前权限确认弹窗 | 2 / 🟡 隔离已做 |
| ~~本地 API~~ | ✅ v1.3 已完成（`api.py` + `hudctl.py`）；**v1.4 补完**：WebSocket `/ws`（RFC 6455 握手、每秒文本帧、`?limit=N`）与 `examples/homeassistant/` 集成包 | ✅ 完成 |
| 社区预设库 | ✅ 已做：`hydrohud/presetslib.py`（远程 JSON 索引 + 离线缓存 + 一键安装，只下载 `.hud.json` 不执行代码）+ 「社区 → 预设库」窗口；未做：缩略图与评分字段 | 2 / 🟡 基础版完成 |
| 自动发布流水线 | ✅ 已做：`.github/workflows/ci.yml` 跑 `pytest`（3.10/3.12/3.13 × ubuntu/windows）+ `black` + `ruff` + `build.ps1` 打包；推 `v*` tag 时 `release` job 复用打包产物并自动建 Release、附 exe、生成变更日志 | ✅ 完成 |
| 代码签名 | 减少 SmartScreen/杀软误报（需代码签名证书，用户自行决定是否购买） | 1 |
| 国际化 | 界面文案抽离，英文 README 与英文 UI | 3 |
| 文档站 | ✅ 已做：`tools/build_docs.py` 把 `README.md` + `docs/*.md` 装配成站点源目录并改写内链（站外链接回落 GitHub），`mkdocs.yml`（Material 主题、中文锚点、搜索），CI 的 `docs` job `mkdocs build --strict` 后由 `pages` job 发到 GitHub Pages：<https://zhangyunfei0415.github.io/hydrohud/> | ✅ 完成 |

---

## 4. 需要先用真机验证的假设

这些实验决定后续设计，成本低、收益大，建议**先做**。

| 编号 | 假设 | 实验方法 | 若成立的收益 |
| --- | --- | --- | --- |
| E1 | 固件接受 >15 包 | ✅ **成立**：46 包完整渲染；截断反证（15300/30600 B 分别只解码到约 180/360 px）证明固件收完整帧再解码 | 单帧上限 15300 → 46920 字节，照片质量 3× |
| E2 | `0x0A`/`0x0B` 能写屏内 flash | 已按静态反汇编的 12 字节块头格式实现 `Control.upload_file()`（含进度回调、桩测试）；**还差真机**：上传一个小文件，重启后看开机图是否变化 | 自定义开机画面（最"炫"的功能之一） |
| E3 | `0x18`/`0x1A`/`0x1E` 有可见效果 | 逐个发不同参数，观察屏幕/温度显示变化 | 也许能省掉软件层温度渲染 |
| E4 | 首包计数字段可被固件用作校验 | 故意发计数与实际包数不一致的帧，看是否被丢弃 | 明确错误处理策略，避免偶发花屏 |
| E5 | 低质量大分辨率（如 640×640 → JPEG）能否被接受 | ✅ **否定**：640×640 JPEG 十几帧后 `WriteFile error 31/433`，整块屏从 USB 掉线，约 5 s 后重新枚举 | 不能靠超采样提升清晰度，只能靠高质量 JPEG |
| E6 | 停止推流后固件回落的精确时序与内容 | 停流后每 500 ms 记录画面变化，直到 15 s | 让「停止」提示与"屏幕约 10 秒后熄灭"文案精确 |

实验脚本放 `tools/experiments/`，结论写进 `docs/PROTOCOL.md` 的「未验证项」小节。

---

## 5. 技术债与工程化

| 项 | 现状 | 计划 |
| --- | --- | --- |
| 测试 | **982 项**（渲染 15 类图层 / 图表仪表 / 场景 / 传感器 / 分帧 / 编码 / 撤销重做 / 画布几何 / 插件参数与隔离 / 预设库 / API + WS / hudctl / 主题包 / 天气 / 捕捉 / 传输 / Linux 打包与 hidraw 后端 / 文档站装配与链接改写）；设备层用 `FakeTransport`、替身传输类与桩 `write_report` 覆盖报文序列 | ✅ CI 已强制（ubuntu + windows × Py 3.10/3.12/3.13 + black + ruff + 打包 + 文档站 `mkdocs build --strict`）；真机路径只能靠 `tools/check_device.py` / `verify_v11.py` / `verify_v13.py` / `gui_check.py`（45 项无人值守 GUI 自检） |
| exe 体积 | 72 MB（含 ffmpeg + numpy） | 提供 Lite 版（无视频，约 40 MB）；或把视频解码拆成可选插件 |
| 首启动速度 | 单文件解包 1–2 s | 提供 one-folder 版本；或按需导入 imageio |
| 类型与风格 | `black` 100 列 + `ruff`（`pyproject.toml` 共享配置，E4/E7/E9/F/W/I/UP/B 规则）全绿 | ✅ CI 已强制（`lint (black + ruff)` job） |
| 仓库存档 | 多个提交，`.github/` 已放入工作区 | ✅ v1.3.0 起 CI 全绿（3×3 矩阵 + lint + 打包），release 手动发布 |
| 文档 | `README.md` + `AGENTS.md` + `docs/{PROTOCOL,LAYERS,AUTOMATION,PLUGINS,LINUX}.md` | 增加架构说明（线程模型、事件队列）；文档站 |
| 崩溃可观测 | 只有 `_warn_once` 到控制台 | 结构化日志 + 可选匿名统计（默认关闭） |

---

## 6. 风险

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| 固件升级改协议 | 软件失效 | 版本探测（查询回复特征）+ 兼容分支；文档记录失败现象 |
| 厂商发法律函 | 项目下架 | 仅互操作、不包含官方资源、不绕过任何保护；LICENSE 已声明 |
| SmartScreen / 杀软误报 | 用户不敢用 | 代码签名（可选）、提供源码自行构建、发布 SHA256 |
| PyInstaller 单文件被杀软隔离 | 无法运行 | 提供 one-folder 版本与源码运行方式 |
| 用户同时开官方软件 | 打不开设备 | 已做检测提示；后续加「一键结束官方进程」 |
| ~~>15 包实验失败~~ | 已证伪 | E1 成立，46 包已上线；E5 否定，超采样路线关闭 |

---

## 7. 建议顺序

1. **~~E1 / E5~~ 已完成**——46 包画质上限成立、640×640 会让设备掉线，两条结论已落到代码与文档。
2. **~~v1.1「顺手」整包~~ 已完成**——托盘 + 轮播 + 开机自启把使用门槛降到"装了就忘了"。
3. **~~v1.2 传感器与图表~~ 已完成**——23 项指标 + 折线图 + 环形仪表盘，屏幕能表达信息了。
4. **~~v1.2 的可视化编辑器~~ 已完成（v1.3）**——画布拖动 / 手柄缩放 / 吸附 / 多选 + 撤销重做，这是"自己做画面"的瓶颈。
5. **E2 开机图真机验证**——代码已就位（`Control.upload_file`），成功的话是最有传播力的功能。
6. **v1.4 Linux + LG700**——Linux 端到端已接线（`Screen`/`Control` 按平台分派到 `/dev/hidrawN`），剩下真机回归与 udev 实测；LG700 只差硬件。
7. **~~v2.0 文档站~~ 已完成**——`tools/build_docs.py` 装配站点源目录 + `mkdocs.yml`，CI 的 `docs` / `pages` job 自动构建并发布到 <https://zhangyunfei0415.github.io/hydrohud/>。
8. **~~v1.5 Aurora~~ 已完成**——性能仪表盘图层（温度 / 占用率常驻、功耗频率可开关）+ 全套界面换肤 + ACPI 温度兜底。
9. **v2.0 国际化**——界面文案抽离 + 英文 UI / README，工作量最大的一块，建议在界面稳定后动。

**立即可开工的 Top 5（都是低风险、高感知）**：
① ~~系统托盘~~ ✅　② ~~工程轮播~~ ✅　③ ~~可视化拖动编辑~~ ✅　④ ~~传感器温度图层~~ ✅　⑤ 新版截图/GIF 演示（放 README 顶部）

---

## 8. 怎么衡量做得好

| 指标 | 目标 |
| --- | --- |
| 推流稳定帧率（30 fps 档） | ≥ 29.0 fps（当前 29.8–30.0） |
| 从双击到画面出现在屏上 | ≤ 4 s（当前约 3–5 s，含解包） |
| 渲染 + 编码单帧耗时 | ≤ 8 ms（v1.2 实测：11 个预设 0.02–3.4 ms 渲染，含编码约 8–12 ms） |
| 新用户成功上屏所需步骤 | ≤ 3 步（检测 → 选预设 → 开始） |
| 未知错误导致的白屏/崩溃 | 0（全部转中文提示） |
| 测试覆盖 | 每条协议路径至少一个用例；982 项全绿，`FakeTransport` 覆盖报文序列；文档站 `mkdocs build --strict` 不允许断链 |
