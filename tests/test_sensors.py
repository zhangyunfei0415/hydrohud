"""``hydrohud.sensors`` 的纯软件测试。

约定：所有底层读取器都用 monkeypatch 注入固定值，不依赖真实 GPU / 共享内存 /
psutil 是否存在；唯一触碰真实系统的是末尾的"真实采样冒烟"，它只断言不抛异常、
耗时在预算内，以及本机内存必然是 0-100 的百分数。
"""

from __future__ import annotations

import math
import os
import struct
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hydrohud import sensors  # noqa: E402

#: 任务要求的 metric 全集。
EXPECTED_METRICS = (
    "cpu",
    "cpu_freq",
    "cpu_temp",
    "cpu_power",
    "ram",
    "ram_used",
    "ram_total",
    "gpu",
    "gpu_temp",
    "gpu_mem",
    "gpu_mem_used",
    "gpu_mem_total",
    "gpu_power",
    "gpu_fan",
    "gpu_clock",
    "disk",
    "disk_read",
    "disk_write",
    "net_down",
    "net_up",
    "uptime",
    "fan",
    "cpu_cores",
)

#: 固定采样结果，测试里注入到各个读取器。
FAKE_VALUES = {
    "cpu": 32.0,
    "cpu_freq": 3700.0,
    "cpu_temp": 46.0,
    "cpu_power": 65.5,
    "ram": 73.7,
    "ram_used": 34.7,
    "ram_total": 47.5,
    "gpu": 25.0,
    "gpu_temp": 44.0,
    "gpu_mem": 40.0,
    "gpu_mem_used": 6.4,
    "gpu_mem_total": 15.9,
    "gpu_power": 87.1,
    "gpu_fan": 34.0,
    "gpu_clock": 2542.0,
    "disk": 86.0,
    "disk_read": 1.5,
    "disk_write": 0.5,
    "net_down": 0.7,
    "net_up": 0.1,
    "uptime": 6.2,
    "fan": 1200.0,
    "cpu_cores": 24.0,
}


# --------------------------------------------------------------------------- #
# 元数据
# --------------------------------------------------------------------------- #
def test_metric_ids_match_required_set():
    assert tuple(sensors.METRIC_IDS) == EXPECTED_METRICS
    assert len(set(sensors.METRIC_IDS)) == len(sensors.METRIC_IDS)


def test_every_metric_has_complete_spec():
    assert set(sensors.METRIC_SPECS) == set(sensors.METRIC_IDS)
    for metric in sensors.METRIC_IDS:
        spec = sensors.METRIC_SPECS[metric]
        for key in ("label", "unit", "max", "digits", "suffix"):
            assert key in spec, f"{metric} 缺少 {key}"
        assert isinstance(spec["label"], str) and spec["label"], f"{metric} label 为空"
        assert isinstance(spec["unit"], str), f"{metric} unit 必须是字符串"
        assert isinstance(spec["suffix"], str), f"{metric} suffix 必须是字符串"
        assert float(spec["max"]) > 0.0, f"{metric} max 必须大于 0"
        assert int(spec["digits"]) >= 0, f"{metric} digits 不能为负"


# --------------------------------------------------------------------------- #
# 格式化
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("metric", "value", "expected"),
    [
        ("cpu", None, "—"),
        ("cpu", 32, "32%"),
        ("cpu", 32.4, "32%"),
        ("gpu_temp", 46.4, "46°C"),
        ("ram_used", 12.34, "12.3 GB"),
        ("gpu_mem_used", 6.358, "6.4 GB"),
        ("cpu_freq", 3700.0, "3700 MHz"),
        ("net_down", 0.6989, "0.7 Mbps"),
        ("cpu_cores", 24.0, "24 核"),
        ("unknown_metric", 12.34, "12.3"),
    ],
)
def test_format_value(metric, value, expected):
    assert sensors.format_value(metric, value) == expected


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_format_value_rejects_non_finite(bad):
    assert sensors.format_value("cpu", bad) == "—"


def test_format_value_dash_is_full_width_dash():
    assert sensors.format_value("gpu", None) == "—"
    assert sensors.format_value("gpu", None) != "-"


def test_format_metric_combines_label_and_value():
    assert sensors.format_metric("gpu_temp", 46.0) == "GPU 温度 46°C"
    assert sensors.format_metric("gpu_temp", None) == "GPU 温度 —"
    assert sensors.format_metric("cpu", 32.0) == "CPU 使用率 32%"


def test_metric_label_and_max():
    assert sensors.metric_label("gpu_temp") == "GPU 温度"
    assert sensors.metric_max("gpu_temp") == 100.0
    assert sensors.metric_max("ram_total") == 128.0
    assert sensors.metric_label("no_such_metric") == "no_such_metric"
    assert sensors.metric_max("no_such_metric") == 100.0


# --------------------------------------------------------------------------- #
# 注入替身
# --------------------------------------------------------------------------- #
class ReaderSpy:
    """把每个读取器替换成返回固定值的替身，并记录调用次数。"""

    def __init__(self, monkeypatch, values=None, overrides=None):
        self.calls: dict[str, int] = {}
        self.values = dict(FAKE_VALUES if values is None else values)
        self.overrides = dict(overrides or {})
        self._install(monkeypatch)

    def _install(self, monkeypatch):
        monkeypatch.setattr(sensors, "_read_cpu", self._wrap("cpu", self._cpu))
        monkeypatch.setattr(sensors, "_read_cpu_freq", self._wrap("cpu_freq", self._cpu_freq))
        monkeypatch.setattr(sensors, "_read_ram", self._wrap("ram", self._ram))
        monkeypatch.setattr(sensors, "_read_gpu", self._wrap("gpu", self._gpu))
        monkeypatch.setattr(sensors, "_read_disk", self._wrap("disk", self._disk))
        monkeypatch.setattr(sensors, "_read_net", self._wrap("net", self._net))
        monkeypatch.setattr(sensors, "_read_uptime", self._wrap("uptime", self._uptime))
        monkeypatch.setattr(sensors, "_read_cores", self._wrap("cores", self._cores))
        monkeypatch.setattr(sensors, "_read_thermal", self._wrap("thermal", self._thermal))

    def _wrap(self, name, function):
        def wrapper(state):
            self.calls[name] = self.calls.get(name, 0) + 1
            return function(state)

        return wrapper

    def total(self) -> int:
        return sum(self.calls.values())

    def _value(self, metric):
        return self.overrides.get(metric, self.values.get(metric))

    def _cpu(self, state):
        return self._value("cpu")

    def _cpu_freq(self, state):
        return self._value("cpu_freq")

    def _ram(self, state):
        return {
            "ram": self._value("ram"),
            "ram_used": self._value("ram_used"),
            "ram_total": self._value("ram_total"),
        }

    def _gpu(self, state):
        gpu_metrics = (
            "gpu",
            "gpu_temp",
            "gpu_mem",
            "gpu_mem_used",
            "gpu_mem_total",
            "gpu_power",
            "gpu_fan",
            "gpu_clock",
        )
        return {metric: self._value(metric) for metric in gpu_metrics}

    def _disk(self, state):
        return {
            "disk": self._value("disk"),
            "disk_read": self._value("disk_read"),
            "disk_write": self._value("disk_write"),
        }

    def _net(self, state):
        return {"net_down": self._value("net_down"), "net_up": self._value("net_up")}

    def _uptime(self, state):
        return self._value("uptime")

    def _cores(self, state):
        return self._value("cpu_cores")

    def _thermal(self, state):
        return {
            "cpu_temp": self._value("cpu_temp"),
            "cpu_power": self._value("cpu_power"),
            "fan": self._value("fan"),
        }


# --------------------------------------------------------------------------- #
# SensorHub 采样
# --------------------------------------------------------------------------- #
def test_sample_returns_all_metric_ids(monkeypatch):
    ReaderSpy(monkeypatch)
    hub = sensors.SensorHub(interval=0.0)
    values = hub.sample()
    assert set(values) == set(sensors.METRIC_IDS)
    for metric in sensors.METRIC_IDS:
        assert values[metric] == pytest.approx(FAKE_VALUES[metric])


def test_sample_is_cached_within_interval(monkeypatch):
    spy = ReaderSpy(monkeypatch)
    hub = sensors.SensorHub(interval=60.0)
    hub.sample()
    first = dict(spy.calls)
    hub.sample()
    hub.sample()
    assert spy.calls == first, "interval 内重复 sample 不应重新调用读取器"
    assert first["cpu"] == 1


def test_force_bypasses_cache(monkeypatch):
    spy = ReaderSpy(monkeypatch)
    hub = sensors.SensorHub(interval=60.0)
    hub.sample()
    hub.sample(force=True)
    assert spy.calls["cpu"] == 2
    assert spy.calls["gpu"] == 2


def test_zero_interval_samples_every_call(monkeypatch):
    spy = ReaderSpy(monkeypatch)
    hub = sensors.SensorHub(interval=0.0)
    for _ in range(3):
        hub.sample()
    assert spy.calls["cpu"] == 3
    assert spy.calls["thermal"] == 3


def test_cache_copies_are_independent(monkeypatch):
    """调用方修改返回值不能污染 hub 的缓存。"""
    ReaderSpy(monkeypatch)
    hub = sensors.SensorHub(interval=60.0)
    first = hub.sample()
    first["cpu"] = -1.0
    assert hub.sample()["cpu"] == pytest.approx(32.0)


def test_sample_survives_broken_reader(monkeypatch):
    """单个读取器抛异常只影响它负责的 metric，其它照常。"""

    def boom(state):
        raise RuntimeError("后端炸了")

    ReaderSpy(monkeypatch)
    monkeypatch.setattr(sensors, "_read_gpu", boom)
    hub = sensors.SensorHub(interval=0.0)
    values = hub.sample()
    assert set(values) == set(sensors.METRIC_IDS)
    assert values["gpu"] is None
    assert values["gpu_temp"] is None
    assert values["ram"] == pytest.approx(73.7)


def test_value_returns_single_metric(monkeypatch):
    ReaderSpy(monkeypatch)
    hub = sensors.SensorHub(interval=0.0)
    assert hub.value("cpu") == pytest.approx(32.0)
    assert hub.value("gpu_temp") == pytest.approx(44.0)


def test_history_accumulates_in_order(monkeypatch):
    spy = ReaderSpy(monkeypatch)
    hub = sensors.SensorHub(interval=0.0)
    samples = [10.0, 20.0, 30.0]
    for value in samples:
        spy.overrides["cpu"] = value
        hub.sample()
    assert hub.history("cpu") == pytest.approx(samples)


def test_history_respects_maxlen(monkeypatch):
    spy = ReaderSpy(monkeypatch)
    hub = sensors.SensorHub(interval=0.0, history=3)
    for value in (1.0, 2.0, 3.0, 4.0, 5.0):
        spy.overrides["cpu"] = value
        hub.sample()
    assert hub.history("cpu") == pytest.approx([3.0, 4.0, 5.0])


def test_history_skips_none_values(monkeypatch):
    spy = ReaderSpy(monkeypatch)
    hub = sensors.SensorHub(interval=0.0, history=10)
    for value in (5.0, None, 7.0):
        spy.overrides["cpu"] = value
        hub.sample()
    assert hub.history("cpu") == pytest.approx([5.0, 7.0])


def test_history_of_unknown_metric_is_empty(monkeypatch):
    ReaderSpy(monkeypatch)
    hub = sensors.SensorHub(interval=0.0)
    hub.sample()
    assert hub.history("no_such_metric") == []


def test_history_zero_size_stores_nothing(monkeypatch):
    ReaderSpy(monkeypatch)
    hub = sensors.SensorHub(interval=0.0, history=0)
    hub.sample()
    hub.sample()
    assert hub.history("cpu") == []


def test_describe_covers_every_metric(monkeypatch):
    ReaderSpy(
        monkeypatch,
        overrides={"cpu_temp": None, "fan": None, "cpu_power": None},
    )
    hub = sensors.SensorHub(interval=0.0)
    described = hub.describe()
    assert set(described) == set(sensors.METRIC_IDS)
    for metric, entry in described.items():
        assert entry["label"] == sensors.METRIC_SPECS[metric]["label"]
        assert entry["unit"] == sensors.METRIC_SPECS[metric]["unit"]
        assert isinstance(entry["source"], str) and entry["source"]
        assert isinstance(entry["available"], bool)
    assert described["gpu"]["available"] is True
    assert described["cpu_temp"]["available"] is False
    assert "nvml" in described["gpu"]["source"].lower()


def test_describe_samples_on_first_use(monkeypatch):
    spy = ReaderSpy(monkeypatch)
    hub = sensors.SensorHub(interval=60.0)
    hub.describe()
    assert spy.total() > 0


def test_none_metrics_do_not_break_describe(monkeypatch):
    """所有读取器都取不到值（Linux 无后端）时，describe 仍然完整。"""
    ReaderSpy(
        monkeypatch,
        values={metric: None for metric in sensors.METRIC_IDS},
    )
    hub = sensors.SensorHub(interval=0.0)
    described = hub.describe()
    assert all(entry["available"] is False for entry in described.values())
    assert all(entry["source"] for entry in described.values())


def test_close_is_idempotent(monkeypatch):
    ReaderSpy(monkeypatch)
    hub = sensors.SensorHub(interval=0.0)
    hub.sample()
    hub.close()
    hub.close()
    hub.sample()


def test_get_hub_is_process_singleton(monkeypatch):
    monkeypatch.setattr(sensors, "_hub", None)
    first = sensors.get_hub()
    second = sensors.get_hub()
    assert first is second
    assert isinstance(first, sensors.SensorHub)
    first.close()


def test_available_metrics_uses_singleton(monkeypatch):
    ReaderSpy(monkeypatch)
    monkeypatch.setattr(sensors, "_hub", sensors.SensorHub(interval=60.0))
    available = sensors.available_metrics()
    assert "cpu" in available
    assert "cpu_temp" in available
    assert "no_such_metric" not in available
    assert tuple(m for m in sensors.METRIC_IDS if m in available) == available


def test_available_metrics_omits_none(monkeypatch):
    ReaderSpy(monkeypatch, overrides={"cpu_temp": None, "gpu": None})
    monkeypatch.setattr(sensors, "_hub", sensors.SensorHub(interval=60.0))
    available = sensors.available_metrics()
    assert "cpu_temp" not in available
    assert "gpu" not in available
    assert "gpu_temp" in available


# --------------------------------------------------------------------------- #
# 共享内存解析（纯软件构造的缓冲区）
# --------------------------------------------------------------------------- #
def _ohm_name(text: str) -> bytes:
    return text.encode("utf-8").ljust(128, b"\x00")


def _build_ohm_buffer(hardware_name: str, sensors_list: list) -> bytes:
    """构造一个 LHM/OHM 共享内存镜像：sensors_list 为 (identifier, name, value)。"""
    body = b"".join(
        _ohm_name(identifier) + b"\x00" * (128 * 3) + struct.pack("<f", value) + b"\x00" * 8
        for identifier, value in sensors_list
    )
    hardware = _ohm_name(hardware_name) + b"\x00" * (128 * 3) + struct.pack("<I", len(sensors_list))
    header = b"OHWM" + struct.pack("<iI", 1, 1)
    return header + hardware + body


def test_parse_ohm_buffer_maps_cpu_metrics():
    buffer = _build_ohm_buffer(
        "/amdcpu/0",
        [
            ("/amdcpu/0/temperature/0", 61.5),
            ("/amdcpu/0/power/0", 88.0),
            ("/lpc/fan/0", 1200.0),
        ],
    )
    found = sensors._parse_ohm_buffer(buffer)
    assert found["cpu_temp"] == pytest.approx(61.5)
    assert found["cpu_power"] == pytest.approx(88.0)
    # fan/cpu 才是 CPU 风扇，lpc/fan 不算
    assert "fan" not in found


def test_parse_ohm_buffer_accepts_fan_cpu():
    buffer = _build_ohm_buffer("/lpc/0", [("/lpc/0/fan/cpu/0", 1450.0)])
    assert sensors._parse_ohm_buffer(buffer)["fan"] == pytest.approx(1450.0)


def test_parse_ohm_buffer_rejects_garbage():
    assert sensors._parse_ohm_buffer(b"") == {}
    assert sensors._parse_ohm_buffer(b"OHWM" + struct.pack("<iI", 1, 0)) == {}
    # 声称有 3 个硬件块但缓冲区不够：只返回已解析部分，不抛异常
    truncated = b"OHWM" + struct.pack("<iI", 1, 3)
    assert sensors._parse_ohm_buffer(truncated) == {}


_HWINFO_SENSOR_ELEMENT_SIZE = 392
_HWINFO_READING_ELEMENT_SIZE = 460


def _hwinfo_sensor(name: str) -> bytes:
    """构造 HWiNFO SM2 的一个 sensor 元素（type + 空位 + name[128] + 补齐）。"""
    blob = struct.pack("<2I", 0xF0000300, 0) + name.encode("ascii").ljust(128, b"\x00")
    assert _HWINFO_SENSOR_ELEMENT_SIZE >= len(blob)
    return blob.ljust(_HWINFO_SENSOR_ELEMENT_SIZE, b"\x00")


def _hwinfo_reading(
    reading_type: int,
    sensor_index: int,
    label: str,
    unit: str | bytes,
    value: float,
) -> bytes:
    """构造 HWiNFO SM2 的一条读数（type/sensor/id + label[128] + unit[16] + double）。

    单位按原始字节写入：中文系统上度符号是 GBK 双字节 ``b"\\xa1\\xe6"``，
    解析器只能靠 ``reading_type`` 判别类型，不能依赖单位串。
    """
    raw_unit = unit.encode("ascii") if isinstance(unit, str) else unit
    blob = (
        struct.pack("<3I", reading_type, sensor_index, 0x08000000 + reading_type)
        + label.encode("ascii").ljust(128, b"\x00")
        + b"\x00" * 128
        + raw_unit.ljust(16, b"\x00")
        + struct.pack("<d", value)
    )
    assert _HWINFO_READING_ELEMENT_SIZE >= len(blob)
    return blob.ljust(_HWINFO_READING_ELEMENT_SIZE, b"\x00")


def _hwinfo_buffer(sensors_blob: list[bytes], readings_blob: list[bytes]) -> bytes:
    """按实测布局拼出 SM2 缓冲区：magic + version/revision + int64 poll_time + 两个数组。"""
    sensor_offset = 44
    sensor_size = _HWINFO_SENSOR_ELEMENT_SIZE
    entry_offset = sensor_offset + len(sensors_blob) * sensor_size
    entry_size = _HWINFO_READING_ELEMENT_SIZE
    header = (
        b"HWiS"
        + struct.pack("<2I", 2, 1)
        + struct.pack("<q", 1_700_000_000)
        + struct.pack("<3I", sensor_offset, sensor_size, len(sensors_blob))
        + struct.pack("<3I", entry_offset, entry_size, len(readings_blob))
    )
    return header.ljust(44, b"\x00") + b"".join(sensors_blob) + b"".join(readings_blob)


def test_parse_hwinfo_buffer_reads_cpu_entries():
    cpu = _hwinfo_sensor("CPU [#0]: Test CPU")
    gpu = _hwinfo_sensor("dGPU [#2]: Test GPU")
    readings = [
        _hwinfo_reading(1, 0, "CPU Package", b"\xa1\xe6", 62.0),
        _hwinfo_reading(1, 0, "P-core 0", b"\xa1\xe6", 55.0),
        _hwinfo_reading(5, 0, "CPU Package Power", "W", 95.5),
        _hwinfo_reading(3, 0, "CPU", "RPM", 1300.0),
        _hwinfo_reading(3, 0, "System 1", "RPM", 900.0),
        _hwinfo_reading(6, 0, "Average Effective Clock", "MHz", 4200.0),
        _hwinfo_reading(1, 1, "GPU Temperature", b"\xa1\xe6", 45.0),
    ]
    found = sensors._parse_hwinfo_buffer(_hwinfo_buffer([cpu, gpu], readings))
    assert found["cpu_temp"] == pytest.approx(62.0)
    assert found["cpu_power"] == pytest.approx(95.5)
    assert found["fan"] == pytest.approx(1300.0)
    assert found["cpu_freq"] == pytest.approx(4200.0)
    assert "gpu_temp" not in found


def test_parse_hwinfo_buffer_falls_back_to_hottest_core():
    """没有 CPU Package 时取最热的核心；Distance to TjMAX 不能当成温度。"""
    cpu = _hwinfo_sensor("CPU [#0]: Test CPU")
    readings = [
        _hwinfo_reading(1, 0, "P-core 0", b"\xa1\xe6", 51.0),
        _hwinfo_reading(1, 0, "E-core 2", b"\xa1\xe6", 63.0),
        _hwinfo_reading(1, 0, "P-core 0 Distance to TjMAX", b"\xa1\xe6", 50.0),
    ]
    found = sensors._parse_hwinfo_buffer(_hwinfo_buffer([cpu], readings))
    assert found["cpu_temp"] == pytest.approx(63.0)


def test_parse_hwinfo_buffer_ignores_power_and_frequency_limits():
    """PL1/PL2 之类的配置值不能被当成实测功耗，核数时钟不能当成有效频率。"""
    cpu = _hwinfo_sensor("CPU [#0]: Test CPU")
    readings = [
        _hwinfo_reading(5, 0, "PL1 Power Limit (Static)", "W", 250.0),
        _hwinfo_reading(6, 0, "P-core 0 Clock", "MHz", 5600.0),
        _hwinfo_reading(6, 0, "P-core 1 Clock", "MHz", 4600.0),
    ]
    found = sensors._parse_hwinfo_buffer(_hwinfo_buffer([cpu], readings))
    assert "cpu_power" not in found
    assert found["cpu_freq"] == pytest.approx(5100.0)


def test_parse_hwinfo_buffer_uses_the_8_byte_poll_time():
    """头部 poll_time 是 int64：按 9 个 u32 从偏移 4 解析会整体错位一格。"""
    cpu = _hwinfo_sensor("CPU [#0]: Test CPU")
    buffer = _hwinfo_buffer([cpu], [_hwinfo_reading(1, 0, "CPU Package", b"\xa1\xe6", 60.0)])
    layout = sensors._hwinfo_layout(buffer)
    assert layout is not None
    assert layout.sensor_offset == 44
    assert layout.sensor_count == 1
    assert layout.entry_size == _HWINFO_READING_ELEMENT_SIZE
    assert layout.poll_time == 1_700_000_000
    assert sensors._hwinfo_bytes_needed(buffer) == len(buffer)
    assert sensors._parse_hwinfo_buffer(buffer)["cpu_temp"] == pytest.approx(60.0)


@pytest.mark.parametrize(
    "buffer",
    [
        b"",
        b"not-hwinfo-mapping",
        b"HWiS" + b"\x00" * 20,
        b"HWiS"
        + struct.pack("<2I", 2, 1)
        + struct.pack("<q", 1)
        + struct.pack("<6I", 0, 0, 99999, 0, 0, 0),
        # 数组互相重叠 / 尺寸过小：布局不自洽
        b"HWiS"
        + struct.pack("<2I", 2, 1)
        + struct.pack("<q", 1)
        + struct.pack("<3I", 44, 8, 2)
        + struct.pack("<3I", 44, 8, 2),
        # 声称的字节数超过上限
        b"HWiS"
        + struct.pack("<2I", 2, 1)
        + struct.pack("<q", 1)
        + struct.pack("<3I", 44, 392, 512)
        + struct.pack("<3I", 44 + 512 * 392, 460, 8192),
    ],
)
def test_parse_hwinfo_buffer_rejects_unexpected_layout(buffer):
    assert sensors._parse_hwinfo_buffer(buffer) == {}


def test_read_thermal_keeps_the_effective_clock_for_cpu_freq(monkeypatch):
    """共享内存里的有效频率要存进 state.shared，供紧随其后的 cpu_freq 直接用。"""
    monkeypatch.setattr(sensors, "_read_lhm", lambda: {})
    monkeypatch.setattr(sensors, "_read_hwinfo", lambda: {"cpu_temp": 55.0, "cpu_freq": 4210.0})
    monkeypatch.setattr(sensors, "_psutil", lambda: None)
    state = sensors._SampleState()
    sensors._read_thermal(state)
    assert state.shared["cpu_freq"] == pytest.approx(4210.0)
    assert "有效频率" in state.sources["cpu_freq"]
    assert sensors._read_cpu_freq(state) == pytest.approx(4210.0)


def test_read_cpu_freq_labels_psutil_as_the_base_clock(monkeypatch):
    """没有共享内存时退回 psutil，必须标注这是基准频率而不是实时频率。"""

    class _Frequency:
        current = 3700.0

    class _FakePsutil:
        @staticmethod
        def cpu_freq():
            return _Frequency()

    monkeypatch.setattr(sensors, "_psutil", lambda: _FakePsutil())
    state = sensors._SampleState()
    assert sensors._read_cpu_freq(state) == pytest.approx(3700.0)
    assert "基准频率" in state.sources["cpu_freq"]


def test_read_thermal_prefers_lhm_for_temperature_but_hwinfo_for_frequency(monkeypatch):
    """LHM 有温度时温度用它，但有效频率仍可由 HWiNFO 补上。"""
    monkeypatch.setattr(sensors, "_read_lhm", lambda: {"cpu_temp": 60.0})
    monkeypatch.setattr(sensors, "_read_hwinfo", lambda: {"cpu_temp": 99.0, "cpu_freq": 3900.0})
    state = sensors._SampleState()
    values = sensors._read_thermal(state)
    assert values["cpu_temp"] == pytest.approx(60.0)
    assert state.shared["cpu_freq"] == pytest.approx(3900.0)

    monkeypatch.setattr(sensors, "_read_lhm", lambda: {})
    monkeypatch.setattr(sensors, "_read_hwinfo", lambda: {})
    monkeypatch.setattr(sensors, "_acpi_thermal_value", lambda: (None, sensors._NO_SHARED_MEMORY))
    values = sensors._read_thermal(sensors._SampleState())
    assert values == {"cpu_temp": None, "cpu_power": None, "fan": None}


def test_read_thermal_prefers_lhm_and_fills_from_hwinfo(monkeypatch):
    monkeypatch.setattr(sensors, "_read_lhm", lambda: {"cpu_temp": 60.0})
    monkeypatch.setattr(sensors, "_read_hwinfo", lambda: {"cpu_temp": 99.0, "fan": 1200.0})
    monkeypatch.setattr(sensors, "_acpi_thermal_value", lambda: (77.0, "acpi"))
    values = sensors._read_thermal(sensors._SampleState())
    assert values["cpu_temp"] == pytest.approx(60.0)
    assert values["fan"] == pytest.approx(1200.0)


def test_read_thermal_falls_back_to_acpi_when_shared_memory_is_missing(monkeypatch):
    monkeypatch.setattr(sensors, "_read_lhm", lambda: {})
    monkeypatch.setattr(sensors, "_read_hwinfo", lambda: {})
    monkeypatch.setattr(sensors, "_acpi_thermal_value", lambda: (31.5, "ACPI 热区"))
    state = sensors._SampleState()
    values = sensors._read_thermal(state)
    assert values["cpu_temp"] == pytest.approx(31.5)
    assert state.sources["cpu_temp"] == "ACPI 热区"
    assert state.sources["cpu_power"] == sensors._NO_SHARED_MEMORY


def test_read_thermal_survives_parser_exception(monkeypatch):
    def boom():
        raise RuntimeError("解析炸了")

    monkeypatch.setattr(sensors, "_read_lhm", boom)
    monkeypatch.setattr(sensors, "_read_hwinfo", boom)
    monkeypatch.setattr(sensors, "_acpi_thermal_value", lambda: (None, sensors._NO_SHARED_MEMORY))
    values = sensors._read_thermal(sensors._SampleState())
    assert values == {"cpu_temp": None, "cpu_power": None, "fan": None}


class _CompletedProcess:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def test_acpi_reader_converts_millikelvin_and_keeps_the_hottest_zone(monkeypatch):
    monkeypatch.setattr(sensors, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        sensors.subprocess,
        "run",
        lambda *args, **kwargs: _CompletedProcess("2900\n3010\n\n"),
    )
    assert sensors._acpi_thermal_celsius() == pytest.approx(3010 / 10.0 - 273.15)


@pytest.mark.parametrize(
    "stdout",
    ["", "not-a-number\n", "0\n", "99000\n"],
)
def test_acpi_reader_rejects_implausible_readings(monkeypatch, stdout):
    monkeypatch.setattr(sensors, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        sensors.subprocess, "run", lambda *args, **kwargs: _CompletedProcess(stdout)
    )
    assert sensors._acpi_thermal_celsius() is None


def test_acpi_reader_hides_the_child_console(monkeypatch):
    """以 pythonw.exe 启动时没有控制台，子进程必须自带 CREATE_NO_WINDOW 才不弹黑窗。"""
    monkeypatch.setattr(sensors, "_IS_WINDOWS", True)
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return _CompletedProcess("3000\n")

    monkeypatch.setattr(sensors.subprocess, "run", fake_run)
    assert sensors._acpi_thermal_celsius() == pytest.approx(3000 / 10.0 - 273.15)
    assert captured["creationflags"] == sensors._CREATE_NO_WINDOW
    assert captured["stdin"] is sensors.subprocess.DEVNULL


def test_acpi_reader_survives_a_missing_powershell(monkeypatch):
    monkeypatch.setattr(sensors, "_IS_WINDOWS", True)

    def boom(*args, **kwargs):
        raise OSError("powershell 不在 PATH 里")

    monkeypatch.setattr(sensors.subprocess, "run", boom)
    assert sensors._acpi_thermal_celsius() is None


def test_acpi_reader_is_windows_only(monkeypatch):
    monkeypatch.setattr(sensors, "_IS_WINDOWS", False)
    assert sensors._acpi_thermal_celsius() is None


# --------------------------------------------------------------------------- #
# 真实采样冒烟（本机必然可读 RAM；不断言 gpu / cpu_temp 有值）
# --------------------------------------------------------------------------- #
def test_real_sample_smoke():
    hub = sensors.SensorHub(interval=0.0)
    started = time.perf_counter()
    try:
        values = hub.sample()
        elapsed = time.perf_counter() - started
    finally:
        hub.close()

    assert set(values) == set(sensors.METRIC_IDS)
    assert elapsed < 0.2, f"单次真实采样耗时 {elapsed * 1000:.1f} ms，超过 200 ms 预算"

    # 本机有 psutil 或处于 Windows 时 RAM 必然可读；其它平台无后端则为 None。
    if sensors._psutil() is not None or sensors._IS_WINDOWS:
        assert values["ram"] is not None
        assert 0.0 <= values["ram"] <= 100.0

    for metric, value in values.items():
        if value is not None:
            assert math.isfinite(value), f"{metric} 采到了非有限值"


def test_real_sample_history_grows():
    hub = sensors.SensorHub(interval=0.0, history=2)
    try:
        for _ in range(4):
            hub.sample()
    finally:
        hub.close()
    history = hub.history("ram")
    assert len(history) <= 2
