# 自动化与扩展

HydroHUD 的日常操作在 GUI 里完成，但下面四条通道让**程序**也能控制它——不必打开窗口点按钮：Home Assistant 联动（开机切场景、夜间调暗）、Stream Deck 一键换画面、直播脚本切换、批量导入别人分享的预设。

- 让外部程序读状态、切场景、调亮度、开关屏幕、启停推流 → [本地 HTTP API](#本地-http-api)
- 在终端 / 批处理 / 计划任务里做同样的事 → [`hudctl` 命令行](#hudctl-命令行)
- 增加 GUI 里没有的图层类型（自定义图表、特效、数据可视化）→ [图层插件](#图层插件)
- 把「场景 + 素材」打包分享给别人 → [主题包 `.hudpack`](#主题包-hudpack)

## 本地 HTTP API

### 启动与监听

| 方式 | 做法 |
| --- | --- |
| GUI | 菜单「高级 → 启动 / 停止本地 API」。启动后状态栏显示地址，再次点同一项即停止；「高级 → 复制 API Token」把当前 token 复制到剪贴板（没启动过 API 时也会先生成）。 |
| 编程 | 实现 `Host`（下表 7 个方法）后 `ApiServer(host, port=4780).start()`，用 `stop()` 关停。 |

默认监听 `127.0.0.1:4780`；`port=0` 由系统分配，`server.port` 读实际端口。构造只准备 token，`start()` 才绑定端口并起 daemon 线程；`stop()` 可重复调用，`running` 表示是否在服务，`token=""` 关闭鉴权。

```python
from hydrohud import api

server = api.ApiServer(my_host)      # my_host 是实现了 Host 协议的对象
server.start()                       # 之后 server.port / server.token 可用
...
server.stop()
```

`Host` 协议（`api.Host`，`runtime_checkable`）：`status()->dict`、`apply_scene(scene)->str`、`get_scene()->dict`、`set_brightness(value:int)->None`、`set_power(on:bool)->None`、`set_stream(on:bool)->None`、`metrics()->dict`。

### 鉴权

| 项 | 说明 |
| --- | --- |
| token 文件 | `%APPDATA%\HydroHUD\api-token.txt`（`api.token_path()`）；不存在时 `api.ensure_token()` 生成 32 位 hex 并写回 |
| token 传递 | `X-HydroHUD-Token` 请求头（推荐用于脚本）或 `?token=` 查询参数（便于浏览器直接验证，会留在历史记录） |
| 比较方式 | `hmac.compare_digest` 常量时间比较；不匹配返回 401 |
| 关闭鉴权 | `ApiServer(..., token="")`，仅建议在完全可信的本机环境使用 |

`GET` 与 `POST` 都需要 token，没有例外。所有响应为 JSON（`Content-Type: application/json; charset=utf-8`）。`GET` 可以把 `?token=` 直接粘到地址栏验证；`POST` 建议用请求头，避免 token 出现在 URL 与访问日志里。

### 路由表

| 方法 | 路径 | 请求体 | 成功响应 | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/status` | — | 状态 dict | 设备、推流、fps、亮度、当前工程等 |
| GET | `/scene` | — | 当前场景 dict | 与 `.hud.json` 同格式 |
| POST | `/scene` | `{"scene": {...}}` 或裸场景 | `{"ok": true, "message": "...", "scene": {...}}` | 先规范化再应用；插件图层按原顺序保留 |
| POST | `/brightness` | `{"value": 0-100}` | `{"ok": true, "value": 60}` | 整数，越界或非整数 → 400 |
| POST | `/power` | `{"on": true}` | `{"ok": true, "on": true}` | 开关屏幕 |
| POST | `/stream` | `{"on": true}` | `{"ok": true, "on": true}` | 开始 / 停止推流 |
| GET | `/metrics` | — | 指标 dict | CPU / GPU / 内存等最新采样 |
| GET | `/events` | — | `text/event-stream` | SSE 状态流，见下 |

`POST /scene` 是**排队应用**：请求线程把场景交给 UI 线程后立刻返回 `"场景已排队应用"`，响应里的 `scene` 是校验后的规范场景。紧接着 `GET /scene` 可能还是旧场景，轮询留一两帧余量。`GET /status` 示例（字段随版本增减）：

```json
{"app": "HydroHUD", "version": "1.3.0", "device": true, "streaming": true, "fps": 30.0,
 "frames": 1024, "brightness": 80, "project": "夜间模式.hud.json", "scene": "夜间模式",
 "dirty": false, "can_undo": true, "can_redo": false}
```

### 错误响应

统一格式 `{"ok": false, "error": "中文原因"}`。

| 状态码 | 触发条件 |
| --- | --- |
| 400 | 请求体不是 JSON 对象、`value` 不是 0-100 整数、`on` 不是布尔、场景非法（图层类型未知等） |
| 401 | 缺少 token 或 token 不匹配（`token 无效或缺失`） |
| 404 | 未知路径（`未知路径：/foo`） |
| 405 | 方法不对，响应头带 `Allow`（例如 `GET /brightness`） |
| 413 | 请求体超过 1 MiB（`MAX_BODY_BYTES`） |
| 500 | `Host` 实现抛异常，消息为 `服务器内部错误：<类型>: <原文>` |

### 示例

```bash
# 1) 读状态（curl，Git Bash / WSL）
TOKEN=$(tr -d '\r\n' < "$APPDATA/HydroHUD/api-token.txt")
curl -s -H "X-HydroHUD-Token: $TOKEN" http://127.0.0.1:4780/status

# 2) 调亮度到 60（PowerShell）
$token = (Get-Content "$env:APPDATA\HydroHUD\api-token.txt" -Raw).Trim()
$headers = @{ "X-HydroHUD-Token" = $token }
Invoke-RestMethod -Method Post -Headers $headers -ContentType "application/json" `
  -Uri "http://127.0.0.1:4780/brightness" -Body '{"value": 60}'

# 3) 把工程文件作为场景推上去（PowerShell）
$scene = Get-Content "examples\性能曲线.hud.json" -Raw -Encoding UTF8 | ConvertFrom-Json
$body = @{ scene = $scene } | ConvertTo-Json -Depth 32 -Compress
Invoke-RestMethod -Method Post -Headers $headers -ContentType "application/json" `
  -Uri "http://127.0.0.1:4780/scene" -Body $body
```

### SSE 状态流

```bash
curl -N -H "X-HydroHUD-Token: $TOKEN" "http://127.0.0.1:4780/events?limit=5"
```

- 连接后**立刻**发第一条，之后每 `1.0` 秒（`EVENT_INTERVAL_SECONDS`）一条，格式 `data: {json}` + 空行；
- `?limit=N` 发满 N 条后服务端主动关闭；不带 `limit`（或 `limit=0`）则一直订阅；
- 事件内容与 `GET /status` 相同；单次采样异常时该条为 `{"ok": false, "error": "..."}`，流不中断；没有 `id:` / `retry:` 字段，断线重连即重新开始。

服务线程绝不直接碰 Tk。`Host` 的每个方法都可能从 HTTP 工作线程被调用，实现方必须把动作**封送回 UI 线程**（投递 `queue.Queue` + `root.after`，用 `threading.Event` 等结果）。仓库内参考实现是 `app.py` 的 `_build_api_host()`：`apply_scene` / `set_power` / `set_stream` 都走 `app.after(0, ...)`。在工作线程里直接调 tkinter 会随机崩溃或卡死界面。

## `hudctl` 命令行

`hudctl` 是上面这套 API 的官方客户端，只依赖标准库。**GUI 必须先启动本地 API**。

```powershell
C:\Python314\python.exe -m hydrohud.hudctl status
```

| 子命令 | 参数 | 作用 |
| --- | --- | --- |
| `status` | — | 查询状态，按键值逐行打印 |
| `scene` | `<file.hud.json>` | 读文件 → 规范化（`scenes.load_project`）→ `POST /scene` |
| `get-scene` | — | 输出当前场景 JSON（人类可读缩进） |
| `brightness` | `<0-100>` | 设置背光亮度 |
| `power` | `on` / `off` | 开关屏幕 |
| `stream` | `on` / `off` | 开始 / 停止推流 |
| `metrics` | — | 每行一个指标：`指标 值` |
| `events` | `[--limit N]` | 订阅 SSE，逐行打印；Ctrl+C 退出 |

- `--host`（默认 `127.0.0.1`）服务地址；`--port`（默认 `4780`）服务端口。
- `--token`：优先级 `--token` > 环境变量 `HYDROHUD_TOKEN` > `%APPDATA%\HydroHUD\api-token.txt`。
- `--json`：紧凑单行 JSON 输出，方便 `ConvertFrom-Json` / `jq` 解析。

参数既可写在子命令前也可写在子命令后（`hudctl --json metrics` 与 `hudctl metrics --json` 等价）。退出码：0 成功；1 HTTP 错误（401/400/404…）、连不上服务、文件缺失或 JSON 非法（错误写 stderr）；2 参数错误（未知子命令、非法 `--port`、`brightness 120`）；`events` 被 Ctrl+C 中断返回 130。所有 HTTP 请求超时 5 秒。

```powershell
C:\Python314\python.exe -m hydrohud.hudctl status
C:\Python314\python.exe -m hydrohud.hudctl --json metrics
C:\Python314\python.exe -m hydrohud.hudctl scene "examples\性能曲线.hud.json"
C:\Python314\python.exe -m hydrohud.hudctl brightness 30
C:\Python314\python.exe -m hydrohud.hudctl events --limit 5
```

token 不一致时的报错会直接提示解决方式：

```text
错误：HTTP 401：{"ok": false, "error": "token 无效或缺失"}（请用 --token 或环境变量 HYDROHUD_TOKEN 提供与 %APPDATA%\HydroHUD\api-token.txt 一致的 token）
```

## 图层插件

插件放在 `%APPDATA%\HydroHUD\plugins\*.py`（`plugins.plugin_dir()`），启动时自动扫描；改完文件后用菜单「插件 → 重载图层插件」重扫（无需重启）。每个文件可以声明一个 `PLUGIN` 字典，或暴露 `register(reg)` 函数（两者同时存在时以 `register` 为准，`register` 里可多次 `reg.add(...)` 注册多个类型）。插件名唯一，允许 1–64 个字母 / 数字 / 下划线 / 点 / 横线；重名时保留先注册的，后来者记入错误列表。

```python
# 写法一：PLUGIN 字典
def draw(image, layer, ctx):
    return True

PLUGIN = {
    "name": "stamp",                     # 图层类型名，场景里以 "type": "stamp" 出现
    "label": "印章",                      # GUI 按钮上显示的名字
    "description": "在指定位置盖一个色块",
    "defaults": {"x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2, "color": "#38BDF8"},
    "draw": draw,
}

# 写法二：register(reg)，一个文件可注册多个
def register(reg):
    reg.add("stamp", draw, label="印章", defaults={"color": "#38BDF8"})
    reg.add("stamp2", draw, label="印章 2")
```

绘制契约 `def draw(image, layer, ctx) -> bool`：

| 参数 / 返回值 | 类型 | 说明 |
| --- | --- | --- |
| `image` | `PIL.Image.Image` | 480×480、RGB；直接在上面绘制，不要 `resize` / 返回新图 |
| `layer` | `dict` | 该图层在场景里的字段，`type` 已等于插件名，含 `defaults` 里的键 |
| `ctx` | `dict` | 键：`t`（秒，动画用）、`metrics`（最新指标采样，可能为空）、`history`（指标历史序列，可能为空）、`scene`（当前完整场景） |
| 返回值 | `bool` | `True` 表示本帧画了内容（渲染层当作动态帧）；`False` 表示跳过，继续画后面的图层 |

坐标系是 0–1 归一化：`x=0.5, y=0.5` 即画面中心，`w` / `h` 是占屏比例。`layer` 字段可能被用户改过，读取时要做容错。

完整示例：存为 `%APPDATA%\HydroHUD\plugins\crosshair.py`，再用「插件 → 重载图层插件」，图层页签就会出现「＋ 十字准星」按钮。

```python
from PIL import ImageDraw


def draw(image, layer, ctx):
    x = float(layer.get("x", 0.5)) * image.width
    y = float(layer.get("y", 0.5)) * image.height
    size = max(0.01, float(layer.get("size", 0.08))) * image.width
    painter = ImageDraw.Draw(image)
    color = str(layer.get("color") or "#38BDF8")
    painter.line((x - size, y, x + size, y), fill=color, width=4)
    painter.line((x, y - size, x, y + size), fill=color, width=4)
    return True


PLUGIN = {"name": "crosshair", "label": "十字准星", "draw": draw,
          "defaults": {"x": 0.5, "y": 0.5, "size": 0.08, "color": "#38BDF8"}}
```

仓库里还有更完整的示例 `examples/plugins/sparkline.py`（矩形内画流动正弦波，含参数容错），可直接复制到插件目录。

**安全边界**：插件是**本机可信 Python 代码**，拥有完整进程权限，HydroHUD 不做沙箱、不限制 import；只安装自己信任的文件。单个插件导入失败或绘制抛异常不会影响其它插件和主程序：导入错误收集在 `PluginRegistry.errors`（中文原因，如 `bad.py: 导入失败：SyntaxError: ...`、`PLUGIN 必须是字典`、`插件重名：...`），绘制异常被 `plugins.draw_layer()` 静默吞掉；`draw` 返回假值、插件未注册、没有 `draw` 时都当作跳过。

**GUI 用法**：插件放进目录 → 启动或「插件 → 重载图层插件」→「图层」页签内置按钮下面一排 `＋ <label>` 插件按钮 → 点击即以 `defaults` 新建图层，可调位置、删除、保存进 `.hud.json`。场景里的插件图层记为 `{"type": "<插件名>", ...}`，重新打开工程时需要同名插件已加载，否则该图层在重新打开时会被丢弃（插件已加载时图层与其自定义字段都会保留）。

## 主题包 `.hudpack`

主题包是**一个普通 zip**（后缀改成 `.zip` 就能直接看），把工程场景和它引用的素材一起打包：

```text
mytheme.hudpack
├── theme.json          # 场景 JSON，素材路径已改写为 assets/ 下的相对路径
└── assets/
    ├── 1.png           # 按出现顺序重新编号的素材，同一文件只存一份
    └── 2.gif
```

| 字段 / 文件 | 说明 |
| --- | --- |
| `theme.json` | 与 `.hud.json` 同格式，额外带 `format` 字段；`background.path`、图层的 `path` / `paths` 指向包内 `assets/` |
| `assets/<n>.<ext>` | 场景引用到的本地图片 / GIF / 视频，导出时复制进包 |
| `format` | `"hydrohud.theme/1"`（常量 `themepack.FORMAT`），主题包结构版本 |

| 方式 | 做法 |
| --- | --- |
| GUI 导出 | 「主题包 → 导出主题包…」，选择保存位置；缺失的素材会跳过并在状态栏 / 弹窗提示 |
| GUI 导入 | 「主题包 → 导入主题包…」，直接成为当前场景（不覆盖原工程文件） |
| Python | `themepack.export_pack(scene, path, name=None)` → `{"path", "assets", "bytes", "missing"}` |
| Python | `themepack.import_pack(path, dest_dir=None, name=None)` → `{"scene", "dir", "assets", "name"}` |

导入时做路径安全检查：只解压 `theme.json` 与 `assets/` 下的普通文件，含 `..` 或绝对路径的条目被忽略。`themepack.packs_dir()` 是 `%APPDATA%\HydroHUD\themes`；导入时素材解压到 `themes\<包名>\`（重名自动加 `-2`、`-3`）。`themepack.list_packs()` 只列出 `themes` 目录下的 `.hudpack` **文件**（不扫描已解压的子目录），`themepack.remove_pack(name)` 只删除对应的 `.hudpack` 文件——「把包文件放进 `themes`」和「用 GUI 导入（解压到子目录）」是两种状态，脚本管理前先确认包在哪。

`format` 目前**只写不校验**：导入时读出来就丢弃，不做版本拒绝，未来格式变更才会用它判断兼容。导入后的场景交给当前 `scenes.normalize_scene` 规范化：旧包缺的字段自动补默认值，未知字段被丢弃（已注册的插件图层连同其自定义字段保留），数值越界退回默认；素材路径在导出时是相对路径、导入后改写为解压目录的绝对路径，移动 `themes` 或手工删素材后需重新导入。

## 别踩的坑

| 坑 | 说明 |
| --- | --- |
| token 泄露 | 拿到 token 的本机程序就能改屏幕、开关屏、启停推流；不要写进日志、截图、聊天记录或共享脚本 |
| 不要改 bind | API 只监听 `127.0.0.1`；`bind="0.0.0.0"` 会把控制面暴露到局域网，且 token 走明文 HTTP |
| SSE 断线 | `/events` 是长连接，网络抖动 / GUI 退出会断；客户端要自己退避重连，并接受重连后首条与上一条重复 |
| 排队应用 | `POST /scene` 返回时 UI 还没应用；需要确认结果就轮询 `GET /scene` 或看 `GET /status` 的 `scene` 字段 |
| `hudctl` 连不上 | 先确认 GUI 已启动本地 API、端口是 4780、token 与 `api-token.txt` 一致（`--token` 可临时覆盖） |
