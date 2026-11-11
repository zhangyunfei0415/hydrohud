"""系统状态采样：CPU 忙闲比例与内存占用比例。

仅在渲染线程中调用，Windows 下通过 ctypes 调用 kernel32 的
``GetSystemTimes`` / ``GlobalMemoryStatusEx``，不依赖第三方库。
非 Windows 平台返回 ``(0.0, 0.0)``，渲染层会显示 0%。
"""

# MIT License
#
# Copyright (c) 2026 HydroHUD contributors
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
import sys
import threading
import time

__all__ = ["CpuSampler"]


class _FILETIME(ctypes.Structure):
    """Windows FILETIME（100 ns 为单位）。"""

    _fields_ = [
        ("dwLowDateTime", ctypes.c_ulong),
        ("dwHighDateTime", ctypes.c_ulong),
    ]

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


class CpuSampler:
    """CPU / 内存占用采样器，内部缓存 ``interval`` 秒内的结果。

    第一次调用没有历史基线，CPU 返回 0.0；内存可以直接取到真实值。
    多次调用间隔小于 ``interval`` 时复用上次结果，避免每帧都做系统调用。
    """

    def __init__(self, interval: float = 0.25) -> None:
        self._interval = max(0.0, float(interval))
        self._lock = threading.Lock()
        self._last_time = 0.0
        self._last_value: tuple[float, float] = (0.0, 0.0)
        self._prev_idle = 0
        self._prev_total = 0
        self._fallback = sys.platform != "win32"

    def sample(self) -> tuple[float, float]:
        """返回 ``(cpu 忙闲 0-1, 内存占用 0-1)``。

        第一次调用返回 ``(0.0, 内存占用)``；此后按调用间隔计算 CPU 占用。
        """
        now = time.monotonic()
        with self._lock:
            if self._last_time and now - self._last_time < self._interval:
                return self._last_value
            self._last_time = now
            if self._fallback:
                self._last_value = (0.0, 0.0)
                return self._last_value
            cpu = self._read_cpu()
            ram = self._read_ram()
            self._last_value = (cpu, ram)
            return self._last_value

    # ------------------------------------------------------------------ 内部
    def _read_cpu(self) -> float:
        """用两次 GetSystemTimes 的差值计算 CPU 忙闲比例。"""
        idle = _FILETIME()
        kernel = _FILETIME()
        user = _FILETIME()
        try:
            ok = ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            )
        except (AttributeError, OSError):
            return 0.0
        if not ok:
            return 0.0
        idle_v = idle.value()
        # kernel 时间包含 idle，所以总时间 = kernel + user。
        total_v = kernel.value() + user.value()
        prev_idle, prev_total = self._prev_idle, self._prev_total
        self._prev_idle, self._prev_total = idle_v, total_v
        if not prev_total:
            return 0.0
        delta_total = total_v - prev_total
        delta_idle = idle_v - prev_idle
        if delta_total <= 0 or delta_idle < 0:
            return 0.0
        busy = 1.0 - delta_idle / delta_total
        return _clamp01(busy)

    def _read_ram(self) -> float:
        """读取物理内存占用比例。"""
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        try:
            ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        except (AttributeError, OSError):
            return 0.0
        if not ok:
            return 0.0
        return _clamp01(status.dwMemoryLoad / 100.0)


def _clamp01(value: float) -> float:
    if value != value:  # NaN
        return 0.0
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else float(value)
