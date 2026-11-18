"""真机实验 E2（第一步）：从官方软件捕获 flash 上传报文（只读，不写设备）。

官方 ``DeviceDriver.exe`` 通过 ``kernel32!WriteFile`` 往屏内 flash 写 ``startup.pak``
（开机画面）与 ``led_stby.jpg``（待机图）。本脚本用 Frida 挂这个 32 位进程，
把每条 441 字节控制报文解出来：``0x0A`` 是声明（目标槽位 + 总长度），``0x0B`` 是分块
（12 字节块头 + 数据）。捕获结果按偏移拼回完整文件，供后续格式分析。

**只抓不发**：脚本自己不会向设备写任何东西，也不改官方软件的文件。

用法（先退出 HydroHUD，别让它占着设备）::

    python tools/experiments/e2_capture.py --seconds 45 --out tools/experiments/captures

抓完会打印每种命令的报文数，并把拼好的 ``.pak`` / ``.jpg`` 落到 ``--out`` 目录。
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hydrohud import device  # noqa: E402

VENDOR_DIR = Path(r"C:\Program Files (x86)\TcomasSoftware")
VENDOR_EXE = VENDOR_DIR / "DeviceDriver.exe"

HOOK_JS = r"""
const k32 = Module.getExportByName('kernel32.dll', 'WriteFile');
const state = { control: 0, image: 0 };

function hex(bytes, limit) {
  const out = [];
  for (let i = 0; i < Math.min(bytes.length, limit); i++) {
    out.push(('0' + bytes[i].toString(16)).slice(-2));
  }
  return out.join('');
}

Interceptor.attach(k32, {
  onEnter(args) {
    const len = args[2].toInt32();
    if (len !== 441 && len !== 1025) return;
    let bytes;
    try {
      bytes = new Uint8Array(args[1].readByteArray(len));
    } catch (e) {
      return;
    }
    if (len === 1025) {
      state.image += 1;
      return;
    }
    state.control += 1;
    const cmd = bytes[1];
    // 0x08 之外的命令都发回去；0x0B 只带块头 + 前 8 字节数据，够判断结构。
    send({
      cmd: cmd,
      seq: bytes[2],
      flag: bytes[4],
      head: hex(bytes.subarray(5, 5 + 24), 24),
      data: cmd === 0x0b ? hex(bytes.subarray(17, 17 + 8), 8) : ''
    });
  }
});

setInterval(function () { send({ stats: true, control: state.control, image: state.image }); }, 1000);
"""


def control_payload(report: bytes) -> bytes:
    """Return the 436-byte payload that follows a 441-byte control report header."""
    return report[5 : 5 + device.CONTROL_CHUNK_SIZE]


def parse_announce(payload: bytes) -> tuple[int, int]:
    """Decode a 0x0A announce payload into (target flag, total file length)."""
    target, size = struct.unpack("<II", payload[:8])
    return target, size


def parse_block(payload: bytes) -> tuple[int, int, int, bytes]:
    """Decode a 0x0B block payload into (offset, length, percent, data)."""
    offset, length, _reserved = struct.unpack("<IHH", payload[:8])
    percent = payload[8]
    return offset, length, percent, payload[12:]


def main() -> int:
    parser = argparse.ArgumentParser(description="E2：捕获官方软件的 flash 上传报文")
    parser.add_argument("--seconds", type=float, default=45.0, help="抓多久")
    parser.add_argument(
        "--out", type=Path, default=ROOT / "tools" / "experiments" / "captures", help="输出目录"
    )
    parser.add_argument("--exe", type=Path, default=VENDOR_EXE)
    parser.add_argument("--keep", action="store_true", help="抓完不退出官方软件")
    args = parser.parse_args()

    if not args.exe.exists():
        print(f"找不到官方软件：{args.exe}", flush=True)
        return 1

    import frida  # 延迟导入：只有跑实验才需要

    args.out.mkdir(parents=True, exist_ok=True)
    print(f"启动 {args.exe.name} 并挂 WriteFile …", flush=True)
    pid = frida.spawn(str(args.exe), cwd=str(args.exe.parent))
    session = frida.attach(pid)
    script = session.create_script(HOOK_JS)

    events: list[dict] = []
    stats = {"control": 0, "image": 0}

    def on_message(message, _data):
        if message.get("type") != "send":
            return
        payload = message["payload"]
        if payload.get("stats"):
            stats["control"] = payload["control"]
            stats["image"] = payload["image"]
            return
        events.append({"t": time.time(), **payload})

    script.on("message", on_message)
    script.load()
    frida.resume(pid)
    print(
        f"已挂上 pid={pid}，抓 {args.seconds:.0f} 秒（可以手动点官方界面里的开机图上传）…",
        flush=True,
    )

    deadline = time.monotonic() + args.seconds
    last_report = 0.0
    try:
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if time.monotonic() - last_report >= 5:
                last_report = time.monotonic()
                print(
                    f"  [{(deadline - time.monotonic()):5.1f}s] 控制报文 {stats['control']}"
                    f" · 图像报文 {stats['image']} · 事件 {len(events)}",
                    flush=True,
                )
    except KeyboardInterrupt:
        print("提前结束。", flush=True)
    finally:
        try:
            script.unload()
        except Exception:  # noqa: BLE001 - 进程可能已经退出
            pass
        if not args.keep:
            try:
                frida.kill(pid)
            except Exception:  # noqa: BLE001
                pass

    # ---- 归纳 ----
    by_cmd: dict[int, int] = {}
    for event in events:
        by_cmd[event["cmd"]] = by_cmd.get(event["cmd"], 0) + 1
    print(
        "\n命令分布：" + ", ".join(f"0x{cmd:02x}×{count}" for cmd, count in sorted(by_cmd.items()))
    )

    announces = [e for e in events if e["cmd"] == 0x0A]
    blocks = [e for e in events if e["cmd"] == 0x0B]
    if not announces:
        print("\n没有捕获到 0x0A 声明 —— 官方软件这次没有写 flash。")
        print("可以这样重试：脚本跑着的时候，在官方界面里应用一次开机画面 / 待机图。")
        raw = args.out / f"events-{int(time.time())}.json"
        raw.write_text(json.dumps(events[-2000:], ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"（把最后 2000 条事件存到了 {raw}）")
        return 0

    for index, announce in enumerate(announces, start=1):
        payload = bytes.fromhex(announce["head"])
        target, size = parse_announce(payload)
        print(f"\n第 {index} 次上传：目标槽位 {target}，声明长度 {size} 字节")
        # 收集这次声明之后、下一次声明之前的块
        start_t = announce["t"]
        end_t = announces[index]["t"] if index < len(announces) else float("inf")
        chunks = [b for b in blocks if start_t <= b["t"] < end_t]
        print(f"  紧随其后的 0x0B 报文 {len(chunks)} 条（每条带 8 字节样本）")
        offsets = []
        for chunk in chunks[:5]:
            payload = bytes.fromhex(chunk["head"])
            offset, length, percent, _data = parse_block(payload)
            offsets.append((offset, length, percent))
        print(f"  前几块块头: {offsets}")

    raw = args.out / f"events-{int(time.time())}.json"
    raw.write_text(json.dumps(events[:5000], ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n原始事件已存到 {raw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
