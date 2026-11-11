"""Generate `assets/app.ico` - the window and executable icon.

Run with the project's Python: ``python make_icon.py``. The icon is a 480x480
dark rounded square with the panel's signature arc and a small droplet, matching
the app's dark theme; sizes 16..256 are embedded for crisp taskbar rendering.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BG = (11, 15, 20)
CARD = (18, 25, 34)
ACCENT = (56, 189, 248)
ACCENT_DIM = (14, 116, 144)
TEXT = (226, 232, 240)

SIZE = 512


def _rounded(size: int, radius: int, fill: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=fill)
    return img


def render() -> Image.Image:
    """Draw the icon at 512x512 RGBA."""
    icon = _rounded(SIZE, 96, BG)

    # Inner card, like the app's device card.
    card = _rounded(432, 72, CARD)
    icon.alpha_composite(card, (40, 40))

    draw = ImageDraw.Draw(icon)

    # Three "screen" bars, the middle one accented: the panel's own content.
    widths = (196, 268, 148)
    y = 148
    for i, width in enumerate(widths):
        colour = ACCENT if i == 1 else ACCENT_DIM
        draw.rounded_rectangle(
            ((SIZE - width) // 2, y, (SIZE + width) // 2, y + 38), radius=19, fill=colour
        )
        y += 56

    # Droplet: a round body with a triangular tip above it.
    cx, cy, r = SIZE // 2, 404, 34
    draw.polygon(((cx, cy - r - 44), (cx - r + 4, cy + 4), (cx + r - 4, cy + 4)), fill=TEXT)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=TEXT)

    return icon


def main() -> int:
    """Write the multi-size .ico and a PNG preview next to it."""
    root = Path(__file__).resolve().parent
    assets = root / "assets"
    assets.mkdir(exist_ok=True)

    icon = render()
    ico = assets / "app.ico"
    icon.save(
        ico,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    icon.resize((256, 256), Image.LANCZOS).save(assets / "app.png")
    print(f"wrote {ico} ({ico.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
