"""真机实验 E2d：确认固化 flash 上传命令的「回包」语义。

控制接口对 ``0x0A`` / ``0x0B`` 是会回包的（441 字节，报头把命令号回显在偏移 1）。
本脚本每条命令只读一次回包 —— 同步句柄上的 ``ReadFile`` 在读不到数据时会一直阻塞，
并发多次读会把句柄堵死，所以绝不批量 drain。

只写入 4 字节的假数据到待机槽位，确认回包格式后立刻用官方素材复原。
"""

from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hydrohud import device  # noqa: E402


def show(label: str, reply: bytes | None) -> None:
    if not reply:
        print(f"{label}: 无回包")
        return
    head = " ".join(f"{byte:02x}" for byte in reply[:32])
    print(f"{label}: len={len(reply)}  前 32 字节 = {head}")
    if len(reply) >= 14:
        offset, length, _pad = struct.unpack("<IHH", reply[5:13])
        print(
            f"    偏移 5..12 当作 u32/u16/u16 解：offset={offset} length={length} percent={reply[13]}"
        )


def main() -> int:
    control = device.Control()
    control.open()
    try:
        print("=== 0x0A 声明：目标 0，长度 4 ===")
        control.send_command(device.CMD_FILE_ANNOUNCE, struct.pack("<II", 0, 4) + b"\x00" * 0)
        time.sleep(0.1)
        show("0x0A 回包", control.read_report(1.0))

        print("\n=== 0x0B 分块：offset=0 len=4 percent=100 data='TEST' ===")
        header = struct.pack("<IHH", 0, 4, 0) + bytes([100, 0, 0, 0])
        control.send_command(device.CMD_FILE_CHUNK, header + b"TEST")
        time.sleep(0.1)
        show("0x0B 回包", control.read_report(1.0))

        print("\n=== 0x81 查询状态 ===")
        control.send_command(device.CMD_QUERY)
        time.sleep(0.1)
        reply = control.read_report(1.0)
        show("0x81 回包", reply)
        if reply:
            print(f"    旋转象限 = {device.control_reply_quadrant(reply)}")
    finally:
        control.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
