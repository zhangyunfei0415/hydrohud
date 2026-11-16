# 图层参考
图层是 `.hud.json` 里 `layers` 数组的元素，每个元素是一个字典；`x` / `y` 是 0–1 归一化屏幕坐标（左上角为原点，480×480 画布），`anchor` 决定图层内容相对 `(x, y)` 的对齐方式。字段缺失时由 `hydrohud/scenes.py::normalize_scene` 补默认值，未知字段被忽略，未知 `type` 的图层被丢弃。

## 通用字段
| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `type` | string | — | 图层类型，取值见下表 |
| `x` | number | `0.5` | 水平位置；`0` = 左边缘，`1` = 右边缘 |
| `y` | number | `0.5` | 垂直位置；`0` = 上边缘，`1` = 下边缘 |
| `anchor` | string | `"center"` | 内容相对 `(x, y)` 的锚点 |
| `visible` | bool | `true` | 关掉后渲染层直接跳过这个图层，编辑时仍保留在图层面板里 |

`visible` 是 v1.5 起所有图层通用的开关：改它不影响图层字段，只影响是否画进画面，所以可以放心用来做「临时藏起来对比效果」。

`anchor` 可取：`center`、`top-left`、`top`（=`top-center`）、`top-right`、`bottom-left`、`bottom`（=`bottom-center`）、`bottom-right`、`left`（=`center-left`）、`right`（=`center-right`）。下划线写法（`top_left`）等价。

`(x, y)` 在不同图层里指向不同的几何点：

| 图层 | `(x, y)` 指向 |
| --- | --- |
| `text` `clock` `sysinfo` `countdown` `weather` `nowplaying` | 文字块 / 内容块的锚点 |
| `ticker` | 文字垂直中线；`x` 被忽略（整屏滚动） |
| `bars` `thermometer` | 外接框左上角，`anchor` 不影响位置 |
| `chart` | 默认左上角；`anchor` 为 `right` / `bottom` 时按宽高回退 |
| `gauge` | 圆心；`anchor` 按直径外接框换算 |
| `image` `sequence` `qrcode` | 贴图锚点：`anchor` 决定以贴图的哪个点对齐 `(x, y)` |

## 图层总表
| `type` | 中文名 | 用途 | 动态 |
| --- | --- | --- | --- |
| `text` | 文字 | 静态文本或模板变量（`{cpu}`、`{time}` 等） | 含模板变量时 |
| `clock` | 时钟 | 按 `strftime` 格式显示当前时间，可选日期 | 是 |
| `sysinfo` | 状态文字 | 一行显示任意指标，空格分隔 | 是 |
| `bars` | 进度条 | 每个指标一行，填充比例 = 值 / 指标上限 | 是 |
| `chart` | 折线图 | 指标历史曲线 + 半透明面积 + 可选网格 | 是 |
| `gauge` | 仪表盘 | 环形进度 + 圆心指标名与数值 | 是 |
| `image` | 图片 | 贴一张图片，可缩放、可半透明 | 否 |
| `thermometer` | 温度计 | 竖向胶囊，按 `[min, max]` 填充 | 是 |
| `countdown` | 倒计时 | 到 `target` 的剩余时间 | 是 |
| `ticker` | 滚动文字 | 整屏横向跑马灯 | 是 |
| `sequence` | 图片序列 | 按 `fps` 轮播一组图片 | 是 |
| `qrcode` | 二维码 | 把 `data` 编码成二维码 | 含模板变量时 |
| `weather` | 天气 | 当前天气文本 + 内置矢量图标 | 是 |
| `nowplaying` | 正在播放 | 系统媒体会话的标题 / 作者 / 封面 | 是 |
| `dashboard` | 性能仪表盘 | CPU / GPU 双英雄卡（占用率 + 温度 + 历史折线）+ 可开关的附加指标行 | 是 |

颜色字段统一接受 `#RRGGBB`、`#RGB`（自动展开）与不带 `#` 的十六进制；大小写不敏感。

## text
单行或多行文字。`text` 里的换行 `\n` 会拆成多行，行距自动追加。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `text` | string | `""` | 文字内容；为空时不绘制；支持模板变量 |
| `size` | int | `56` | 字号（像素） |
| `color` | string | `"#FFFFFF"` | 文字颜色 |
| `bold` | bool | `false` | 是否使用粗体字重 |
| `align` | string | `"center"` | 多行文字的水平对齐：`left` / `center` / `right` |
| `shadow` | bool | `true` | 3 px 黑色投影 |

```json
{"type": "text", "x": 0.5, "y": 0.1, "anchor": "center", "text": "CPU {cpu}%", "size": 36, "color": "#E2E8F0"}
```
## clock
按 `strftime` 格式显示 `datetime.now()`，可选第二行日期。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `format` | string | `"%H:%M"` | 时间格式，`strftime` 语法 |
| `show_date` | bool | `false` | 是否在时间下方加一行日期 |
| `date_format` | string | `"%Y-%m-%d"` | 日期格式 |
| `date_size` | int | `max(10, size × 0.26)` | 日期字号 |
| `size` | int | `140` | 时间字号 |
| `color` | string | `"#FFFFFF"` | 文字颜色 |
| `bold` | bool | `true` | 是否粗体 |
| `shadow` | bool | `true` | 3 px 黑色投影 |

```json
{"type": "clock", "x": 0.5, "y": 0.42, "format": "%H:%M", "show_date": true, "date_format": "%Y-%m-%d", "size": 150}
```
## sysinfo
把选中的指标拼成一行文字，用 `separator` 连接。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `fields` | string[] | `["cpu", "ram"]` | 指标 id 列表，见「指标 id 表」，最多 8 个 |
| `separator` | string | `"    "` | 指标之间的分隔字符串（默认 4 个空格） |
| `size` | int | `30` | 字号 |
| `color` | string | `"#8FD3FF"` | 文字颜色 |
| `bold` | bool | `false` | 是否粗体 |
| `shadow` | bool | `true` | 3 px 黑色投影 |

```json
{"type": "sysinfo", "x": 0.5, "y": 0.24, "fields": ["cpu", "ram", "gpu_temp"], "separator": "  ", "size": 36}
```
## bars
每个指标一行圆角进度条，`(x, y)` 为整个条组的左上角，自上而下排列。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `items` | string[] | `["cpu", "ram"]` | 指标 id 列表，每项一行，最多 8 个 |
| `w` | number | `0.84` | 条组宽度，屏幕宽度比例（0.01–1） |
| `h` | number | `0.10` | 条组总高度，屏幕高度比例（0.01–1） |
| `gap` | number | `0.015` | 行间距，屏幕高度比例（0–0.5） |
| `color` | string | `"#38BDF8"` | 奇数行填充色 |
| `color2` | string | `"#F472B6"` | 偶数行填充色 |
| `bg` | string | `"#1E293B"` | 轨道底色 |
| `label` | bool | `true` | 是否在行内左侧画指标名、右侧画数值 |
| `label_color` | string | `"#E2E8F0"` | 标签颜色 |
| `size` | int | `22` | 标签字号（会按行高收缩，最小 9） |
| `radius` | number | `1.0` | 圆角，相对行高的一半（0–1，上限 18 px） |

```json
{"type": "bars", "x": 0.08, "y": 0.4, "items": ["cpu", "ram"], "w": 0.84, "h": 0.16, "color": "#22D3EE", "color2": "#A78BFA"}
```
## chart
指标历史折线图：圆角底 + 三条横网格 + 半透明面积 + 折线 + 左上角当前值。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `metric` | string | `"cpu"` | 指标 id |
| `w` | number | `0.8` | 宽度，屏幕宽度比例（0.05–1） |
| `h` | number | `0.22` | 高度，屏幕高度比例（0.05–1） |
| `color` | string | `"#38BDF8"` | 折线颜色 |
| `color2` | string | `"#0EA5E9"` | 面积填充色（透明度 90/255） |
| `bg` | string | `"#0F172A"` | 图表底色 |
| `line_width` | int | `2` | 折线宽度（1–24） |
| `fill` | bool | `true` | 是否填充折线下方面积 |
| `grid` | bool | `true` | 是否画三条水平网格线 |
| `label` | bool | `true` | 是否在左上角显示当前值 |
| `size` | int | `18` | 当前值字号 |
| `radius` | number | `0.5` | 圆角，相对短边的一半（0–1，绘制上限 0.5） |
| `min` | number | `null` | 纵轴下限；`null` 表示 `0` |
| `max` | number | `null` | 纵轴上限；`null` 表示指标自身上限 |
| `points` | int | `120` | 取最近多少个历史点（2–480） |
| `smooth` | bool | `false` | 是否用 Chaikin 细分把折线插值成曲线 |

```json
{"type": "chart", "x": 0.1, "y": 0.55, "metric": "cpu", "w": 0.8, "h": 0.22, "color": "#38BDF8", "points": 120}
```
## gauge
环形仪表：轨道弧 + 值弧，圆心上方指标名、下方数值。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `metric` | string | `"cpu"` | 指标 id |
| `diameter` | number | `0.5` | 直径，屏幕宽度比例（0.05–1.5） |
| `thickness` | number | `0.06` | 环宽，屏幕宽度比例（0.005–0.5） |
| `color` | string | `"#38BDF8"` | 值弧与数值颜色 |
| `track` | string | `"#1E293B"` | 轨道弧颜色 |
| `label` | bool | `true` | 是否在圆心上方画指标名 |
| `size` | int | `20` | 指标名字号 |
| `value_size` | int | `48` | 数值字号 |
| `min` | number | `null` | 量程下限；`null` 表示 `0` |
| `max` | number | `null` | 量程上限；`null` 表示指标自身上限 |
| `start_angle` | number | `135.0` | 起始角（度，`0` = 3 点钟方向，顺时针） |
| `sweep` | number | `270.0` | 扫过角度（1–360） |
| `show_unit` | bool | `true` | 数值是否带单位；`false` 时只保留数字 |

```json
{"type": "gauge", "x": 0.5, "y": 0.5, "metric": "cpu", "diameter": 0.5, "thickness": 0.06, "color": "#38BDF8"}
```
## image
贴一张图片。`scale` 以屏幕宽度为基准缩放，高度按原比例；文件缺失时画「图片缺失」红框占位。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `path` | string | `""` | 图片路径（JPG / PNG / WebP / BMP） |
| `scale` | number | `0.4` | 宽度比例：`scale × 480` 像素（0.01–10） |
| `opacity` | number | `1.0` | 不透明度（0–1） |

```json
{"type": "image", "x": 0.5, "y": 0.3, "path": "D:\\logo.png", "scale": 0.4, "opacity": 1.0}
```
## thermometer
竖向胶囊温度计：轨道 + 自下而上的填充；标签在胶囊上方，数值按胶囊尺寸画在胶囊内底部或下方。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `metric` | string | `"cpu_temp"` | 指标 id |
| `w` | number | `0.14` | 胶囊宽度，屏幕宽度比例（0.02–1） |
| `h` | number | `0.62` | 胶囊高度，屏幕高度比例（0.05–1） |
| `color` | string | `"#FB7185"` | 填充色（小尺寸时数值也用此色） |
| `track` | string | `"#1E293B"` | 轨道色 |
| `radius` | number | `0.5` | 圆角，相对宽度的一半（0–1） |
| `label` | bool | `true` | 是否在胶囊上方画指标名 |
| `size` | int | `20` | 指标名字号 |
| `show_value` | bool | `true` | 是否显示数值 |
| `value_size` | int | `34` | 数值字号 |
| `show_unit` | bool | `true` | 数值是否带单位 |
| `min` | number | `null` | 填充下限；`null` 表示 `0` |
| `max` | number | `null` | 填充上限；`null` 表示指标自身上限 |

```json
{"type": "thermometer", "x": 0.1, "y": 0.2, "metric": "cpu_temp", "w": 0.14, "h": 0.62, "color": "#FB7185"}
```
## countdown
到 `target` 的剩余时间：小号标题 + 大号倒计时。`target` 为空或无法解析时显示弱化的「设置目标时间」，到期后显示 `done_text`。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `target` | string | `""` | 目标时刻：`YYYY-MM-DD`、ISO 8601 或带时区后缀 |
| `title` | string | `""` | 倒计时上方标题；为空不绘制 |
| `done_text` | string | `"时间到"` | 到期后显示的文字 |
| `format` | string | `"{d} 天 {h}:{m}:{s}"` | 剩余时间格式：`{d}` 天、`{h}`/`{m}`/`{s}` 时分秒（两位补零） |
| `size` | int | `44` | 主文字字号 |
| `title_size` | int | `22` | 标题字号 |
| `color` | string | `"#38BDF8"` | 主文字颜色（到期/无目标时用 `title_color`） |
| `title_color` | string | `"#94A3B8"` | 标题颜色，也是占位状态的主文字颜色 |
| `shadow` | bool | `true` | 3 px 黑色投影 |

```json
{"type": "countdown", "x": 0.5, "y": 0.5, "target": "2027-01-01T00:00:00", "title": "距离新年", "format": "{d} 天 {h}:{m}:{s}"}
```
## ticker
整屏横向跑马灯：文本按 `gap` 间隔重复平铺，相位 `(t × speed) % 周期` 只由时间决定。`x` 被忽略，`y` 是文字垂直中线。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `text` | string | `""` | 滚动文字；为空不绘制 |
| `size` | int | `34` | 字号 |
| `color` | string | `"#E2E8F0"` | 文字颜色 |
| `bold` | bool | `false` | 是否粗体 |
| `speed` | number | `60.0` | 滚动速度（像素/秒，5–600） |
| `gap` | number | `0.5` | 文本间隔，屏幕宽度比例（0–4，即 0–1920 px） |
| `direction` | string | `"left"` | 滚动方向：`left` / `right` |
| `band` | bool | `false` | 是否在文字上下画一条色带 |
| `band_color` | string | `"#0B0F14"` | 色带颜色（高度 = 行高 + 16 px） |
| `shadow` | bool | `true` | 3 px 黑色投影 |

```json
{"type": "ticker", "y": 0.92, "text": "HydroHUD · 480×480 · CPU {cpu}%", "size": 28, "speed": 80, "band": true}
```
## sequence
按 `fps` 轮播一组图片：帧索引 `floor(t × fps)`，`loop=true` 取模，`false` 停在最后一帧。图片按「绝对路径 + mtime + 尺寸 + fit/圆角/透明度」缓存。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `paths` | string[] | `[]` | 图片路径列表（最多 64 个，重复项去重） |
| `fps` | number | `8.0` | 播放帧率（0.5–60） |
| `fit` | string | `"crop"` | 适配方式：`crop` 裁切填满 / `fit` 留边完整显示 / `stretch` 拉伸 |
| `w` | number | `0.5` | 盒子宽度，屏幕宽度比例（0.02–1） |
| `h` | number | `0.5` | 盒子高度，屏幕高度比例（0.02–1） |
| `radius` | number | `0.0` | 圆角，相对短边的一半（0–1） |
| `loop` | bool | `true` | 是否循环；`false` 时播完停在末帧 |
| `opacity` | number | `1.0` | 不透明度（0–1） |

```json
{"type": "sequence", "x": 0.5, "y": 0.5, "paths": ["D:\\frames\\01.png", "D:\\frames\\02.png"], "fps": 8, "w": 0.6, "h": 0.6, "fit": "crop"}
```
## qrcode
把 `data` 编码成二维码（`hydrohud/qrmini.py` 纯 Python 实现，零第三方依赖）。实际模块边长 `module = floor(size × 480 / (码字数 + 2 × border))`；`module < 1` 或编码失败时画「二维码」占位。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `data` | string | `""` | 编码内容；支持模板变量；为空显示占位 |
| `size` | number | `0.5` | 二维码边长，屏幕宽度比例（0.05–1） |
| `fg` | string | `"#0B0F14"` | 深色模块颜色 |
| `bg` | string | `"#FFFFFF"` | 浅色模块颜色（可与 `fg` 反色） |
| `border` | int | `2` | 静区宽度，单位模块（0–8） |
| `error` | string | `"M"` | 纠错级别：`L` / `M` / `Q` / `H` |
| `label` | bool | `false` | 是否在二维码下方画可读文本 |
| `label_size` | int | `18` | 标签字号 |
| `label_color` | string | `"#FFFFFF"` | 标签颜色 |

```json
{"type": "qrcode", "x": 0.5, "y": 0.5, "data": "https://github.com/zhangyunfei0415/hydrohud", "size": 0.5, "fg": "#0B0F14", "bg": "#FFFFFF", "error": "M"}
```
## weather
当前天气：左侧内置矢量图标 + 右侧一行文本。数据来自 Open-Meteo（无需 API key），失败时沿用本地缓存；无任何可用数据时显示「天气 —」。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `lat` | number | `null` | 纬度；`null` 时用默认坐标（北京） |
| `lon` | number | `null` | 经度；`null` 时用默认坐标（北京） |
| `place` | string | `""` | 地名前缀；为空时用快照里的地名 |
| `fields` | string[] | `["temp", "desc"]` | 显示字段：`temp` 温度、`feels` 体感、`humidity` 湿度、`wind` 风速、`desc` 天气描述（按数组顺序排列） |
| `icon` | bool | `true` | 是否显示矢量图标 |
| `icon_size` | int | `56` | 图标尺寸（像素，最小 12） |
| `size` | int | `28` | 文本字号 |
| `color` | string | `"#E2E8F0"` | 文本颜色 |
| `shadow` | bool | `true` | 3 px 黑色投影 |
| `separator` | string | `"  "` | 字段之间的分隔字符串 |

```json
{"type": "weather", "x": 0.5, "y": 0.08, "lat": 39.9042, "lon": 116.4074, "fields": ["temp", "desc"], "icon": true}
```
## nowplaying
系统媒体会话（Windows SMTC）：可选圆角封面 + 标题 / 作者 / 专辑文本。`(x, y)` 是「封面 + 文本」整块的锚点。无播放会话时画弱化的 `placeholder`。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `fields` | string[] | `["title", "artist"]` | 显示字段：`title` / `artist` / `album` |
| `show_cover` | bool | `true` | 是否显示封面（居中裁正方形、12% 圆角） |
| `cover_size` | number | `0.22` | 封面边长，屏幕宽度比例（0–0.8） |
| `size` | int | `26` | 标题字号；作者/专辑用 `0.78 × size` |
| `color` | string | `"#E2E8F0"` | 标题颜色 |
| `label_color` | string | `"#94A3B8"` | 作者/专辑与占位文案颜色 |
| `separator` | string | `" · "` | `artist` 与 `album` 之间的连接符 |
| `placeholder` | string | `"未在播放"` | 无播放会话时显示的文字 |
| `scroll` | bool | `false` | 文本超出可用宽度时是否横向滚动 |
| `speed` | number | `40.0` | 滚动速度（像素/秒，5–600；滚动实现上限 400） |

```json
{"type": "nowplaying", "x": 0.5, "y": 0.75, "fields": ["title", "artist"], "show_cover": true, "cover_size": 0.22, "scroll": true}
```
## dashboard
一块自成一体的性能面板：顶部标题栏（左标题 + 右时钟），中间并排两张「英雄卡」显示 CPU 与 GPU 的占用率、温度和底部历史折线，下方是若干行附加指标（标签 + 迷你进度条 + 数值）。`(x, y)` 是整块卡片的锚点，`w` / `h` 是占屏比例。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `title` | string | `"AURORA"` | 左上角标题 |
| `cpu_metric` | string | `"cpu"` | 左卡大号主数值用的指标 |
| `gpu_metric` | string | `"gpu"` | 右卡大号主数值用的指标 |
| `cpu_temp_metric` | string | `"cpu_temp"` | 左卡温度行的指标 |
| `gpu_temp_metric` | string | `"gpu_temp"` | 右卡温度行的指标 |
| `extras` | string[] | `["gpu_power", "gpu_clock", "ram"]` | 附加指标行；**这是「功耗 / 频率可开关」的数据层**，留空 `[]` 就只显示英雄卡 |
| `w` | number | `0.92` | 卡片宽度，屏幕比例（0.2–1.0） |
| `h` | number | `0.92` | 卡片高度，屏幕比例（0.2–1.0） |
| `cpu_color` | string | `"#38BDF8"` | CPU 卡主色 |
| `gpu_color` | string | `"#A78BFA"` | GPU 卡主色 |
| `accent` | string | `"#22D3EE"` | 顶部细线与强调色 |
| `bg` | string | `"#0F172A"` | 卡片底色 |
| `border` | string | `"#1E293B"` | 卡片描边色 |
| `text_color` | string | `"#E2E8F0"` | 主文字色 |
| `muted_color` | string | `"#94A3B8"` | 次要文字色 |
| `size` | int | `20` | 标题基准字号 |
| `value_size` | int | `64` | 英雄卡大号数值的字号上限（实际会按卡片尺寸收） |
| `show_header` | bool | `true` | 是否画标题栏（标题 + 时钟 + 分隔线） |
| `show_history` | bool | `true` | 是否画英雄卡底部历史折线 |
| `show_extras` | bool | `true` | 是否画附加指标行 |
| `radius` | number | `0.5` | 圆角程度（0–1，按卡片短边百分比） |

```json
{"type": "dashboard", "x": 0.5, "y": 0.5, "w": 0.94, "h": 0.94, "title": "AURORA", "extras": ["gpu_power", "gpu_clock", "ram", "gpu_mem"]}
```

`extras` 里的指标在本机读不到时画「—」，不会崩；界面里勾选附加指标时会自动把采不到值的项标成「本机不可用」。中等性能机器上单帧渲染约 1 ms（首次含字体加载约 58 ms）。
## 背景
`background` 描述整屏底图，与 `tint`（整屏叠色）同级。

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `type` | string | `"solid"` | `solid` 纯色 / `image` 图片 / `gif` 动图 / `video` 视频 / `capture` 屏幕捕获 |
| `color` | string | `"#0B0F14"` | `solid` 的背景色 |
| `path` | string | `""` | `image` / `gif` / `video` 的文件路径 |
| `fit` | string | `"crop"` | 媒体适配：`crop` / `fit` / `stretch` |
| `region` | number[4] | `[0, 0, 1, 1]` | `capture` 捕获区域 `[x, y, w, h]`，相对虚拟桌面归一化（0–1） |
| `fps` | number | `5.0` | `capture` 帧率（0.5–30） |
| `monitor` | int | `0` | 预留的多显示器选择（0–8），当前忽略 |

`tint` 字段：`color`（默认 `"#000000"`）与 `alpha`（默认 `0.0`，0–1）。

```json
{"type": "capture", "region": [0.0, 0.0, 0.5, 1.0], "fps": 5}
```

`capture` 是低帧率桌面镜像：同一时间槽内所有图层共用一张截图，预览帧率再高也不会提高截屏频率；截屏失败时回退纯色，不抛错。静态画面场景不要使用 `capture`。

## 模板变量
`text` 的 `text` 与 `qrcode` 的 `data` 支持模板变量，渲染时替换；含变量的图层被视为动态图层。

| 变量 | 含义 | 示例输出 |
| --- | --- | --- |
| `{time}` | 当前时间 `%H:%M` | `21:36` |
| `{date}` | 当前日期 `%Y-%m-%d` | `2026-11-15` |
| `{uptime}` | 运行时间（小时，1 位小数） | `12.4` |
| `{指标id}` | 对应指标当前值（格式化后） | `{cpu}` → `23%`、`{gpu_temp}` → `45°C` |

未知占位符与无数据时的指标占位符保持原样输出。

## 指标 id 表
| id | 中文标签 | 单位 |
| --- | --- | --- |
| `cpu` | CPU 使用率 | % |
| `cpu_freq` | CPU 频率 | MHz |
| `cpu_temp` | CPU 温度 | °C |
| `cpu_power` | CPU 功耗 | W |
| `ram` | 内存使用率 | % |
| `ram_used` | 已用内存 | GB |
| `ram_total` | 内存总量 | GB |
| `gpu` | GPU 使用率 | % |
| `gpu_temp` | GPU 温度 | °C |
| `gpu_mem` | 显存使用率 | % |
| `gpu_mem_used` | 已用显存 | GB |
| `gpu_mem_total` | 显存总量 | GB |
| `gpu_power` | GPU 功耗 | W |
| `gpu_fan` | GPU 风扇 | % |
| `gpu_clock` | GPU 频率 | MHz |
| `disk` | 磁盘使用率 | % |
| `disk_read` | 磁盘读取 | MB/s |
| `disk_write` | 磁盘写入 | MB/s |
| `net_down` | 网络下载 | Mbps |
| `net_up` | 网络上传 | Mbps |
| `uptime` | 运行时间 | 小时 |
| `fan` | 风扇转速 | RPM |
| `cpu_cores` | CPU 核心数 | 核 |

指标数据来自 `hydrohud/sensors.py`；某项后端不可用时显示「—」。不在表内的 id 会被 `fields` / `items` 过滤掉。

`cpu_temp` 的取值顺序：LHM / OHM 共享内存 → HWiNFO SM2 共享内存 → **ACPI 热区兜底**（`MSAcpi_ThermalZoneTemperature`，取最热的一个热区）。前两者读的是 CPU 核心温度；ACPI 热区贴的是主板，只是量级参考，界面里会在指标卡下方注明「CPU 温度取自 ACPI 主板传感器」。`cpu_power` 与 `fan` 只能来自共享内存，没有 ACPI 兜底。

## 注意事项
- `weather` 需要网络（Open-Meteo，无需 key）；离线或请求失败时继续显示上次缓存，从未成功时显示「天气 —」。
- `nowplaying` 需要 `pip install winsdk`（Windows 10 1809+ 的 SMTC）；未安装或没有播放会话时显示 `placeholder`（默认「未在播放」）。
- `sequence` 的图片按文件签名缓存：替换同名图片后需要重新加载工程（或重启预览）才会生效。
- `qrcode` 由仓库内的纯 Python 编码器生成，不依赖 Pillow 之外的第三方库；二维码矩阵按最近邻放大，屏幕显示清晰。
- 数值字段会被静默限制到合法范围；未知字段忽略，未知 `type` 的图层直接丢弃，因此手写 `.hud.json` 不会因为拼写错误导致整屏无法渲染。
