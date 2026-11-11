"""系统状态采集中心：CPU / GPU / 内存 / 磁盘 / 网络 / 温度的统一定义与采样。

本模块为渲染层提供统一的系统状态数据源，不依赖 GUI 或 USB 设备：

- 全部 metric 见 :data:`METRIC_IDS`，展示元数据见 :data:`METRIC_SPECS`。
  :func:`format_value` / :func:`format_metric` / :func:`metric_label` /
  :func:`metric_max` 把原始数值转成可直接绘制的文本。
- :class:`SensorHub` 负责采集、按 ``interval`` 缓存、维护 history 环形缓冲，
  进程级单例由 :func:`get_hub` 提供。
- 后端逐项懒加载并降级：psutil 是可选加速路径，缺失时退回 kernel32 的
  ``GetSystemTimes`` / ``GlobalMemoryStatusEx`` / ``GetDiskFreeSpaceExW`` /
  ``GetTickCount64``；NVIDIA GPU 走 ``nvml.dll``；网络速率在 Windows 上优先用
  ``iphlpapi!GetIfTable2``（psutil 的 C 扩展单次约 17 ms，超出采样预算）；
  CPU 温度 / CPU 功耗 / 风扇转速只认 LibreHardwareMonitor（或
  OpenHardwareMonitor）与 HWiNFO 的共享内存，取不到就是 ``None``——绝不拿
  ACPI thermal zone 之类的近似值冒充 CPU 温度。

任一后端缺失或调用失败时对应 metric 为 ``None``，采集层永不向渲染层抛异常。
导入本模块不做任何系统调用：所有后端都在第一次采样时才初始化。
"""

# MIT License
#
# Copyright (c) 2026 LG600 Studio contributors
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from __future__ import annotations

import ctypes
import math
import os
import struct
import sys
import threading
import time
from collections import deque
from typing import Any

__all__ = [
    "METRIC_IDS",
    "METRIC_SPECS",
    "SensorHub",
    "available_metrics",
    "format_metric",
    "format_value",
    "get_hub",
    "metric_label",
    "metric_max",
]

_IS_WINDOWS = sys.platform == "win32"

_GB = float(1024**3)

#: 全部 metric，顺序即渲染层的建议顺序。
METRIC_IDS: tuple[str, ...] = (
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

#: 每个 metric 的展示元数据；``max`` 供进度条 / 折线归一化使用。
METRIC_SPECS: dict[str, dict] = {
    "cpu": {"label": "CPU 使用率", "unit": "%", "max": 100.0, "digits": 0, "suffix": "%"},
    "cpu_freq": {"label": "CPU 频率", "unit": "MHz", "max": 6000.0, "digits": 0, "suffix": " MHz"},
    "cpu_temp": {"label": "CPU 温度", "unit": "°C", "max": 100.0, "digits": 0, "suffix": "°C"},
    "cpu_power": {"label": "CPU 功耗", "unit": "W", "max": 300.0, "digits": 1, "suffix": " W"},
    "ram": {"label": "内存使用率", "unit": "%", "max": 100.0, "digits": 0, "suffix": "%"},
    "ram_used": {"label": "已用内存", "unit": "GB", "max": 128.0, "digits": 1, "suffix": " GB"},
    "ram_total": {"label": "内存总量", "unit": "GB", "max": 128.0, "digits": 1, "suffix": " GB"},
    "gpu": {"label": "GPU 使用率", "unit": "%", "max": 100.0, "digits": 0, "suffix": "%"},
    "gpu_temp": {"label": "GPU 温度", "unit": "°C", "max": 100.0, "digits": 0, "suffix": "°C"},
    "gpu_mem": {"label": "显存使用率", "unit": "%", "max": 100.0, "digits": 0, "suffix": "%"},
    "gpu_mem_used": {"label": "已用显存", "unit": "GB", "max": 32.0, "digits": 1, "suffix": " GB"},
    "gpu_mem_total": {"label": "显存总量", "unit": "GB", "max": 32.0, "digits": 1, "suffix": " GB"},
    "gpu_power": {"label": "GPU 功耗", "unit": "W", "max": 600.0, "digits": 1, "suffix": " W"},
    "gpu_fan": {"label": "GPU 风扇", "unit": "%", "max": 100.0, "digits": 0, "suffix": "%"},
    "gpu_clock": {"label": "GPU 频率", "unit": "MHz", "max": 4000.0, "digits": 0, "suffix": " MHz"},
    "disk": {"label": "磁盘使用率", "unit": "%", "max": 100.0, "digits": 0, "suffix": "%"},
    "disk_read": {
        "label": "磁盘读取",
        "unit": "MB/s",
        "max": 2000.0,
        "digits": 1,
        "suffix": " MB/s",
    },
    "disk_write": {
        "label": "磁盘写入",
        "unit": "MB/s",
        "max": 2000.0,
        "digits": 1,
        "suffix": " MB/s",
    },
    "net_down": {
        "label": "网络下载",
        "unit": "Mbps",
        "max": 1000.0,
        "digits": 1,
        "suffix": " Mbps",
    },
    "net_up": {"label": "网络上传", "unit": "Mbps", "max": 1000.0, "digits": 1, "suffix": " Mbps"},
    "uptime": {"label": "运行时间", "unit": "小时", "max": 720.0, "digits": 1, "suffix": " 小时"},
    "fan": {"label": "风扇转速", "unit": "RPM", "max": 3000.0, "digits": 0, "suffix": " RPM"},
    "cpu_cores": {"label": "CPU 核心数", "unit": "核", "max": 64.0, "digits": 0, "suffix": " 核"},
}

#: 未知 metric 的回退元数据，保证格式化函数永不抛 KeyError。
_DEFAULT_SPEC: dict = {"label": "", "unit": "", "max": 100.0, "digits": 1, "suffix": ""}

#: GPU 相关 metric：NVML 一次采样的完整输出集合。
_GPU_METRICS: tuple[str, ...] = (
    "gpu",
    "gpu_temp",
    "gpu_mem",
    "gpu_mem_used",
    "gpu_mem_total",
    "gpu_power",
    "gpu_fan",
    "gpu_clock",
)

#: ``describe()`` 在读取器没有写来源时的兜底描述。
_BASE_SOURCES: dict[str, str] = {
    "cpu": "psutil.cpu_percent / kernel32!GetSystemTimes 差值",
    "cpu_freq": "psutil.cpu_freq / 注册表 ~MHz",
    "cpu_temp": "LibreHardwareMonitor / HWiNFO 共享内存",
    "cpu_power": "LibreHardwareMonitor / HWiNFO 共享内存",
    "ram": "psutil.virtual_memory / kernel32!GlobalMemoryStatusEx",
    "ram_used": "psutil.virtual_memory / kernel32!GlobalMemoryStatusEx",
    "ram_total": "psutil.virtual_memory / kernel32!GlobalMemoryStatusEx",
    "gpu": "NVML",
    "gpu_temp": "NVML",
    "gpu_mem": "NVML",
    "gpu_mem_used": "NVML",
    "gpu_mem_total": "NVML",
    "gpu_power": "NVML",
    "gpu_fan": "NVML",
    "gpu_clock": "NVML",
    "disk": "kernel32!GetDiskFreeSpaceExW / psutil.disk_usage",
    "disk_read": "psutil.disk_io_counters 差值",
    "disk_write": "psutil.disk_io_counters 差值",
    "net_down": "iphlpapi!GetIfTable2 差值 / psutil.net_io_counters 差值",
    "net_up": "iphlpapi!GetIfTable2 差值 / psutil.net_io_counters 差值",
    "uptime": "psutil.boot_time / kernel32!GetTickCount64",
    "fan": "LibreHardwareMonitor / HWiNFO 共享内存",
    "cpu_cores": "psutil.cpu_count / os.cpu_count",
}

_UNSET = object()

_PSUTIL: Any = _UNSET

_DLL_CACHE: dict[str, Any] = {}

_NO_PSUTIL_RATE = "psutil 不可用（无法计算速率）"

_NO_SHARED_MEMORY = "未检测到硬件监控共享内存（LHM/OHM/HWiNFO 均不可用）"


# --------------------------------------------------------------------------- #
# 格式化
# --------------------------------------------------------------------------- #
def _spec(metric: str) -> dict:
    """返回 metric 的展示元数据；未知 metric 返回回退值。"""
    return METRIC_SPECS.get(metric) or _DEFAULT_SPEC


def format_value(metric: str, value: float | None) -> str:
    """把原始数值格式化成 ``"32%"`` / ``"46°C"`` / ``"1.2 GB"`` 形式。

    ``None`` 与 NaN / Inf 一律返回全角破折号 ``"—"``，渲染层无需再判断。
    """
    spec = _spec(metric)
    if value is None or not math.isfinite(value):
        return "—"
    return f"{float(value):.{int(spec['digits'])}f}{spec['suffix']}"


def format_metric(metric: str, value: float | None) -> str:
    """返回 ``"GPU 温度 46°C"`` 形式的完整展示文本（label + 空格 + 数值）。"""
    return f"{metric_label(metric)} {format_value(metric, value)}"


def metric_label(metric: str) -> str:
    """返回 metric 的中文标签；未知 metric 回退为 metric 本身。"""
    return str(_spec(metric)["label"]) or metric


def metric_max(metric: str) -> float:
    """返回 metric 的归一化上限；未知 metric 返回 ``100.0``。"""
    try:
        return float(_spec(metric)["max"])
    except (TypeError, ValueError):
        return 100.0


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def _now() -> float:
    """单调时钟，抽成函数便于测试控制缓存与速率时间差。"""
    return time.monotonic()


def _psutil() -> Any:
    """懒加载 psutil；缺失时返回 ``None``（本模块仍可工作）。"""
    global _PSUTIL
    if _PSUTIL is _UNSET:
        try:
            import psutil as psutil_module
        except Exception:  # noqa: BLE001 - 可选依赖，任何导入失败都视为缺失
            psutil_module = None
        _PSUTIL = psutil_module
    return _PSUTIL


def _dll(name: str) -> Any:
    """懒加载并缓存 Windows DLL；非 Windows 或加载失败返回 ``None``。"""
    if name in _DLL_CACHE:
        return _DLL_CACHE[name]
    library = None
    if _IS_WINDOWS:
        try:
            library = ctypes.WinDLL(name, use_last_error=True)
        except (OSError, AttributeError, ValueError):
            library = None
    _DLL_CACHE[name] = library
    return library


def _clamp_percent(value: float | None) -> float | None:
    """把百分数裁剪到 0-100；非有限值返回 ``None``。"""
    if value is None or not math.isfinite(value):
        return None
    return 0.0 if value < 0.0 else 100.0 if value > 100.0 else float(value)


def _clamp_rate(value: float | None) -> float | None:
    """把差值型速率裁剪为非负；非有限值返回 ``None``。"""
    if value is None or not math.isfinite(value):
        return None
    return float(value) if value > 0.0 else 0.0


def _decode_cstr(raw: bytes) -> str:
    """按 UTF-8 解码定长字符串，截断到第一个 NUL。"""
    end = raw.find(b"\x00")
    if end >= 0:
        raw = raw[:end]
    return raw.decode("utf-8", "replace").strip()


class _FILETIME(ctypes.Structure):
    """Windows FILETIME（100 ns 为单位）。"""

    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

    def value(self) -> int:
        return (self.dwHighDateTime << 32) | self.dwLowDateTime


class _MEMORYSTATUSEX(ctypes.Structure):
    """Windows MEMORYSTATUSEX。"""

    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _MEMORY_BASIC_INFORMATION(ctypes.Structure):
    """Windows MEMORY_BASIC_INFORMATION（x64 布局，用于取共享内存映射大小）。"""

    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.c_uint32),
        ("__alignment1", ctypes.c_uint32),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_uint32),
        ("Protect", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("__alignment2", ctypes.c_uint32),
    ]


class _GUID(ctypes.Structure):
    """Windows GUID。"""

    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _MIB_IF_ROW2(ctypes.Structure):
    """Windows MIB_IF_ROW2（GetIfTable2 返回的接口行）。

    只声明到 ``OutQLen`` 为止，其余尾部字段对读取收发字节数没有影响。
    """

    _fields_ = [
        ("InterfaceLuid", ctypes.c_ulonglong),
        ("InterfaceIndex", ctypes.c_ulong),
        ("InterfaceGuid", _GUID),
        ("Alias", ctypes.c_wchar * 257),
        ("Description", ctypes.c_wchar * 257),
        ("PhysicalAddressLength", ctypes.c_ulong),
        ("PhysicalAddress", ctypes.c_ubyte * 32),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * 32),
        ("Mtu", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
        ("TunnelType", ctypes.c_ulong),
        ("MediaType", ctypes.c_ulong),
        ("PhysicalMediumType", ctypes.c_ulong),
        ("AccessType", ctypes.c_ulong),
        ("DirectionType", ctypes.c_ulong),
        ("InterfaceAndOperStatusFlags", ctypes.c_ubyte),
        ("OperStatus", ctypes.c_ulong),
        ("AdminStatus", ctypes.c_ulong),
        ("MediaConnectState", ctypes.c_ulong),
        ("NetworkGuid", _GUID),
        ("ConnectionType", ctypes.c_ulong),
        ("TransmitLinkSpeed", ctypes.c_ulonglong),
        ("ReceiveLinkSpeed", ctypes.c_ulonglong),
        ("InOctets", ctypes.c_ulonglong),
        ("InUcastPkts", ctypes.c_ulonglong),
        ("InNUcastPkts", ctypes.c_ulonglong),
        ("InDiscards", ctypes.c_ulonglong),
        ("InErrors", ctypes.c_ulonglong),
        ("InUnknownProtos", ctypes.c_ulonglong),
        ("InUcastOctets", ctypes.c_ulonglong),
        ("InMulticastOctets", ctypes.c_ulonglong),
        ("InBroadcastOctets", ctypes.c_ulonglong),
        ("OutOctets", ctypes.c_ulonglong),
        ("OutUcastPkts", ctypes.c_ulonglong),
        ("OutNUcastPkts", ctypes.c_ulonglong),
        ("OutDiscards", ctypes.c_ulonglong),
        ("OutErrors", ctypes.c_ulonglong),
        ("OutUcastOctets", ctypes.c_ulonglong),
        ("OutMulticastOctets", ctypes.c_ulonglong),
        ("OutBroadcastOctets", ctypes.c_ulonglong),
        ("OutQLen", ctypes.c_ulonglong),
    ]


#: IF_HARDWARE：MIB_IF_ROW2.InterfaceAndOperStatusFlags 的硬件接口标志位。
_IF_HARDWARE = 0x01

_KERNEL32_READY = False


def _configure_kernel32() -> None:
    """设置 kernel32 相关函数的签名（只做一次）。"""
    global _KERNEL32_READY
    if _KERNEL32_READY:
        return
    kernel32 = _dll("kernel32")
    if kernel32 is None:
        return
    kernel32.GetSystemTimes.argtypes = [
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    kernel32.GetSystemTimes.restype = ctypes.c_int
    kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
    kernel32.GlobalMemoryStatusEx.restype = ctypes.c_int
    kernel32.GetDiskFreeSpaceExW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
    ]
    kernel32.GetDiskFreeSpaceExW.restype = ctypes.c_int
    kernel32.GetTickCount64.argtypes = []
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    _KERNEL32_READY = True


# --------------------------------------------------------------------------- #
# NVML（NVIDIA GPU）
# --------------------------------------------------------------------------- #
_NVML_DLL = r"C:\Windows\System32\nvml.dll"
_NVML_TEMPERATURE_GPU = 0
_NVML_CLOCK_GRAPHICS = 0


class _NvmlUtilization(ctypes.Structure):
    """NVML 利用率结构：``gpu`` 为 GPU 忙闲，``memory`` 为显存控制器忙闲。"""

    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlMemory(ctypes.Structure):
    """NVML 显存信息结构（单位：字节）。"""

    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class _NvmlSampler:
    """NVML 采样器：懒初始化，全部入口点显式 argtypes，任一失败只影响单项。"""

    def __init__(self) -> None:
        self._lib: Any = None
        self._handle: ctypes.c_void_p | None = None
        self._name = ""
        self._loaded = False

    # ---------------------------------------------------------------- 生命周期
    def ensure(self) -> bool:
        """确保 NVML 已初始化并拿到 0 号设备句柄；失败返回 ``False``。"""
        if self._loaded:
            return self._lib is not None
        self._loaded = True
        if not _IS_WINDOWS:
            return False
        try:
            library = ctypes.WinDLL(_NVML_DLL, use_last_error=True)
        except (OSError, AttributeError, ValueError):
            return False
        try:
            self._configure(library)
            if library.nvmlInit_v2() != 0:
                return False
            handle = ctypes.c_void_p()
            if library.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle)) != 0:
                library.nvmlShutdown()
                return False
        except (AttributeError, OSError, ValueError):
            return False
        self._lib = library
        self._handle = handle
        self._name = self._read_name(library, handle)
        return True

    def close(self) -> None:
        """释放 NVML 句柄；可重复调用，之后再次采样会重新懒初始化。"""
        library = self._lib
        self._lib = None
        self._handle = None
        self._name = ""
        self._loaded = False
        if library is not None:
            try:
                library.nvmlShutdown()
            except (AttributeError, OSError, ValueError):
                pass

    def device_name(self) -> str:
        """返回 0 号设备名称，例如 ``"NVIDIA GeForce RTX 5070 Ti"``。"""
        return self._name

    @staticmethod
    def _configure(library: Any) -> None:
        """为 NVML 的每个入口点设置 argtypes / restype。"""
        library.nvmlInit_v2.argtypes = []
        library.nvmlInit_v2.restype = ctypes.c_int
        library.nvmlShutdown.argtypes = []
        library.nvmlShutdown.restype = ctypes.c_int
        library.nvmlDeviceGetCount_v2.argtypes = [ctypes.POINTER(ctypes.c_uint)]
        library.nvmlDeviceGetCount_v2.restype = ctypes.c_int
        library.nvmlDeviceGetHandleByIndex_v2.argtypes = [
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        library.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
        library.nvmlDeviceGetName.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint]
        library.nvmlDeviceGetName.restype = ctypes.c_int
        library.nvmlDeviceGetTemperature.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_uint),
        ]
        library.nvmlDeviceGetTemperature.restype = ctypes.c_int
        library.nvmlDeviceGetUtilizationRates.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_NvmlUtilization),
        ]
        library.nvmlDeviceGetUtilizationRates.restype = ctypes.c_int
        library.nvmlDeviceGetMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_NvmlMemory),
        ]
        library.nvmlDeviceGetMemoryInfo.restype = ctypes.c_int
        library.nvmlDeviceGetPowerUsage.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint),
        ]
        library.nvmlDeviceGetPowerUsage.restype = ctypes.c_int
        library.nvmlDeviceGetFanSpeed.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint),
        ]
        library.nvmlDeviceGetFanSpeed.restype = ctypes.c_int
        library.nvmlDeviceGetClockInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_uint),
        ]
        library.nvmlDeviceGetClockInfo.restype = ctypes.c_int

    # ------------------------------------------------------------------ 读取
    @staticmethod
    def _read_name(library: Any, handle: ctypes.c_void_p) -> str:
        """读取设备名称；失败返回空串。"""
        try:
            buffer = ctypes.create_string_buffer(96)
            if library.nvmlDeviceGetName(handle, buffer, 96) != 0:
                return ""
            return buffer.value.decode("utf-8", "replace").strip()
        except (AttributeError, OSError, ValueError):
            return ""

    def _uint(self, function_name: str, *args: Any) -> float | None:
        """调用一个以 ``c_uint`` 出参的 NVML 函数；失败返回 ``None``。"""
        library = self._lib
        if library is None or self._handle is None:
            return None
        try:
            out = ctypes.c_uint()
            function = getattr(library, function_name)
            if function(self._handle, *args, ctypes.byref(out)) != 0:
                return None
            return float(out.value)
        except (AttributeError, OSError, ValueError):
            return None

    def _utilization(self) -> tuple[float | None, float | None]:
        """返回 ``(GPU 使用率 %, 显存控制器使用率 %)``。"""
        library = self._lib
        if library is None or self._handle is None:
            return None, None
        try:
            utilization = _NvmlUtilization()
            if library.nvmlDeviceGetUtilizationRates(self._handle, ctypes.byref(utilization)) != 0:
                return None, None
            return _clamp_percent(float(utilization.gpu)), _clamp_percent(float(utilization.memory))
        except (AttributeError, OSError, ValueError):
            return None, None

    def _memory(self) -> tuple[float | None, float | None]:
        """返回 ``(已用显存 GB, 显存总量 GB)``。"""
        library = self._lib
        if library is None or self._handle is None:
            return None, None
        try:
            memory = _NvmlMemory()
            if library.nvmlDeviceGetMemoryInfo(self._handle, ctypes.byref(memory)) != 0:
                return None, None
            return float(memory.used) / _GB, float(memory.total) / _GB
        except (AttributeError, OSError, ValueError):
            return None, None

    def read(self) -> dict[str, float | None]:
        """采集全部 GPU metric；任何单项失败都只影响该项。"""
        values: dict[str, float | None] = {metric: None for metric in _GPU_METRICS}
        if not self.ensure():
            return values
        values["gpu"], controller_utilization = self._utilization()
        values["gpu_temp"] = _clamp_percent(
            self._uint("nvmlDeviceGetTemperature", _NVML_TEMPERATURE_GPU)
        )
        used, total = self._memory()
        values["gpu_mem_used"] = used
        values["gpu_mem_total"] = total
        if used is not None and total:
            values["gpu_mem"] = _clamp_percent(used / total * 100.0)
        elif controller_utilization is not None:
            values["gpu_mem"] = controller_utilization
        power = self._uint("nvmlDeviceGetPowerUsage")
        values["gpu_power"] = power / 1000.0 if power is not None else None
        values["gpu_fan"] = _clamp_percent(self._uint("nvmlDeviceGetFanSpeed"))
        values["gpu_clock"] = self._uint("nvmlDeviceGetClockInfo", _NVML_CLOCK_GRAPHICS)
        return values


# --------------------------------------------------------------------------- #
# 共享内存（LibreHardwareMonitor / OpenHardwareMonitor / HWiNFO）
# --------------------------------------------------------------------------- #
_FILE_MAP_READ = 0x0004
_MAX_MAPPING_READ = 8 * 1024 * 1024

_OHM_MAPPINGS: tuple[str, ...] = (
    r"Global\LibreHardwareMonitor",
    r"Global\OpenHardwareMonitor",
)
_OHM_HEADER_SIZE = 12
_OHM_NAME_SIZE = 128
_OHM_HARDWARE_SIZE = 4 * _OHM_NAME_SIZE + 4
_OHM_SENSOR_SIZE = 4 * _OHM_NAME_SIZE + 12
_OHM_MAX_HARDWARE = 128
_OHM_MAX_SENSORS = 1024

_HWINFO_MAPPING = r"Global\HWiNFO_SENS_SM2"
_HWINFO_MAGIC = b"HWiS"
_HWINFO_HEADER_SIZE = 40
_HWINFO_SENSOR_NAME_SIZE = 128
_HWINFO_SENSOR_MIN_SIZE = _HWINFO_SENSOR_NAME_SIZE + 4
_HWINFO_ENTRY_LABEL_SIZE = 128
_HWINFO_ENTRY_UNIT_OFFSET = _HWINFO_ENTRY_LABEL_SIZE
_HWINFO_ENTRY_UNIT_SIZE = 16
_HWINFO_ENTRY_VALUE_TYPE_OFFSET = _HWINFO_ENTRY_UNIT_OFFSET + _HWINFO_ENTRY_UNIT_SIZE
_HWINFO_ENTRY_VALUE_OFFSET = _HWINFO_ENTRY_VALUE_TYPE_OFFSET + 8
_HWINFO_ENTRY_MIN_SIZE = _HWINFO_ENTRY_VALUE_OFFSET + 8
_HWINFO_VALUE_TYPE_INT = 1
_HWINFO_VALUE_TYPE_DOUBLE = 2
_HWINFO_MAX_SENSORS = 64
_HWINFO_MAX_ENTRIES = 4096

_SHARED_MAPPING_READY = False


def _configure_shared_mapping() -> None:
    """设置共享内存读取用到的 kernel32 函数签名（只做一次）。"""
    global _SHARED_MAPPING_READY
    if _SHARED_MAPPING_READY:
        return
    kernel32 = _dll("kernel32")
    if kernel32 is None:
        return
    kernel32.OpenFileMappingW.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.OpenFileMappingW.restype = ctypes.c_void_p
    kernel32.MapViewOfFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_size_t,
    ]
    kernel32.MapViewOfFile.restype = ctypes.c_void_p
    kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    kernel32.UnmapViewOfFile.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.VirtualQuery.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_MEMORY_BASIC_INFORMATION),
        ctypes.c_size_t,
    ]
    kernel32.VirtualQuery.restype = ctypes.c_size_t
    _SHARED_MAPPING_READY = True


def _read_shared_mapping(name: str) -> bytes | None:
    """打开命名共享内存并整块读回；不存在或失败返回 ``None``。"""
    if not _IS_WINDOWS:
        return None
    try:
        _configure_shared_mapping()
        kernel32 = _dll("kernel32")
    except (AttributeError, OSError, ValueError):
        return None
    if kernel32 is None:
        return None
    handle = None
    address = None
    try:
        handle = kernel32.OpenFileMappingW(_FILE_MAP_READ, 0, name)
        if not handle:
            return None
        address = kernel32.MapViewOfFile(handle, _FILE_MAP_READ, 0, 0, 0)
        if not address:
            return None
        info = _MEMORY_BASIC_INFORMATION()
        written = kernel32.VirtualQuery(address, ctypes.byref(info), ctypes.sizeof(info))
        if not written or not info.RegionSize:
            return None
        return ctypes.string_at(address, min(int(info.RegionSize), _MAX_MAPPING_READ))
    except (AttributeError, OSError, ValueError):
        return None
    finally:
        if address:
            try:
                kernel32.UnmapViewOfFile(address)
            except (AttributeError, OSError, ValueError):
                pass
        if handle:
            try:
                kernel32.CloseHandle(handle)
            except (AttributeError, OSError, ValueError):
                pass


def _parse_ohm_buffer(buffer: bytes) -> dict[str, float]:
    """解析 LHM/OHM 共享内存，返回 ``{metric: value}``（可能为空）。

    结构：``signature|version|count``（12 字节）后跟 ``count`` 个硬件块；
    硬件块 = 4×``char[128]`` + ``int sensorCount``，传感器 = 4×``char[128]``
    + ``float value|min|max``。传感器的 Identifier 经
    ``_classify_ohm_identifier`` 归类到 CPU 温度 / 风扇 / 功耗。
    结构不符预期时返回已解析出的部分或空字典，绝不抛异常。
    """
    found: dict[str, float] = {}
    if len(buffer) < _OHM_HEADER_SIZE:
        return found
    count = int.from_bytes(buffer[8:12], "little")
    if not 0 < count <= _OHM_MAX_HARDWARE:
        return found
    offset = _OHM_HEADER_SIZE
    for _ in range(count):
        if offset + _OHM_HARDWARE_SIZE > len(buffer):
            break
        hardware_text = " ".join(
            text
            for text in (
                _decode_cstr(
                    buffer[offset + index * _OHM_NAME_SIZE : offset + (index + 1) * _OHM_NAME_SIZE]
                )
                for index in range(4)
            )
            if text
        )
        sensor_count = int.from_bytes(
            buffer[offset + 4 * _OHM_NAME_SIZE : offset + _OHM_HARDWARE_SIZE], "little"
        )
        offset += _OHM_HARDWARE_SIZE
        if not 0 <= sensor_count <= _OHM_MAX_SENSORS:
            break
        for _ in range(sensor_count):
            if offset + _OHM_SENSOR_SIZE > len(buffer):
                return found
            sensor_text = " ".join(
                text
                for text in (
                    _decode_cstr(
                        buffer[
                            offset + index * _OHM_NAME_SIZE : offset + (index + 1) * _OHM_NAME_SIZE
                        ]
                    )
                    for index in range(4)
                )
                if text
            )
            value = struct.unpack_from("<f", buffer, offset + 4 * _OHM_NAME_SIZE)[0]
            offset += _OHM_SENSOR_SIZE
            if not math.isfinite(value):
                continue
            identifier = (sensor_text or hardware_text).lower()
            metric = _classify_ohm_identifier(identifier)
            if metric is not None:
                found.setdefault(metric, float(value))
    return found


def _classify_ohm_identifier(identifier: str) -> str | None:
    """把 LHM/OHM 传感器的 Identifier 归类到 ``cpu_temp`` / ``fan`` / ``cpu_power``。

    LHM 的 ``Identifier`` 本身就是带硬件前缀的完整路径（``/amdcpu/0/temperature/0``），
    因此判据是：路径里出现 CPU 且带对应 sensor 类别。这样 ``/lpc/fan/0`` 这类
    主板风扇不会被误判为 CPU 风扇。任务约定的 ``/temperature/cpu``、``/fan/cpu``、
    ``/power/cpu`` 三种写法同样满足该判据。
    """
    if "cpu" not in identifier:
        return None
    if "/temperature/" in identifier:
        return "cpu_temp"
    if "/fan/" in identifier:
        return "fan"
    if "/power/" in identifier:
        return "cpu_power"
    return None


def _hwinfo_entry_value(buffer: bytes, base: int, value_type: int) -> float | None:
    """按 HWiNFO SM2 的 value_type 读取一个 entry 的数值。"""
    try:
        if value_type == _HWINFO_VALUE_TYPE_DOUBLE:
            value = struct.unpack_from("<d", buffer, base + _HWINFO_ENTRY_VALUE_OFFSET)[0]
        elif value_type == _HWINFO_VALUE_TYPE_INT:
            value = float(struct.unpack_from("<q", buffer, base + _HWINFO_ENTRY_VALUE_OFFSET)[0])
        else:
            return None
    except struct.error:
        return None
    return float(value) if math.isfinite(value) else None


def _parse_hwinfo_buffer(buffer: bytes) -> dict[str, float]:
    """解析 HWiNFO SM2 共享内存；magic / 偏移不符预期返回空字典。

    只按 entry 的 label 含 ``CPU`` 且单位匹配来识别 CPU 温度 / 功耗 / 风扇，
    不做任何猜测性替换。
    """
    found: dict[str, float] = {}
    if len(buffer) < _HWINFO_HEADER_SIZE or buffer[0:4] != _HWINFO_MAGIC:
        return found
    (
        _version,
        _revision,
        _poll_time,
        sensor_offset,
        sensor_size,
        sensor_count,
        entry_offset,
        entry_size,
        entry_count,
    ) = struct.unpack_from("<9I", buffer, 4)
    if not 0 < sensor_count <= _HWINFO_MAX_SENSORS:
        return found
    if not 0 < entry_count <= _HWINFO_MAX_ENTRIES:
        return found
    if sensor_size < _HWINFO_SENSOR_MIN_SIZE or entry_size < _HWINFO_ENTRY_MIN_SIZE:
        return found
    if sensor_offset < _HWINFO_HEADER_SIZE:
        return found
    if sensor_offset + sensor_count * sensor_size > len(buffer):
        return found
    if entry_offset < _HWINFO_HEADER_SIZE:
        return found
    if entry_offset + entry_count * entry_size > len(buffer):
        return found
    for index in range(entry_count):
        base = entry_offset + index * entry_size
        label = _decode_cstr(buffer[base : base + _HWINFO_ENTRY_LABEL_SIZE])
        if "cpu" not in label.lower():
            continue
        unit_base = base + _HWINFO_ENTRY_UNIT_OFFSET
        unit = _decode_cstr(buffer[unit_base : unit_base + _HWINFO_ENTRY_UNIT_SIZE])
        value_type = int.from_bytes(
            buffer[
                base + _HWINFO_ENTRY_VALUE_TYPE_OFFSET : base + _HWINFO_ENTRY_VALUE_TYPE_OFFSET + 4
            ],
            "little",
        )
        value = _hwinfo_entry_value(buffer, base, value_type)
        if value is None:
            continue
        unit_upper = unit.upper()
        if "°C" in unit or unit_upper in ("C", "CELSIUS"):
            found.setdefault("cpu_temp", value)
        elif unit_upper == "W":
            found.setdefault("cpu_power", value)
        elif unit_upper == "RPM":
            found.setdefault("fan", value)
    return found


# --------------------------------------------------------------------------- #
# 采样状态
# --------------------------------------------------------------------------- #
class _SampleState:
    """跨采样保留的计数器与 NVML 句柄。"""

    def __init__(self) -> None:
        self.sources: dict[str, str] = {}
        self.cpu_times: tuple[int, int] | None = None
        self.disk_io: tuple[float, int, int] | None = None
        self.net_io: tuple[float, int, int] | None = None
        self.nvml = _NvmlSampler()


# --------------------------------------------------------------------------- #
# 读取器：每个函数负责一组 metric，供测试 monkeypatch 注入替身
# --------------------------------------------------------------------------- #
def _read_cpu(state: _SampleState) -> float | None:
    """CPU 占用百分数：psutil 优先，退回 ``GetSystemTimes`` 差值。"""
    psutil = _psutil()
    if psutil is not None:
        try:
            value = psutil.cpu_percent(interval=None)
        except Exception:  # noqa: BLE001 - psutil 后端不可用时退回 ctypes
            value = None
        if value is not None:
            state.sources["cpu"] = "psutil.cpu_percent"
            return _clamp_percent(float(value))
    state.sources["cpu"] = "kernel32!GetSystemTimes 差值"
    return _cpu_from_system_times(state)


def _cpu_from_system_times(state: _SampleState) -> float | None:
    """用两次 ``GetSystemTimes`` 的差值计算 CPU 忙闲比例（0-100）。"""
    if not _IS_WINDOWS:
        return None
    try:
        _configure_kernel32()
        kernel32 = _dll("kernel32")
    except (AttributeError, OSError, ValueError):
        return None
    if kernel32 is None:
        return None
    try:
        idle = _FILETIME()
        kernel = _FILETIME()
        user = _FILETIME()
        if not kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
    except (AttributeError, OSError, ValueError):
        return None
    idle_value = idle.value()
    total_value = kernel.value() + user.value()
    previous = state.cpu_times
    state.cpu_times = (idle_value, total_value)
    if previous is None:
        return None
    delta_total = total_value - previous[1]
    delta_idle = idle_value - previous[0]
    if delta_total <= 0 or delta_idle < 0:
        return None
    return _clamp_percent((1.0 - delta_idle / delta_total) * 100.0)


def _read_cpu_freq(state: _SampleState) -> float | None:
    """CPU 当前频率（MHz）：psutil 优先，退回注册表 ``~MHz``。"""
    psutil = _psutil()
    if psutil is not None:
        try:
            frequency = psutil.cpu_freq()
        except Exception:  # noqa: BLE001 - 部分平台无实现，退回注册表
            frequency = None
        current = getattr(frequency, "current", None) if frequency is not None else None
        if current:
            state.sources["cpu_freq"] = "psutil.cpu_freq"
            return float(current)
    state.sources["cpu_freq"] = "注册表 HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0"
    return _cpu_freq_from_registry()


def _cpu_freq_from_registry() -> float | None:
    """读取注册表里的标称频率（MHz）；非 Windows 返回 ``None``。"""
    if not _IS_WINDOWS:
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "~MHz")
        return float(value) if value else None
    except (OSError, ValueError, TypeError):
        return None


def _read_ram(state: _SampleState) -> dict[str, float | None]:
    """内存使用率与容量（GB）：psutil 优先，退回 ``GlobalMemoryStatusEx``。"""
    psutil = _psutil()
    if psutil is not None:
        try:
            memory = psutil.virtual_memory()
        except Exception:  # noqa: BLE001 - psutil 后端不可用时退回 ctypes
            memory = None
        if memory is not None:
            state.sources["ram"] = "psutil.virtual_memory"
            state.sources["ram_used"] = "psutil.virtual_memory"
            state.sources["ram_total"] = "psutil.virtual_memory"
            return {
                "ram": _clamp_percent(float(memory.percent)),
                "ram_used": float(memory.used) / _GB,
                "ram_total": float(memory.total) / _GB,
            }
    state.sources["ram"] = "kernel32!GlobalMemoryStatusEx"
    state.sources["ram_used"] = "kernel32!GlobalMemoryStatusEx"
    state.sources["ram_total"] = "kernel32!GlobalMemoryStatusEx"
    return _ram_from_memory_status()


def _ram_from_memory_status() -> dict[str, float | None]:
    """用 ``GlobalMemoryStatusEx`` 读取内存；非 Windows 返回全 ``None``。"""
    values: dict[str, float | None] = {"ram": None, "ram_used": None, "ram_total": None}
    if not _IS_WINDOWS:
        return values
    try:
        _configure_kernel32()
        kernel32 = _dll("kernel32")
    except (AttributeError, OSError, ValueError):
        return values
    if kernel32 is None:
        return values
    try:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return values
    except (AttributeError, OSError, ValueError):
        return values
    total = float(status.ullTotalPhys)
    available = float(status.ullAvailPhys)
    values["ram"] = _clamp_percent(float(status.dwMemoryLoad))
    values["ram_total"] = total / _GB
    values["ram_used"] = max(0.0, total - available) / _GB
    return values


def _read_gpu(state: _SampleState) -> dict[str, float | None]:
    """NVIDIA GPU 全部指标；无 N 卡 / 无 ``nvml.dll`` 时全部为 ``None``。"""
    nvml = state.nvml
    if not nvml.ensure():
        for metric in _GPU_METRICS:
            state.sources[metric] = "NVML 不可用（无 NVIDIA 驱动或显卡）"
        return {metric: None for metric in _GPU_METRICS}
    device = nvml.device_name() or "NVIDIA GPU"
    for metric in _GPU_METRICS:
        state.sources[metric] = f"NVML（{device}）"
    return nvml.read()


def _read_disk(state: _SampleState) -> dict[str, float | None]:
    """磁盘占用率 + 读写速率（MB/s，来自 psutil 计数器差值）。"""
    values: dict[str, float | None] = {"disk": None, "disk_read": None, "disk_write": None}
    state.sources["disk"] = "kernel32!GetDiskFreeSpaceExW" if _IS_WINDOWS else "psutil.disk_usage"
    values["disk"] = _disk_usage_percent()
    psutil = _psutil()
    if psutil is None:
        state.sources["disk_read"] = _NO_PSUTIL_RATE
        state.sources["disk_write"] = _NO_PSUTIL_RATE
        return values
    try:
        counters = psutil.disk_io_counters()
    except Exception:  # noqa: BLE001 - 计数器不可读时速率保持 None
        counters = None
    now = _now()
    if counters is None:
        return values
    current = (now, int(counters.read_bytes), int(counters.write_bytes))
    previous = state.disk_io
    state.disk_io = current
    if previous is None or now - previous[0] <= 0.0:
        return values
    delta = now - previous[0]
    values["disk_read"] = _clamp_rate((current[1] - previous[1]) / delta / 1e6)
    values["disk_write"] = _clamp_rate((current[2] - previous[2]) / delta / 1e6)
    state.sources["disk_read"] = "psutil.disk_io_counters 差值"
    state.sources["disk_write"] = "psutil.disk_io_counters 差值"
    return values


def _disk_usage_percent() -> float | None:
    """C 盘占用百分数；Windows 用 ``GetDiskFreeSpaceExW``，其它平台用 psutil。"""
    if _IS_WINDOWS:
        try:
            _configure_kernel32()
            kernel32 = _dll("kernel32")
        except (AttributeError, OSError, ValueError):
            return None
        if kernel32 is None:
            return None
        try:
            free = ctypes.c_ulonglong()
            total = ctypes.c_ulonglong()
            available = ctypes.c_ulonglong()
            if not kernel32.GetDiskFreeSpaceExW(
                "C:\\", ctypes.byref(available), ctypes.byref(total), ctypes.byref(free)
            ):
                return None
        except (AttributeError, OSError, ValueError):
            return None
        if not total.value:
            return None
        return _clamp_percent((1.0 - free.value / total.value) * 100.0)
    psutil = _psutil()
    if psutil is None:
        return None
    try:
        return _clamp_percent(float(psutil.disk_usage("/").percent))
    except Exception:  # noqa: BLE001 - 目录不可读时降级为 None
        return None


_IPHLPAPI_READY = False


def _configure_iphlpapi() -> Any:
    """懒加载 iphlpapi 并设置 ``GetIfTable2`` / ``FreeMibTable`` 签名。"""
    global _IPHLPAPI_READY
    if not _IS_WINDOWS:
        return None
    iphlpapi = _dll("iphlpapi")
    if iphlpapi is None:
        return None
    if not _IPHLPAPI_READY:
        try:
            iphlpapi.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
            iphlpapi.GetIfTable2.restype = ctypes.c_ulong
            iphlpapi.FreeMibTable.argtypes = [ctypes.c_void_p]
            iphlpapi.FreeMibTable.restype = None
        except (AttributeError, ValueError):
            return None
        _IPHLPAPI_READY = True
    return iphlpapi


def _net_counters_from_iphlpapi() -> tuple[int, int] | None:
    """用 ``iphlpapi!GetIfTable2`` 汇总接口收发字节数；失败返回 ``None``。

    优先只统计 ``IF_HARDWARE`` 接口（口径与 psutil 一致），一个都没匹配到时
    退回统计全部接口。单次约 2.5 ms；psutil 的 ``net_io_counters()`` 单次约
    17 ms，会突破采样预算。
    """
    iphlpapi = _configure_iphlpapi()
    if iphlpapi is None:
        return None
    table = ctypes.c_void_p()
    try:
        if iphlpapi.GetIfTable2(ctypes.byref(table)) != 0 or not table.value:
            return None
    except (OSError, AttributeError, ValueError):
        return None
    try:
        count = ctypes.c_ulong.from_address(table.value).value
        if not 0 < count <= 4096:
            return None
        rows = (_MIB_IF_ROW2 * count).from_address(table.value + 8)
        hardware: list[_MIB_IF_ROW2] = [
            row for row in rows if int(row.InterfaceAndOperStatusFlags) & _IF_HARDWARE
        ]
        selected = hardware or list(rows)
        received = sum(int(row.InOctets) for row in selected)
        sent = sum(int(row.OutOctets) for row in selected)
        return received, sent
    except (ValueError, OSError, AttributeError):
        return None
    finally:
        try:
            iphlpapi.FreeMibTable(table)
        except (ValueError, OSError, AttributeError):
            pass


def _net_counters_from_psutil() -> tuple[int, int] | None:
    """用 psutil 汇总收发字节数；psutil 缺失或读取失败返回 ``None``。"""
    psutil = _psutil()
    if psutil is None:
        return None
    try:
        counters = psutil.net_io_counters()
    except Exception:  # noqa: BLE001 - 计数器不可读即视为不可用
        return None
    return int(counters.bytes_recv), int(counters.bytes_sent)


def _read_net(state: _SampleState) -> dict[str, float | None]:
    """网络上下行速率（Mbps，来自收发字节计数器的差值）。"""
    values: dict[str, float | None] = {"net_down": None, "net_up": None}
    counters = _net_counters_from_iphlpapi()
    if counters is not None:
        state.sources["net_down"] = "iphlpapi!GetIfTable2 差值"
        state.sources["net_up"] = "iphlpapi!GetIfTable2 差值"
    else:
        counters = _net_counters_from_psutil()
        if counters is not None:
            state.sources["net_down"] = "psutil.net_io_counters 差值"
            state.sources["net_up"] = "psutil.net_io_counters 差值"
    if counters is None:
        state.sources.setdefault("net_down", _NO_PSUTIL_RATE)
        state.sources.setdefault("net_up", _NO_PSUTIL_RATE)
        return values
    received, sent = counters
    now = _now()
    current = (now, received, sent)
    previous = state.net_io
    state.net_io = current
    if previous is None or now - previous[0] <= 0.0:
        return values
    delta = now - previous[0]
    values["net_down"] = _clamp_rate((received - previous[1]) * 8.0 / 1e6 / delta)
    values["net_up"] = _clamp_rate((sent - previous[2]) * 8.0 / 1e6 / delta)
    return values


def _read_uptime(state: _SampleState) -> float | None:
    """系统运行时长（小时）：psutil.boot_time 优先，退回 ``GetTickCount64``。"""
    psutil = _psutil()
    if psutil is not None:
        try:
            elapsed = (time.time() - psutil.boot_time()) / 3600.0
        except Exception:  # noqa: BLE001 - 退回 GetTickCount64
            elapsed = None
        if elapsed is not None:
            state.sources["uptime"] = "psutil.boot_time"
            return max(0.0, float(elapsed))
    state.sources["uptime"] = "kernel32!GetTickCount64"
    return _uptime_from_tick_count()


def _uptime_from_tick_count() -> float | None:
    """用 ``GetTickCount64`` 计算运行时长；非 Windows 返回 ``None``。"""
    if not _IS_WINDOWS:
        return None
    try:
        _configure_kernel32()
        kernel32 = _dll("kernel32")
    except (AttributeError, OSError, ValueError):
        return None
    if kernel32 is None:
        return None
    try:
        return float(kernel32.GetTickCount64()) / 1000.0 / 3600.0
    except (AttributeError, OSError, ValueError):
        return None


def _read_cores(state: _SampleState) -> float | None:
    """逻辑核心数：psutil 优先，退回 ``os.cpu_count``。"""
    psutil = _psutil()
    if psutil is not None:
        try:
            count = psutil.cpu_count(logical=True)
        except Exception:  # noqa: BLE001 - 退回 os.cpu_count
            count = None
        if count:
            state.sources["cpu_cores"] = "psutil.cpu_count(logical=True)"
            return float(count)
    count = os.cpu_count()
    state.sources["cpu_cores"] = "os.cpu_count"
    return float(count) if count else None


def _read_thermal(state: _SampleState) -> dict[str, float | None]:
    """CPU 温度 / CPU 功耗 / 风扇转速：只认 LHM/OHM 与 HWiNFO 共享内存。"""
    values: dict[str, float | None] = {"cpu_temp": None, "cpu_power": None, "fan": None}
    readers = (
        (_read_lhm, "LibreHardwareMonitor/OpenHardwareMonitor 共享内存"),
        (_read_hwinfo, "HWiNFO SM2 共享内存"),
    )
    for reader, description in readers:
        try:
            found = reader()
        except Exception:  # noqa: BLE001 - 解析失败即视为该后端不可用
            found = {}
        for metric, value in found.items():
            if metric in values and values[metric] is None and value is not None:
                values[metric] = float(value)
                state.sources[metric] = description
        if all(value is not None for value in values.values()):
            break
    for metric, value in values.items():
        if value is None:
            state.sources[metric] = _NO_SHARED_MEMORY
    return values


def _read_lhm() -> dict[str, float]:
    """依次尝试 LHM / OHM 共享内存；都不可用返回空字典。"""
    for mapping in _OHM_MAPPINGS:
        buffer = _read_shared_mapping(mapping)
        if not buffer:
            continue
        try:
            found = _parse_ohm_buffer(buffer)
        except (struct.error, ValueError, IndexError):
            found = {}
        if found:
            return found
    return {}


def _read_hwinfo() -> dict[str, float]:
    """尝试 HWiNFO SM2 共享内存；不可用返回空字典。"""
    buffer = _read_shared_mapping(_HWINFO_MAPPING)
    if not buffer:
        return {}
    try:
        return _parse_hwinfo_buffer(buffer)
    except (struct.error, ValueError, IndexError):
        return {}


def _collect(state: _SampleState) -> dict[str, float | None]:
    """依次调用全部读取器；单个读取器抛异常只让它负责的 metric 保持 ``None``。"""
    state.sources = {}
    values: dict[str, float | None] = {metric: None for metric in METRIC_IDS}
    probes = (
        ("cpu", _read_cpu),
        ("cpu_freq", _read_cpu_freq),
        ("ram", _read_ram),
        ("gpu", _read_gpu),
        ("disk", _read_disk),
        ("net", _read_net),
        ("uptime", _read_uptime),
        ("cpu_cores", _read_cores),
        ("thermal", _read_thermal),
    )
    for metric, probe in probes:
        try:
            result = probe(state)
        except Exception:  # noqa: BLE001 - 采集层必须永不向渲染层抛异常
            continue
        if isinstance(result, dict):
            for key, value in result.items():
                if key in values:
                    values[key] = value
        elif metric in values and result is not None:
            values[metric] = result
    return values


# --------------------------------------------------------------------------- #
# SensorHub
# --------------------------------------------------------------------------- #
class SensorHub:
    """系统状态采样中心：按 ``interval`` 缓存、维护 history 环形缓冲。

    ``sample()`` 在缓存有效期内直接返回上次结果的副本，因此渲染层可以每帧
    调用而不重复触发系统调用；``interval=0`` 时每次调用都重新采样。

    线程安全：内部用 ``RLock`` 保护缓存、缓冲与 NVML 句柄，可从渲染线程与
    UI 线程交替调用。

    首次采样会一次性完成 psutil 导入、DLL 加载与 NVML 初始化（本机约 60 ms），
    之后稳态单次约 3 ms。差值型 metric（``disk_read`` / ``disk_write`` /
    ``net_down`` / ``net_up``）与 ``GetSystemTimes`` 回退路径下的 ``cpu`` 都需要
    两次采样才有值，第一次返回 ``None``（``cpu`` 在 psutil 路径下第一次为 0.0）。
    """

    def __init__(self, interval: float = 0.5, history: int = 120) -> None:
        self._interval = max(0.0, float(interval))
        self._history_size = max(0, int(history))
        self._lock = threading.RLock()
        self._values: dict[str, float | None] | None = None
        self._sources: dict[str, str] = {}
        self._last_sample = 0.0
        self._history: dict[str, deque[float]] = {
            metric: deque(maxlen=self._history_size) for metric in METRIC_IDS
        }
        self._state = _SampleState()

    # ------------------------------------------------------------------ 采样
    def sample(self, force: bool = False) -> dict[str, float | None]:
        """返回全部 metric 的当前值；缓存未过期时复用上次结果。

        :param force: 忽略 ``interval`` 缓存，强制重新采样。
        """
        now = _now()
        with self._lock:
            if (
                not force
                and self._values is not None
                and self._interval > 0.0
                and now - self._last_sample < self._interval
            ):
                return dict(self._values)
            values = _collect(self._state)
            self._values = values
            self._last_sample = now
            self._sources = dict(self._state.sources)
            for metric in METRIC_IDS:
                value = values.get(metric)
                if value is not None:
                    self._history[metric].append(float(value))
            return dict(values)

    def value(self, metric: str) -> float | None:
        """返回单个 metric 的当前值（会触发一次带缓存的采样）。"""
        return self.sample().get(metric)

    def history(self, metric: str) -> list[float]:
        """返回该 metric 的历史值，旧的在前，最多 ``history`` 个点（跳过 None）。"""
        with self._lock:
            buffer = self._history.get(metric)
            return list(buffer) if buffer is not None else []

    def describe(self) -> dict[str, dict]:
        """返回每个 metric 的标签、单位、数据来源与当前是否可用。"""
        with self._lock:
            if self._values is None:
                self.sample()
            values = self._values or {}
            described: dict[str, dict] = {}
            for metric in METRIC_IDS:
                spec = METRIC_SPECS[metric]
                described[metric] = {
                    "label": spec["label"],
                    "unit": spec["unit"],
                    "source": self._sources.get(metric) or _BASE_SOURCES[metric],
                    "available": values.get(metric) is not None,
                }
            return described

    def close(self) -> None:
        """释放 NVML 等系统资源；可重复调用，之后再次采样会重新懒初始化。"""
        with self._lock:
            self._state.nvml.close()


_hub: SensorHub | None = None
_hub_lock = threading.Lock()


def get_hub() -> SensorHub:
    """返回进程级 :class:`SensorHub` 单例（首次调用时懒创建）。"""
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = SensorHub()
        return _hub


def available_metrics() -> tuple[str, ...]:
    """返回当前机器上真实采到值的 metric（会触发一次带缓存的采样）。"""
    values = get_hub().sample()
    return tuple(metric for metric in METRIC_IDS if values.get(metric) is not None)
