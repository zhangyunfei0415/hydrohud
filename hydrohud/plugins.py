"""HydroHUD 插件系统：把自定义图层交给用户自己的 Python 文件。

插件放在 ``%APPDATA%\\HydroHUD\\plugins\\`` 下的 ``*.py`` 文件里，启动时由
:func:`discover` 扫描导入。插件文件可以两种写法：

**写法一：声明一个 PLUGIN 字典**

.. code-block:: python

    PLUGIN = {
        "name": "sparkline",            # 图层类型名，唯一，1-64 个标识符字符
        "label": "火花线",               # 界面上显示的名字
        "description": "在矩形里画一条正弦波",
        "defaults": {"w": 0.8, "h": 0.2, "color": "#38BDF8"},
        "draw": draw,                    # 见下面的绘制契约
    }

**写法二：暴露 register(reg) 函数**

.. code-block:: python

    def register(reg):
        reg.add("sparkline", draw, label="火花线", defaults={"color": "#38BDF8"})

两者同时存在时优先 ``register``。

绘制契约
--------
.. code-block:: python

    def draw(image, layer, ctx) -> bool

- ``image``：RGB 模式的 480×480 :class:`PIL.Image.Image`，插件直接在上面画；
- ``layer``：该图层在场景里的 dict（``type`` 已等于插件名，含插件 defaults 里的字段）；
- ``ctx``：渲染上下文，键为 ``t``（秒，浮点）、``metrics``（指标 dict）、
  ``history``（历史采样，可能是空 dict）、``scene``（当前场景 dict）；
- 返回 ``True`` 表示本帧画了内容（渲染层据决定是否算作动态帧），返回 ``False``
  表示本插件没有绘制，渲染层继续画后面的图层。

参数声明
--------
插件可以在 ``PLUGIN`` 字典里声明 ``params``，界面据此自动生成编辑表单：

.. code-block:: python

    PLUGIN = {
        "name": "sparkline",
        "params": [
            {"key": "thickness", "label": "线宽", "type": "int", "default": 3,
             "min": 1, "max": 12},
            {"key": "color", "label": "颜色", "type": "color", "default": "#38BDF8"},
            {"key": "shape", "label": "波形", "type": "choice", "default": "sine",
             "options": [["sine", "正弦"], ["square", "方波"]]},
        ],
    }

支持的 ``type`` 只有 ``int`` / ``float`` / ``bool`` / ``text`` / ``color`` /
``choice`` 六种；声明非法时该条被丢弃，不影响其它参数（见 :func:`normalize_params`）。

安全说明
--------
插件是本机可信 Python 代码，拥有完整进程权限；HydroHUD 不做沙箱、不限制导入。
只安装你自己信任的插件文件。单个插件导入失败或绘制抛异常不会影响其它插件与主程序：
导入错误收集在 :attr:`PluginRegistry.errors`；绘制异常由 :meth:`PluginRegistry.draw_layer`
计数，连续失败达到 :data:`MAX_CONSECUTIVE_ERRORS` 次后该插件被隔离（不再调用）。
``permissions`` 只是声明，用于在界面上提示插件需要网络或文件访问，不做任何强制。
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import math
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

__all__ = [
    "DRAW_TIME_LIMIT",
    "MAX_CONSECUTIVE_ERRORS",
    "PARAM_TYPES",
    "ParamSpec",
    "PluginSpec",
    "PluginRegistry",
    "apply_params",
    "coerce_param",
    "discover",
    "draw_layer",
    "is_registered",
    "normalize_params",
    "plugin_dir",
    "registry",
]

DEFAULT_PLUGIN_DIR_NAME = "plugins"
MODULE_PREFIX = "hydrohud_plugin_"

#: 单次绘制耗时的软预算（秒）：超出只记入 :attr:`PluginRegistry.slow`，不中断绘制。
DRAW_TIME_LIMIT = 0.25
#: 同一插件连续绘制失败的次数上限，达到后隔离（不再调用它的 draw）。
MAX_CONSECUTIVE_ERRORS = 3
#: :class:`ParamSpec` 认可的字段类型。
PARAM_TYPES = ("int", "float", "bool", "text", "color", "choice")

_NAME_RE = re.compile(r"^[\w.-]{1,64}$", re.UNICODE)
_MODULE_STEM_RE = re.compile(r"[^0-9A-Za-z_]+")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

DrawFunc = Callable[[Any, dict, dict], bool]


@dataclass(frozen=True)
class ParamSpec:
    """插件参数声明，界面据此生成表单控件。

    :param key: 图层字段名，写入图层 dict 时的键；
    :param label: 界面上显示的中文标签；
    :param type: ``int`` / ``float`` / ``bool`` / ``text`` / ``color`` / ``choice``；
    :param default: 缺省值，类型须与 ``type`` 一致（``choice`` 为选项值之一）；
    :param min: ``int`` / ``float`` 的下界，``None`` 时取类型缺省（0 或 0.0）；
    :param max: ``int`` / ``float`` 的上界，``None`` 时取类型缺省（100 或 1.0）；
    :param step: 控件步长，``None`` 表示由界面自行决定；
    :param options: ``choice`` 的 ``((值, 显示名), ...)``，其它类型为空。
    """

    key: str
    label: str
    type: str
    default: Any
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class PluginSpec:
    """一个插件的元数据。

    :param name: 图层类型名，唯一，``1-64`` 个字母/数字/下划线/点/横线；
    :param label: 界面显示名，缺省与 ``name`` 相同；
    :param description: 一句话说明，显示在工具提示里；
    :param defaults: 该插件图层缺省字段，插入图层时复制到图层 dict；
    :param draw: 绘制函数 ``draw(image, layer, ctx) -> bool``，``None`` 表示不可绘制；
    :param source: 插件文件绝对路径，便于界面显示与排错；
    :param params: 参数声明，界面据此生成自动表单（见 :func:`normalize_params`）；
    :param permissions: 插件声明的能力（``"network"`` / ``"filesystem"``），只用于界面提示。
    """

    name: str
    label: str
    description: str = ""
    defaults: dict[str, Any] = field(default_factory=dict)
    draw: DrawFunc | None = None
    source: str = ""
    params: tuple[ParamSpec, ...] = ()
    permissions: tuple[str, ...] = ()


def plugin_dir() -> Path:
    """返回插件目录 ``%APPDATA%\\HydroHUD\\plugins``（不创建目录）。"""
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    return root / "HydroHUD" / DEFAULT_PLUGIN_DIR_NAME


def _module_name(path: Path) -> str:
    """为插件文件生成稳定且唯一的模块名（同一文件 reload 后覆盖同一模块）。"""
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path)
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:10]
    stem = _MODULE_STEM_RE.sub("_", path.stem).strip("_") or "plugin"
    return f"{MODULE_PREFIX}{stem}_{digest}"


def _normalize_defaults(raw: Any) -> dict[str, Any]:
    """校验插件 defaults：必须是 Mapping（不是则返回空 dict 由调用方报错）。"""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise TypeError("defaults 必须是字典")
    return {str(key): copy.deepcopy(value) for key, value in raw.items()}


def _is_number(value: Any) -> bool:
    """是否是有限实数（``bool`` 不算数字，避免 ``True`` 被当成 1）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _default_bounds(param_type: str) -> tuple[float, float]:
    """``int`` / ``float`` 参数在声明里省略 min/max 时使用的边界。"""
    if param_type == "int":
        return 0.0, 100.0
    return 0.0, 1.0


def _normalize_options(raw: Any) -> tuple[tuple[str, str], ...]:
    """把 ``choice`` 的选项表规整成 ``((值, 显示名), ...)``；非法形状抛 ValueError。"""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("choice 参数的 options 必须是列表")
    options: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, Mapping):
            value = item.get("value")
            label = item.get("label", value)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) == 2:
            value, label = item[0], item[1]
        else:
            raise ValueError("choice 的每个选项必须是 (value, label) 或含 value/label 的字典")
        if isinstance(value, (Mapping, list, tuple)) or value is None:
            raise ValueError("choice 选项的值必须是标量")
        options.append((str(value), str(label)))
    if len(options) < 2:
        raise ValueError("choice 至少需要 2 个选项")
    if len({value for value, _label in options}) != len(options):
        raise ValueError("choice 的选项值不能重复")
    return tuple(options)


def _make_param(raw: Mapping[str, Any]) -> ParamSpec:
    """把一条原始声明转成 :class:`ParamSpec`；任何不合法之处抛异常（由上层丢弃）。"""
    key = str(raw.get("key") or "").strip()
    if not key:
        raise ValueError("参数缺少 key")
    if not _NAME_RE.match(key):
        raise ValueError(f"参数 key 非法：{key!r}")
    param_type = str(raw.get("type") or "").strip().lower()
    if param_type not in PARAM_TYPES:
        raise ValueError(f"参数 {key} 的 type 非法：{param_type!r}")
    label = str(raw.get("label") or key)
    raw_default = raw.get("default")

    if param_type in ("int", "float"):
        low, high = _default_bounds(param_type)
        low = _coerce_bound(raw.get("min"), low)
        high = _coerce_bound(raw.get("max"), high)
        if high < low:
            raise ValueError(f"参数 {key} 的 max 小于 min")
        if raw_default is None:
            default: Any = int(low) if param_type == "int" else float(low)
        elif _is_number(raw_default):
            default = int(round(float(raw_default))) if param_type == "int" else float(raw_default)
            default = min(max(default, low), high)
        else:
            raise ValueError(f"参数 {key} 的 default 不是数字")
        step = raw.get("step")
        step_value = float(step) if _is_number(step) and float(step) > 0 else None
        return ParamSpec(
            key=key,
            label=label,
            type=param_type,
            default=default,
            min=low,
            max=high,
            step=step_value,
        )

    if param_type == "bool":
        return ParamSpec(key=key, label=label, type=param_type, default=bool(raw_default))

    if param_type == "color":
        text = str(raw_default or "#FFFFFF").strip().upper()
        if not _COLOR_RE.match(text):
            raise ValueError(f"参数 {key} 的 default 不是 #RRGGBB 颜色")
        return ParamSpec(key=key, label=label, type=param_type, default=text)

    if param_type == "text":
        return ParamSpec(key=key, label=label, type=param_type, default=str(raw_default or ""))

    options = _normalize_options(raw.get("options"))
    values = {value for value, _label in options}
    default_text = str(raw_default if raw_default is not None else options[0][0])
    if default_text not in values:
        raise ValueError(f"参数 {key} 的 default 不在 options 里")
    return ParamSpec(
        key=key,
        label=label,
        type=param_type,
        default=default_text,
        options=options,
    )


def _coerce_bound(raw: Any, fallback: float) -> float:
    """min/max 声明值：非数字时用类型缺省边界。"""
    if raw is None or not _is_number(raw):
        return fallback
    return float(raw)


def normalize_params(raw: Any) -> tuple[ParamSpec, ...]:
    """校验插件声明的参数列表，返回可用的 :class:`ParamSpec` 元组。

    非法条目被静默丢弃（不抛异常），录制原因无关紧要：插件作者可以在界面上看到
    少了一个控件，也能在 ``PluginRegistry.errors`` 里看到注册失败信息。
    ``key`` 重复时保留第一条。
    """
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    specs: list[ParamSpec] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        try:
            spec = _make_param(item)
        except (TypeError, ValueError):
            continue
        if spec.key in seen:
            continue
        seen.add(spec.key)
        specs.append(spec)
    return tuple(specs)


def params_defaults(params: Sequence[ParamSpec]) -> dict[str, Any]:
    """把参数声明里的 default 汇总成图层缺省字段。"""
    return {spec.key: copy.deepcopy(spec.default) for spec in params}


def coerce_param(spec: ParamSpec, value: Any) -> Any:
    """把界面/场景里的值转成参数声明的类型；非法值回落到缺省值。

    用于把用户输入（可能是字符串）写进图层 dict 之前做一次校验，保证渲染层拿到的
    字段类型稳定。任何异常都回落 ``spec.default``。
    """
    try:
        if spec.type == "int":
            low = int(spec.min if spec.min is not None else 0)
            high = int(spec.max if spec.max is not None else 100)
            number = int(round(float(value)))
            return min(max(number, low), high)
        if spec.type == "float":
            low = float(spec.min if spec.min is not None else 0.0)
            high = float(spec.max if spec.max is not None else 1.0)
            number = float(value)
            if not math.isfinite(number):
                return spec.default
            return min(max(number, low), high)
        if spec.type == "bool":
            if isinstance(value, str):
                return value.strip().lower() not in ("", "0", "false", "no", "off")
            return bool(value)
        if spec.type == "color":
            text = str(value or "").strip().upper()
            return text if _COLOR_RE.match(text) else spec.default
        if spec.type == "text":
            return str(value)
        if spec.type == "choice":
            text = str(value)
            allowed = {option for option, _label in spec.options}
            return text if text in allowed else spec.default
    except (TypeError, ValueError, OverflowError):
        return copy.deepcopy(spec.default)
    return copy.deepcopy(spec.default)


def apply_params(spec: PluginSpec, layer: Mapping[str, Any] | None) -> dict[str, Any]:
    """按参数声明规整一个插件图层的字段，返回新 dict（不修改输入）。

    缺的键补缺省值，已有的键过 :func:`coerce_param`；``params`` 之外的字段（``type``、
    ``x``、``y`` 等）原样保留。
    """
    result: dict[str, Any] = dict(layer or {})
    for param in spec.params:
        result[param.key] = coerce_param(param, result.get(param.key, param.default))
    return result


def _normalize_permissions(raw: Any) -> tuple[str, ...]:
    """把 ``permissions`` 声明规整成去重后的字符串元组（非列表则视为空）。"""
    if raw is None:
        return ()
    if isinstance(raw, (str, bytes)):
        items: Sequence[Any] = [raw]
    elif isinstance(raw, Sequence):
        items = raw
    else:
        return ()
    seen: list[str] = []
    for item in items:
        text = str(item).strip().lower()
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


class _Registrar:
    """传给插件 ``register(reg)`` 的注册器；插件用它声明一个或多个图层类型。"""

    def __init__(self, registry: PluginRegistry, source: str) -> None:
        self._registry = registry
        self._source = source

    def add(
        self,
        name: str,
        draw: DrawFunc | None = None,
        *,
        label: str = "",
        description: str = "",
        defaults: Mapping[str, Any] | None = None,
        params: Sequence[Mapping[str, Any]] | None = None,
        permissions: str | Sequence[str] | None = None,
    ) -> None:
        """注册一个插件图层；``name`` 唯一，``draw`` 允许为 ``None``。"""
        self._registry._register_spec(
            name,
            label or name,
            description,
            _normalize_defaults(defaults),
            draw,
            self._source,
            normalize_params(params),
            _normalize_permissions(permissions),
        )

    # 允许插件写成 reg.register(...)
    register = add


class PluginRegistry:
    """插件目录的扫描器与注册表；GUI 持有一个实例。"""

    def __init__(self, plugin_dir: str | Path | None = None) -> None:
        self.plugin_dir = Path(plugin_dir) if plugin_dir is not None else globals()["plugin_dir"]()
        self._specs: dict[str, PluginSpec] = {}
        self._module_paths: dict[str, Path] = {}
        self.errors: list[str] = []
        #: 各插件连续绘制失败次数；绘制成功后清零。
        self.failures: dict[str, int] = {}
        #: 连续失败达到上限、已被停止调用的插件名。
        self.quarantined: set[str] = set()
        #: 各插件观测到的最慢一次绘制耗时（秒），仅作诊断。
        self.slow: dict[str, float] = {}

    # -- 只读视图 --------------------------------------------------------- #
    @property
    def specs(self) -> dict[str, PluginSpec]:
        """已注册插件（按名字索引的浅拷贝，避免外部改写注册表）。"""
        return dict(self._specs)

    def get(self, name: str) -> PluginSpec | None:
        """按名字取插件；不存在返回 ``None``。"""
        return self._specs.get(str(name))

    def params_for(self, name: str) -> tuple[ParamSpec, ...]:
        """该插件的参数声明；插件不存在或未声明时返回空元组。"""
        spec = self._specs.get(str(name))
        return spec.params if spec is not None else ()

    def is_quarantined(self, name: str) -> bool:
        """该插件是否因连续绘制失败已被隔离（不再调用它的 draw）。"""
        return str(name) in self.quarantined

    def default_layer(self, name: str = "") -> dict[str, Any] | None:
        """构造一个插件图层的初始 dict：``name`` 省略时取第一个已注册插件。

        返回 ``{"type": name, **defaults}``；名字不存在且未给出可用插件时返回 ``None``。
        """
        if name:
            spec = self._specs.get(str(name))
        else:
            spec = next(iter(self._specs.values()), None)
        if spec is None:
            return None
        layer: dict[str, Any] = {"type": spec.name}
        layer.update(copy.deepcopy(spec.defaults))
        return layer

    # -- 生命周期 --------------------------------------------------------- #
    def discover(self) -> int:
        """清空后重新扫描插件目录，返回成功注册的插件数量。

        每个文件的导入/声明错误记录在 :attr:`errors`，坏插件不影响其它插件。
        """
        self._specs.clear()
        self.errors.clear()
        self.failures.clear()
        self.quarantined.clear()
        self.slow.clear()
        for module_name in self._module_paths:
            sys.modules.pop(module_name, None)
        self._module_paths.clear()

        directory = self.plugin_dir
        if not directory.is_dir():
            return 0
        count = 0
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("__"):
                continue
            count += self._load_file(path)
        return count

    def reload(self) -> int:
        """``discover`` 的别名，供界面"重新扫描插件"按钮使用。"""
        return self.discover()

    # -- 绘制 ------------------------------------------------------------- #
    def draw_layer(self, image: Any, layer: Any, ctx: dict[str, Any] | None = None) -> bool:
        """用本注册表里的插件绘制一个图层；未注册/无绘制函数/抛异常时返回 ``False``。

        有隔离保护：连续失败 :data:`MAX_CONSECUTIVE_ERRORS` 次的插件会被加入
        :attr:`quarantined`，之后不再调用（渲染线程不会因为一个坏插件反复抛异常而变慢）；
        成功一次即清零失败计数。单次耗时超过 :data:`DRAW_TIME_LIMIT` 只记入
        :attr:`slow`，不打断绘制。
        """
        if not isinstance(layer, Mapping):
            return False
        kind = str(layer.get("type") or "")
        if not kind:
            return False
        spec = self._specs.get(kind)
        if spec is None or spec.draw is None:
            return False
        if kind in self.quarantined:
            return False
        started = time.perf_counter()
        try:
            drawn = bool(spec.draw(image, dict(layer), dict(ctx or {})))
        except KeyboardInterrupt:
            raise
        except BaseException:
            failures = self.failures.get(kind, 0) + 1
            self.failures[kind] = failures
            if failures >= MAX_CONSECUTIVE_ERRORS:
                self.quarantined.add(kind)
                self.errors.append(
                    f"{kind}: 连续绘制失败 {failures} 次，已停止调用该插件（重载插件可恢复）"
                )
            return False
        elapsed = time.perf_counter() - started
        self.failures[kind] = 0
        if elapsed > DRAW_TIME_LIMIT:
            self.slow[kind] = max(self.slow.get(kind, 0.0), elapsed)
        return drawn

    # -- 内部 ------------------------------------------------------------- #
    def _load_file(self, path: Path) -> int:
        """导入一个插件文件并注册其中声明的插件；返回新增数量。"""
        module_name = _module_name(path)
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError("无法为该文件创建导入器")
            module = importlib.util.module_from_spec(spec)
            source = spec.loader.get_source(module_name)
            if source is None:
                raise ImportError("无法读取插件源码")
            sys.modules[module_name] = module
            self._module_paths[module_name] = path
            # 直接编译最新源码而不是 exec_module：避免 __pycache__ 里的旧字节码
            # 让"改完插件点重新扫描"读到上一版内容。
            code = compile(source, str(path), "exec")
            exec(code, module.__dict__)
        except BaseException as exc:  # 插件可能 raise SystemExit；KeyboardInterrupt 需放行
            sys.modules.pop(module_name, None)
            self._module_paths.pop(module_name, None)
            if isinstance(exc, KeyboardInterrupt):
                raise
            self.errors.append(f"{path.name}: 导入失败：{type(exc).__name__}: {exc}")
            return 0

        before = set(self._specs)
        try:
            self._register_module(module, path)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            self.errors.append(f"{path.name}: 注册失败：{type(exc).__name__}: {exc}")
        return len(set(self._specs) - before)

    def _register_module(self, module: ModuleType, path: Path) -> None:
        """读取模块里的 PLUGIN 字典或 register()，注册其声明的图层类型。"""
        declared = getattr(module, "PLUGIN", None)
        register = getattr(module, "register", None)
        if callable(register):
            register(_Registrar(self, str(path)))
            return
        if declared is None:
            raise ValueError("缺少 PLUGIN 字典或 register(reg) 函数")
        if not isinstance(declared, Mapping):
            raise TypeError("PLUGIN 必须是字典")
        name = str(declared.get("name") or path.stem)
        draw = declared.get("draw")
        if draw is not None and not callable(draw):
            raise TypeError("PLUGIN['draw'] 必须是函数")
        self._register_spec(
            name,
            str(declared.get("label") or name),
            str(declared.get("description") or ""),
            _normalize_defaults(declared.get("defaults")),
            draw,
            str(path),
            normalize_params(declared.get("params")),
            _normalize_permissions(declared.get("permissions")),
        )

    def _register_spec(
        self,
        name: Any,
        label: str,
        description: str,
        defaults: dict[str, Any],
        draw: DrawFunc | None,
        source: str,
        params: tuple[ParamSpec, ...] = (),
        permissions: tuple[str, ...] = (),
    ) -> None:
        """校验并写入一条插件元数据；重名或非法名记错误并跳过。"""
        plugin_name = str(name or "").strip()
        if not _NAME_RE.match(plugin_name):
            raise ValueError(f"插件名非法：{plugin_name!r}（需 1-64 个字母/数字/下划线/点/横线）")
        if plugin_name in self._specs:
            raise ValueError(
                f"插件重名：{plugin_name}（已注册于 {self._specs[plugin_name].source}）"
            )
        if draw is not None and not callable(draw):
            raise TypeError("draw 必须是函数或 None")
        merged_defaults = dict(defaults)
        for param in params:
            merged_defaults.setdefault(param.key, copy.deepcopy(param.default))
        self._specs[plugin_name] = PluginSpec(
            name=plugin_name,
            label=str(label or plugin_name),
            description=str(description or ""),
            defaults=merged_defaults,
            draw=draw,
            source=str(source),
            params=params,
            permissions=permissions,
        )


_REGISTRY: PluginRegistry | None = None


def registry() -> PluginRegistry:
    """返回进程级插件注册表单例（不自动扫描目录）。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = PluginRegistry()
    return _REGISTRY


def discover() -> int:
    """扫描默认插件目录并刷新单例注册表，返回注册数量。"""
    return registry().discover()


def draw_layer(image: Any, layer: Any, ctx: dict[str, Any] | None = None) -> bool:
    """用单例注册表的插件绘制一个图层；未注册、无 draw 或抛异常时返回 ``False``。

    渲染线程与推流线程都可能调用本函数，异常一律吞掉以保证主程序不崩。
    """
    return registry().draw_layer(image, layer, ctx)


def is_registered(name: Any) -> bool:
    """单例注册表里是否存在名为 ``name`` 的插件（供 :mod:`hydrohud.scenes` 放行图层）。"""
    kind = str(name or "")
    if not kind:
        return False
    return kind in registry().specs
