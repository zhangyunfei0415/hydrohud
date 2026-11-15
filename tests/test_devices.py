"""Tests for the device table in `hydrohud.devices` (pure data, no hardware)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from hydrohud import devices

WINDOWS_PATH = (
    r"\\?\HID#VID_1A2C&PID_1520&MI_01#7&2f4a1b3c&0&0000#" r"{4d1e55b2-f16f-11cf-88cb-001111000030}"
)


def test_table_contains_the_verified_lg600() -> None:
    spec = devices.find_spec(0x1A2C, 0x1520)
    assert spec is not None
    assert spec.key == "lg600"
    assert spec.label == "TCOMAS LG600"
    assert spec.verified is True


def test_lg700_is_registered_but_not_claimed_as_supported() -> None:
    spec = devices.find_spec(0x1A2C, 0x151C)
    assert spec is not None
    assert spec.key == "lg700"
    assert spec.verified is False
    assert "未实测" in spec.note


def test_lg600_protocol_numbers_match_the_hardware_contract() -> None:
    spec = devices.find_spec(0x1A2C, 0x1520)
    assert spec is not None
    assert spec.image_report_len == 1025
    assert spec.control_report_len == 441
    assert spec.chunk_size == 1020
    assert spec.max_chunks == 46
    assert spec.max_frame_bytes == 46920
    assert spec.control_chunk_size == 436
    assert (spec.width, spec.height) == (480, 480)
    assert spec.jpeg is True


def test_unknown_ids_do_not_match() -> None:
    assert devices.find_spec(0x1234, 0x5678) is None


def test_all_specs_returns_the_table() -> None:
    specs = devices.all_specs()
    assert isinstance(specs, tuple)
    assert specs == devices.DEVICES
    assert {spec.key for spec in specs} >= {"lg600", "lg700"}


def test_specs_are_immutable() -> None:
    spec = devices.find_spec(0x1A2C, 0x1520)
    assert spec is not None
    with pytest.raises(FrozenInstanceError):
        spec.key = "other"  # type: ignore[misc]


def test_match_hidinfo_accepts_int_ids() -> None:
    hidinfo = {"vendor_id": 0x1A2C, "product_id": 0x1520, "interface_number": 1}
    spec = devices.match_hidinfo(hidinfo)
    assert spec is not None
    assert spec.key == "lg600"


def test_match_hidinfo_accepts_hidapi_hex_strings() -> None:
    hidinfo = {"vendor": "0x1a2c", "product": "0x1520"}
    spec = devices.match_hidinfo(hidinfo)
    assert spec is not None
    assert spec.key == "lg600"


def test_match_hidinfo_accepts_bare_hex_ids() -> None:
    hidinfo = {"vendor_id": "1a2c", "product_id": "151c"}
    spec = devices.match_hidinfo(hidinfo)
    assert spec is not None
    assert spec.key == "lg700"


def test_match_hidinfo_falls_back_to_the_path() -> None:
    spec = devices.match_hidinfo({"path": WINDOWS_PATH})
    assert spec is not None
    assert spec.key == "lg600"


def test_match_hidinfo_ignores_unsupported_or_incomplete_data() -> None:
    assert devices.match_hidinfo({}) is None
    assert devices.match_hidinfo({"vendor_id": 0x1A2C}) is None
    assert devices.match_hidinfo({"vendor_id": 0x1234, "product_id": 0x5678}) is None
    assert devices.match_hidinfo({"path": "/dev/hidraw3"}) is None


def test_match_path_reads_a_windows_device_interface_path() -> None:
    spec = devices.match_path(WINDOWS_PATH)
    assert spec is not None
    assert spec.key == "lg600"


def test_match_path_is_case_insensitive() -> None:
    spec = devices.match_path(r"hid#vid_1a2c&pid_151c&mi_01")
    assert spec is not None
    assert spec.key == "lg700"


def test_match_path_reads_a_libhidapi_style_id_pair() -> None:
    spec = devices.match_path("1a2c:1520")
    assert spec is not None
    assert spec.key == "lg600"


def test_match_path_returns_none_without_ids() -> None:
    assert devices.match_path("/dev/hidraw3") is None
    assert devices.match_path("") is None
    assert devices.match_path(r"HID#VID_1234&PID_1520&MI_00") is None


def test_spec_key_and_describe_are_stable_for_logs() -> None:
    lg600 = devices.find_spec(0x1A2C, 0x1520)
    lg700 = devices.find_spec(0x1A2C, 0x151C)
    assert lg600 is not None and lg700 is not None
    assert devices.spec_key(lg600) == "lg600"
    assert devices.describe(lg600) == "TCOMAS LG600 [1a2c:1520]"
    assert devices.describe(lg700).endswith("(unverified)")
    assert "TCOMAS LG700" in devices.describe(lg700)
