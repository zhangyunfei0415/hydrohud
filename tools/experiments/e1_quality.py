"""真机实验 E1b：确认固件能否正确显示超过 15 包的高质量画面。

上一轮 e1_chunks.py 里 20/30/46 包都能写入且没有报错；640×640 变体则会让设备掉线。
这个脚本专门做 A/B 对比：同一张 480×480 照片，一张压到 15 包上限（15300 B），
一张压到 46 包上限（45900 B），左下角用小字标出当前是哪一个变体。

如果屏幕上 "46" 那版比 "15" 那版明显更清晰、并且没有花屏/残缺/错位，
就说明固件其实能处理远多于 15 包，MAX_FRAME_BYTES 可以从 15300 提到 45900。

用法（先退出官方 TCOMAS 软件）::

    python tools/experiments/e1_quality.py --seconds 6 --cycles 4
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from lg6studio import device  # noqa: E402

PHOTO = ROOT / "tools" / "_assets" / "demo_photo.png"
FONT = "C:/Windows/Fonts/msyhbd.ttc"

VARIANTS = (("15", 15), ("46", 46))


def tagged(source: Image.Image, tag: str) -> Image.Image:
    """480×480 frame with a small corner tag naming the variant."""
    frame = source.convert("RGB")
    if frame.size != (480, 480):
        frame = device.fit_square(frame, 480)
    draw = ImageDraw.Draw(frame)
    font = ImageFont.truetype(FONT, 34)
    draw.rectangle((6, 430, 110, 474), fill="#000000")
    draw.text((14, 432), tag, font=font, fill="#FFFFFF")
    return frame


def build_variants() -> list[tuple[str, list[bytes]]]:
    source = Image.open(PHOTO)
    result: list[tuple[str, list[bytes]]] = []
    for tag, chunks in VARIANTS:
        limit = chunks * device.CHUNK_SIZE
        frame = tagged(source, f"P{tag}")
        jpeg = device.encode_jpeg(frame, max_bytes=limit)
        payload = jpeg + b"\x00" * (limit - len(jpeg))
        reports = device.build_packets(
            device.CMD_STREAM, payload, device.CHUNK_SIZE, device.REPORT_LEN
        )
        print(f"  变体 {tag}: JPEG {len(jpeg)} B ≤ {limit} B, {len(reports)} 包")
        result.append((tag, reports))
    return result


def push(reports: list[bytes], seconds: float) -> tuple[int, str | None]:
    written = 0
    try:
        with device.Screen() as screen:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                for report in reports:
                    screen.write_report(report)
                written += 1
                time.sleep(1 / 30)
    except OSError as exc:
        return written, str(exc)
    return written, None


def main() -> int:
    parser = argparse.ArgumentParser(description="E1b：15 包 vs 46 包画质对比")
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument("--cycles", type=int, default=4, help="轮播几轮（0 = 无限）")
    args = parser.parse_args()

    print("变体清单：")
    variants = build_variants()
    print()
    print(
        "屏幕左下角会显示当前变体名：P15 是 15 包（15300 B 上限），P46 是 46 包（45900 B 上限）。"
    )
    print("同一张照片、同样 480×480，P46 的压缩质量明显更高。", flush=True)

    cycle = 0
    while args.cycles == 0 or cycle < args.cycles:
        for tag, reports in variants:
            print(f"[{time.strftime('%H:%M:%S')}] 显示 P{tag}", flush=True)
            frames, error = push(reports, args.seconds)
            print(
                f"    P{tag}: {frames} 帧{'，失败: ' + error if error else '，无错误'}", flush=True
            )
        cycle += 1
    print("结束。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
