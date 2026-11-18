"""Tests for `hydrohud.transport` (fake transport + sysfs parsing; no hardware).

The hidraw tests never touch a real ``/dev`` node: the sysfs scan is pointed at a
temporary tree through ``HYDROHUD_SYSFS_ROOT`` / the ``sysfs_root`` argument.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hydrohud import transport


def _write_uevent(
    root: Path,
    node: str,
    *,
    hid_id: str = "0003:00001A2C:00001520",
    phys: str = "usb-0000:00:14.0-3/input1:1.1",
) -> Path:
    """Create ``<root>/class/hidraw/<node>/device/uevent`` and return the uevent."""
    uevent = root / "class" / "hidraw" / node / "device" / "uevent"
    uevent.parent.mkdir(parents=True, exist_ok=True)
    uevent.write_text(
        "DRIVER=hid-generic\n"
        f"HID_ID={hid_id}\n"
        "HID_NAME=TCOMAS LG600\n"
        f"HID_PHYS={phys}\n"
        "HID_UNIQ=\n",
        encoding="utf-8",
    )
    return uevent


# --------------------------------------------------------------------------- #
# FakeTransport
# --------------------------------------------------------------------------- #
def test_fake_transport_tracks_open_state() -> None:
    fake = transport.FakeTransport("fake://lg600")
    assert fake.path == "fake://lg600"
    assert fake.is_open is False
    fake.open()
    assert fake.is_open is True
    fake.close()
    assert fake.is_open is False
    fake.close()  # idempotent


def test_fake_transport_records_writes_in_order() -> None:
    fake = transport.FakeTransport()
    fake.open()
    fake.write_report(b"\x00\x08")
    fake.write_report(b"\x00\x12")
    assert fake.sent == [b"\x00\x08", b"\x00\x12"]


def test_fake_transport_reports_that_it_must_be_opened_first() -> None:
    fake = transport.FakeTransport()
    with pytest.raises(RuntimeError, match="not open"):
        fake.write_report(b"\x00")
    with pytest.raises(RuntimeError, match="not open"):
        fake.read_report(8)


def test_fake_transport_replays_fed_reports_in_order() -> None:
    fake = transport.FakeTransport()
    fake.open()
    fake.feed(b"\x00\x81reply-one")
    fake.feed(b"\x00\x81reply-two")
    assert fake.read_report(64) == b"\x00\x81reply-one"
    assert fake.read_report(64) == b"\x00\x81reply-two"
    assert fake.read_report(64) is None


def test_fake_transport_truncates_reads_to_the_requested_length() -> None:
    fake = transport.FakeTransport()
    fake.open()
    fake.feed(b"0123456789")
    assert fake.read_report(4) == b"0123"


def test_fake_transport_injects_a_hard_write_error() -> None:
    fake = transport.FakeTransport()
    fake.open()
    fake.fail_on_write = OSError("panel vanished")
    with pytest.raises(OSError, match="panel vanished"):
        fake.write_report(b"\x00")
    assert fake.sent == []


def test_fake_transport_fails_after_a_number_of_writes() -> None:
    fake = transport.FakeTransport()
    fake.open()
    fake.fail_after(2)
    fake.write_report(b"\x00one")
    fake.write_report(b"\x00two")
    with pytest.raises(OSError, match="injected write failure"):
        fake.write_report(b"\x00three")
    with pytest.raises(OSError, match="injected write failure"):
        fake.write_report(b"\x00four")
    assert fake.sent == [b"\x00one", b"\x00two"]


# --------------------------------------------------------------------------- #
# HidrawTransport (Linux-only open path)
# --------------------------------------------------------------------------- #
def test_hidraw_transport_is_closed_until_opened(tmp_path: Path) -> None:
    node = tmp_path / "hidraw-does-not-exist"
    hidraw = transport.HidrawTransport(str(node), 1025)
    assert hidraw.path == str(node)
    assert hidraw.report_len == 1025
    assert hidraw.is_open is False
    hidraw.close()  # closing an unopened transport is a no-op


def test_hidraw_transport_requires_an_open_handle() -> None:
    hidraw = transport.HidrawTransport("/dev/hidraw-does-not-exist", 441)
    with pytest.raises(RuntimeError, match="not open"):
        hidraw.write_report(b"\x00" * 441)
    with pytest.raises(RuntimeError, match="not open"):
        hidraw.read_report(441)


def test_hidraw_transport_open_is_linux_only(tmp_path: Path) -> None:
    hidraw = transport.HidrawTransport(str(tmp_path / "hidraw0"), 1025)
    if sys.platform.startswith("linux"):
        with pytest.raises(OSError):
            hidraw.open()
    else:
        with pytest.raises(RuntimeError, match="Linux-only"):
            hidraw.open()
        assert hidraw.is_open is False


# --------------------------------------------------------------------------- #
# enumerate_hidraw / report_len_for
# --------------------------------------------------------------------------- #
def test_enumerate_hidraw_finds_the_image_interface(tmp_path: Path) -> None:
    _write_uevent(tmp_path, "hidraw0", phys="usb-0000:00:14.0-3/input1:1.0")
    _write_uevent(tmp_path, "hidraw1", phys="usb-0000:00:14.0-3/input1:1.1/mi_01")

    found = transport.enumerate_hidraw(0x1A2C, 0x1520, sysfs_root=tmp_path)

    assert [info.path for info in found] == ["/dev/hidraw0", "/dev/hidraw1"]
    assert all(info.vendor_id == 0x1A2C for info in found)
    assert all(info.product_id == 0x1520 for info in found)
    assert [info.interface_number for info in found] == [None, 1]
    assert all(info.report_len is None for info in found)


def test_enumerate_hidraw_ignores_other_devices_and_nodes_without_uevent(
    tmp_path: Path,
) -> None:
    _write_uevent(tmp_path, "hidraw0", hid_id="0003:00001A2C:00001520")
    _write_uevent(tmp_path, "hidraw1", hid_id="0003:00001A2C:0000151C")
    _write_uevent(tmp_path, "hidraw2", hid_id="0003:00001234:00005678")
    # A node whose device directory has no uevent must not crash the scan.
    (tmp_path / "class" / "hidraw" / "hidraw3").mkdir(parents=True)

    found = transport.enumerate_hidraw(0x1A2C, 0x1520, sysfs_root=tmp_path)

    assert [info.path for info in found] == ["/dev/hidraw0"]


def test_enumerate_hidraw_uses_the_environment_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_uevent(tmp_path, "hidraw2")
    monkeypatch.setenv("HYDROHUD_SYSFS_ROOT", str(tmp_path))

    found = transport.enumerate_hidraw(0x1A2C, 0x1520)

    assert [info.path for info in found] == ["/dev/hidraw2"]


def test_enumerate_hidraw_returns_empty_without_sysfs(tmp_path: Path) -> None:
    assert transport.enumerate_hidraw(0x1A2C, 0x1520, sysfs_root=tmp_path) == []
    assert transport.enumerate_hidraw(0x1A2C, 0x1520, sysfs_root=tmp_path / "missing") == []


def test_report_len_for_refuses_to_guess(tmp_path: Path) -> None:
    assert transport.report_len_for(str(tmp_path / "hidraw0")) is None


# --------------------------------------------------------------------------- #
# _read_usb_serial (the identity multi-panel grouping keys on)
# --------------------------------------------------------------------------- #
def _usb_device_tree(
    tmp_path: Path,
    *,
    serial: str | None = None,
    devpath: str | None = None,
) -> tuple[Path, Path]:
    """Build the sysfs shape below one hidraw node and return (node, usb device).

    Real sysfs names the interface directory ``1-3:1.1`` and the HID device
    ``0003:1A2C:1520.0001``, and ``hidrawN/device`` is a symlink to the latter;
    colons are illegal in Windows path names, so the stand-in keeps the nesting
    depth but not the exact spelling. Without symlink privileges the device entry
    is a real directory and two levels up is the hidraw class directory, which is
    where the USB attribute files then go.
    """
    node = tmp_path / "class" / "hidraw" / "hidraw0"
    node.mkdir(parents=True)
    usb_device = tmp_path / "usb" / "1-3"
    hid_device = usb_device / "1-3.1" / "0003_1A2C_1520.0001"
    hid_device.mkdir(parents=True)
    try:
        (node / "device").symlink_to(hid_device, target_is_directory=True)
    except OSError:
        (node / "device").mkdir()
        usb_device = node.parent
    if serial is not None:
        (usb_device / "serial").write_text(serial, encoding="utf-8")
    if devpath is not None:
        (usb_device / "devpath").write_text(devpath, encoding="utf-8")
    return node, usb_device


def test_read_usb_serial_returns_the_device_serial(tmp_path: Path) -> None:
    node, _ = _usb_device_tree(tmp_path, serial="SN-A1B2C3\n", devpath="1-3")

    # The serial identifies the panel itself, so it beats the port path.
    assert transport._read_usb_serial(node) == "SN-A1B2C3"


def test_read_usb_serial_falls_back_to_the_usb_port_path(tmp_path: Path) -> None:
    node, usb_device = _usb_device_tree(tmp_path, serial="  \n", devpath="1-3\n")

    assert (usb_device / "serial").exists()
    # An empty serial file means the device has none: the port is stable enough.
    assert transport._read_usb_serial(node) == "port:1-3"


def test_read_usb_serial_returns_empty_when_the_node_has_no_device(tmp_path: Path) -> None:
    node = tmp_path / "class" / "hidraw" / "hidraw0"
    node.mkdir(parents=True)

    assert transport._read_usb_serial(node) == ""


def test_list_hidraw_carries_the_serial_onto_each_node(tmp_path: Path) -> None:
    node, _ = _usb_device_tree(tmp_path, serial="SN-A1B2C3", devpath="1-3")
    uevent = _write_uevent(tmp_path, "hidraw0")

    (info,) = transport.enumerate_hidraw(0x1A2C, 0x1520, sysfs_root=tmp_path)

    assert uevent.exists() and node.exists()
    assert info.serial == "SN-A1B2C3"
    assert transport.HidrawInfo("x", 0, 0, None, None).serial == ""
