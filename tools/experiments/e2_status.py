"""真机实验 E2f：干净地取一条 0x81 状态快照并完整打印（每条命令只读一次回包）。

控制接口对**每条**命令都回一条 441 字节报文（0x0A / 0x0B 回的是命令本身的回显），
所以「先读一次 = 拿到这条命令的回包」是对的；反过来说，如果一次发了多条命令却只读
一条，队列里就会残留旧回包，之后 `query()` 读到的就不是状态快照了。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hydrohud import device  # noqa: E402


def dump(reply: bytes) -> None:
    for index in range(0, len(reply), 16):
        chunk = reply[index : index + 16]
        text = " ".join(f"{byte:02x}" for byte in chunk)
        print(f"  {index:04x}  {text}")


def main() -> int:
    control = device.Control()
    control.open()
    try:
        control.send_command(device.CMD_QUERY)
        reply = control.read_report(1.5)
        if not reply:
            print("没有回包")
            return 1
        print(f"0x81 状态快照：{len(reply)} 字节")
        dump(reply)
        print(f"\n旋转象限 = {device.control_reply_quadrant(reply)}")
    finally:
        control.close()
    time.sleep(0.2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
