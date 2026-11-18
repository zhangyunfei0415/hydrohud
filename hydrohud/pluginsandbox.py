"""HydroHUD 插件权限审计与受限导入（``hydrohud.plugins`` 的辅助模块）。

``plugins.py`` 里插件的 ``permissions`` 只是一句声明：界面拿它做提示，进程本身不做任何
强制。本模块补上两件互相独立的能力：

- **静态审计**：:func:`audit_source` / :func:`audit_file` 用 ``ast`` 解析插件源码，找出
  网络 / 文件 / 子进程相关的导入与调用，得到插件「实际需要」的权限清单；
- **受限导入**：:func:`restricted_imports` 在插件导入或渲染期间临时替换
  ``builtins.__import__``，拦截未声明的危险模块，把声明变成一次真正的检查。

权限词（也是 :data:`AUDIT_PERMISSIONS` 的顺序与 :func:`required_permissions` 的排序依据）：

- ``network``（网络访问）：``socket`` / ``ssl`` / ``urllib`` / ``http`` / ``ftplib`` /
  ``smtplib`` / ``telnetlib`` / ``xmlrpc`` / ``requests`` / ``httpx`` / ``aiohttp`` /
  ``websockets`` / ``asyncio``；
- ``filesystem``（文件读写）：整个 ``os`` 模块，以及内置 ``open`` / ``eval`` / ``exec`` /
  ``compile`` / ``__import__``（后四个是「进程内不受限能力」，与文件同属一类）；
- ``subprocess``（启动子进程）：``subprocess`` / ``multiprocessing`` / ``ctypes``，以及
  ``os.system`` / ``os.popen`` / ``os.exec*`` / ``os.spawn*``。

**误报取舍**：``import os`` 算 filesystem 是刻意的保守判断——``os.remove`` /
``os.open`` / ``os.listdir`` 都在这个模块里，静态审计无法便宜地区分「只用
``os.environ``」和「删文件」，所以整体标记。反过来 ``pathlib`` / ``shutil`` /
``tempfile`` / ``glob`` / ``io`` 这类常用模块刻意**不**标记，把误报压下来；
``import math`` / ``import time`` / ``from PIL import Image, ImageDraw`` 一律零发现。
``SyntaxError`` 时退化为正则扫描，并至少产出一条 ``symbol="unparseable"`` 的
filesystem 发现，表示「无法静态确认，按最高风险处理」。

**这不是安全沙箱。** :func:`restricted_imports` 只挡住 ``builtins.__import__`` 这一条
路径：已经导入的模块、``importlib`` 的底层导入函数、``ctypes`` 执行的原生代码、插件里的
``exec`` / ``eval`` 都绕得过去。它的目标是「降低误装风险 + 给用户一份可读的权限清单」，
不是把不可信代码关进笼子。
"""

from __future__ import annotations

import ast
import builtins
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "AUDIT_PERMISSIONS",
    "Finding",
    "audit_file",
    "audit_source",
    "declared_permissions",
    "required_permissions",
    "restricted_imports",
    "summarize",
    "undeclared_permissions",
]

#: 审计认可的权限词；同时也是 :func:`required_permissions` 的排序依据。
AUDIT_PERMISSIONS: tuple[str, ...] = ("network", "filesystem", "subprocess")

_PERMISSION_LABELS: dict[str, str] = {
    "network": "网络访问",
    "filesystem": "文件读写",
    "subprocess": "启动子进程",
}

#: 危险模块第一段 → 所需权限。``os`` 在模块层面整体算 filesystem（保守口径，见模块文档）。
_MODULE_PERMISSIONS: dict[str, str] = {
    "socket": "network",
    "ssl": "network",
    "urllib": "network",
    "http": "network",
    "ftplib": "network",
    "smtplib": "network",
    "telnetlib": "network",
    "xmlrpc": "network",
    "requests": "network",
    "httpx": "network",
    "aiohttp": "network",
    "websockets": "network",
    "asyncio": "network",
    "os": "filesystem",
    "subprocess": "subprocess",
    "multiprocessing": "subprocess",
    "ctypes": "subprocess",
}

#: 「进程内不受限能力」的内置函数，统一算 filesystem。
_FILESYSTEM_BUILTINS: frozenset[str] = frozenset({"open", "eval", "exec", "compile", "__import__"})

#: ``os`` 下真正会拉起外部进程的属性名（``os.exec*`` / ``os.spawn*`` 另按前缀判断）。
_OS_SUBPROCESS_ATTRS: frozenset[str] = frozenset({"system", "popen"})
_OS_SUBPROCESS_PREFIXES: tuple[str, ...] = ("exec", "spawn")

#: :func:`restricted_imports` 永远放行的模块：常见于绘制插件，且本身无网络/进程能力。
_IMPORT_WHITELIST: frozenset[str] = frozenset(
    {
        "abc",
        "bisect",
        "collections",
        "colorsys",
        "copy",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "fractions",
        "functools",
        "heapq",
        "itertools",
        "json",
        "logging",
        "math",
        "PIL",
        "random",
        "re",
        "statistics",
        "string",
        "textwrap",
        "time",
        "typing",
        "unicodedata",
        "warnings",
    }
)

#: 保护 ``builtins.__import__`` 的保存/替换/恢复：嵌套（同线程可重入）与多线程都安全。
_IMPORT_LOCK = threading.RLock()

#: 区分「对象没有 permissions 字段」与「字段值是 None」。
_MISSING = object()


@dataclass(frozen=True)
class Finding:
    """一条静态审计发现。

    :param permission: 需要的权限词，取值见 :data:`AUDIT_PERMISSIONS`；
    :param symbol: 触发它的符号，如 ``"socket.socket"`` / ``"open"`` / ``"subprocess"``；
    :param line: 1 起的行号；无法定位（例如语法错误后的兜底发现）时为 ``0``。
    """

    permission: str
    symbol: str
    line: int


def _line_of(text: str, offset: int) -> int:
    """字符偏移量 ``offset`` 对应的 1 起行号。"""
    return text.count("\n", 0, offset) + 1


def _finalize(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    """按 ``(权限, 符号, 行号)`` 去重，并按行号 / 符号 / 权限稳定排序。"""
    unique: list[Finding] = []
    seen: set[tuple[str, str, int]] = set()
    for finding in findings:
        key = (finding.permission, finding.symbol, finding.line)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    unique.sort(key=lambda finding: (finding.line, finding.symbol, finding.permission))
    return tuple(unique)


def _dotted(node: ast.AST) -> str | None:
    """把 ``a.b.c`` 形式的属性链还原成点号字符串；基座不是名字时返回 ``None``。"""
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _module_permission(module_name: str) -> str | None:
    """模块名（可带点）第一段对应的权限；不在危险模块表里时返回 ``None``。"""
    root = str(module_name).split(".", 1)[0]
    return _MODULE_PERMISSIONS.get(root)


def _string_literal(node: ast.AST) -> str | None:
    """节点是字符串字面量时返回其内容，否则 ``None``。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_os_subprocess_name(name: str) -> bool:
    """``os`` 下这个名字是否拉起外部进程（``system`` / ``popen`` / ``exec*`` / ``spawn*``）。"""
    return name in _OS_SUBPROCESS_ATTRS or name.startswith(_OS_SUBPROCESS_PREFIXES)


class _Auditor(ast.NodeVisitor):
    """AST 遍历器：只认真正的导入语句与调用表达式，收集 :class:`Finding`。"""

    def __init__(self) -> None:
        self.findings: list[Finding] = []
        #: ``from importlib import import_module`` 之类在本地引入的动态导入函数名。
        self._import_module_names: set[str] = set()

    def _add(self, permission: str, symbol: str, line: int) -> None:
        """记录一条发现（去重交给 :func:`_finalize`）。"""
        self.findings.append(Finding(permission=permission, symbol=symbol, line=line))

    def visit_Import(self, node: ast.Import) -> None:
        """``import X`` / ``import X.Y``：按完整模块名检查第一段。"""
        for alias in node.names:
            permission = _module_permission(alias.name)
            if permission is not None:
                self._add(permission, alias.name, node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """``from X import ...``：相对导入跳过（那是插件本地模块，不是标准库）。"""
        module = node.module or ""
        if node.level == 0 and module:
            permission = _module_permission(module)
            if permission is not None:
                self._add(permission, module, node.lineno)
            if module == "os":
                # ``from os import system`` 之后直接调 ``system(...)``：按属性名归类。
                for alias in node.names:
                    if _is_os_subprocess_name(alias.name):
                        self._add("subprocess", f"os.{alias.name}", node.lineno)
            if module == "importlib":
                for alias in node.names:
                    if alias.name in ("import_module", "__import__"):
                        self._import_module_names.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """内置函数、模块属性调用、动态导入三种触发方式。"""
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in _FILESYSTEM_BUILTINS:
                self._add("filesystem", func.id, node.lineno)
            if func.id == "__import__" or func.id in self._import_module_names:
                self._add_dynamic_target(node)
        elif isinstance(func, ast.Attribute):
            chain = _dotted(func)
            if chain is not None:
                self._add_call_chain(chain, node.lineno)
                if chain in ("importlib.import_module", "importlib.__import__"):
                    self._add_dynamic_target(node)
        self.generic_visit(node)

    def _add_dynamic_target(self, node: ast.Call) -> None:
        """``__import__("os")`` / ``import_module("socket")`` 的字符串参数当作导入目标。"""
        target = _string_literal(node.args[0]) if node.args else None
        if target is None:
            for keyword in node.keywords:
                if keyword.arg == "name":
                    target = _string_literal(keyword.value)
        if not target:
            return
        permission = _module_permission(target)
        if permission is not None:
            self._add(permission, target, node.lineno)

    def _add_call_chain(self, chain: str, line: int) -> None:
        """按属性调用链的根模块归类；``os`` 单独看属性名。"""
        root, _, tail = chain.partition(".")
        if root == "os":
            if _is_os_subprocess_name(tail.split(".", 1)[0]):
                self._add("subprocess", chain, line)
            return
        permission = _MODULE_PERMISSIONS.get(root)
        if permission is not None:
            self._add(permission, chain, line)


_DANGER_MODULE_PATTERN = "|".join(
    re.escape(name) for name in sorted(_MODULE_PERMISSIONS, key=len, reverse=True)
)
_FALLBACK_MODULE_RE = re.compile(rf"\b(?:{_DANGER_MODULE_PATTERN})\b")
_FALLBACK_BUILTIN_RE = re.compile(r"\b(open|eval|exec|compile|__import__)\s*\(")
_FALLBACK_OS_SUBPROCESS_RE = re.compile(r"\bos\.(?:system|popen|exec\w*|spawn\w*)\b")


def _fallback_audit(text: str) -> tuple[Finding, ...]:
    """``SyntaxError`` 后的正则兜底：扫危险符号，并固定报告一条 ``unparseable``。"""
    findings: list[Finding] = [Finding("filesystem", "unparseable", 0)]
    for match in _FALLBACK_MODULE_RE.finditer(text):
        permission = _MODULE_PERMISSIONS.get(match.group(0))
        if permission is not None:
            findings.append(Finding(permission, match.group(0), _line_of(text, match.start())))
    for match in _FALLBACK_OS_SUBPROCESS_RE.finditer(text):
        findings.append(Finding("subprocess", match.group(0), _line_of(text, match.start())))
    for match in _FALLBACK_BUILTIN_RE.finditer(text):
        findings.append(Finding("filesystem", match.group(1), _line_of(text, match.start())))
    return _finalize(findings)


def audit_source(text: str) -> tuple[Finding, ...]:
    """审计一段插件源码，返回全部发现（按行号、符号排序并去重）。

    解析成功后按 AST 遍历，只认真正的导入语句与调用表达式，所以文档字符串或普通字符串里
    出现的 ``socket`` / ``open(`` 不会误报。``SyntaxError``（以及源码含 NUL 字节等
    ``ValueError``）时退化为正则扫描，并固定产出一条 ``symbol="unparseable"``、
    ``line=0`` 的 filesystem 发现，表示无法静态确认。

    :param text: 插件源码；
    :returns: 发现元组，安全插件返回空元组。
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return _fallback_audit(text)
    auditor = _Auditor()
    auditor.visit(tree)
    return _finalize(auditor.findings)


def audit_file(path: str | Path) -> tuple[Finding, ...]:
    """审计磁盘上的插件文件。

    UTF-8 解码，坏字节用替换字符兜住，因此二进制/损坏文件会走
    :func:`_fallback_audit` 而不是在这里抛解码异常。

    :param path: 插件文件路径，``str`` 或 :class:`pathlib.Path`；
    :returns: 同 :func:`audit_source`；
    :raises OSError: 文件不存在或不可读。
    """
    return audit_source(Path(path).read_bytes().decode("utf-8", "replace"))


def declared_permissions(spec_or_raw: Any) -> tuple[str, ...]:
    """读取插件声明的权限，归一成去重后的权限词元组。

    兼容 :class:`hydrohud.plugins.PluginSpec`（取 ``permissions`` 字段）、带
    ``permissions`` 键的 dict，以及 ``"network"`` / ``["network"]`` 这类原始值。归一规则
    与 ``plugins._normalize_permissions`` 一致（去空白、转小写、去重、保序），但只保留
    :data:`AUDIT_PERMISSIONS` 里的词，非法类型一律当作空声明。这里刻意不 import
    ``hydrohud.plugins``，避免插件系统与本模块互相依赖。

    :param spec_or_raw: 插件元数据或原始权限声明；
    :returns: 权限词元组，可能为空。
    """
    raw = spec_or_raw
    if isinstance(raw, Mapping):
        raw = raw.get("permissions")
    else:
        attribute = getattr(raw, "permissions", _MISSING)
        if attribute is not _MISSING:
            raw = attribute
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if raw is None:
        return ()
    if isinstance(raw, str):
        items: Sequence[Any] = [raw]
    elif isinstance(raw, Sequence):
        items = raw
    else:
        return ()
    result: list[str] = []
    for item in items:
        text = str(item).strip().lower()
        if text in AUDIT_PERMISSIONS and text not in result:
            result.append(text)
    return tuple(result)


def required_permissions(findings: Iterable[Finding]) -> tuple[str, ...]:
    """静态审计发现「实际需要」的权限：去重并按 :data:`AUDIT_PERMISSIONS` 顺序排列。"""
    used = {
        finding.permission
        for finding in findings
        if isinstance(getattr(finding, "permission", None), str)
    }
    return tuple(permission for permission in AUDIT_PERMISSIONS if permission in used)


def undeclared_permissions(declared: Any, findings: Iterable[Finding]) -> tuple[str, ...]:
    """审计发现里用了、但插件没有声明的权限（按 :data:`AUDIT_PERMISSIONS` 排序）。

    :param declared: 权限声明，接受 :func:`declared_permissions` 的全部输入；
    :param findings: 审计发现；
    :returns: 越界权限元组；没有越界时为空元组。
    """
    declared_names = set(declared_permissions(declared))
    return tuple(
        permission
        for permission in required_permissions(findings)
        if permission not in declared_names
    )


def summarize(declared: Any, findings: Iterable[Finding]) -> str:
    """把声明与审计拼成一行中文，给界面状态栏或日志用。

    三种形态：只用了安全能力、声明后未越界、发现越界（建议不要安装）。

    :param declared: 权限声明，接受 :func:`declared_permissions` 的全部输入；
    :param findings: 审计发现；
    :returns: 一行中文摘要，不含换行。
    """
    declared_names = declared_permissions(declared)
    used = required_permissions(findings)
    missing = tuple(name for name in used if name not in declared_names)
    if not declared_names and not missing:
        return "只用了安全能力（无网络 / 文件 / 子进程调用）"
    if not missing:
        return f"声明了 {_labels(declared_names)}；静态审计未发现越界调用"
    if not declared_names:
        return f"没有声明任何权限，但代码里还用了 {_labels(missing)} —— 建议不要安装"
    return f"声明了 {_labels(declared_names)}，但代码里还用了 {_labels(missing)} —— 建议不要安装"


def _labels(permissions: Iterable[str]) -> str:
    """权限词元组拼成「网络访问、文件读写」这种中文列表。"""
    return "、".join(_PERMISSION_LABELS.get(permission, permission) for permission in permissions)


def _missing_permission_message(name: str, permission: str) -> str:
    """拦截导入时抛出的中文错误消息：写清缺哪个权限、哪个模块。"""
    label = _PERMISSION_LABELS.get(permission, permission)
    return (
        f"插件未声明「{label}」（{permission}）权限，禁止导入 {name!r}；"
        f"确实需要时请在插件的 permissions 里声明 {permission!r}"
    )


def _make_import_guard(previous: Callable[..., Any], allowed: frozenset[str]) -> Callable[..., Any]:
    """构造替换 :func:`builtins.__import__` 的守卫；``previous`` 是进入时保存的原函数。"""
    # 放行一次被许可的导入后，它内部还会再导入依赖（例如 ``random`` 会导入 ``os``），
    # 那些子导入不能再按插件权限拦截，否则白名单模块自己都导不进来。深度按线程计，
    # 避免别的线程同时导入时把计数器搅乱。
    state = threading.local()

    def guard(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: Sequence[str] | None = (),
        level: int = 0,
    ) -> Any:
        """先按权限表检查第一段模块名，放行时转交 ``previous`` 完成真正的导入。"""
        depth = getattr(state, "depth", 0)
        root = str(name).split(".", 1)[0]
        permission = _MODULE_PERMISSIONS.get(root)
        if (
            depth == 0
            and permission is not None
            and level == 0
            and root not in _IMPORT_WHITELIST
            and permission not in allowed
        ):
            raise PermissionError(_missing_permission_message(str(name), permission))
        state.depth = depth + 1
        try:
            return previous(name, globals, locals, fromlist, level)
        finally:
            state.depth = depth

    return guard


@contextmanager
def restricted_imports(declared: Any) -> Iterator[None]:
    """临时替换 :func:`builtins.__import__`，拦截未声明的危险模块。

    进入时保存当前 ``builtins.__import__`` 并换成守卫；守卫放行白名单模块与已声明的权限，
    其它命中 :data:`_MODULE_PERMISSIONS` 的模块抛 :exc:`PermissionError`（中文消息写明缺
    哪个权限）。退出时（含异常路径、含嵌套）恢复进入前的那个函数对象，不会把别人的替换
    覆盖掉。

    嵌套时限制是**累积**的：内层把外层守卫当作「原始导入」，两层都放行才真正导入。
    模块级 :data:`_IMPORT_LOCK` 保护替换/恢复，所以多个线程各自进入退出不会把
    ``builtins.__import__`` 永久换掉；代价是同一时刻只有一个线程处在受限区间内，而不是
    各线程互相隔离——被替换的是进程级全局，这一点无法用锁消除。

    :param declared: 权限声明，接受 :func:`declared_permissions` 的全部输入；
    :returns: 上下文管理器，``with`` 体内生效。
    """
    allowed = frozenset(declared_permissions(declared))
    with _IMPORT_LOCK:
        previous = builtins.__import__
        builtins.__import__ = _make_import_guard(previous, allowed)
        try:
            yield
        finally:
            builtins.__import__ = previous
