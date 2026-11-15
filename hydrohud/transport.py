"""Transport layer: the I/O half of the panel protocol.

`hydrohud.device` owns framing and encoding, but it opens the panel through
ctypes/`hid.dll`, so it only runs on Windows. This module isolates that last
step behind the small `Transport` protocol so the same framing can be driven by:

* `FakeTransport` -- in-memory, for tests and for replaying captures;
* `HidrawTransport` -- Linux `hidraw` device nodes, one per HID collection;
* the Windows HID implementation in `hydrohud.device`.

Report-id semantics differ between the two real backends, and every parser above
this layer has to stay defensive about it: `hidraw` keeps the report id in both
directions (a 441-byte output report is written with the id as byte 0 and read
back as 441 bytes including it), while cython-hidapi on Windows strips it from
`read()` (440 bytes for the same address query). Frames are written with the
report id as the first byte on both platforms.
"""

from __future__ import annotations

import os
import re
import select
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class Transport(Protocol):
    """One opened HID collection, speaking whole reports.

    Implementations are opened explicitly, own their file descriptor or handle,
    and must make `close()` idempotent: the streaming loop reconnects by closing
    the old transport and opening a fresh one.
    """

    path: str

    @property
    def is_open(self) -> bool:
        """Whether the underlying handle or file descriptor is held."""
        ...

    def open(self) -> None:
        """Acquire the handle; raises `RuntimeError`/`OSError` when unavailable."""
        ...

    def close(self) -> None:
        """Release the handle; safe to call when already closed."""
        ...

    def write_report(self, data: bytes) -> None:
        """Write one complete output report, report id included as byte 0."""
        ...

    def read_report(self, length: int, timeout: float = 1.0) -> bytes | None:
        """Read one input report, or `None` when nothing arrives in `timeout`."""
        ...


class FakeTransport:
    """In-memory transport: records writes and replays pre-fed reads.

    Tests use `feed()` to queue the replies the panel would send and inspect
    `sent` afterwards. `fail_on_write` and `fail_after()` inject the two failure
    modes the streaming loop has to survive: a hard write error and a link that
    dies after a number of reports.
    """

    def __init__(self, path: str = "fake://panel") -> None:
        self._path = path
        self._open = False
        self._reads: deque[bytes] = deque()
        self._writes_until_failure: int | None = None
        self.sent: list[bytes] = []
        self.fail_on_write: BaseException | None = None

    @property
    def path(self) -> str:
        """The synthetic device path this transport pretends to talk to."""
        return self._path

    @property
    def is_open(self) -> bool:
        """Whether `open()` has been called and `close()` has not."""
        return self._open

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def feed(self, data: bytes) -> None:
        """Queue one input report for the next `read_report()` call."""
        self._reads.append(bytes(data))

    def fail_after(self, writes: int) -> None:
        """Raise `OSError` from every write after `writes` successful ones."""
        self._writes_until_failure = max(0, int(writes))

    def write_report(self, data: bytes) -> None:
        if not self._open:
            raise RuntimeError("fake transport is not open")
        if self.fail_on_write is not None:
            raise self.fail_on_write
        if self._writes_until_failure is not None:
            if self._writes_until_failure <= 0:
                raise OSError("injected write failure")
            self._writes_until_failure -= 1
        self.sent.append(bytes(data))

    def read_report(self, length: int, timeout: float = 1.0) -> bytes | None:
        if not self._open:
            raise RuntimeError("fake transport is not open")
        if not self._reads:
            return None
        data = self._reads.popleft()
        return data[:length] if length > 0 else data


class HidrawTransport:
    """Linux `hidraw` transport for one composite-HID interface.

    `path` is a device node such as ``/dev/hidraw3`` and `report_len` is the
    output report length of that collection (see `hydrohud.devices`). Opening is
    Linux-only; on other platforms `open()` raises `RuntimeError` so callers can
    fall back to the platform implementation instead of guessing.
    """

    def __init__(self, path: str, report_len: int) -> None:
        self._path = str(path)
        self.report_len = int(report_len)
        self._fd: int | None = None

    @property
    def path(self) -> str:
        """The hidraw device node, e.g. ``/dev/hidraw3``."""
        return self._path

    @property
    def is_open(self) -> bool:
        """Whether the device node is open."""
        return self._fd is not None

    def open(self) -> None:
        """Open the device node for read/write; idempotent."""
        if self._fd is not None:
            return
        if not _is_linux():
            raise RuntimeError("hidraw transport is Linux-only")
        self._fd = os.open(self._path, os.O_RDWR)

    def close(self) -> None:
        """Close the device node; safe to call twice."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> HidrawTransport:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def write_report(self, data: bytes) -> None:
        """Write one output report; hidraw prepends nothing, so include the id."""
        if self._fd is None:
            raise RuntimeError("hidraw transport is not open")
        if self.report_len and len(data) != self.report_len:
            raise ValueError(f"report must be {self.report_len} bytes, got {len(data)}")
        written = os.write(self._fd, bytes(data))
        if written != len(data):
            raise OSError(f"short hidraw write: {written} of {len(data)} bytes")

    def read_report(self, length: int, timeout: float = 1.0) -> bytes | None:
        """Read one input report (report id included) or `None` on timeout."""
        if self._fd is None:
            raise RuntimeError("hidraw transport is not open")
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return None
        return os.read(self._fd, length) or None


@dataclass(frozen=True)
class HidrawInfo:
    """One hidraw node discovered under sysfs."""

    path: str
    vendor_id: int
    product_id: int
    interface_number: int | None
    report_len: int | None


_HID_ID_PREFIX = "HID_ID="
_MI_RE = re.compile(r"mi_([0-9a-f]{2})", re.IGNORECASE)


def list_hidraw(sysfs_root: str | Path | None = None) -> list[HidrawInfo]:
    """List every hidraw node under sysfs, whatever its USB ids.

    Scans ``<sysfs_root>/class/hidraw/*/device/uevent`` for ``HID_ID``. The
    interface number comes from ``MI_XX`` in ``HID_PHYS`` when the kernel writes
    it there, otherwise from the ``bInterfaceNumber`` of the parent USB interface
    directory: composite panels expose both collections as separate interfaces,
    and only the second spelling is guaranteed across kernels.

    `sysfs_root` defaults to the ``HYDROHUD_SYSFS_ROOT`` environment variable (so
    tests can point it at a temporary tree on any OS) and then to ``/sys``.
    """
    if sysfs_root is None:
        sysfs_root = os.environ.get("HYDROHUD_SYSFS_ROOT", "/sys")
    class_dir = Path(sysfs_root) / "class" / "hidraw"
    if not class_dir.is_dir():
        return []

    found: list[HidrawInfo] = []
    for node in sorted(class_dir.iterdir()):
        try:
            uevent = (node / "device" / "uevent").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        ids = _parse_hid_id(uevent)
        if ids is None:
            continue
        interface = _parse_interface_number(uevent)
        if interface is None:
            interface = _read_interface_number(node)
        path = f"/dev/{node.name}"
        found.append(
            HidrawInfo(
                path=path,
                vendor_id=ids[0],
                product_id=ids[1],
                interface_number=interface,
                report_len=report_len_for(path),
            )
        )
    return found


def enumerate_hidraw(
    vendor_id: int, product_id: int, sysfs_root: str | Path | None = None
) -> list[HidrawInfo]:
    """List the hidraw nodes of a panel's HID collections, filtered by USB ids."""
    return [
        info
        for info in list_hidraw(sysfs_root)
        if (info.vendor_id, info.product_id) == (vendor_id, product_id)
    ]


def report_len_for(path: str) -> int | None:
    """Best-effort output report length of a hidraw node; `None` when unknown.

    hidraw exposes the raw report descriptor (``HIDIOCGRDESC``) and the bus /
    VID / PID triple (``HIDIOCGRAWINFO``) but not the parsed HID report lengths,
    so there is nothing to measure here. Returning `None` is deliberate: read
    the value from the device table (`hydrohud.devices`) instead of inventing one.
    Windows HID exposes the parsed caps, but that path is `hydrohud.device`, not
    this module.
    """
    if not _is_linux():
        return None
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    os.close(fd)
    return None


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _parse_hid_id(uevent: str) -> tuple[int, int] | None:
    """Parse ``HID_ID=0003:00001A2C:00001520`` into ``(vendor, product)``."""
    for line in uevent.splitlines():
        if not line.startswith(_HID_ID_PREFIX):
            continue
        parts = line[len(_HID_ID_PREFIX) :].split(":")
        if len(parts) != 3:
            return None
        try:
            return int(parts[1], 16), int(parts[2], 16)
        except ValueError:
            return None
    return None


def _parse_interface_number(uevent: str) -> int | None:
    """Read the ``MI_XX`` interface number from ``HID_PHYS``, if present."""
    for line in uevent.splitlines():
        if not line.startswith("HID_PHYS="):
            continue
        match = _MI_RE.search(line)
        if match:
            return int(match.group(1), 16)
    return None


def _read_interface_number(node: Path) -> int | None:
    """Read ``bInterfaceNumber`` from the USB interface above a hidraw node.

    ``/sys/class/hidraw/hidrawN/device`` links to the HID device
    (``.../1-3:1.1/0003:1A2C:1520.0001``); its parent is the USB interface
    directory, which always carries ``bInterfaceNumber``. Kernels differ on
    whether ``HID_PHYS`` includes the ``MI_XX`` suffix, so this is the fallback.
    """
    try:
        resolved = (node / "device").resolve()
        text = (resolved.parent / "bInterfaceNumber").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None
