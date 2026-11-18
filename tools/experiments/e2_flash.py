"""真机实验 E2：往屏内 flash 写一张待机图 / 开机图，验证 0x0A + 0x0B 子协议。

官方软件对两种素材的处理不同（`DeviceDriver.exe` 里的路径常量）：

* ``led_stby\\led_stby.jpg`` —— **裸 JPEG**，待机图
* ``led_startup\\startup.pak`` —— 自定义容器，开机动画

所以风险最低的第一步是把一张自制 480×480 JPEG 写进**待机槽位**（`FLASH_TARGET_STANDBY = 0`），
格式与官方完全一致，只差文件内容。开机槽位需要 `.pak` 容器格式，尚未解出。

写完后把屏幕关掉再打开（`0x10`），就能看到待机图是否换成了测试图。

用法（先退出官方 TCOMAS 软件，它会独占设备）::

    python tools/experiments/e2_flash.py --target standby --dry-run   # 只生成图片，不写设备
    python tools/experiments/e2_flash.py --target standby              # 真写
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

FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_REGULAR = "C:/Windows/Fonts/msyh.ttc"


def test_plate(text: str, subtitle: str, size: int = 480) -> Image.Image:
    """480×480 test image: solid gradient, huge label, thin frame, bottom caption."""
    image = Image.new("RGB", (size, size), "#06121F")
    draw = ImageDraw.Draw(image)
    for y in range(size):
        ratio = y / (size - 1)
        red = int(8 + 30 * ratio)
        green = int(20 + 150 * ratio)
        blue = int(40 + 190 * ratio)
        draw.line((0, y, size, y), fill=(red, green, blue))
    draw.rectangle((14, 14, size - 14, size - 14), outline="#38BDF8", width=3)
    big = ImageFont.truetype(FONT_BOLD, 74)
    small = ImageFont.truetype(FONT_REGULAR, 26)
    tiny = ImageFont.truetype(FONT_REGULAR, 20)
    draw.text((size / 2, size / 2 - 46), text, font=big, fill="#FFFFFF", anchor="mm")
    draw.text((size / 2, size / 2 + 34), subtitle, font=small, fill="#A5F3FC", anchor="mm")
    draw.text(
        (size / 2, size - 46),
        "HydroHUD E2 · 10240-byte blocks",
        font=tiny,
        fill="#7DD3FC",
        anchor="mm",
    )
    return image


def encode(image: Image.Image, limit: int = 46000) -> bytes:
    """Encode as baseline JPEG, shrinking quality until it fits a single frame budget."""
    for quality in (95, 92, 88, 84, 80, 76, 72, 68, 64, 58, 52, 46, 40):
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True, subsampling=2)
        if buffer.tell() <= limit:
            data = buffer.getvalue()
            print(f"JPEG 质量 {quality} → {len(data)} 字节", flush=True)
            return data
    data = buffer.getvalue()
    print(f"JPEG 质量 40 → {len(data)} 字节（仍偏大，但远低于 flash 上限）", flush=True)
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="E2：写入待机图 / 开机图到屏内 flash")
    parser.add_argument("--target", choices=("standby", "startup"), default="standby")
    parser.add_argument("--text", default="HydroHUD")
    parser.add_argument("--subtitle", default="待机图测试")
    parser.add_argument("--dry-run", action="store_true", help="只生成图片，不打开设备")
    parser.add_argument("--out", type=Path, default=ROOT / "tools" / "experiments" / "captures")
    parser.add_argument("--blank", action="store_true", help="写完后关屏再开屏，触发待机图刷新")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    image = test_plate(args.text, args.subtitle)
    jpeg = encode(image)
    path = args.out / "e2-test.jpg"
    path.write_bytes(jpeg)
    print(f"测试图已存到 {path}", flush=True)

    target = (
        device.FLASH_TARGET_STANDBY if args.target == "standby" else device.FLASH_TARGET_STARTUP
    )
    if args.dry_run:
        print(f"dry-run：不会写设备（目标槽位 {target}，{len(jpeg)} 字节）")
        return 0

    print("打开控制接口 …", flush=True)
    control = device.Control()
    control.open()
    try:
        print(f"开始上传 {len(jpeg)} 字节到槽位 {target} …", flush=True)
        started = time.monotonic()

        def report(ratio: float) -> None:
            if ratio >= 1.0 or int(ratio * 100) % 20 == 0:
                print(
                    f"  进度 {ratio * 100:5.1f}%  ({time.monotonic() - started:.1f}s)", flush=True
                )

        control.upload_file(jpeg, target=target, progress=report)
        print(f"写完，用时 {time.monotonic() - started:.1f}s", flush=True)
        if args.blank:
            print("关屏 1.5 秒再开屏 …", flush=True)
            control.set_power(False)
            time.sleep(1.5)
            control.set_power(True)
            print("已重新开屏", flush=True)
    finally:
        control.close()
    print("完成。请观察屏幕：关屏/开屏后应显示刚写的测试图。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
