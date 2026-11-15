# 图层插件开发

插件是放在 `%APPDATA%\HydroHUD\plugins\*.py` 的普通 Python 文件，给 HydroHUD 增加它内建的 14 类图层之外的绘制能力。启动时自动扫描；改完文件用菜单「插件 → 重载图层插件」重扫，不必重启程序。

运行期用法（放哪个目录、怎么点按钮、`permissions` 与隔离的界面表现）见 [AUTOMATION.md](AUTOMATION.md#图层插件)；本文只讲**插件作者**需要遵守的协议。

## 两种写法

**写法一：声明一个 `PLUGIN` 字典**（一个文件一个图层类型）

```python
def draw(image, layer, ctx):
    return True


PLUGIN = {
    "name": "stamp",                       # 图层类型名，场景里写作 "type": "stamp"
    "label": "印章",                        # 界面按钮上的名字
    "description": "在指定位置盖一个色块",
    "defaults": {"x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2, "color": "#38BDF8"},
    "params": [],                          # 可选，见下文「参数声明」
    "permissions": [],                     # 可选，只用于界面提示
    "draw": draw,
}
```

**写法二：暴露 `register(reg)`**（一个文件可注册多个类型）

```python
def register(reg):
    reg.add("stamp", draw, label="印章", defaults={"color": "#38BDF8"},
            params=[{"key": "size", "label": "大小", "type": "float",
                     "default": 0.2, "min": 0.02, "max": 0.6}],
            permissions=["filesystem"])
    reg.add("stamp2", draw, label="印章 2")
```

两者同时存在时以 `register` 为准。`reg.add()` 的签名：

```python
reg.add(name, draw=None, *, label="", description="", defaults=None,
        params=None, permissions=None)
```

`name` 必须唯一，规则是 1–64 个字母 / 数字 / 下划线 / 点 / 横线（`^[\w.-]{1,64}$`）；重名时保留先注册的那个，后来者在 `PluginRegistry.errors` 里留一条 `插件重名：...`。

## 绘制契约

```python
def draw(image, layer, ctx) -> bool
```

| 参数 / 返回值 | 类型 | 说明 |
| --- | --- | --- |
| `image` | `PIL.Image.Image` | 480×480、RGB；**直接在上面画**，不要 `resize`、不要返回新图 |
| `layer` | `dict` | 该图层在场景里的字段，`type` 已等于插件名，含 `defaults` 里的键（插件图层总有 `x` / `y`） |
| `ctx` | `dict` | `t`（秒，动画用）、`metrics`（最新指标采样，可能为空）、`history`（指标历史序列，可能为空）、`scene`（当前完整场景） |
| 返回值 | `bool` | `True` 表示本帧画了内容（渲染层当作动态帧逐帧重绘）；`False` 表示跳过，继续画后面的图层 |

坐标系 0–1 归一化：`x=0.5, y=0.5` 是画面中心，`w` / `h` 是占屏比例。`layer` 的字段可能被用户改过或来自别人分享的 `.hud.json`，读取时要做容错，别假设类型。

完整的可运行示例是 [`examples/plugins/sparkline.py`](../examples/plugins/sparkline.py)（矩形内画流动正弦波，声明了 4 个可调参数）。

## 参数声明

在 `PLUGIN["params"]`（或 `reg.add(..., params=[...])`）里声明参数，界面就会**自动生成控件**，用户不用手改 JSON。每条声明是一个 dict：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `key` | ✅ | 图层字段名，写入图层 dict 时的键；同时受插件名规则约束 |
| `label` | — | 控件左边显示的中文名，省略时用 `key` |
| `type` | ✅ | `int` / `float` / `bool` / `text` / `color` / `choice`（`PARAM_TYPES`） |
| `default` | — | 缺省值，类型须与 `type` 一致；省略时按类型取（数字取 `min`、颜色 `#FFFFFF`、文本空串、`choice` 取第一个选项） |
| `min` / `max` | — | 只对 `int` / `float` 有效，省略时 `int` 取 0–100、`float` 取 0–1 |
| `step` | — | 控件步长，`float` 滑块的显示精度按它推导 |
| `options` | `choice` 必填 | `[["值", "显示名"], ...]` 或 `[{"value": "值", "label": "显示名"}, ...]`，至少 2 项且值不重复 |

```python
PLUGIN = {
    "name": "gauge2",
    "label": "第二仪表",
    "params": [
        {"key": "thickness", "label": "线宽", "type": "int", "default": 8, "min": 1, "max": 40},
        {"key": "ratio", "label": "弧度占比", "type": "float", "default": 0.75},
        {"key": "shape", "label": "波形", "type": "choice", "default": "sine",
         "options": [["sine", "正弦"], ["square", "方波"]]},
        {"key": "accent", "label": "高亮色", "type": "color", "default": "#38BDF8"},
        {"key": "show_label", "label": "显示标签", "type": "bool", "default": True},
        {"key": "caption", "label": "说明", "type": "text", "default": ""},
    ],
}
```

规则（实现见 `plugins.normalize_params()`）：

- **非法条目被静默丢弃**，不影响其它参数。不合法包括：缺 `key`、`key` 形状非法、`type` 不在六种之内、`int`/`float` 的 `max < min`、`default` 不是数字、颜色不是 `#RRGGBB`、`choice` 的 `default` 不在 `options` 里、`options` 少于 2 项或值重复。
- `key` 重复时保留第一条。
- 声明的 `default` 会自动合并进 `PluginSpec.defaults`（显式写在 `PLUGIN["defaults"]` 里的值优先），所以「用 `defaults` 建图层」和「用参数默认值建图层」结果一致。
- 场景里已有的值会经 `plugins.coerce_param()` 校验：`int` 四舍五入并夹到 `[min, max]`，`float` 检出 NaN/Inf 后回落默认值，`bool` 认 `"0"/"false"/"no"/"off"`，`color` 非法回落默认色，`choice` 不在选项内回落默认值。**任何异常都回落默认值，不会让渲染线程崩掉。**
- 声明了 `params` 的插件图层，编辑器不再注入通用的「颜色」控件——外观字段完全由你自己的声明决定。

## 故障隔离

插件是**本机可信代码**，画在渲染线程上。为了不让一个写坏的插件拖垮整个程序：

| 常量 | 值 | 行为 |
| --- | --- | --- |
| `plugins.MAX_CONSECUTIVE_ERRORS` | 3 | `draw` 连续抛异常达到该次数后，插件被加入 `PluginRegistry.quarantined`，之后**不再调用**它的 `draw`；`errors` 里留一条 `连续绘制失败 N 次，已停止调用该插件（重载插件可恢复）`。任何一次成功绘制都会把计数清零 |
| `plugins.DRAW_TIME_LIMIT` | 0.25 秒 | 单次绘制超过它只记入 `PluginRegistry.slow`（诊断用），**不打断绘制**、不隔离 |

`KeyboardInterrupt` 会照常向上抛（不当作插件故障）。编辑器对已隔离的插件显示一行黄色提示，用户点「插件 → 重载图层插件」即可清空隔离状态重试。

被隔离或抛异常的插件返回 `False`，渲染层跳过该图层，画面其余部分正常。

## permissions

`permissions` 接受字符串或字符串列表，规整成小写去重元组（`plugins._normalize_permissions`）。当前识别的值是 `"network"` 与 `"filesystem"`，编辑器会显示 `需要网络访问、文件读写（仅供参考，不做强制）`。

**这只是声明，HydroHUD 不做任何拦截**：插件仍然拥有完整进程权限。它的意义是让用户在装第三方插件前能一眼看到「这个插件要联网」，而不是安全边界。真正的沙箱（子进程 + 限权）仍在 v2.0 计划里，见 [ROADMAP.md](../ROADMAP.md)。

## 测试自己的插件

插件不依赖 GUI 即可验证：`plugins.PluginRegistry` 可以指向任意目录。

```python
from pathlib import Path
from PIL import Image
from hydrohud import plugins

registry = plugins.PluginRegistry(Path("我的插件目录"))
assert registry.discover() == 1, registry.errors
spec = registry.get("stamp")
layer = registry.default_layer("stamp")

image = Image.new("RGB", (480, 480), "#0B0F14")
drawn = registry.draw_layer(image, layer, {"t": 1.0, "metrics": {}, "history": {}, "scene": {}})
assert drawn is True
```

仓库自己的插件用例在 [`tests/test_plugins.py`](../tests/test_plugins.py)（32 项，含参数规整、类型转换、隔离与恢复）；无人值守的 GUI 冒烟在 `tools/gui_check.py` 的「插件参数表单」检查里（写一个临时插件、加图层、断言编辑器生成了控件）。
