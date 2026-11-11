"""Tests for the HID framing and JPEG encoding in `hydrohud.device` (no hardware)."""

from __future__ import annotations

import random

import pytest
from PIL import Image

from hydrohud import device


def _noise_image(size: int = 480) -> Image.Image:
    """Worst case for JPEG: per-pixel noise, so the encoder has to work hard."""
    rng = random.Random(1234)
    img = Image.new("RGB", (size, size))
    img.putdata(
        [(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(size * size)]
    )
    return img


def test_report_lengths_match_panel_contract() -> None:
    assert device.REPORT_LEN == 1025
    assert device.CONTROL_REPORT_LEN == 441
    assert device.MAX_FRAME_BYTES == 46920
    assert device.MAX_FRAME_BYTES == device.CHUNK_SIZE * device.MAX_CHUNKS


def test_build_packets_marks_first_packet_and_numbers_the_rest() -> None:
    payload = bytes(range(256)) * 8  # 2048 bytes -> 3 chunks of 1020
    packets = device.build_packets(device.CMD_STREAM, payload, 1020, 1025)

    assert len(packets) == 3
    assert all(len(packet) == 1025 for packet in packets)
    # report id, command, flags
    assert [packet[0] for packet in packets] == [0x00, 0x00, 0x00]
    assert [packet[1] for packet in packets] == [0x08, 0x08, 0x08]
    # first packet carries the chunk count, then 1-based indices
    assert [packet[2] for packet in packets] == [3, 1, 2]
    assert [packet[3] for packet in packets] == [0x00, 0x00, 0x00]
    assert [packet[4] for packet in packets] == [0x80, 0x00, 0x00]
    # payload starts at offset 5 and is zero padded to the chunk boundary
    assert packets[0][5:261] == payload[:256]
    assert packets[2][5 + 8 :] == b"\x00" * 1012
    assert packets[2][5:13] == payload[2040:]


def test_single_packet_transfer_reports_count_one() -> None:
    packets = device.build_packets(device.CMD_QUERY, b"", device.CONTROL_CHUNK_SIZE, 441)
    assert len(packets) == 1
    assert len(packets[0]) == 441
    assert packets[0][:5] == bytes([0x00, device.CMD_QUERY, 1, 0x00, 0x80])


def test_control_command_parameter_lands_at_offset_five() -> None:
    packets = device.build_packets(device.CMD_BRIGHTNESS, bytes([42]), 436, 441)
    assert packets[0][1] == device.CMD_BRIGHTNESS
    assert packets[0][5] == 42


def test_a_frame_is_padded_to_a_whole_packet_count() -> None:
    jpeg = device.encode_jpeg(Image.new("RGB", (480, 480), (10, 20, 30)))
    packets = device.frame_reports(jpeg)
    expected = max(1, -(-len(jpeg) // device.CHUNK_SIZE))
    assert len(packets) == expected
    assert all(len(packet) == device.REPORT_LEN for packet in packets)
    # First packet carries the chunk count, the rest 1-based indices.
    assert packets[0][2] == expected
    assert [packet[2] for packet in packets[1:]] == list(range(1, expected))
    # The payload is zero padded up to the last packet boundary.
    padding = expected * device.CHUNK_SIZE - len(jpeg)
    assert padding < device.CHUNK_SIZE
    if padding:
        assert packets[-1][-padding:] == b"\x00" * padding


def test_frame_over_the_panel_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        device.frame_reports(b"\x00" * (device.MAX_FRAME_BYTES + 1))
    with pytest.raises(ValueError):
        device.frame_reports(b"")


def test_encode_jpeg_stays_under_the_frame_limit() -> None:
    assert (
        len(device.encode_jpeg(Image.new("RGB", (480, 480), (200, 30, 30))))
        <= device.MAX_FRAME_BYTES
    )
    assert len(device.encode_jpeg(_noise_image(480))) <= device.MAX_FRAME_BYTES


def test_encode_jpeg_rotation_keeps_the_panel_size() -> None:
    img = Image.open  # keep the import meaningful for readers
    del img
    source = Image.new("RGB", (480, 480), (0, 0, 0))
    source.paste((255, 0, 0), (0, 0, 240, 480))  # left half red

    upright = Image.open(__import__("io").BytesIO(device.encode_jpeg(source, rotate=0)))
    rotated = Image.open(__import__("io").BytesIO(device.encode_jpeg(source, rotate=90)))
    assert upright.size == rotated.size == (480, 480)
    # A clockwise rotation moves the left half to the top.
    assert rotated.getpixel((240, 12))[0] > 200
    assert rotated.getpixel((240, 468))[0] < 60


def test_fit_square_centre_crops_non_square_images() -> None:
    wide = Image.new("RGB", (1200, 600), (0, 0, 0))
    wide.paste((255, 255, 255), (600, 0, 1200, 600))  # right half white
    squared = device.fit_square(wide)
    assert squared.size == (480, 480)
    assert squared.getpixel((470, 240))[0] > 200  # right edge keeps the white half


def test_enumerate_devices_is_safe_without_hardware() -> None:
    collections = device.enumerate_devices()
    assert isinstance(collections, list)
    for collection in collections:
        assert set(collection) >= {"role", "interface", "path", "output_len"}
        assert collection["role"] in {"image", "control", "unknown"}


def test_gif_frames_encodes_animation(tmp_path) -> None:
    path = tmp_path / "anim.gif"
    frames = [Image.new("RGB", (240, 240), colour) for colour in ((255, 0, 0), (0, 255, 0))]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=100, loop=0)

    encoded, fps = device.gif_frames(path)
    assert len(encoded) == 2
    assert fps == pytest.approx(10.0)
    assert all(len(jpeg) <= device.MAX_FRAME_BYTES for jpeg in encoded)
