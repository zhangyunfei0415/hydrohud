"""Pure-Python QR Code encoder (byte mode, versions 1-20).

Only the standard library is used to build the module matrix; :func:`matrix_to_image`
and :func:`make_image` import Pillow lazily, so importing this module never requires an
image library.

Public surface:

* :func:`make` / :func:`size_for` - encode text or bytes into a boolean matrix
  (``True`` = dark module, row-major, no quiet zone).
* :func:`matrix_to_image` / :func:`make_image` - rasterise a matrix with Pillow.

The encoder follows ISO/IEC 18004: byte mode only (``str`` input is UTF-8 encoded),
error correction levels L/M/Q/H, versions 1-20, Reed-Solomon over GF(256), all eight
mask patterns scored with the four standard penalty rules, and BCH-protected format
and version information.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

__all__ = [
    "EC_BLOCK_TABLE",
    "EC_CODEWORDS",
    "ERROR_LEVELS",
    "MAX_VERSION",
    "MIN_VERSION",
    "make",
    "make_image",
    "matrix_to_image",
    "size_for",
]

ERROR_LEVELS = ("L", "M", "Q", "H")
MAX_VERSION = 20
MIN_VERSION = 1

#: Format-information bits for each error correction level (ISO/IEC 18004 table 12).
_ERROR_LEVEL_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}

_FORMAT_POLY = 0x537
_FORMAT_MASK = 0x5412
_VERSION_POLY = 0x1F25

#: Reed-Solomon block layout per (version, level): one or two groups of
#: ``(block count, total codewords per block, data codewords per block)``.
EC_BLOCK_TABLE: dict[tuple[int, str], tuple[tuple[int, int, int], ...]] = {}

#: EC codewords per block per (version, level), from the standard table.
EC_CODEWORDS: dict[tuple[int, str], int] = {}

_BLOCK_LAYOUT: tuple[tuple[int, ...], ...] = (
    # version 1: L, M, Q, H
    (1, 26, 19),
    (1, 26, 16),
    (1, 26, 13),
    (1, 26, 9),
    # version 2
    (1, 44, 34),
    (1, 44, 28),
    (1, 44, 22),
    (1, 44, 16),
    # version 3
    (1, 70, 55),
    (1, 70, 44),
    (2, 35, 17),
    (2, 35, 13),
    # version 4
    (1, 100, 80),
    (2, 50, 32),
    (2, 50, 24),
    (4, 25, 9),
    # version 5
    (1, 134, 108),
    (2, 67, 43),
    (2, 33, 15, 2, 34, 16),
    (2, 33, 11, 2, 34, 12),
    # version 6
    (2, 86, 68),
    (4, 43, 27),
    (4, 43, 19),
    (4, 43, 15),
    # version 7
    (2, 98, 78),
    (4, 49, 31),
    (2, 32, 14, 4, 33, 15),
    (4, 39, 13, 1, 40, 14),
    # version 8
    (2, 121, 97),
    (2, 60, 38, 2, 61, 39),
    (4, 40, 18, 2, 41, 19),
    (4, 40, 14, 2, 41, 15),
    # version 9
    (2, 146, 116),
    (3, 58, 36, 2, 59, 37),
    (4, 36, 16, 4, 37, 17),
    (4, 36, 12, 4, 37, 13),
    # version 10
    (2, 86, 68, 2, 87, 69),
    (4, 69, 43, 1, 70, 44),
    (6, 43, 19, 2, 44, 20),
    (6, 43, 15, 2, 44, 16),
    # version 11
    (4, 101, 81),
    (1, 80, 50, 4, 81, 51),
    (4, 50, 22, 4, 51, 23),
    (3, 36, 12, 8, 37, 13),
    # version 12
    (2, 116, 92, 2, 117, 93),
    (6, 58, 36, 2, 59, 37),
    (4, 46, 20, 6, 47, 21),
    (7, 42, 14, 4, 43, 15),
    # version 13
    (4, 133, 107),
    (8, 59, 37, 1, 60, 38),
    (8, 44, 20, 4, 45, 21),
    (12, 33, 11, 4, 34, 12),
    # version 14
    (3, 145, 115, 1, 146, 116),
    (4, 64, 40, 5, 65, 41),
    (11, 36, 16, 5, 37, 17),
    (11, 36, 12, 5, 37, 13),
    # version 15
    (5, 109, 87, 1, 110, 88),
    (5, 65, 41, 5, 66, 42),
    (5, 54, 24, 7, 55, 25),
    (11, 36, 12, 7, 37, 13),
    # version 16
    (5, 122, 98, 1, 123, 99),
    (7, 73, 45, 3, 74, 46),
    (15, 43, 19, 2, 44, 20),
    (3, 45, 15, 13, 46, 16),
    # version 17
    (1, 135, 107, 5, 136, 108),
    (10, 74, 46, 1, 75, 47),
    (1, 50, 22, 15, 51, 23),
    (2, 42, 14, 17, 43, 15),
    # version 18
    (5, 150, 120, 1, 151, 121),
    (9, 69, 43, 4, 70, 44),
    (17, 50, 22, 1, 51, 23),
    (2, 42, 14, 19, 43, 15),
    # version 19
    (3, 141, 113, 4, 142, 114),
    (3, 70, 44, 11, 71, 45),
    (17, 47, 21, 4, 48, 22),
    (9, 39, 13, 16, 40, 14),
    # version 20
    (3, 135, 107, 5, 136, 108),
    (3, 67, 41, 13, 68, 42),
    (15, 54, 24, 5, 55, 25),
    (15, 43, 15, 10, 44, 16),
)

for _version in range(MIN_VERSION, MAX_VERSION + 1):
    for _index, _level in enumerate(ERROR_LEVELS):
        _layout = _BLOCK_LAYOUT[(_version - 1) * 4 + _index]
        _groups = tuple(
            (_layout[i], _layout[i + 1], _layout[i + 2]) for i in range(0, len(_layout), 3)
        )
        EC_BLOCK_TABLE[(_version, _level)] = _groups
        EC_CODEWORDS[(_version, _level)] = _groups[0][1] - _groups[0][2]

del _version, _index, _level, _layout, _groups

#: Alignment pattern centre coordinates, indexed by version (index 0 is unused).
_ALIGNMENT_POSITIONS: tuple[tuple[int, ...], ...] = (
    (),
    (),
    (6, 18),
    (6, 22),
    (6, 26),
    (6, 30),
    (6, 34),
    (6, 22, 38),
    (6, 24, 42),
    (6, 26, 46),
    (6, 28, 50),
    (6, 30, 54),
    (6, 32, 58),
    (6, 34, 62),
    (6, 26, 46, 66),
    (6, 26, 48, 70),
    (6, 26, 50, 74),
    (6, 30, 54, 78),
    (6, 30, 56, 82),
    (6, 30, 58, 86),
    (6, 34, 62, 90),
)

# --------------------------------------------------------------------------- #
# Galois field GF(256) with the QR primitive polynomial x^8 + x^4 + x^3 + x^2 + 1
# --------------------------------------------------------------------------- #

_EXP = [0] * 512
_LOG = [0] * 256
_value = 1
for _power in range(255):
    _EXP[_power] = _value
    _LOG[_value] = _power
    _value <<= 1
    if _value & 0x100:
        _value ^= 0x11D
for _power in range(255, 512):
    _EXP[_power] = _EXP[_power - 255]
del _value, _power


def _gf_mul(left: int, right: int) -> int:
    if left == 0 or right == 0:
        return 0
    return _EXP[_LOG[left] + _LOG[right]]


_RS_DIVISORS: dict[int, tuple[int, ...]] = {}


def _rs_divisor(degree: int) -> tuple[int, ...]:
    """Return the Reed-Solomon generator polynomial for ``degree`` EC codewords."""
    cached = _RS_DIVISORS.get(degree)
    if cached is not None:
        return cached
    result = [0] * degree
    result[-1] = 1
    root = 1
    for _ in range(degree):
        for index in range(degree):
            result[index] = _gf_mul(result[index], root)
            if index + 1 < degree:
                result[index] ^= result[index + 1]
        root = _gf_mul(root, 2)
    divisor = tuple(result)
    _RS_DIVISORS[degree] = divisor
    return divisor


def _rs_remainder(data: Sequence[int], divisor: Sequence[int]) -> list[int]:
    """Return ``data * x^len(divisor) mod divisor`` as EC codewords."""
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        if factor:
            for index, coefficient in enumerate(divisor):
                result[index] ^= _gf_mul(coefficient, factor)
    return result


def _bch_remainder(value: int, generator: int, degree: int) -> int:
    """Return the BCH check bits of ``value`` for the given generator polynomial."""
    remainder = value << degree
    while remainder.bit_length() > degree:
        remainder ^= generator << (remainder.bit_length() - generator.bit_length())
    return remainder


# --------------------------------------------------------------------------- #
# Data encoding
# --------------------------------------------------------------------------- #


class _BitBuffer:
    """Append-only big-endian bit buffer used by the byte-mode data encoder."""

    __slots__ = ("data", "length")

    def __init__(self) -> None:
        self.data: list[int] = []
        self.length = 0

    def put(self, value: int, length: int) -> None:
        for shift in range(length - 1, -1, -1):
            self.put_bit((value >> shift) & 1)

    def put_bit(self, bit: int) -> None:
        index = self.length // 8
        if len(self.data) <= index:
            self.data.append(0)
        if bit:
            self.data[index] |= 0x80 >> (self.length % 8)
        self.length += 1

    def to_bytes(self) -> bytes:
        return bytes(self.data)


def _check_error(error: str) -> str:
    level = str(error).strip().upper()
    if level not in _ERROR_LEVEL_BITS:
        raise ValueError("纠错等级必须是 L、M、Q、H 之一：" + repr(error))
    return level


def _payload_bytes(data: str | bytes | bytearray) -> bytes:
    if isinstance(data, str):
        payload = data.encode("utf-8")
    elif isinstance(data, bytes):
        payload = data
    elif isinstance(data, bytearray):
        payload = bytes(data)
    else:
        raise TypeError("二维码数据必须是 str 或 bytes")
    if not payload:
        raise ValueError("二维码数据不能为空")
    return payload


def _data_codewords(version: int, level: str) -> int:
    return sum(count * data_count for count, _, data_count in EC_BLOCK_TABLE[(version, level)])


def _count_bits(version: int) -> int:
    return 8 if version <= 9 else 16


def _max_payload_length(version: int, level: str) -> int:
    available = _data_codewords(version, level) * 8 - 4 - _count_bits(version)
    return available // 8


def _best_version(payload_length: int, level: str) -> int:
    for version in range(MIN_VERSION, MAX_VERSION + 1):
        if payload_length <= _max_payload_length(version, level):
            return version
    raise ValueError(
        f"数据太长：{payload_length} 字节超出第 {MAX_VERSION} 版（纠错等级 {level}）的容量"
    )


def _check_version(version: int) -> int:
    if not isinstance(version, int) or isinstance(version, bool):
        raise TypeError("版本号必须是整数")
    if not MIN_VERSION <= version <= MAX_VERSION:
        raise ValueError(f"版本号必须在 {MIN_VERSION}-{MAX_VERSION} 之间：" + repr(version))
    return version


def _check_mask(mask: int) -> int:
    if not isinstance(mask, int) or isinstance(mask, bool):
        raise TypeError("掩码编号必须是整数")
    if not 0 <= mask <= 7:
        raise ValueError("掩码编号必须在 0-7 之间：" + repr(mask))
    return mask


def _encode_data(payload: bytes, version: int, level: str) -> bytes:
    capacity = _data_codewords(version, level) * 8
    count_bits = _count_bits(version)
    needed = 4 + count_bits + 8 * len(payload)
    if len(payload) >= 1 << count_bits:
        raise ValueError("数据太长：无法放入字符计数域")
    if needed > capacity:
        raise ValueError(f"数据太长：{len(payload)} 字节放不进第 {version} 版（纠错等级 {level}）")

    buffer = _BitBuffer()
    buffer.put(0b0100, 4)  # byte mode indicator
    buffer.put(len(payload), count_bits)
    for byte in payload:
        buffer.put(byte, 8)

    buffer.put(0, min(4, capacity - buffer.length))
    if buffer.length % 8:
        buffer.put(0, 8 - buffer.length % 8)
    pad = 0
    while buffer.length < capacity:
        buffer.put(0xEC if pad % 2 == 0 else 0x11, 8)
        pad += 1

    return _interleave(buffer.to_bytes(), version, level)


def _interleave(data: bytes, version: int, level: str) -> bytes:
    blocks: list[tuple[int, int]] = []
    for count, total, data_count in EC_BLOCK_TABLE[(version, level)]:
        blocks.extend([(data_count, total - data_count)] * count)
    ec_length = blocks[0][1]
    divisor = _rs_divisor(ec_length)

    data_blocks: list[bytes] = []
    ec_blocks: list[list[int]] = []
    offset = 0
    for data_count, _ in blocks:
        chunk = data[offset : offset + data_count]
        offset += data_count
        data_blocks.append(chunk)
        ec_blocks.append(_rs_remainder(chunk, divisor))

    out = bytearray()
    longest = max(len(block) for block in data_blocks)
    for index in range(longest):
        for block in data_blocks:
            if index < len(block):
                out.append(block[index])
    for index in range(ec_length):
        for block in ec_blocks:
            out.append(block[index])
    return bytes(out)


# --------------------------------------------------------------------------- #
# Module matrix construction
# --------------------------------------------------------------------------- #


def _draw_finder_patterns(modules: list[list[bool]], function: list[list[bool]], size: int) -> None:
    for row, col in ((0, 0), (0, size - 7), (size - 7, 0)):
        for delta_row in range(-1, 8):
            for delta_col in range(-1, 8):
                target_row = row + delta_row
                target_col = col + delta_col
                if 0 <= target_row < size and 0 <= target_col < size:
                    distance = max(abs(delta_row - 3), abs(delta_col - 3))
                    modules[target_row][target_col] = distance in (0, 1, 3)
                    function[target_row][target_col] = True


def _draw_alignment_patterns(
    modules: list[list[bool]], function: list[list[bool]], version: int, size: int
) -> None:
    positions = _ALIGNMENT_POSITIONS[version]
    for row in positions:
        for col in positions:
            if function[row][col]:
                continue  # centre already occupied by a finder pattern
            for delta_row in range(-2, 3):
                for delta_col in range(-2, 3):
                    dark = max(abs(delta_row), abs(delta_col)) != 1
                    modules[row + delta_row][col + delta_col] = dark
                    function[row + delta_row][col + delta_col] = True


def _draw_timing_patterns(modules: list[list[bool]], function: list[list[bool]], size: int) -> None:
    for index in range(8, size - 8):
        if not function[index][6]:
            modules[index][6] = index % 2 == 0
            function[index][6] = True
        if not function[6][index]:
            modules[6][index] = index % 2 == 0
            function[6][index] = True


def _draw_version_bits(
    modules: list[list[bool]], function: list[list[bool]], version: int, size: int
) -> None:
    if version < 7:
        return
    bits = (version << 12) | _bch_remainder(version, _VERSION_POLY, 12)
    for index in range(18):
        dark = ((bits >> index) & 1) == 1
        anchor_row = size - 11 + index % 3
        anchor_col = index // 3
        modules[anchor_col][anchor_row] = dark
        function[anchor_col][anchor_row] = True
        modules[anchor_row][anchor_col] = dark
        function[anchor_row][anchor_col] = True


def _format_cells(size: int) -> list[tuple[int, int, int | None]]:
    """Return ``(row, col, bit index)`` for every format-information module.

    A bit index of ``None`` marks the dark module, which is always dark.
    """
    cells: list[tuple[int, int, int | None]] = [(index, 8, index) for index in range(6)]
    cells += [(7, 8, 6), (8, 8, 7), (8, 7, 8)]
    cells += [(8, 14 - index, index) for index in range(9, 15)]
    cells += [(8, size - 1 - index, index) for index in range(8)]
    cells += [(size - 15 + index, 8, index) for index in range(8, 15)]
    cells.append((size - 8, 8, None))
    return cells


def _draw_format_bits(modules: list[list[bool]], level: str, mask: int, size: int) -> None:
    data = (_ERROR_LEVEL_BITS[level] << 3) | mask
    bits = ((data << 10) | _bch_remainder(data, _FORMAT_POLY, 10)) ^ _FORMAT_MASK
    for row, col, index in _format_cells(size):
        modules[row][col] = True if index is None else ((bits >> index) & 1) == 1


_MASKS: tuple[Callable[[int, int], bool], ...] = (
    lambda row, col: (row + col) % 2 == 0,
    lambda row, col: row % 2 == 0,
    lambda row, col: col % 3 == 0,
    lambda row, col: (row + col) % 3 == 0,
    lambda row, col: (row // 2 + col // 3) % 2 == 0,
    lambda row, col: (row * col) % 2 + (row * col) % 3 == 0,
    lambda row, col: ((row * col) % 2 + (row * col) % 3) % 2 == 0,
    lambda row, col: ((row * col) % 3 + (row + col) % 2) % 2 == 0,
)


def _place_data(
    modules: list[list[bool]],
    function: list[list[bool]],
    codewords: bytes,
    size: int,
) -> None:
    direction = -1
    row = size - 1
    bit_index = 7
    byte_index = 0
    for col in range(size - 1, 0, -2):
        if col <= 6:
            col -= 1
        columns = (col, col - 1)
        while True:
            for target_col in columns:
                if not function[row][target_col]:
                    if byte_index < len(codewords):
                        dark = ((codewords[byte_index] >> bit_index) & 1) == 1
                    else:
                        dark = False
                    modules[row][target_col] = dark
                    bit_index -= 1
                    if bit_index == -1:
                        byte_index += 1
                        bit_index = 7
            row += direction
            if row < 0 or row >= size:
                row -= direction
                direction = -direction
                break


def _masked_copy(
    modules: list[list[bool]], function: list[list[bool]], mask: int
) -> list[list[bool]]:
    size = len(modules)
    mask_func = _MASKS[mask]
    result: list[list[bool]] = []
    for row in range(size):
        source = modules[row]
        fn = function[row]
        out_row = list(source)
        for col in range(size):
            if not fn[col] and mask_func(row, col):
                out_row[col] = not out_row[col]
        result.append(out_row)
    return result


_PENALTY_PATTERN_A = (True, False, True, True, True, False, True, False, False, False, False)
_PENALTY_PATTERN_B = (False, False, False, False, True, False, True, True, True, False, True)


def _runs_penalty(line: Sequence[bool]) -> int:
    score = 0
    run = 1
    previous = line[0]
    for value in line[1:]:
        if value == previous:
            run += 1
        else:
            if run >= 5:
                score += run - 2
            previous = value
            run = 1
    if run >= 5:
        score += run - 2
    return score


def _finder_penalty(line: Sequence[bool]) -> int:
    score = 0
    for start in range(len(line) - 10):
        window = tuple(line[start : start + 11])
        if window == _PENALTY_PATTERN_A or window == _PENALTY_PATTERN_B:
            score += 40
    return score


def _penalty(modules: list[list[bool]]) -> int:
    size = len(modules)
    score = 0

    for row in modules:
        score += _runs_penalty(row) + _finder_penalty(row)
    for col in range(size):
        line = [modules[row][col] for row in range(size)]
        score += _runs_penalty(line) + _finder_penalty(line)

    for row in range(size - 1):
        upper = modules[row]
        lower = modules[row + 1]
        for col in range(size - 1):
            value = upper[col]
            if value == upper[col + 1] == lower[col] == lower[col + 1]:
                score += 3

    dark = sum(sum(1 for value in row if value) for row in modules)
    score += 10 * (abs(100 * dark - 50 * size * size) // (5 * size * size))
    return score


def _build_matrix(codewords: bytes, version: int, level: str, mask: int | None) -> list[list[bool]]:
    size = version * 4 + 17
    modules = [[False] * size for _ in range(size)]
    function = [[False] * size for _ in range(size)]

    _draw_finder_patterns(modules, function, size)
    _draw_alignment_patterns(modules, function, version, size)
    _draw_timing_patterns(modules, function, size)
    _draw_version_bits(modules, function, version, size)
    for row, col, _index in _format_cells(size):
        function[row][col] = True
    _place_data(modules, function, codewords, size)

    if mask is not None:
        result = _masked_copy(modules, function, mask)
        _draw_format_bits(result, level, mask, size)
        return result

    best: list[list[bool]] | None = None
    best_score = -1
    for candidate in range(8):
        trial = _masked_copy(modules, function, candidate)
        _draw_format_bits(trial, level, candidate, size)
        score = _penalty(trial)
        if best is None or score < best_score:
            best = trial
            best_score = score
    assert best is not None
    return best


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def make(
    data: str | bytes,
    error: str = "M",
    version: int | None = None,
    mask: int | None = None,
) -> list[list[bool]]:
    """Encode ``data`` into a QR Code module matrix (``True`` = dark).

    Args:
        data: Text (encoded as UTF-8) or raw bytes. Empty input raises ``ValueError``.
        error: Error correction level, one of ``"L"``, ``"M"``, ``"Q"``, ``"H"``
            (case-insensitive).
        version: Explicit version 1-20, or ``None`` to pick the smallest one that fits.
            An explicit version that is too small raises ``ValueError``.
        mask: Forced mask pattern 0-7, or ``None`` to score all eight and pick the best.

    Returns:
        Square row-major matrix of ``4 * version + 17`` booleans, without quiet zone.
    """
    level = _check_error(error)
    payload = _payload_bytes(data)
    forced_mask = None if mask is None else _check_mask(mask)
    if version is None:
        chosen = _best_version(len(payload), level)
    else:
        chosen = _check_version(version)
        if len(payload) > _max_payload_length(chosen, level):
            raise ValueError(
                f"数据太长：{len(payload)} 字节放不进第 {chosen} 版（纠错等级 {level}）"
            )
    return _build_matrix(_encode_data(payload, chosen, level), chosen, level, forced_mask)


def size_for(data: str | bytes, error: str = "M") -> int:
    """Return the smallest version (1-20) that can hold ``data`` at ``error``."""
    level = _check_error(error)
    payload = _payload_bytes(data)
    return _best_version(len(payload), level)


def _check_scale(scale: int) -> int:
    if not isinstance(scale, int) or isinstance(scale, bool):
        raise TypeError("scale 必须是整数")
    if scale < 1:
        raise ValueError("scale 必须大于等于 1")
    return scale


def _check_border(border: int) -> int:
    if not isinstance(border, int) or isinstance(border, bool):
        raise TypeError("border 必须是整数")
    if border < 0:
        raise ValueError("border 不能为负数")
    return border


def _parse_color(
    color: str | tuple[int, int, int] | tuple[int, int, int, int],
) -> tuple[int, int, int]:
    from PIL import ImageColor

    if isinstance(color, str):
        try:
            return ImageColor.getrgb(color)[:3]
        except ValueError:
            raise ValueError("无效颜色：" + repr(color)) from None
    if isinstance(color, (tuple, list)) and len(color) in (3, 4):
        values = [int(value) for value in color[:3]]
        if all(0 <= value <= 255 for value in values):
            return values[0], values[1], values[2]
    raise ValueError("无效颜色：" + repr(color))


def matrix_to_image(
    matrix: Sequence[Sequence[bool]],
    scale: int = 1,
    border: int = 2,
    fg: str | tuple[int, int, int] = "#000000",
    bg: str | tuple[int, int, int] = "#FFFFFF",
):
    """Rasterise a module matrix into a Pillow ``RGB`` image.

    Args:
        matrix: Square row-major matrix where ``True`` marks a dark module.
        scale: Integer pixels per module (``>= 1``).
        border: Quiet zone width in modules (``>= 0``).
        fg: Dark module colour, ``"#RRGGBB"`` or an ``(r, g, b)`` tuple.
        bg: Light module / quiet zone colour.

    Returns:
        A ``PIL.Image.Image`` of size ``(len(matrix) + 2 * border) * scale``.

    Raises:
        ValueError: If the matrix is empty, ragged, or the arguments are invalid.
    """
    from PIL import Image, ImageDraw, ImageOps

    rows = [[bool(value) for value in row] for row in matrix]
    size = len(rows)
    if size == 0 or any(len(row) != size for row in rows):
        raise ValueError("二维码矩阵必须是非空方阵")
    scale = _check_scale(scale)
    border = _check_border(border)
    foreground = _parse_color(fg)
    background = _parse_color(bg)

    span = size + 2 * border
    bitmap = Image.new("L", (span, span), 255)
    draw = ImageDraw.Draw(bitmap)
    for row_index, row in enumerate(rows):
        for col_index, dark in enumerate(row):
            if dark:
                draw.point((col_index + border, row_index + border), fill=0)
    scaled = bitmap.resize((span * scale, span * scale), Image.NEAREST)
    return ImageOps.colorize(scaled, black=foreground, white=background).convert("RGB")


def make_image(
    data: str | bytes,
    error: str = "M",
    scale: int = 1,
    border: int = 2,
    fg: str | tuple[int, int, int] = "#000000",
    bg: str | tuple[int, int, int] = "#FFFFFF",
):
    """Encode ``data`` and rasterise it in one step (see :func:`make`)."""
    return matrix_to_image(make(data, error=error), scale=scale, border=border, fg=fg, bg=bg)
