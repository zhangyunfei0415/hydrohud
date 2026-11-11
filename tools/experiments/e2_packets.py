"""真机实验 E1d：慢速二选一，判定固件是否会在 15300 字节处截断画面。

只播两个画面，每个 10 秒，画面上方用超大字体写着自己是谁：

    G15  把画质压到 15300 B 以内、15 包           —— 必然完整（参照）
    F46  质量 100 的 ~45 KB JPEG、46 包           —— 若固件只看前 15300 B，下半部就会缺失

两个画面底部都有同一条绿字 "BOTTOM OK!"，左侧有每 60 px 的刻度数字。
只要盯住 F46 出现的时候：绿字在不在、刻度到不到 480。

用法（先退出官方 TCOMAS 软件）::

    python tools/experiments/e2_packets.py --seconds 10 --cycles 0
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from hydrohud import device  # noqa: E402

PHOTO = ROOT / "tools" / "_assets" / "demo_photo.png"
FONT = "C:/Windows/Fonts/msyhbd.ttc"

FULL_LIMIT = 46920  # 46 包 × 1020


def plate(tag: str) -> Image.Image:
    """480×480 test image: giant variant tag on top, ruler on the left, banner at the bottom."""
    frame = Image.open(PHOTO).convert("RGB").resize((480, 480), Image.LANCZOS)
    draw = ImageDraw.Draw(frame)
    small = ImageFont.truetype(FONT, 24)
    huge = ImageFont.truetype(FONT, 96)
    banner = ImageFont.truetype(FONT, 40)
    for y in range(60, 481, 60):
        draw.line((0, y - 2, 480, y - 2), fill="#000000", width=3)
        draw.rectangle((4, y - 24, 58, y + 2), fill="#000000")
        draw.text((8, y - 23), str(y), font=small, fill="#FFFF00")
    draw.rectangle((0, 0, 480, 118), fill="#000000")
    draw.text((10, 6), tag, font=huge, fill="#FF2D55")
    draw.rectangle((0, 404, 480, 480), fill="#000000")
    draw.text((40, 414), "BOTTOM OK!", font=banner, fill="#00FF66")
    return frame


def encode(frame: Image.Image, limit: int) -> bytes:
    for quality in (100, 96, 92, 88, 84, 80, 76, 72, 68, 64, 60, 50, 40, 30):
        buf = io.BytesIO()
        frame.save(buf, format="JPEG", quality=quality, optimize=True, subsampling=2)
        if buf.tell() <= limit:
            return buf.getvalue()
    buf = io.BytesIO()
    frame.save(buf, format="JPEG", quality=20, optimize=True, subsampling=2)
    return buf.getvalue()


def make_reports(jpeg: bytes, chunks: int) -> list[bytes]:
    limit = chunks * device.CHUNK_SIZE
    payload = jpeg + b"\x00" * (limit - len(jpeg))
    return device.build_packets(device.CMD_STREAM, payload, device.CHUNK_SIZE, device.REPORT_LEN)


def build_variants() -> list[tuple[str, list[bytes], str]]:
    g15_frame = plate("G15")
    f46_frame = plate("F46")
    g15 = encode(g15_frame, 15300)
    f46 = encode(f46_frame, FULL_LIMIT)
    print(f"G15 JPEG {len(g15)} B / 15 包")
    print(f"F46 JPEG {len(f46)} B / {max(1, -(-len(f46) // device.CHUNK_SIZE))} 包")
    return [
        ("G15", make_reports(g15, 15), f"质量压缩到 {len(g15)} B，15 包"),
        ("F46", make_reports(f46, 46), f"质量 100 的 {len(f46)} B，46 包"),
    ]


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
    parser = argparse.ArgumentParser(description="E1d：G15 vs F46")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--cycles", type=int, default=0, help="轮播几轮（0 = 无限）")
    args = parser.parse_args()

    variants = build_variants()
    print("\n屏幕每 10 秒在 G15 / F46 之间切换，左上角超大字体写着当前是哪个。")
    print("盯住 F46：底部绿字 BOTTOM OK! 出现吗？左侧刻度能到 480 吗？", flush=True)

    cycle = 0
    while args.cycles == 0 or cycle < args.cycles:
        for tag, reports, _note in variants:
            print(f"[{time.strftime('%H:%M:%S')}] 显示 {tag}", flush=True)
            frames, error = push(reports, args.seconds)
            print(
                f"    {tag}: {frames} 帧{'，失败: ' + error if error else '，无错误'}", flush=True
            )
        cycle += 1
    print("结束。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
