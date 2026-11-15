"""``hydrohud.qrmini`` 纯 Python 二维码编码器的测试。

正确性的主证据是与参考实现 ``qrcode``（可选依赖）逐格交叉校验：同一 data、纠错等级、
版本和掩码下矩阵必须完全一致。参考实现不可用时相关用例自动跳过，结构自证与边界用例
仍然运行。

约定：不写文件、不访问网络（``qrcode`` 已安装时也不触发 pip）；矩阵方向统一为
``matrix[row][col]``，``True`` 表示深色模块。
"""

from __future__ import annotations

import time

import pytest

from hydrohud import qrmini

ERROR_LEVELS = ("L", "M", "Q", "H")

_FORMAT_POLY = 0x537
_FORMAT_MASK = 0x5412
_VERSION_POLY = 0x1F25
_ERROR_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}


# --------------------------------------------------------------------------- #
# 参考实现辅助
# --------------------------------------------------------------------------- #
def _require_reference():
    """返回参考实现 ``qrcode`` 模块；未安装时跳过当前用例。"""
    return pytest.importorskip("qrcode")


def _reference_matrix(data: bytes, error: str = "M", version=None, mask=None) -> list[list[bool]]:
    """用参考实现生成无静区矩阵；data 固定按 8-bit byte 模式编码。"""
    qrcode = _require_reference()
    from qrcode.util import MODE_8BIT_BYTE, QRData

    levels = {
        "L": qrcode.constants.ERROR_CORRECT_L,
        "M": qrcode.constants.ERROR_CORRECT_M,
        "Q": qrcode.constants.ERROR_CORRECT_Q,
        "H": qrcode.constants.ERROR_CORRECT_H,
    }
    qr = qrcode.QRCode(version=version, error_correction=levels[error], border=0, mask_pattern=mask)
    qr.add_data(QRData(data, mode=MODE_8BIT_BYTE))
    qr.make(fit=version is None)
    return qr.get_matrix()


def _reference_capacity(version: int, error: str) -> int:
    """参考实现该版本/等级可容纳的 byte 模式字节数。"""
    qrcode = _require_reference()
    from qrcode import base

    levels = {
        "L": qrcode.constants.ERROR_CORRECT_L,
        "M": qrcode.constants.ERROR_CORRECT_M,
        "Q": qrcode.constants.ERROR_CORRECT_Q,
        "H": qrcode.constants.ERROR_CORRECT_H,
    }
    blocks = base.rs_blocks(version, levels[error])
    data_bits = sum(block.data_count for block in blocks) * 8
    return (data_bits - 4 - (8 if version <= 9 else 16)) // 8


def _assert_same_matrix(actual: list[list[bool]], expected: list[list[bool]]) -> None:
    assert len(actual) == len(expected)
    for row_index, (got_row, want_row) in enumerate(zip(actual, expected)):
        if got_row != want_row:
            wrong = [col for col, (got, want) in enumerate(zip(got_row, want_row)) if got != want]
            raise AssertionError(f"row {row_index} differs at columns {wrong[:10]}")


# --------------------------------------------------------------------------- #
# 规范算法辅助（测试内独立实现，用于结构自证）
# --------------------------------------------------------------------------- #
def _bch_remainder(value: int, generator: int, degree: int) -> int:
    remainder = value << degree
    while remainder.bit_length() > degree:
        remainder ^= generator << (remainder.bit_length() - generator.bit_length())
    return remainder


def _expected_format_bits(level: str, mask: int) -> int:
    data = (_ERROR_BITS[level] << 3) | mask
    return ((data << 10) | _bch_remainder(data, _FORMAT_POLY, 10)) ^ _FORMAT_MASK


def _expected_version_bits(version: int) -> int:
    return (version << 12) | _bch_remainder(version, _VERSION_POLY, 12)


def _read_format_bits(matrix: list[list[bool]], size: int) -> tuple[int, int]:
    """从两份格式信息副本中读出 15 位数值。"""
    cells = [(index, 8, index) for index in range(6)]
    cells += [(7, 8, 6), (8, 8, 7), (8, 7, 8)]
    cells += [(8, 14 - index, index) for index in range(9, 15)]
    first = 0
    for row, col, index in cells:
        if matrix[row][col]:
            first |= 1 << index
    second = 0
    for index in range(8):
        if matrix[8][size - 1 - index]:
            second |= 1 << index
    for index in range(8, 15):
        if matrix[size - 15 + index][8]:
            second |= 1 << index
    return first, second


def _read_version_bits(matrix: list[list[bool]], size: int) -> tuple[int, int]:
    """从两份版本信息副本中读出 18 位数值。"""
    top_right = 0
    bottom_left = 0
    for index in range(18):
        if matrix[index // 3][size - 11 + index % 3]:
            top_right |= 1 << index
        if matrix[size - 11 + index % 3][index // 3]:
            bottom_left |= 1 << index
    return top_right, bottom_left


def _chosen_mask(matrix: list[list[bool]], size: int) -> int:
    first, _ = _read_format_bits(matrix, size)
    return ((first ^ _FORMAT_MASK) >> 10) & 0x7


# --------------------------------------------------------------------------- #
# 与参考实现交叉校验
# --------------------------------------------------------------------------- #
def test_forced_masks_match_reference():
    """同一 data/error/version 下，8 种强制掩码都必须与参考实现逐格一致。"""
    data = b"HELLO WORLD"
    for mask in range(8):
        expected = _reference_matrix(data, "M", None, mask)
        _assert_same_matrix(qrmini.make(data, "M", mask=mask), expected)


@pytest.mark.parametrize("version", range(1, 21))
@pytest.mark.parametrize("level", ERROR_LEVELS)
def test_all_versions_and_levels_match_reference(version, level):
    """版本 1-20 × 4 个纠错等级，覆盖版本信息、分块与交错。"""
    mask = (version + ERROR_LEVELS.index(level)) % 8
    expected = _reference_matrix(b"QR", level, version, mask)
    actual = qrmini.make(b"QR", level, version=version, mask=mask)
    _assert_same_matrix(actual, expected)


@pytest.mark.parametrize("level", ERROR_LEVELS)
def test_capacity_boundary_matches_reference(level):
    """正好填满容量时仍需与参考实现一致，容量 +1 必须报错。"""
    for version in (1, 9, 10, 20):
        capacity = qrmini._max_payload_length(version, level)
        assert capacity == _reference_capacity(version, level)
        data = b"a" * capacity
        _assert_same_matrix(
            qrmini.make(data, level, version=version, mask=3),
            _reference_matrix(data, level, version, 3),
        )
        with pytest.raises(ValueError):
            qrmini.make(data + b"a", level, version=version)


@pytest.mark.parametrize("level", ERROR_LEVELS)
def test_utf8_text_matches_reference(level):
    """中文与 ° % × 等符号走 UTF-8 byte 模式，必须与参考实现一致。"""
    text = "水冷屏 HydroHUD 480×480 °C 100%"
    data = text.encode("utf-8")
    _assert_same_matrix(
        qrmini.make(text, level, mask=5),
        _reference_matrix(data, level, None, 5),
    )
    assert qrmini.make(text, level, mask=5) == qrmini.make(data, level, mask=5)


def test_auto_version_matches_reference():
    """自动选版必须与参考实现的 best_fit 一致（掩码固定以隔离选版逻辑）。"""
    for level in ERROR_LEVELS:
        for length in (1, 5, 17, 30, 100, 200):
            data = b"x" * length
            version = qrmini.size_for(data, level)
            expected = _reference_matrix(data, level, None, 2)
            actual = qrmini.make(data, level, mask=2)
            assert len(actual) == 4 * version + 17
            _assert_same_matrix(actual, expected)


def test_auto_mask_is_penalty_minimum():
    """自动选出的掩码必须让完整符号（含格式信息）的罚分最小。

    参考实现 ``qrcode`` 在选掩码时故意隐藏格式信息位，因此不能直接比较它选出的掩码；
    这里用它独立实现的 ``lost_point`` 对 8 个完整符号评分来验证最小罚分。
    """
    _require_reference()
    from qrcode.util import lost_point

    cases = [
        (b"HELLO WORLD", "M"),
        (b"HydroHUD 480x480", "L"),
        (b"x" * 60, "Q"),
        ("中文二维码测试".encode(), "H"),
    ]
    for data, level in cases:
        actual = qrmini.make(data, level)
        size = len(actual)
        chosen = _chosen_mask(actual, size)
        scores = [lost_point(_reference_matrix(data, level, None, mask)) for mask in range(8)]
        best = min(range(8), key=lambda mask: scores[mask])
        assert chosen == best, (data, level, chosen, best, scores)
        _assert_same_matrix(actual, _reference_matrix(data, level, None, chosen))


def test_ec_block_tables_match_reference():
    """EC 分块表、总码字数与参考实现逐项一致。"""
    qrcode = _require_reference()
    from qrcode import base

    levels = {
        "L": qrcode.constants.ERROR_CORRECT_L,
        "M": qrcode.constants.ERROR_CORRECT_M,
        "Q": qrcode.constants.ERROR_CORRECT_Q,
        "H": qrcode.constants.ERROR_CORRECT_H,
    }
    assert len(qrmini.EC_CODEWORDS) == 20 * 4
    for version in range(1, 21):
        for level in ERROR_LEVELS:
            groups = qrmini.EC_BLOCK_TABLE[(version, level)]
            blocks = base.rs_blocks(version, levels[level])
            expected_pairs = [(block.total_count, block.data_count) for block in blocks]
            actual_pairs = [
                (total, data_count) for count, total, data_count in groups for _ in range(count)
            ]
            assert actual_pairs == expected_pairs
            ec_lengths = {total - data_count for total, data_count in actual_pairs}
            assert ec_lengths == {qrmini.EC_CODEWORDS[(version, level)]}
            assert all(
                total - data_count == qrmini.EC_CODEWORDS[(version, level)]
                for total, data_count in actual_pairs
            )


# --------------------------------------------------------------------------- #
# 结构自证（不依赖参考实现）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("version", range(1, 21))
def test_matrix_size_follows_version(version):
    matrix = qrmini.make(b"QR", "M", version=version)
    size = 4 * version + 17
    assert len(matrix) == size
    assert all(len(row) == size for row in matrix)


def test_finder_patterns_and_separators():
    """三个定位图形为 7×7 标准图案，且外侧一圈分隔符为浅色。"""
    matrix = qrmini.make(b"QR", "M", version=1)
    size = len(matrix)
    corners = ((0, 0), (0, size - 7), (size - 7, 0))
    for corner_row, corner_col in corners:
        for delta_row in range(-1, 8):
            for delta_col in range(-1, 8):
                row = corner_row + delta_row
                col = corner_col + delta_col
                if not (0 <= row < size and 0 <= col < size):
                    continue
                distance = max(abs(delta_row - 3), abs(delta_col - 3))
                expected_dark = distance in (0, 1, 3)
                assert matrix[row][col] is expected_dark


def test_timing_patterns_alternate():
    """时序图形在第 6 行/列交替，版本 1、2 无对齐图形干扰可全程检查。"""
    for version in (1, 2):
        matrix = qrmini.make(b"QR", "M", version=version)
        size = len(matrix)
        for index in range(8, size - 8):
            expected = index % 2 == 0
            assert matrix[6][index] is expected
            assert matrix[index][6] is expected


def test_alignment_patterns_for_version_7():
    """版本 7 的对齐图形中心坐标必须符合标准表（含被时序图形打断的位置）。"""
    matrix = qrmini.make(b"QR", "M", version=7)
    positions = (6, 22, 38)
    for row in positions:
        for col in positions:
            if (row, col) in ((6, 6), (6, 38), (38, 6)):
                continue
            for delta_row in range(-2, 3):
                for delta_col in range(-2, 3):
                    dark = max(abs(delta_row), abs(delta_col)) != 1
                    assert matrix[row + delta_row][col + delta_col] is dark


def test_dark_module_is_always_dark():
    for version in (1, 5, 7, 10, 20):
        matrix = qrmini.make(b"QR", "M", version=version)
        assert matrix[len(matrix) - 8][8] is True


@pytest.mark.parametrize("level", ERROR_LEVELS)
@pytest.mark.parametrize("mask", range(8))
def test_format_information_bits(level, mask):
    """两份格式信息副本都必须等于标准 BCH 编码结果。"""
    matrix = qrmini.make(b"QR", level, version=2, mask=mask)
    size = len(matrix)
    first, second = _read_format_bits(matrix, size)
    expected = _expected_format_bits(level, mask)
    assert first == expected
    assert second == expected
    assert _chosen_mask(matrix, size) == mask


@pytest.mark.parametrize("version", (7, 10, 20))
def test_version_information_bits(version):
    """版本 ≥7 的两份版本信息副本都必须等于标准 BCH 编码结果。"""
    matrix = qrmini.make(b"QR", "M", version=version)
    size = len(matrix)
    first, second = _read_version_bits(matrix, size)
    expected = _expected_version_bits(version)
    assert first == expected
    assert second == expected


# --------------------------------------------------------------------------- #
# 边界与参数
# --------------------------------------------------------------------------- #
def test_empty_data_rejected():
    with pytest.raises(ValueError):
        qrmini.make("")
    with pytest.raises(ValueError):
        qrmini.make(b"")
    with pytest.raises(ValueError):
        qrmini.size_for("")


def test_oversized_data_rejected():
    data = b"x" * 4000
    with pytest.raises(ValueError):
        qrmini.make(data, "L")
    with pytest.raises(ValueError):
        qrmini.size_for(data, "L")


def test_explicit_version_too_small_rejected():
    with pytest.raises(ValueError):
        qrmini.make(b"x" * 100, "H", version=1)


def test_error_level_is_case_insensitive():
    assert qrmini.make("hi", "l") == qrmini.make("hi", "L")
    assert qrmini.make("hi", "q") == qrmini.make("hi", "Q")


def test_invalid_error_level_rejected():
    for value in ("X", "", "LM"):
        with pytest.raises(ValueError):
            qrmini.make("hi", value)


def test_invalid_version_rejected():
    for value in (0, 21, -1):
        with pytest.raises(ValueError):
            qrmini.make("hi", version=value)


def test_invalid_mask_rejected():
    for value in (-1, 8):
        with pytest.raises(ValueError):
            qrmini.make("hi", mask=value)


def test_invalid_data_type_rejected():
    with pytest.raises(TypeError):
        qrmini.make(123)  # type: ignore[arg-type]


def test_bytearray_input_supported():
    data = bytearray(b"HydroHUD")
    assert qrmini.make(data, "M", mask=1) == qrmini.make(bytes(data), "M", mask=1)


def test_size_for_is_minimal_and_consistent():
    for level in ERROR_LEVELS:
        for length in (1, 20, 150):
            data = b"x" * length
            version = qrmini.size_for(data, level)
            assert isinstance(version, int)
            matrix = qrmini.make(data, level)
            assert len(matrix) == 4 * version + 17
            if version > 1:
                with pytest.raises(ValueError):
                    qrmini.make(data, level, version=version - 1)


def test_returned_matrix_is_independent():
    first = qrmini.make(b"QR", "M", mask=0)
    first[0][0] = not first[0][0]
    second = qrmini.make(b"QR", "M", mask=0)
    assert second[0][0] is not first[0][0]


def test_performance_version_10_under_50ms():
    data = b"x" * 200  # 版本 10 / M 级
    assert qrmini.size_for(data, "M") == 10
    qrmini.make(data, "M")  # 预热各级缓存
    start = time.perf_counter()
    qrmini.make(data, "M", mask=3)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.05, f"版本 10 生成耗时 {elapsed * 1000:.1f} ms"


def test_performance_auto_mask_version_10_under_50ms():
    data = b"x" * 200
    start = time.perf_counter()
    qrmini.make(data, "M")
    elapsed = time.perf_counter() - start
    assert elapsed < 0.05, f"版本 10 自动掩码耗时 {elapsed * 1000:.1f} ms"


# --------------------------------------------------------------------------- #
# 图片输出
# --------------------------------------------------------------------------- #
def test_matrix_to_image_geometry_and_colors():
    from PIL import Image

    matrix = [[True, False], [False, True]]
    image = qrmini.matrix_to_image(matrix, scale=2, border=1, fg="#FF0000", bg=(0, 0, 255))
    assert isinstance(image, Image.Image)
    assert image.mode == "RGB"
    assert image.size == (8, 8)
    assert image.getpixel((0, 0)) == (0, 0, 255)  # 静区
    assert image.getpixel((2, 2)) == (255, 0, 0)  # 模块 (0,0) 深色
    assert image.getpixel((2, 4)) == (0, 0, 255)  # 模块 (0,1) 浅色
    assert image.getpixel((4, 2)) == (0, 0, 255)
    assert image.getpixel((4, 4)) == (255, 0, 0)


def test_make_image_matches_matrix_to_image():
    data = "https://example.com/HydroHUD"
    direct = qrmini.make_image(data, "Q", scale=3, border=4)
    manual = qrmini.matrix_to_image(qrmini.make(data, "Q"), scale=3, border=4)
    assert direct.size == manual.size
    assert direct.tobytes() == manual.tobytes()
    size = len(qrmini.make(data, "Q"))
    assert direct.size == ((size + 8) * 3, (size + 8) * 3)


def test_matrix_to_image_invalid_arguments():
    with pytest.raises(ValueError):
        qrmini.matrix_to_image([])  # 空矩阵
    with pytest.raises(ValueError):
        qrmini.matrix_to_image([[True, False], [True]])  # 参差矩阵
    with pytest.raises(ValueError):
        qrmini.matrix_to_image([[True]], scale=0)
    with pytest.raises(ValueError):
        qrmini.matrix_to_image([[True]], border=-1)
    with pytest.raises(ValueError):
        qrmini.matrix_to_image([[True]], fg="not-a-colour")
    with pytest.raises(TypeError):
        qrmini.matrix_to_image([[True]], scale=1.5)  # type: ignore[arg-type]


def test_constants_exposed():
    assert qrmini.ERROR_LEVELS == ("L", "M", "Q", "H")
    assert qrmini.MAX_VERSION == 20
    assert isinstance(qrmini.EC_CODEWORDS, dict)
    assert all(isinstance(key, tuple) and key[0] in range(1, 21) for key in qrmini.EC_CODEWORDS)
