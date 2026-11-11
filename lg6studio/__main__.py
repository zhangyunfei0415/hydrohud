"""``python -m lg6studio`` 的入口。

只做参数转发；真正的窗口与命令行参数在 :mod:`lg6studio.app` 里。
"""

from __future__ import annotations

import os
import sys

try:
    from .app import main
except ImportError:  # 直接运行本文件（python lg6studio/__main__.py）时没有包上下文
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from lg6studio.app import main


if __name__ == "__main__":
    raise SystemExit(main())
