"""USB HID access to the TCOMAS LG600 panel.

The panel exposes two HID collections (VID 1A2C, PID 1520). Frames go to the
collection that accepts 1025-byte output reports; brightness/rotation/power go
to the 441-byte one. Both speak the same framing:

    byte 0      report id            0x00
    byte 1      command              0x08 frames, 0x10/0x11/0x12/0x81 control
    byte 2      chunk count (first packet) or 1-based chunk index
    byte 3      reserved             0x00
    byte 4      flags                0x80 on the first packet of a transfer
    bytes 5..   payload

A frame is a run of 1020-byte chunks; the vendor firmware pads every frame to its
own packet count and keeps only 15 of them, but the panel renders up to 46 packets
(46920 bytes) correctly, which is the ceiling used here. Packets beyond the JPEG's
end-of-image marker are zero bytes on the wire and cost no decode time, so the
larger budget buys picture quality, not latency. The frame must always be 480x480:
a 640x640 JPEG stalls the USB endpoint and the panel drops off the bus.
The panel has a watchdog: it blanks a few seconds after the stream stops, so a
still image has to be retransmitted continuously (see `Screen.keepalive`).

This module is a dependency-free port of a verified standalone prototype: it
talks to hid.dll through ctypes.
"""

from __future__ import annotations

import ctypes
import io
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

VENDOR_ID = 0x1A2C
PRODUCT_ID = 0x1520
DEVICE_NAME = "TCOMAS LG600"

SCREEN_W = 480
SCREEN_H = 480

CHUNK_SIZE = 1020
MAX_CHUNKS = 46  # 46920 bytes per frame: the most the panel has been verified to render
MAX_FRAME_BYTES = CHUNK_SIZE * MAX_CHUNKS
HARD_MAX_CHUNKS = 255  # the chunk-count field is one byte, so 255 is the protocol ceiling
ABSOLUTE_MAX_FRAME_BYTES = CHUNK_SIZE * HARD_MAX_CHUNKS

CMD_STREAM = 0x08
FLAG_FIRST = 0x80

REPORT_LEN = 1025

CONTROL_REPORT_LEN = 441
CONTROL_CHUNK_SIZE = CONTROL_REPORT_LEN - 5  # 436
CMD_FILE_ANNOUNCE = 0x0A
CMD_FILE_CHUNK = 0x0B
CMD_POWER = 0x10
CMD_ROTATE = 0x11
CMD_BRIGHTNESS = 0x12
CMD_QUERY = 0x81


# --------------------------------------------------------------------------- #
# HID plumbing (ctypes over hid.dll / setupapi.dll / kernel32.dll)
# --------------------------------------------------------------------------- #
GUID_DEVINTERFACE_HID = "{4D1E55B2-F16F-11CF-88CB-001111000030}"
DIGCF_PRESENT = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("InterfaceClassGuid", GUID),
        ("Flags", ctypes.c_uint32),
        ("Reserved", ctypes.c_void_p),
    ]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Size", ctypes.c_uint32),
        ("VendorID", ctypes.c_uint16),
        ("ProductID", ctypes.c_uint16),
        ("VersionNumber", ctypes.c_uint16),
    ]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_uint16),
        ("UsagePage", ctypes.c_uint16),
        ("InputReportByteLength", ctypes.c_uint16),
        ("OutputReportByteLength", ctypes.c_uint16),
        ("FeatureReportByteLength", ctypes.c_uint16),
        ("Reserved", ctypes.c_uint16 * 17),
        ("NumberLinkCollectionNodes", ctypes.c_uint16),
        ("NumberInputButtonCaps", ctypes.c_uint16),
        ("NumberInputValueCaps", ctypes.c_uint16),
        ("NumberInputDataIndices", ctypes.c_uint16),
        ("NumberOutputButtonCaps", ctypes.c_uint16),
        ("NumberOutputValueCaps", ctypes.c_uint16),
        ("NumberOutputDataIndices", ctypes.c_uint16),
        ("NumberFeatureButtonCaps", ctypes.c_uint16),
        ("NumberFeatureValueCaps", ctypes.c_uint16),
        ("NumberFeatureDataIndices", ctypes.c_uint16),
    ]


setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiGetClassDevsW.argtypes = [
    ctypes.POINTER(GUID),
    ctypes.c_wchar_p,
    ctypes.c_void_p,
    ctypes.c_uint32,
]
setupapi.SetupDiEnumDeviceInterfaces.restype = ctypes.c_int
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(GUID),
    ctypes.c_uint32,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = ctypes.c_int
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.c_void_p,
]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
hid.HidD_GetAttributes.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetPreparsedData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hid.HidD_SetNumInputBuffers.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.argtypes = [
    ctypes.c_wchar_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.WriteFile.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.c_void_p,
]
kernel32.WriteFile.restype = ctypes.c_int

TH32CS_SNAPPROCESS = 0x00000002
VENDOR_PROCESS_NAMES = ("devicedriver.exe", "tcomas.exe", "xyui.exe")
SYNCHRONIZE = 0x00100000
PROCESS_TERMINATE = 0x0001
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
kernel32.TerminateProcess.restype = ctypes.c_int


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]


@dataclass
class HidInfo:
    """One HID collection belonging to the panel."""

    path: str
    vendor_id: int
    product_id: int
    interface_index: int
    input_len: int
    output_len: int
    feature_len: int

    @property
    def role(self) -> str:
        """``"image"``, ``"control"`` or ``"unknown"``, decided by report size."""
        if self.output_len == REPORT_LEN:
            return "image"
        if self.output_len == CONTROL_REPORT_LEN:
            return "control"
        return "unknown"


def _hid_guid() -> GUID:
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    return guid


def enumerate_hid(vid: int | None = None, pid: int | None = None) -> list[HidInfo]:
    """List present HID collections, optionally filtered by vendor/product id."""
    guid = _hid_guid()
    devinfo = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    )
    if devinfo == INVALID_HANDLE_VALUE or not devinfo:
        raise OSError(f"SetupDiGetClassDevsW failed, error {ctypes.get_last_error()}")

    found: list[HidInfo] = []
    try:
        index = 0
        while True:
            did = SP_DEVICE_INTERFACE_DATA()
            did.cbSize = ctypes.sizeof(did)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                devinfo, None, ctypes.byref(guid), index, ctypes.byref(did)
            ):
                break
            index += 1

            needed = ctypes.c_uint32(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                devinfo, ctypes.byref(did), None, 0, ctypes.byref(needed), None
            )
            if not needed.value:
                continue

            detail = ctypes.create_string_buffer(needed.value)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W: DWORD cbSize, then the WCHAR path.
            ctypes.cast(detail, ctypes.POINTER(ctypes.c_uint32))[0] = (
                8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            )
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                devinfo, ctypes.byref(did), detail, needed.value, ctypes.byref(needed), None
            ):
                continue
            path = detail.raw[4:].decode("utf-16-le", "replace").split("\x00")[0]

            handle = kernel32.CreateFileW(
                path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None
            )
            if handle == INVALID_HANDLE_VALUE or handle is None:
                continue
            try:
                attrs = HIDD_ATTRIBUTES()
                attrs.Size = ctypes.sizeof(attrs)
                if not hid.HidD_GetAttributes(handle, ctypes.byref(attrs)):
                    continue
                if vid is not None and attrs.VendorID != vid:
                    continue
                if pid is not None and attrs.ProductID != pid:
                    continue

                preparsed = ctypes.c_void_p()
                caps = HIDP_CAPS()
                if not (
                    hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed))
                    and hid.HidP_GetCaps(preparsed, ctypes.byref(caps))
                ):
                    continue
                hid.HidD_FreePreparsedData(preparsed)

                mi = -1
                for token in path.replace("#", "&").split("&"):
                    if token.lower().startswith("mi_"):
                        try:
                            mi = int(token[3:], 16)
                        except ValueError:
                            mi = -1
                found.append(
                    HidInfo(
                        path=path,
                        vendor_id=attrs.VendorID,
                        product_id=attrs.ProductID,
                        interface_index=mi,
                        input_len=caps.InputReportByteLength,
                        output_len=caps.OutputReportByteLength,
                        feature_len=caps.FeatureReportByteLength,
                    )
                )
            finally:
                kernel32.CloseHandle(handle)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devinfo)
    return found


def enumerate_devices(vid: int = VENDOR_ID, pid: int = PRODUCT_ID) -> list[dict]:
    """Describe the panel's HID collections without opening them for I/O.

    Returns one dict per collection (``role``, ``interface``, report sizes,
    device path). An empty list means the panel is not plugged in.
    """
    try:
        collections = enumerate_hid(vid, pid)
    except OSError:
        return []
    return [
        {
            "role": info.role,
            "interface": f"mi_{info.interface_index:02d}",
            "interface_index": info.interface_index,
            "path": info.path,
            "input_len": info.input_len,
            "output_len": info.output_len,
            "feature_len": info.feature_len,
        }
        for info in collections
    ]


def vendor_processes() -> list[tuple[int, str]]:
    """Return ``(pid, exe_name)`` for every running official TCOMAS process."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE or snapshot is None:
        return []
    found: list[tuple[int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            if entry.szExeFile.lower() in VENDOR_PROCESS_NAMES:
                found.append((entry.th32ProcessID, entry.szExeFile))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                return found
    finally:
        kernel32.CloseHandle(snapshot)


def kill_vendor_software(timeout: float = 3.0) -> list[str]:
    """Terminate the official TCOMAS processes and wait until they are gone.

    Returns the exe names that were terminated (empty when nothing was running).
    The caller needs this before streaming because both programs write to the
    same panel, and the vendor software keeps re-pushing its own picture.
    """
    processes = vendor_processes()
    if not processes:
        return []
    terminated: list[str] = []
    for pid, name in processes:
        handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_TERMINATE, False, pid)
        if not handle:
            continue
        try:
            kernel32.TerminateProcess(handle, 1)
            terminated.append(name)
        finally:
            kernel32.CloseHandle(handle)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and vendor_processes():
        time.sleep(0.1)
    return terminated


def vendor_software_running() -> bool:
    """Whether the official TCOMAS software currently owns the panel.

    The vendor holds the HID handles with shared access, so this program can
    still open the device and stream into the same interface - the two streams
    then fight over the panel. Callers use this to warn the user instead.
    """
    return bool(vendor_processes())


def find_image_interface(vid: int = VENDOR_ID, pid: int = PRODUCT_ID) -> HidInfo:
    """Return the HID collection that accepts 1025-byte output reports (MI_01)."""
    collections = enumerate_hid(vid, pid)
    for info in collections:
        if info.output_len == REPORT_LEN:
            return info
    if not collections:
        raise RuntimeError(f"no HID device {vid:04X}:{pid:04X} found - is the cooler plugged in?")
    detail = ", ".join(f"mi_{d.interface_index:02d} out={d.output_len}" for d in collections)
    raise RuntimeError(f"no {REPORT_LEN}-byte image collection ({detail})")


def find_control_interface(vid: int = VENDOR_ID, pid: int = PRODUCT_ID) -> HidInfo:
    """Return the HID collection that accepts 441-byte output reports (MI_00)."""
    for info in enumerate_hid(vid, pid):
        if info.output_len == CONTROL_REPORT_LEN:
            return info
    raise RuntimeError(f"no {CONTROL_REPORT_LEN}-byte control collection found")


# --------------------------------------------------------------------------- #
# Frame encoding
# --------------------------------------------------------------------------- #
def fit_square(img: Image.Image, size: int = SCREEN_W) -> Image.Image:
    """Centre-crop to a square, then resize to the panel resolution."""
    img = img.convert("RGB")
    w, h = img.size
    if w != h:
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
    return img.resize((size, size), Image.LANCZOS)


def rotate_image(img: Image.Image, degrees: int) -> Image.Image:
    """Rotate a finished 480x480 frame clockwise by 0/90/180/270 degrees.

    The panel's own rotation command (`Control.set_rotation`) only stores a
    quadrant in its configuration; it does not reorient streamed JPEGs.
    """
    degrees %= 360
    if degrees == 0:
        return img
    if degrees == 90:
        return img.transpose(Image.Transpose.ROTATE_270)
    if degrees == 180:
        return img.transpose(Image.Transpose.ROTATE_180)
    if degrees == 270:
        return img.transpose(Image.Transpose.ROTATE_90)
    raise ValueError(f"rotation must be 0/90/180/270, got {degrees}")


def encode_jpeg(img: Image.Image, max_bytes: int = MAX_FRAME_BYTES, rotate: int = 0) -> bytes:
    """Encode a frame to JPEG, lowering quality until it fits `max_bytes`.

    Quality starts at 100 so a photo can use the panel's full 46920-byte frame
    budget; the ladder still reaches far below anything sensible because a
    pathological input (per-pixel noise) ramps up to ~230 KB at quality 100.

    Raises `ValueError` when even the lowest quality overflows.
    """
    img = rotate_image(img.convert("RGB"), rotate)
    ladder = (
        100,
        98,
        95,
        92,
        88,
        84,
        80,
        76,
        72,
        68,
        64,
        60,
        55,
        50,
        45,
        40,
        35,
        30,
        25,
        20,
        18,
        15,
        12,
        10,
        8,
        6,
        5,
    )
    for quality in ladder:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True, subsampling=2)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            return data
    raise ValueError(
        f"cannot fit the frame into {max_bytes} bytes even at quality 5; use a simpler picture"
    )


def build_packets(cmd: int, payload: bytes, chunk_size: int, report_len: int) -> list[bytes]:
    """Split a payload into framed HID reports.

    Framing (verified on hardware and in the vendor binary): report id `0x00`,
    command byte, then a 3-byte protocol header -- chunk *count* on the first
    packet or a 1-based chunk *index* afterwards, a reserved `0x00`, and a flags
    byte carrying `0x80` on the first packet of the transfer.
    """
    chunk_count = max(1, -(-len(payload) // chunk_size))
    if chunk_count > HARD_MAX_CHUNKS:
        raise ValueError(f"payload is {len(payload)} bytes, at most {HARD_MAX_CHUNKS} chunks fit")
    padded = payload + b"\x00" * (chunk_count * chunk_size - len(payload))
    reports: list[bytes] = []
    for index in range(chunk_count):
        chunk = padded[index * chunk_size : (index + 1) * chunk_size]
        header = bytes(
            [
                0x00,
                cmd,
                chunk_count if index == 0 else index,
                0x00,
                FLAG_FIRST if index == 0 else 0x00,
            ]
        )
        report = header + chunk
        reports.append(report + b"\x00" * (report_len - len(report)))
    return reports


def build_reports(
    jpeg: bytes, chunk_size: int = CHUNK_SIZE, report_len: int = REPORT_LEN
) -> list[bytes]:
    """Split a JPEG into the HID output reports the image interface expects."""
    return build_packets(CMD_STREAM, jpeg, chunk_size, report_len)


def frame_reports(jpeg: bytes) -> list[bytes]:
    """Validate a frame and return its HID reports, padded to a whole packet count.

    The panel buffers a full packet boundary and only decodes up to the JPEG's
    end-of-image marker, so a short picture is padded with zero bytes to the
    next 1020-byte boundary instead of being rejected.
    """
    if not jpeg:
        raise ValueError("empty frame")
    chunk_count = max(1, -(-len(jpeg) // CHUNK_SIZE))
    if chunk_count > MAX_CHUNKS:
        raise ValueError(
            f"frame is {len(jpeg)} bytes ({chunk_count} packets), "
            f"the panel is verified up to {MAX_FRAME_BYTES} bytes ({MAX_CHUNKS} packets)"
        )
    padding = chunk_count * CHUNK_SIZE - len(jpeg)
    return build_packets(CMD_STREAM, jpeg + b"\x00" * padding, CHUNK_SIZE, REPORT_LEN)


# --------------------------------------------------------------------------- #
# Devices
# --------------------------------------------------------------------------- #
class _HidDevice:
    """A HID collection opened read/write, with one fixed report format."""

    report_len: int = 0

    def __init__(self, info: HidInfo | None = None) -> None:
        self.info = info
        self._handle: int | None = None

    def _default_info(self) -> HidInfo:
        raise NotImplementedError

    @property
    def is_open(self) -> bool:
        """Whether the underlying Windows handle is held."""
        return bool(self._handle)

    def open(self) -> _HidDevice:
        """Open the device; raises `RuntimeError` when it is missing or busy."""
        if self._handle:
            return self
        if self.info is None:
            self.info = self._default_info()
        handle = kernel32.CreateFileW(
            self.info.path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE or handle is None:
            error = ctypes.get_last_error()
            raise RuntimeError(
                f"cannot open {self.info.path} (error {error}); "
                "close the vendor TCOMAS software and retry"
            )
        self._handle = handle
        # Deep input queue so a status reply is never dropped between writes.
        hid.HidD_SetNumInputBuffers(handle, 512)
        return self

    def close(self) -> None:
        """Release the Windows handle; safe to call twice."""
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = None

    def reopen(self) -> _HidDevice:
        """Drop the cached handle and path, then re-enumerate and open.

        The device path changes whenever Windows re-enumerates the panel (USB
        reset, sleep/wake, a firmware stall), so the cached `HidInfo` must not
        be reused across a reconnect.
        """
        self.close()
        self.info = None
        return self.open()

    def __enter__(self) -> _HidDevice:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def write_report(self, report: bytes) -> None:
        """Write one complete output report."""
        if self._handle is None:
            raise RuntimeError(f"{type(self).__name__} is not open")
        if len(report) != self.report_len:
            raise ValueError(f"report must be {self.report_len} bytes, got {len(report)}")
        buf = ctypes.create_string_buffer(report, len(report))
        written = ctypes.c_uint32(0)
        if not kernel32.WriteFile(self._handle, buf, len(report), ctypes.byref(written), None):
            raise OSError(f"WriteFile failed, error {ctypes.get_last_error()}")

    def read_report(self, timeout: float = 1.0) -> bytes | None:
        """Read one input report, returning `None` if nothing arrives in `timeout`."""
        if self._handle is None:
            raise RuntimeError(f"{type(self).__name__} is not open")
        handle = self._handle
        outcome: dict[str, object] = {}

        def worker() -> None:
            buf = ctypes.create_string_buffer(self.report_len)
            read = ctypes.c_uint32(0)
            ok = kernel32.ReadFile(handle, buf, self.report_len, ctypes.byref(read), None)
            outcome["ok"] = bool(ok)
            outcome["data"] = buf.raw[: read.value] if ok else b""

        # The handle is synchronous, so ReadFile blocks until the panel answers;
        # a watchdog thread keeps a silent panel from hanging the caller forever.
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive() or not outcome.get("ok"):
            return None
        return outcome["data"]  # type: ignore[return-value]


class Screen(_HidDevice):
    """An opened LG600 panel, ready to receive frames."""

    report_len = REPORT_LEN

    def _default_info(self) -> HidInfo:
        return find_image_interface()

    def send_jpeg(self, jpeg: bytes) -> None:
        """Transmit one complete frame (15 reports)."""
        for report in frame_reports(jpeg):
            self.write_report(report)

    def keepalive(self, jpeg: bytes, fps: float = 30.0, hold: float = 0.0) -> None:
        """Repeat one frame forever (or for `hold` seconds).

        The panel does not latch a single frame: it blanks a few seconds after
        the stream stops, so a still image has to be retransmitted continuously.
        Retransmitting the same encoded JPEG costs no CPU beyond the USB writes.
        """
        reports = frame_reports(jpeg)
        period = 1.0 / max(1.0, fps)
        deadline = time.monotonic() + hold if hold > 0 else None
        while deadline is None or time.monotonic() < deadline:
            start = time.monotonic()
            for report in reports:
                self.write_report(report)
            remaining = period - (time.monotonic() - start)
            if remaining > 0:
                time.sleep(remaining)

    def show_image(
        self, source: str | Path | Image.Image, hold: float = 0.0, rotate: int = 0
    ) -> None:
        """Encode an image and keep it on screen for `hold` seconds (0 = forever)."""
        if isinstance(source, Image.Image):
            jpeg = encode_jpeg(source, rotate=rotate)
        else:
            img = Image.open(source)
            img.load()
            jpeg = encode_jpeg(fit_square(img), rotate=rotate)
        self.keepalive(jpeg, hold=hold)

    def play(self, frames: Iterable[bytes], fps: float = 30.0, loops: int = 1) -> None:
        """Stream pre-encoded JPEG frames at the given rate; loops=0 means forever."""
        period = 1.0 / max(1.0, fps)
        rounds = 0
        while loops <= 0 or rounds < loops:
            for jpeg in frames:
                start = time.monotonic()
                self.send_jpeg(jpeg)
                remaining = period - (time.monotonic() - start)
                if remaining > 0:
                    time.sleep(remaining)
            rounds += 1


class Control(_HidDevice):
    """The panel's 441-byte control interface (MI_00): brightness, rotation, power.

    Framing matches the image interface, with one command per report and the
    parameter in the first payload byte. The panel stores values itself, so a
    command is only needed when the setting changes.
    """

    report_len = CONTROL_REPORT_LEN

    def _default_info(self) -> HidInfo:
        return find_control_interface()

    def send_command(self, cmd: int, payload: bytes = b"") -> None:
        """Send one control command, zero-padding the payload to the full report."""
        for report in build_packets(cmd, payload, CONTROL_CHUNK_SIZE, CONTROL_REPORT_LEN):
            self.write_report(report)

    def query(self, timeout: float = 1.0) -> bytes:
        """Ask the panel for its current settings; returns the raw 441-byte reply.

        Raises `RuntimeError` when the panel does not answer within `timeout`.
        """
        self.send_command(CMD_QUERY)
        reply = self.read_report(timeout)
        if not reply:
            raise RuntimeError("the panel did not answer the status query")
        return reply

    def set_brightness(self, percent: int) -> None:
        """Set backlight brightness, 0..100 (the panel defaults to 100)."""
        if not 0 <= percent <= 100:
            raise ValueError(f"brightness must be 0..100, got {percent}")
        self.send_command(CMD_BRIGHTNESS, bytes([percent]))

    def set_rotation(self, degrees: int) -> None:
        """Store the panel's rotation quadrant 0..3 in its own configuration.

        On the LG600 the firmware does not rotate the incoming JPEG stream, so
        this does not visibly rotate the picture; rotate pixels instead (see
        `rotate_image`).
        """
        if degrees % 90 or not 0 <= degrees < 360:
            raise ValueError(f"rotation must be 0/90/180/270, got {degrees}")
        self.send_command(CMD_ROTATE, bytes([degrees // 90]))

    def set_power(self, on: bool) -> None:
        """Turn the panel on or off (this is its only blank command)."""
        self.send_command(CMD_POWER, bytes([0 if on else 1]))


# --------------------------------------------------------------------------- #
# Media sources
# --------------------------------------------------------------------------- #
def gif_frames(path: str | Path, rotate: int = 0) -> tuple[list[bytes], float]:
    """Encode every frame of a GIF/WebP animation for the panel."""
    img = Image.open(path)
    duration = img.info.get("duration", 100) or 100
    fps = max(1.0, min(60.0, 1000.0 / duration))
    frames: list[bytes] = []
    index = 0
    while True:
        try:
            img.seek(index)
        except EOFError:
            break
        frames.append(encode_jpeg(fit_square(img.convert("RGB")), rotate=rotate))
        index += 1
        if index > 2000:
            break
    if not frames:
        raise ValueError(f"no frames in {path}")
    return frames, fps


def video_frames(path: str | Path, fps: float = 30.0, rotate: int = 0) -> Iterator[bytes]:
    """Decode a video into panel-ready JPEG frames (needs imageio + imageio-ffmpeg)."""
    try:
        import imageio.v3 as iio
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError(
            "video sources need imageio: pip install imageio imageio-ffmpeg"
        ) from exc
    for frame in iio.imiter(str(path)):
        img = Image.fromarray(frame)
        yield encode_jpeg(fit_square(img), rotate=rotate)
