"""Device table: the panel facts that differ between supported screens.

HydroHUD was written against one panel, the TCOMAS LG600, so most of the
protocol is shared: a 1025-byte image report, a 441-byte control report, JPEG
payloads and the same framing (see `hydrohud.device.build_packets`). What varies
is enumerated here and nothing else: USB ids, report lengths, the JPEG size, the
verified packet budget and whether the model has been tested on real hardware.

Only the verified LG600 is enabled by default. The LG700 entry exists so a
future model can be probed without touching the framing code; `verified=False`
is a promise that its numbers are inferred, not measured -- do not present an
unverified entry as supported (see ROADMAP v1.4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Composite HID interface numbers on the panel: image frames on MI_01, control
#: commands on MI_00. Interface numbering is a transport detail (see
#: `hydrohud.transport`); the report lengths below are the reliable role check.
IMAGE_INTERFACE = 1
CONTROL_INTERFACE = 0

_VID_RE = re.compile(r"vid[_:]([0-9a-f]{4})", re.IGNORECASE)
_PID_RE = re.compile(r"pid[_:]([0-9a-f]{4})", re.IGNORECASE)
_ID_PAIR_RE = re.compile(r"\b([0-9a-f]{4}):([0-9a-f]{4})\b", re.IGNORECASE)
_HEX_OR_DECIMAL_RE = re.compile(r"^(?:0x)?([0-9a-f]+)$", re.IGNORECASE)


@dataclass(frozen=True)
class DeviceSpec:
    """One supported panel, with every value the protocol layer needs.

    The defaults describe the verified LG600 and are shared by its protocol
    family. `verified` defaults to `False` on purpose: adding a new entry must be
    an explicit claim that the hardware was tested.
    """

    key: str
    label: str
    vendor_id: int
    product_id: int
    image_report_len: int = 1025
    control_report_len: int = 441
    chunk_size: int = 1020
    max_chunks: int = 46
    control_chunk_size: int = 436
    width: int = 480
    height: int = 480
    jpeg: bool = True
    verified: bool = False
    note: str = ""

    @property
    def max_frame_bytes(self) -> int:
        """Largest frame the panel has been verified to render, in bytes."""
        return self.chunk_size * self.max_chunks


DEVICES: tuple[DeviceSpec, ...] = (
    DeviceSpec(
        key="lg600",
        label="TCOMAS LG600",
        vendor_id=0x1A2C,
        product_id=0x1520,
        verified=True,
        note="Reference panel; 46 packets (46920 bytes) render completely.",
    ),
    DeviceSpec(
        key="lg700",
        label="TCOMAS LG700",
        vendor_id=0x1A2C,
        product_id=0x151C,
        verified=False,
        note="协议推定与 LG600 相同，未实测",
    ),
)


def all_specs() -> tuple[DeviceSpec, ...]:
    """Every registered panel, in table order."""
    return DEVICES


def find_spec(vendor_id: int, product_id: int) -> DeviceSpec | None:
    """Look a panel up by USB ids; `None` means "not a supported device"."""
    for spec in DEVICES:
        if spec.vendor_id == vendor_id and spec.product_id == product_id:
            return spec
    return None


def match_hidinfo(hidinfo: dict) -> DeviceSpec | None:
    """Look a panel up from a hidapi-style ``dict`` of device attributes.

    Field names differ between backends (``vendor_id``/``product_id`` from the
    Windows enumeration, ``vendor``/``product`` from hidapi), so both spellings
    and hex strings are accepted. When the ids are missing the device path is
    the fallback.
    """
    vendor_id = _as_int(hidinfo.get("vendor_id", hidinfo.get("vendor")))
    product_id = _as_int(hidinfo.get("product_id", hidinfo.get("product")))
    if vendor_id is not None and product_id is not None:
        return find_spec(vendor_id, product_id)
    path = hidinfo.get("path")
    return match_path(path) if isinstance(path, str) else None


def match_path(path: str) -> DeviceSpec | None:
    """Look a panel up from a HID device path.

    Windows device interface paths look like
    ``\\\\?\\HID#VID_1A2C&PID_1520&MI_01#...``; libhidapi reports the same ids as
    ``1a2c:1520``. Linux ``/dev/hidraw3`` paths carry no ids, so they never match
    here -- use `hydrohud.transport.enumerate_hidraw` for those.
    """
    vendor = _VID_RE.search(path)
    product = _PID_RE.search(path)
    if vendor and product:
        return find_spec(int(vendor.group(1), 16), int(product.group(1), 16))
    pair = _ID_PAIR_RE.search(path)
    if pair:
        return find_spec(int(pair.group(1), 16), int(pair.group(2), 16))
    return None


def spec_key(spec: DeviceSpec) -> str:
    """Stable short id (``"lg600"``), safe for settings and log lines."""
    return spec.key


def describe(spec: DeviceSpec) -> str:
    """One human-readable line for logs and the GUI's device display."""
    line = f"{spec.label} [{spec.vendor_id:04x}:{spec.product_id:04x}]"
    if not spec.verified:
        line += " (unverified)"
    return line


def _as_int(value: object) -> int | None:
    """Coerce an id to `int`, accepting ints, ``"0x1a2c"`` and ``"1a2c"``."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        match = _HEX_OR_DECIMAL_RE.match(text)
        if not match:
            return None
        digits = match.group(1)
        hexadecimal = text.startswith("0x") or bool(re.search(r"[a-f]", digits))
        return int(digits, 16 if hexadecimal else 10)
    return None
