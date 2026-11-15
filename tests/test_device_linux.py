"""Tests for the Linux hidraw backend of `hydrohud.device` (no hardware, no /dev).

`Screen` and `Control` were written against `hid.dll`; the Linux port routes the
same framing through `hydrohud.transport.HidrawTransport`. These tests swap the
platform flag and the transport class, so nothing here touches a real panel.
"""

from __future__ import annotations

import pytest

from hydrohud import device, devices


class RecordingTransport:
    """Stand-in for `HidrawTransport` that records writes and replays replies."""

    instances: list[RecordingTransport] = []
    open_error: BaseException | None = None

    def __init__(self, path: str, report_len: int) -> None:
        self._path = path
        self.report_len = report_len
        self._open = False
        self.sent: list[bytes] = []
        self.replies: list[bytes] = []
        RecordingTransport.instances.append(self)

    @property
    def path(self) -> str:
        return self._path

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        if RecordingTransport.open_error is not None:
            raise RecordingTransport.open_error
        self._open = True

    def close(self) -> None:
        self._open = False

    def write_report(self, data: bytes) -> None:
        if not self._open:
            raise RuntimeError("recording transport is not open")
        self.sent.append(bytes(data))

    def read_report(self, length: int, timeout: float = 1.0) -> bytes | None:
        if not self._open:
            raise RuntimeError("recording transport is not open")
        return self.replies.pop(0) if self.replies else None


@pytest.fixture
def linux_backend(monkeypatch: pytest.MonkeyPatch) -> type[RecordingTransport]:
    """Pretend to run on Linux with the recording transport installed."""
    RecordingTransport.instances = []
    RecordingTransport.open_error = None
    monkeypatch.setattr(device, "_IS_LINUX", True)
    monkeypatch.setattr(device, "transport_mod", _TransportModule(RecordingTransport))
    return RecordingTransport


class _TransportModule:
    """The slice of `hydrohud.transport` that `hydrohud.device` uses."""

    def __init__(self, transport_class: type[RecordingTransport]) -> None:
        self.HidrawTransport = transport_class

    @staticmethod
    def enumerate_hidraw(vendor_id: int, product_id: int) -> list[object]:
        return []


def _image_info(path: str = "/dev/hidraw1") -> device.HidInfo:
    return device.HidInfo(
        path=path,
        vendor_id=0x1A2C,
        product_id=0x1520,
        interface_index=devices.IMAGE_INTERFACE,
        input_len=device.REPORT_LEN,
        output_len=device.REPORT_LEN,
        feature_len=0,
    )


def _control_info(path: str = "/dev/hidraw0") -> device.HidInfo:
    return device.HidInfo(
        path=path,
        vendor_id=0x1A2C,
        product_id=0x1520,
        interface_index=devices.CONTROL_INTERFACE,
        input_len=device.CONTROL_REPORT_LEN,
        output_len=device.CONTROL_REPORT_LEN,
        feature_len=0,
    )


def test_backend_name_follows_the_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device, "_IS_LINUX", True)
    assert device.Screen().backend == "hidraw"
    monkeypatch.setattr(device, "_IS_LINUX", False)
    assert device.Screen().backend == "hid"


def test_screen_opens_the_hidraw_node(linux_backend: type[RecordingTransport]) -> None:
    screen = device.Screen(_image_info("/dev/hidraw7"))

    screen.open()

    assert screen.is_open is True
    transport = linux_backend.instances[0]
    assert transport.path == "/dev/hidraw7"
    assert transport.report_len == device.REPORT_LEN  # from the class, not the node
    screen.close()
    assert screen.is_open is False
    assert transport.is_open is False


def test_opening_twice_keeps_one_transport(linux_backend: type[RecordingTransport]) -> None:
    screen = device.Screen(_image_info())
    screen.open()
    screen.open()
    assert len(linux_backend.instances) == 1
    screen.close()


def test_a_frame_reaches_the_transport_report_by_report(
    linux_backend: type[RecordingTransport],
) -> None:
    screen = device.Screen(_image_info())
    screen.open()
    reports = device.frame_reports(b"\xff\xd8" + b"\x00" * 3000)
    transport = linux_backend.instances[0]

    for report in reports:
        screen.write_report(report)

    assert transport.sent == reports
    assert all(len(report) == device.REPORT_LEN for report in transport.sent)
    screen.close()


def test_write_report_checks_the_length_and_open_state(
    linux_backend: type[RecordingTransport],
) -> None:
    screen = device.Screen(_image_info())
    with pytest.raises(RuntimeError, match="not open"):
        screen.write_report(b"\x00" * device.REPORT_LEN)
    screen.open()
    with pytest.raises(ValueError, match="must be 1025 bytes"):
        screen.write_report(b"\x00" * 10)
    screen.close()


def test_control_query_returns_the_panel_reply(
    linux_backend: type[RecordingTransport],
) -> None:
    control = device.Control(_control_info())
    control.open()
    reply = bytes([0x00, device.CMD_QUERY, 0x01, 0x00, 0x00, 0x02]) + b"\x00" * 435
    linux_backend.instances[0].replies.append(reply)

    assert control.query(timeout=0.1) == reply
    sent = linux_backend.instances[0].sent
    assert len(sent) == 1
    assert sent[0][1] == device.CMD_QUERY
    assert len(sent[0]) == device.CONTROL_REPORT_LEN
    control.close()


def test_control_query_without_a_reply_raises(
    linux_backend: type[RecordingTransport],
) -> None:
    control = device.Control(_control_info())
    control.open()
    with pytest.raises(RuntimeError, match="did not answer"):
        control.query(timeout=0.01)
    control.close()


def test_a_permission_error_becomes_an_actionable_message(
    linux_backend: type[RecordingTransport],
) -> None:
    linux_backend.open_error = PermissionError(13, "Permission denied", "/dev/hidraw0")
    control = device.Control(_control_info())

    with pytest.raises(RuntimeError) as excinfo:
        control.open()

    message = str(excinfo.value)
    assert "/dev/hidraw0" in message
    assert "packaging/linux/" in message
    assert control.is_open is False


def test_close_after_a_failed_open_is_a_no_op(
    linux_backend: type[RecordingTransport],
) -> None:
    linux_backend.open_error = OSError("no such device")
    screen = device.Screen(_image_info())
    with pytest.raises(RuntimeError):
        screen.open()
    screen.close()
    assert screen.is_open is False


def test_linux_collections_take_report_lengths_from_the_device_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Node:
        def __init__(self, path: str, interface: int | None) -> None:
            self.path = path
            self.vendor_id = 0x1A2C
            self.product_id = 0x1520
            self.interface_number = interface
            self.report_len = None

    class TransportModule(_TransportModule):
        @staticmethod
        def enumerate_hidraw(vendor_id: int, product_id: int) -> list[object]:
            return [Node("/dev/hidraw0", 0), Node("/dev/hidraw1", 1)]

    monkeypatch.setattr(device, "_IS_LINUX", True)
    monkeypatch.setattr(device, "transport_mod", TransportModule(RecordingTransport))

    collections = device.enumerate_collections()

    assert [(c.interface_index, c.output_len, c.role) for c in collections] == [
        (devices.CONTROL_INTERFACE, device.CONTROL_REPORT_LEN, "control"),
        (devices.IMAGE_INTERFACE, device.REPORT_LEN, "image"),
    ]


def test_linux_collections_survive_an_unlabelled_node(monkeypatch: pytest.MonkeyPatch) -> None:
    class Node:
        path = "/dev/hidraw9"
        vendor_id = 0x1A2C
        product_id = 0x1520
        interface_number = None
        report_len = None

    class TransportModule(_TransportModule):
        @staticmethod
        def enumerate_hidraw(vendor_id: int, product_id: int) -> list[object]:
            return [Node()]

    monkeypatch.setattr(device, "_IS_LINUX", True)
    monkeypatch.setattr(device, "transport_mod", TransportModule(RecordingTransport))

    (collection,) = device.enumerate_collections()

    assert collection.interface_index == -1
    assert collection.output_len == 0
    assert collection.role == "unknown"


def test_find_image_interface_reports_a_missing_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device, "enumerate_collections", lambda *a, **k: [])

    with pytest.raises(RuntimeError, match="is the cooler plugged in"):
        device.find_image_interface()


def test_enumerate_devices_is_empty_when_the_scan_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> list[device.HidInfo]:
        raise OSError("sysfs is gone")

    monkeypatch.setattr(device, "enumerate_collections", boom)

    assert device.enumerate_devices() == []
