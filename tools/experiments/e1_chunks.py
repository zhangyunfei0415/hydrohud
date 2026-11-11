"""真机实验 E1/E5：固件是否接受超过 15 个分包 / 更大的 JPEG 分辨率。

脚本在屏幕上循环轮播 5 个变体，每个变体把序号画在画面正中，方便目视判断：

    15   = 15 包（15300 B 上限），480×480，已知正常，作为对照
    20   = 20 包（20400 B 上限），480×480
    30   = 30 包（30600 B 上限），480×480
    46   = 46 包（45900 B 上限），480×480
    640  = 15 包（15300 B 上限），但画面是 640×640（测固件是否缩放）

每个变体都单独开一次设备句柄：某个变体把设备搞坏后，下一个变体仍然拿到干净句柄，
这样「哪个变体写入失败」能精确归因，而不是被前一个变体污染。

用法（先退出官方 TCOMAS 软件）::

    python tools/experiments/e1_chunks.py --seconds 7 --cycles 3
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

QUALITY_LADDER = (100, 98, 96, 95, 92, 88, 84, 80, 76, 72, 68, 64, 60, 55, 50, 45, 40, 35, 30)


def encode_under(img: Image.Image, limit: int) -> bytes:
    """Encode as JPEG at the highest quality whose payload fits `limit` bytes."""
    for quality in QUALITY_LADDER:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True, subsampling=2)
        if buf.tell() <= limit:
            return buf.getvalue()
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=20, optimize=True, subsampling=2)
    return buf.getvalue()


def labelled(source: Image.Image, size: int, tag: str) -> Image.Image:
    """Return a square frame with a big tag in the middle so the variant is obvious."""
    frame = source.convert("RGB")
    if frame.size != (size, size):
        frame = frame.resize((size, size), Image.LANCZOS)
    draw = ImageDraw.Draw(frame)
    font = ImageFont.truetype(FONT, int(size * 0.42))
    box = draw.textbbox((0, 0), tag, font=font)
    text_width = box[2] - box[0]
    text_height = box[3] - box[1]
    x = (size - text_width) // 2
    y = (size - text_height) // 2 - box[1]
    draw.rectangle(
        (x - 24, y + box[1] - 18, x + text_width + 24, y + box[1] + text_height + 18),
        fill="#000000",
    )
    draw.text((x, y), tag, font=font, fill="#FFFFFF")
    return frame


VARIANTS = (
    ("15", 15, 480),
    ("20", 20, 480),
    ("30", 30, 480),
    ("46", 46, 480),
)

# 640×640 变体（E5）单独开关：实测推十几帧后设备会从 USB 上掉线（WriteFile error 31
# → 433，随后枚举不到设备，几秒后 Windows 重新枚举），所以默认不跑。
VARIANT_640 = ("640", 15, 640)


def build_variants(include_640: bool = False) -> list[tuple[str, list[bytes], str]]:
    """Return (tag, reports, note) for every variant.

    Every variant is padded with zero bytes to exactly its target packet count,
    so the only thing that differs is the packet count (the vendor firmware
    pads with zeros too). Variant 15 is the control.
    """
    source = Image.open(PHOTO)
    specs = VARIANTS + (VARIANT_640,) if include_640 else VARIANTS
    variants: list[tuple[str, list[bytes], str]] = []
    for tag, chunks, size in specs:
        limit = chunks * device.CHUNK_SIZE
        frame = labelled(source, size, tag)
        jpeg = encode_under(frame, limit)
        payload = jpeg + b"\x00" * (limit - len(jpeg))
        reports = device.build_packets(
            device.CMD_STREAM, payload, device.CHUNK_SIZE, device.REPORT_LEN
        )
        note = f"{size}×{size}, JPEG {len(jpeg)} B, 补零 {limit - len(jpeg)} B, {len(reports)} 包"
        variants.append((tag, reports, note))
    return variants


def push_variant(reports: list[bytes], seconds: float) -> tuple[int, str | None]:
    """Push one variant on a fresh handle; return (frames written, error text)."""
    written = 0
    try:
        with device.Screen() as screen:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                started = time.monotonic()
                for report in reports:
                    screen.write_report(report)
                written += 1
                remaining = (1 / 30) - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(remaining)
    except OSError as exc:
        return written, str(exc)
    return written, None


def recovery_check() -> str:
    """Push a known-good 15-packet frame and report whether the panel still takes writes."""
    frame = device.encode_jpeg(Image.new("RGB", (480, 480), (30, 160, 90)))
    try:
        with device.Screen() as screen:
            for _ in range(15):
                screen.send_jpeg(frame)
        return "恢复正常"
    except OSError as exc:
        return f"仍写不进（{exc}）"


def main() -> int:
    parser = argparse.ArgumentParser(description="E1/E5：分包上限与分辨率实验")
    parser.add_argument("--seconds", type=float, default=7.0, help="每个变体显示多久")
    parser.add_argument("--cycles", type=int, default=3, help="轮播几轮（0 = 无限）")
    parser.add_argument(
        "--include-640",
        action="store_true",
        help="同时跑 640×640 变体（会把这台机器的屏幕从 USB 上踢掉线，需谨慎）",
    )
    args = parser.parse_args()

    variants = build_variants()
    print("变体清单：")
    for tag, _reports, note in variants:
        print(f"  {tag:<4} {note}")
    print()
    print("屏幕会循环轮播上面 5 个画面，画面正中就是序号。")
    print("请记住哪个序号出现时画面花屏 / 残缺 / 错位 / 显示不对。")
    print("按 Ctrl+C 可随时停止。", flush=True)

    cycle = 0
    while args.cycles == 0 or cycle < args.cycles:
        for tag, reports, _note in variants:
            print(f"[{time.strftime('%H:%M:%S')}] 正在显示变体 {tag}", flush=True)
            frames, error = push_variant(reports, args.seconds)
            if error is None:
                print(f"    变体 {tag}: 写入 {frames} 帧，无错误", flush=True)
            else:
                print(f"    变体 {tag}: 只写入 {frames} 帧就失败 —— {error}", flush=True)
            print(f"    恢复检查：{recovery_check()}", flush=True)
        cycle += 1
        print(f"[{time.strftime('%H:%M:%S')}] 第 {cycle} 轮结束", flush=True)
    print("实验结束。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
