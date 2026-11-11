"""检查所有预设里单行文本是否超出 480 屏幕宽度（一次性诊断脚本）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lg6studio import render, scenes, sensors  # noqa: E402

LIMIT = 470  # 留 5 px 边距


def measure(text: str, size: int, bold: bool = False) -> int:
    font = render.load_font(size, bool(bold))
    return font.getbbox(text)[2]


def main() -> int:
    problems = 0
    for name, scene in scenes.preset_scenes().items():
        for layer in scene["layers"]:
            kind = layer["type"]
            text = ""
            if kind == "sysinfo":
                sep = layer.get("separator", "    ")
                text = sep.join(sensors.format_metric(f, None) for f in layer["fields"])
            elif kind == "text":
                text = str(layer.get("text") or "")
                if "{" in text:
                    continue
            elif kind == "bars":
                texts = [
                    f"{sensors.metric_label(i)} {sensors.format_value(i, None)}"
                    for i in layer["items"]
                ]
                width = max(measure(t, layer["size"], True) for t in texts)
                if width > LIMIT:
                    problems += 1
                    print(f"OVER {name} bars {width}px {texts[0]}")
                continue
            else:
                continue
            width = measure(text, layer["size"], layer.get("bold", False))
            flag = "OVER" if width > LIMIT else "ok  "
            if width > LIMIT:
                problems += 1
            print(f"{flag} {name} {kind} @{layer['size']} {width:.0f}px  {text}")
    print(f"\n{problems} overflow(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
