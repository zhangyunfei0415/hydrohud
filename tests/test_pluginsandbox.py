"""Tests for :mod:`hydrohud.pluginsandbox` (permission audit + restricted imports; no GUI)."""

from __future__ import annotations

import builtins
import threading
from pathlib import Path

import pytest

from hydrohud import plugins, pluginsandbox

SAFE_PLUGIN = """
import math
import random
import time

from PIL import Image, ImageDraw


def draw(image, layer, ctx):
    value = math.sin(ctx.get("t", 0.0) * 2.0) * math.pi
    color = int(128 + 127 * value)
    ImageDraw.Draw(image).rectangle((0, 0, 10, 10), fill=(color, 0, 0))
    return True


PLUGIN = {"name": "safe", "label": "安全插件", "draw": draw}
"""


def _write(directory: Path, name: str, text: str) -> Path:
    """Write ``directory/name`` (UTF-8) and return the path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def _symbols(findings: tuple[pluginsandbox.Finding, ...]) -> set[str]:
    """The set of symbols in ``findings``."""
    return {finding.symbol for finding in findings}


# -- audit_source: safe sources -------------------------------------------- #


def test_safe_drawing_plugin_has_no_findings() -> None:
    assert pluginsandbox.audit_source(SAFE_PLUGIN) == ()


def test_benign_standard_library_imports_are_not_flagged() -> None:
    source = (
        "import datetime\n"
        "import json\n"
        "import math\n"
        "import re\n"
        "import statistics\n"
        "import time\n"
        "import unicodedata\n"
    )

    assert pluginsandbox.audit_source(source) == ()


def test_linux_style_imports_are_not_flagged() -> None:
    source = "from collections import OrderedDict\nfrom typing import Any\n\nX = OrderedDict()\n"

    assert pluginsandbox.audit_source(source) == ()


def test_string_literals_and_comments_do_not_trigger_findings() -> None:
    source = (
        "# 这里只是提到 socket 和 open( 两个词，不该被审计\n"
        'HELP = "不要调用 socket.socket 或 open()"\n'
        'DOC = """subprocess.Popen 是危险符号"""\n'
    )

    assert pluginsandbox.audit_source(source) == ()


# -- audit_source: network ------------------------------------------------- #


def test_import_socket_is_network() -> None:
    findings = pluginsandbox.audit_source("import socket\n")

    assert findings == (pluginsandbox.Finding("network", "socket", 1),)


def test_import_urllib_request_is_network() -> None:
    findings = pluginsandbox.audit_source("import urllib.request\n")

    assert findings == (pluginsandbox.Finding("network", "urllib.request", 1),)


def test_requests_and_asyncio_calls_are_network() -> None:
    source = "import requests\n\nresponse = requests.get('http://example.com')\n"

    findings = pluginsandbox.audit_source(source)

    assert {finding.permission for finding in findings} == {"network"}
    assert _symbols(findings) == {"requests", "requests.get"}


def test_socket_call_without_import_is_still_network() -> None:
    source = "import socket\n\nsock = socket.socket()\n"

    findings = pluginsandbox.audit_source(source)

    assert [finding.permission for finding in findings] == ["network", "network"]
    assert _symbols(findings) == {"socket", "socket.socket"}


# -- audit_source: filesystem ---------------------------------------------- #


def test_builtin_open_is_filesystem() -> None:
    source = 'handle = open("a.txt", "w")\nhandle.close()\n'

    findings = pluginsandbox.audit_source(source)

    assert findings == (pluginsandbox.Finding("filesystem", "open", 1),)


def test_import_os_is_filesystem_by_conservative_rule() -> None:
    findings = pluginsandbox.audit_source("import os\n")

    assert findings == (pluginsandbox.Finding("filesystem", "os", 1),)


def test_eval_exec_and_compile_are_filesystem() -> None:
    source = "eval('1 + 1')\nexec('x = 1')\ncompile('x = 1', '<s>', 'exec')\n"

    findings = pluginsandbox.audit_source(source)

    assert {finding.permission for finding in findings} == {"filesystem"}
    assert _symbols(findings) == {"eval", "exec", "compile"}


def test_dynamic_import_of_os_is_filesystem() -> None:
    findings = pluginsandbox.audit_source('module = __import__("os")\n')

    assert {finding.permission for finding in findings} == {"filesystem"}
    assert _symbols(findings) == {"__import__", "os"}


def test_importlib_import_module_is_audited() -> None:
    findings = pluginsandbox.audit_source(
        'import importlib\n\nmod = importlib.import_module("socket")\n'
    )

    assert {finding.permission for finding in findings} == {"network"}
    assert "socket" in _symbols(findings)


# -- audit_source: subprocess ---------------------------------------------- #


def test_os_system_is_subprocess() -> None:
    source = "import os\n\nos.system('calc')\n"

    findings = pluginsandbox.audit_source(source)

    assert {finding.permission for finding in findings} == {"filesystem", "subprocess"}
    assert "os.system" in _symbols(findings)


def test_from_os_import_system_is_subprocess() -> None:
    source = "from os import system\n\nsystem('calc')\n"

    findings = pluginsandbox.audit_source(source)

    assert {"filesystem", "subprocess"} == {finding.permission for finding in findings}
    assert "os.system" in _symbols(findings)


def test_subprocess_popen_is_subprocess() -> None:
    source = "import subprocess\n\nsubprocess.Popen(['calc'])\n"

    findings = pluginsandbox.audit_source(source)

    assert {finding.permission for finding in findings} == {"subprocess"}
    assert _symbols(findings) == {"subprocess", "subprocess.Popen"}


def test_multiprocessing_and_ctypes_are_subprocess() -> None:
    source = "import ctypes\nimport multiprocessing\n\nmultiprocessing.Process(target=print)\n"

    findings = pluginsandbox.audit_source(source)

    assert {finding.permission for finding in findings} == {"subprocess"}
    assert _symbols(findings) == {"ctypes", "multiprocessing", "multiprocessing.Process"}


# -- audit_source: line numbers & fallback --------------------------------- #


def test_line_numbers_point_at_the_offending_line() -> None:
    source = (
        "import math\n"
        "import socket\n"
        "\n"
        "def draw(image, layer, ctx):\n"
        '    with open("a.txt", "w") as handle:\n'
        '        handle.write("x")\n'
        "    return True\n"
    )

    findings = pluginsandbox.audit_source(source)
    by_symbol = {finding.symbol: finding.line for finding in findings}

    assert by_symbol["socket"] == 2
    assert by_symbol["open"] == 5
    assert all(finding.line > 0 for finding in findings)


def test_syntax_error_yields_unparseable_finding() -> None:
    findings = pluginsandbox.audit_source("def draw(:\n    pass\n")

    assert pluginsandbox.Finding("filesystem", "unparseable", 0) in findings
    assert {finding.permission for finding in findings} == {"filesystem"}


def test_syntax_error_fallback_still_reports_dangerous_symbols() -> None:
    source = "import socket\nimport subprocess\n\ndef draw(:\n    pass\n"

    findings = pluginsandbox.audit_source(source)

    assert "unparseable" in _symbols(findings)
    assert {"socket", "subprocess"} <= _symbols(findings)
    assert {"network", "subprocess"} <= {finding.permission for finding in findings}


# -- audit_file ------------------------------------------------------------ #


def test_audit_file_audits_real_files(tmp_path: Path) -> None:
    path = _write(tmp_path, "net_plugin.py", "import socket\n\nsock = socket.socket()\n")
    safe = _write(tmp_path, "safe_plugin.py", SAFE_PLUGIN)

    assert {finding.permission for finding in pluginsandbox.audit_file(path)} == {"network"}
    assert pluginsandbox.audit_file(safe) == ()
    assert pluginsandbox.audit_file(str(path)) == pluginsandbox.audit_file(path)


def test_audit_file_missing_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        pluginsandbox.audit_file(tmp_path / "missing.py")


# -- declared_permissions -------------------------------------------------- #


def test_declared_permissions_accepts_mapping_and_raw_values() -> None:
    assert pluginsandbox.declared_permissions({"permissions": ["network"]}) == ("network",)
    assert pluginsandbox.declared_permissions({"permissions": "filesystem"}) == ("filesystem",)
    assert pluginsandbox.declared_permissions({"permissions": [" Subprocess ", "NETWORK"]}) == (
        "subprocess",
        "network",
    )
    assert pluginsandbox.declared_permissions({}) == ()
    assert pluginsandbox.declared_permissions(["network", "network"]) == ("network",)


def test_declared_permissions_accepts_plugin_spec() -> None:
    spec = plugins.PluginSpec(name="p", label="p", permissions=("filesystem", "network", "network"))

    assert pluginsandbox.declared_permissions(spec) == ("filesystem", "network")


def test_declared_permissions_handles_none_and_invalid_values() -> None:
    assert pluginsandbox.declared_permissions(None) == ()
    assert pluginsandbox.declared_permissions({"permissions": None}) == ()
    assert pluginsandbox.declared_permissions({"permissions": 42}) == ()
    assert pluginsandbox.declared_permissions({"permissions": ["bogus", "root"]}) == ()
    assert pluginsandbox.declared_permissions(object()) == ()


# -- required / undeclared / summarize ------------------------------------- #


def test_required_permissions_dedupes_and_orders() -> None:
    findings = (
        pluginsandbox.Finding("subprocess", "subprocess.Popen", 9),
        pluginsandbox.Finding("network", "socket", 1),
        pluginsandbox.Finding("network", "requests", 2),
        pluginsandbox.Finding("filesystem", "open", 3),
    )

    assert pluginsandbox.required_permissions(findings) == (
        "network",
        "filesystem",
        "subprocess",
    )
    assert pluginsandbox.required_permissions(()) == ()


def test_undeclared_permissions_positive_and_negative() -> None:
    findings = pluginsandbox.audit_source("import socket\n\nopen('a.txt', 'w')\n")

    assert pluginsandbox.undeclared_permissions({"permissions": ["filesystem"]}, findings) == (
        "network",
    )
    assert pluginsandbox.undeclared_permissions({"permissions": ["network"]}, findings) == (
        "filesystem",
    )
    assert (
        pluginsandbox.undeclared_permissions({"permissions": ["network", "filesystem"]}, findings)
        == ()
    )
    assert pluginsandbox.undeclared_permissions({}, ()) == ()


def test_summarize_three_forms() -> None:
    assert pluginsandbox.summarize({}, ()) == "只用了安全能力（无网络 / 文件 / 子进程调用）"
    network_findings = pluginsandbox.audit_source("import socket\n")
    assert (
        pluginsandbox.summarize({"permissions": ["network"]}, network_findings)
        == "声明了 网络访问；静态审计未发现越界调用"
    )
    mixed = pluginsandbox.audit_source("import socket\n\nopen('a.txt')\n")
    assert (
        pluginsandbox.summarize({"permissions": ["filesystem"]}, mixed)
        == "声明了 文件读写，但代码里还用了 网络访问 —— 建议不要安装"
    )
    assert (
        pluginsandbox.summarize({}, mixed)
        == "没有声明任何权限，但代码里还用了 网络访问、文件读写 —— 建议不要安装"
    )


# -- restricted_imports ---------------------------------------------------- #


def test_restricted_imports_blocks_undeclared_network() -> None:
    with pytest.raises(PermissionError) as excinfo:
        with pluginsandbox.restricted_imports({}):
            import socket  # noqa: F401
    message = str(excinfo.value)
    assert "网络访问" in message
    assert "network" in message
    assert "socket" in message


def test_restricted_imports_blocks_undeclared_os() -> None:
    with pytest.raises(PermissionError):
        with pluginsandbox.restricted_imports({"permissions": ["network"]}):
            import os  # noqa: F401


def test_restricted_imports_allows_declared_permissions() -> None:
    with pluginsandbox.restricted_imports({"permissions": ["network", "filesystem"]}):
        import os  # noqa: F401
        import socket  # noqa: F401


def test_restricted_imports_whitelist_is_always_importable() -> None:
    with pluginsandbox.restricted_imports({}):
        import json  # noqa: F401
        import math  # noqa: F401
        import random  # noqa: F401
        from collections import OrderedDict  # noqa: F401

        from PIL import Image  # noqa: F401


def test_restricted_imports_restores_import_on_normal_exit() -> None:
    original = builtins.__import__

    with pluginsandbox.restricted_imports({}):
        assert builtins.__import__ is not original

    assert builtins.__import__ is original


def test_restricted_imports_restores_import_on_exception() -> None:
    original = builtins.__import__

    with pytest.raises(ValueError):
        with pluginsandbox.restricted_imports({"permissions": ["network"]}):
            raise ValueError("插件炸了")

    assert builtins.__import__ is original


def test_restricted_imports_nests_and_restores_each_level() -> None:
    original = builtins.__import__

    with pluginsandbox.restricted_imports({"permissions": ["network"]}):
        outer = builtins.__import__
        assert outer is not original
        with pluginsandbox.restricted_imports({"permissions": ["network", "filesystem"]}):
            inner = builtins.__import__
            assert inner is not outer
            import socket  # noqa: F401  内层与外层都允许 network

            # 内层放行 filesystem，但外层没声明，累积后仍然拦截。
            with pytest.raises(PermissionError):
                import os  # noqa: F401

        assert builtins.__import__ is outer
        import socket  # noqa: F401

    assert builtins.__import__ is original


def test_restricted_imports_nested_exception_restores_both_levels() -> None:
    original = builtins.__import__

    with pytest.raises(PermissionError):
        with pluginsandbox.restricted_imports({"permissions": ["network"]}):
            with pluginsandbox.restricted_imports({"permissions": ["filesystem"]}):
                import socket  # noqa: F401  两层都不放行 network

    assert builtins.__import__ is original


def test_restricted_imports_threads_do_not_leak_a_replacement() -> None:
    original = builtins.__import__
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(50):
                with pluginsandbox.restricted_imports({"permissions": ["network"]}):
                    import math  # noqa: F401
                    import socket  # noqa: F401
        except BaseException as exc:  # 线程里的异常要带回主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert builtins.__import__ is original


def test_restricted_imports_does_not_block_unrelated_modules() -> None:
    with pluginsandbox.restricted_imports({}):
        import pathlib  # noqa: F401
        import string  # noqa: F401
        import textwrap  # noqa: F401


def test_finding_is_frozen() -> None:
    finding = pluginsandbox.Finding("network", "socket", 3)

    with pytest.raises(Exception):
        finding.line = 4  # type: ignore[misc]
