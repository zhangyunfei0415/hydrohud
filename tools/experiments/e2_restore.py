"""真机实验 E2e：把官方 480×480 素材写回待机槽位，恢复出厂样式的待机图。

前面两次实验往待机槽位写过测试数据（4 字节和一张自制图），这里用官方素材覆盖回来。
官方待机图路径是 ``led_stby\\led_stby.jpg``（裸 JPEG），所以用官方素材里尺寸相符的
480×480 图片写入是最接近出厂状态的选择。

每条命令后只读一次回包：控制接口是同步句柄，并发/重复读会把句柄堵死。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hydrohud import device  # noqa: E402

VENDOR = Path(r"C:\Program Files (x86)\TcomasSoftware")
CANDIDATES = [VENDOR / "led1" / "10002.jpg", VENDOR / "led3" / "10002.jpg"]


def pick_asset() -> Path:
    for path in CANDIDATES:
        if path.exists():
            return path
    raise SystemExit("找不到官方 480×480 素材")


def describe(reply: bytes | None) -> str:
    if not reply:
        return "无回包"
    return f"len={len(reply)} cmd=0x{reply[1]:02x} 参数={reply[5:14].hex()}"


def main() -> int:
    asset = pick_asset()
    data = asset.read_bytes()
    print(f"官方素材 {asset.name}：{len(data)} 字节", flush=True)

    control = device.Control()
    control.open()
    try:
        control.send_command(
            device.CMD_FILE_ANNOUNCE,
            device._pack_u32(device.FLASH_TARGET_STANDBY) + device._pack_u32(len(data)),
        )
        time.sleep(0.1)
        print("0x0A 回包：", describe(control.read_report(0.8)), flush=True)

        offset = 0
        index = 0
        while offset < len(data):
            block = data[offset : offset + device.FLASH_BLOCK_SIZE]
            percent = min(100, int((offset + len(block)) * 100 / len(data)))
            header = (
                device._pack_u32(offset)
                + device._pack_u16(len(block))
                + device._pack_u16(0)
                + bytes([percent, 0, 0, 0])
            )
            control.send_command(device.CMD_FILE_CHUNK, header + block)
            reply = control.read_report(0.8)
            print(f"  块 {index}: offset={offset} len={len(block)} → {describe(reply)}", flush=True)
            offset += len(block)
            index += 1
        print("写回完成。", flush=True)
    finally:
        control.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
