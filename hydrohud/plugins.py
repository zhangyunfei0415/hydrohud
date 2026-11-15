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

安全说明
--------
插件是本机可信 Python 代码，拥有完整进程权限；HydroHUD 不做沙箱、不限制导入。
只安装你自己信任的插件文件。单个插件导入失败或绘制抛异常不会影响其它插件与主程序：
导入错误收集在 :attr:`PluginRegistry.errors`，绘制异常被 :func:`draw_layer` 静默丢弃。
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping

__all__ = [
    "PluginSpec",
    "PluginRegistry",
    "plugin_dir",
    "registry",
    "discover",
    "draw_layer",
    "is_registered",
]

DEFAULT_PLUGIN_DIR_NAME = "plugins"
MODULE_PREFIX = "hydrohud_plugin_"

_NAME_RE = re.compile(r"^[\w.-]{1,64}$", re.UNICODE)
_MODULE_STEM_RE = re.compile(r"[^0-9A-Za-z_]+")

DrawFunc = Callable[[Any, dict, dict], bool]


@dataclass(frozen=True)
class PluginSpec:
    """一个插件的元数据。

    :param name: 图层类型名，唯一，``1-64`` 个字母/数字/下划线/点/横线；
    :param label: 界面显示名，缺省与 ``name`` 相同；
    :param description: 一句话说明，显示在工具提示里；
    :param defaults: 该插件图层缺省字段，插入图层时复制到图层 dict；
    :param draw: 绘制函数 ``draw(image, layer, ctx) -> bool``，``None`` 表示不可绘制；
    :param source: 插件文件绝对路径，便于界面显示与排错。
    """

    name: str
    label: str
    description: str = ""
    defaults: dict[str, Any] = field(default_factory=dict)
    draw: DrawFunc | None = None
    source: str = ""


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


class _Registrar:
    """传给插件 ``register(reg)`` 的注册器；插件用它声明一个或多个图层类型。"""

    def __init__(self, registry: "PluginRegistry", source: str) -> None:
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
    ) -> None:
        """注册一个插件图层；``name`` 唯一，``draw`` 允许为 ``None``。"""
        self._registry._register_spec(
            name,
            label or name,
            description,
            _normalize_defaults(defaults),
            draw,
            self._source,
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

    # -- 只读视图 --------------------------------------------------------- #
    @property
    def specs(self) -> dict[str, PluginSpec]:
        """已注册插件（按名字索引的浅拷贝，避免外部改写注册表）。"""
        return dict(self._specs)

    def get(self, name: str) -> PluginSpec | None:
        """按名字取插件；不存在返回 ``None``。"""
        return self._specs.get(str(name))

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
        """用本注册表里的插件绘制一个图层；未注册/无绘制函数/抛异常时返回 ``False``。"""
        if not isinstance(layer, Mapping):
            return False
        kind = str(layer.get("type") or "")
        if not kind:
            return False
        spec = self._specs.get(kind)
        if spec is None or spec.draw is None:
            return False
        try:
            return bool(spec.draw(image, dict(layer), dict(ctx or {})))
        except KeyboardInterrupt:
            raise
        except BaseException:
            return False

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
        )

    def _register_spec(
        self,
        name: Any,
        label: str,
        description: str,
        defaults: dict[str, Any],
        draw: DrawFunc | None,
        source: str,
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
        self._specs[plugin_name] = PluginSpec(
            name=plugin_name,
            label=str(label or plugin_name),
            description=str(description or ""),
            defaults=dict(defaults),
            draw=draw,
            source=str(source),
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
