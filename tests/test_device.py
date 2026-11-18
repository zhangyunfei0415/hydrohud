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


def test_flash_upload_announces_then_streams_blocks() -> None:
    """0x0A 声明总长与目标，0x0B 逐块携带 12 字节头 + 数据。"""
    control = device.Control()
    sent: list[bytes] = []
    control.write_report = lambda report: sent.append(report)  # type: ignore[method-assign]
    control._handle = 1  # 让 send_command 的桩替身生效前不被 is_open 挡下

    data = bytes(range(256)) * 100  # 25600 bytes -> 3 blocks of 10240/10240/5120
    control.upload_file(data, target=device.FLASH_TARGET_STARTUP)

    assert sent, "no reports were written"
    announce = sent[0]
    assert announce[0] == 0x00
    assert announce[1] == device.CMD_FILE_ANNOUNCE
    assert announce[2] == 1  # 单包
    assert announce[4] == 0x80
    assert announce[5:9] == (1).to_bytes(4, "little")
    assert announce[9:13] == len(data).to_bytes(4, "little")

    blocks = sent[1:]
    total = sum(len(packet) - 5 for packet in blocks)  # 每包 436 字节载荷
    assert all(len(packet) == device.CONTROL_REPORT_LEN for packet in blocks)
    assert all(packet[1] == device.CMD_FILE_CHUNK for packet in blocks)
    # 每块首个包的头：offset / length / pad / progress
    firsts = [packet for packet in blocks if packet[5:9] in (b"\x00\x00\x00\x00",)]
    assert firsts, "no block started at offset 0"
    first = firsts[0]
    assert int.from_bytes(first[5:9], "little") == 0
    block_len = int.from_bytes(first[9:11], "little")
    assert block_len == device.FLASH_BLOCK_SIZE
    assert first[11:13] == b"\x00\x00"
    assert first[13] == 40  # 10240 / 25600 = 40%
    assert total >= len(data)


def test_flash_upload_rejects_oversized_and_empty_payloads() -> None:
    control = device.Control()
    control.write_report = lambda report: None  # type: ignore[method-assign]
    control._handle = 1
    with pytest.raises(ValueError):
        control.upload_file(b"")
    with pytest.raises(ValueError):
        control.upload_file(b"\x00" * (device.FLASH_MAX_BYTES + 1))


def test_flash_block_header_is_little_endian() -> None:
    assert device._pack_u32(0x01020304) == b"\x04\x03\x02\x01"
    assert device._pack_u16(0x0102) == b"\x02\x01"
    assert device.FLASH_HEADER_LEN == 12


def test_control_reply_quadrant_tolerates_both_report_id_conventions() -> None:
    # Windows hid.dll keeps the report id, so the quadrant lands at offset 5.
    with_id = bytes([0x81, 0, 0, 0, 0, 2]) + bytes(435)
    assert len(with_id) == 441
    assert device.control_reply_quadrant(with_id) == 2
    # hidapi-style reads strip it, shifting the same field to offset 4.
    without_id = bytes([0x81, 0, 0, 0, 3]) + bytes(435)
    assert len(without_id) == 440
    assert device.control_reply_quadrant(without_id) == 3


def test_control_reply_quadrant_returns_none_for_short_or_empty_replies() -> None:
    assert device.control_reply_quadrant(b"") is None
    assert device.control_reply_quadrant(b"\x81\x00") is None


def test_describe_panel_matches_the_device_table() -> None:
    spec = device.detected_spec()
    if spec is None:
        pytest.skip("panel not connected")
    text = device.describe_panel()
    assert spec.label in text
    assert f"{spec.vendor_id:04X}:{spec.product_id:04X}" in text


def test_prepare_flash_image_centre_crops_to_the_panel_square() -> None:
    """待机图不是推流帧：先居中裁成 480×480，再按 flash 上限（1 MiB）编码。"""
    wide = Image.new("RGB", (1200, 600), (10, 20, 30))
    wide.paste((250, 250, 250), (600, 0, 1200, 600))

    payload = device.prepare_flash_image(wide)

    assert payload[:2] == b"\xff\xd8" and payload[-2:] == b"\xff\xd9"
    assert len(payload) <= device.FLASH_IMAGE_MAX_BYTES
    decoded = Image.open(__import__("io").BytesIO(payload))
    assert decoded.size == (device.SCREEN_W, device.SCREEN_H)
    assert decoded.getpixel((470, 240))[0] > 200  # 右半边保住了，说明是居中裁切


def test_flash_image_encoding_keeps_more_detail_than_a_stream_frame() -> None:
    """flash 图没有 46920 字节的推流上限，所以能用比推流帧更高的质量。"""
    img = Image.new("RGB", (480, 480))
    img.putdata(
        [
            ((x * 7) % 256, (y * 5) % 256, ((x + y) * 3) % 256)
            for y in range(480)
            for x in range(480)
        ]
    )

    flash_payload = device.prepare_flash_image(img)
    stream_payload = device.encode_jpeg(img)

    assert len(flash_payload) <= device.FLASH_IMAGE_MAX_BYTES
    assert len(flash_payload) > len(stream_payload)


def test_prepare_flash_image_accepts_a_path(tmp_path) -> None:
    path = tmp_path / "standby.png"
    Image.new("RGB", (480, 480), (200, 30, 30)).save(path)

    payload = device.prepare_flash_image(path)

    assert len(payload) <= device.FLASH_IMAGE_MAX_BYTES
    assert len(payload) < 20000  # 纯色图不需要很多字节


def test_encode_flash_image_reports_a_picture_it_cannot_fit() -> None:
    with pytest.raises(ValueError, match="cannot fit the picture"):
        device.encode_flash_image(_noise_image(480), max_bytes=512)


def test_flash_image_cap_is_below_the_firmware_ceiling() -> None:
    assert 0 < device.FLASH_IMAGE_MAX_BYTES < device.FLASH_MAX_BYTES
