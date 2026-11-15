# Home Assistant 集成

HydroHUD 的本地 API 可以让 Home Assistant 读取面板状态（推流、fps、亮度、当前场景）并下发命令（亮度、开关屏、启停推流、切换场景）。整套集成只用 HA 自带的 `rest:` / `rest_command:` / `template:`，不需要自定义组件。

## 1. 取 token

1. 启动 HydroHUD，打开菜单「高级 → 启动本地 API」（默认监听 `127.0.0.1:4780`）。
2. 菜单「高级 → 复制 API Token」，或直接打开 `%APPDATA%\HydroHUD\api-token.txt`。
3. 把 token 粘到 `hydrohud.yaml` 里所有 `X-HydroHUD-Token` 占位处。

token 等同于控制权限，只留在本机配置文件里，不要提交到公开仓库或贴进聊天。

## 2. 放置配置

推荐使用 packages：

```yaml
# configuration.yaml
homeassistant:
  packages: !include_dir_named packages
```

把本目录的 `hydrohud.yaml` 复制到 HA 配置目录的 `packages/hydrohud.yaml`。也可以把文件里的 `rest:`、`rest_command:`、`template:` 三段直接合并进 `configuration.yaml`。

## 3. 重启

`rest_command` 是启动时注册的，新增后必须重启 HA 核心；只调整 sensor 的轮询间隔可「开发者工具 → YAML → 全部重新加载」。

## 4. 能监控什么

| 来源 | 字段 | 说明 |
| --- | --- | --- |
| `GET /status` | `device` | 是否检测到屏幕（`false` 时实体显示「离线」） |
| `GET /status` | `streaming` / `fps` / `frames` | 是否推流、目标帧率、已推帧数 |
| `GET /status` | `brightness` | 当前背光亮度 0–100 |
| `GET /status` | `project` / `scene` / `dirty` | 当前工程文件、场景名、是否有未保存修改 |
| `GET /metrics` | `cpu` / `gpu` / `gpu_temp` / `memory` … | 最新一次传感器采样，随机器后端增减 |

示例里 `sensor.hydrohud_status` 的 `json_attributes` 已带上面这些字段；`template:` 段把它们拼成一句中文摘要。

## 5. 能下发什么

| 动作 | 服务 | 数据 |
| --- | --- | --- |
| 调亮度 | `rest_command.hydrohud_brightness` | `value: 0-100`（小数会取整，越界由 API 拒绝） |
| 开关屏幕 | `rest_command.hydrohud_power_on` / `_off` | — |
| 启停推流 | `rest_command.hydrohud_stream_on` / `_off` | — |
| 切换场景 | `rest_command.hydrohud_scene` | `scene: {完整场景 JSON}` |

自动化示例（到家自动调亮）：

```yaml
automation:
  - alias: "到家调亮 HydroHUD"
    trigger:
      - platform: state
        entity_id: person.me
        to: home
    action:
      - service: rest_command.hydrohud_brightness
        data:
          value: 80
      - service: rest_command.hydrohud_stream_on
```

## 6. WebSocket（可选）

`GET /ws` 是同一份数据的推送通道：握手后每秒一条 JSON 文本帧，内容与 `GET /status` 相同，适合自己写集成或常驻脚本；HA 的 `rest:` 轮询已经够用，不需要它。

## 7. 401 排查

- 确认 HydroHUD 里「启动本地 API」已经点过（状态栏会显示地址）。
- 对比 `%APPDATA%\HydroHUD\api-token.txt` 的内容与 yaml 里的 token；token 被重新生成后旧值立即失效。
- 端口冲突：`4780` 被别的程序占用时 API 启动失败，状态栏会有中文提示。
- 仍失败时看 HA「设置 → 系统 → 日志」，搜索 `hydrohud` 或 `401`，错误体会写明原因。
