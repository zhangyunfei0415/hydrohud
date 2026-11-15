"""Quick hardware check for the panel (no GUI, prints Chinese results).

Usage::

    python tools/check_device.py            # 只读检查（枚举 + 查询设置）
    python tools/check_device.py --push 4   # 额外推 4 秒纯色测试图

On Windows run it with the official TCOMAS software closed. On Linux install the
udev rule from ``packaging/linux/`` first, otherwise opening the device fails.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from hydrohud import device  # noqa: E402


def _missing_panel_hint() -> str:
    if sys.platform == "win32":
        return "未找到 TCOMAS 水冷屏（1A2C:1520）。检查 USB 连接，或确认设备没被官方软件独占。"
    return (
        "未找到 TCOMAS 水冷屏（1A2C:1520）。检查 USB 连接，并确认已安装 "
        "packaging/linux/ 里的 udev 规则（见 docs/LINUX.md）。"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="水冷屏硬件自检")
    parser.add_argument("--push", type=float, default=0.0, help="推纯色测试图的秒数（0 = 不推）")
    args = parser.parse_args()

    collections = device.enumerate_devices()
    if not collections:
        print(_missing_panel_hint())
        return 1
    for collection in collections:
        print(
            f"找到接口 {collection['interface']:<6} 角色 {collection['role']:<8} "
            f"输出报文 {collection['output_len']} 字节"
        )
    spec = device.detected_spec()
    if spec is not None:
        print(f"设备表：{spec.label}（{spec.key}）· 已验证 {spec.verified}")

    print(f"官方软件在运行：{device.vendor_software_running()}")

    with device.Control() as control:
        reply = control.query()
        quadrant = device.control_reply_quadrant(reply)
        print(
            f"查询设置：收到 {len(reply)} 字节，旋转象限 = {quadrant if quadrant is not None else '?'}"
        )

    if args.push > 0:
        jpeg = device.encode_jpeg(Image.new("RGB", (480, 480), (220, 30, 30)))
        reports = device.frame_reports(jpeg)
        print(f"测试帧：JPEG {len(jpeg)} 字节 → {len(reports)} 个报文")
        written = 0
        deadline = time.monotonic() + args.push
        with device.Screen() as screen:
            while time.monotonic() < deadline:
                screen.send_jpeg(jpeg)
                written += 1
                time.sleep(1 / 30)
        print(f"已推送 {written} 帧，屏幕应显示红色测试图（停止后约 10 秒回看门狗黑屏）")

    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
