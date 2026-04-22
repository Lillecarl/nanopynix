"""
System resource monitoring and concurrency gating for LocalSocketStore.
"""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import structlog

from .exceptions import ResourceExhaustedError
from .psi import (
    CgroupCpuStat,
    parse_psi_output,
    parse_meminfo,
    parse_cpu_stat,
    parse_cpu_max,
    compute_cpu_util,
    count_cpus_from_proc_stat,
)

if TYPE_CHECKING:
    from .config import PynixdSettings

log = structlog.get_logger(__name__)


class ResourceGate:
    """Async synchronization primitive that gates access based on system pressure."""

    def __init__(self) -> None:
        self.cpu_clear = asyncio.Event()
        self.mem_clear = asyncio.Event()
        self.cpu_clear.set()
        self.mem_clear.set()

    async def wait_cpu_clear(self, timeout: float = 5.0) -> None:
        """Wait for CPU pressure to drop below threshold."""
        try:
            await asyncio.wait_for(self.cpu_clear.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise ResourceExhaustedError("CPU pressure remains too high after timeout")

    async def wait_mem_clear(self, timeout: float = 5.0) -> None:
        """Wait for Memory pressure to drop below threshold."""
        try:
            await asyncio.wait_for(self.mem_clear.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise ResourceExhaustedError(
                "Memory pressure remains too high after timeout"
            )


class ResourceMonitor(ABC):
    """Abstract base for local system monitoring."""

    def __init__(self, gate: ResourceGate, settings: PynixdSettings) -> None:
        self.gate = gate
        self.settings = settings
        self.running = False
        self.task: asyncio.Task[None] | None = None

    @abstractmethod
    async def run(self) -> None:
        """Monitor loop."""
        ...

    def start(self) -> None:
        if not self.running:
            self.running = True
            self.task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


class LocalPSIMonitor(ResourceMonitor):
    """Linux cgroupv2 PSI monitor using instant triggers/notifications."""

    def __init__(self, gate: ResourceGate, settings: PynixdSettings) -> None:
        super().__init__(gate, settings)
        self.cpu_fd: int | None = None
        self.mem_fd: int | None = None

    async def run(self) -> None:
        """Uses loop.add_reader to listen for kernel PSI events."""
        loop = asyncio.get_running_loop()

        # thresholds are 'some 150000 1000000' (150ms over 1s)
        # we'll use a conservative default or from settings
        cpu_threshold = f"some {int(self.settings.psi_cpu_threshold * 1000)} 1000000"
        mem_threshold = f"some {int(self.settings.psi_mem_threshold * 1000)} 1000000"

        try:
            # Check for existence and permission before opening
            if not os.access("/sys/fs/cgroup/cpu.pressure", os.R_OK | os.W_OK):
                raise PermissionError("Insufficient permissions for PSI triggers")

            self.cpu_fd = os.open(
                "/sys/fs/cgroup/cpu.pressure", os.O_RDWR | os.O_NONBLOCK
            )
            os.write(self.cpu_fd, cpu_threshold.encode())

            self.mem_fd = os.open(
                "/sys/fs/cgroup/memory.pressure", os.O_RDWR | os.O_NONBLOCK
            )
            os.write(self.mem_fd, mem_threshold.encode())

            def on_cpu_event():
                log.warning("cpu_pressure_event_fired")
                self.gate.cpu_clear.clear()
                # Schedule a re-check after a cooldown
                loop.call_later(2.0, self.check_pressure_manually)

            def on_mem_event():
                log.warning("mem_pressure_event_fired")
                self.gate.mem_clear.clear()
                loop.call_later(2.0, self.check_pressure_manually)

            loop.add_reader(self.cpu_fd, on_cpu_event)
            loop.add_reader(self.mem_fd, on_mem_event)

            log.info("psi_notifier_started", cpu=cpu_threshold, mem=mem_threshold)

            while self.running:
                # Notifications are event-driven, but we re-verify periodically
                await asyncio.sleep(10)
                self.check_pressure_manually()

        except Exception as e:
            log.info("psi_notifier_unavailable", reason=str(e))
            # Fallback to polling if triggers fail (e.g. older kernels)
            await self._fallback_polling()
        finally:
            if self.cpu_fd:
                loop.remove_reader(self.cpu_fd)
                os.close(self.cpu_fd)
            if self.mem_fd:
                loop.remove_reader(self.mem_fd)
                os.close(self.mem_fd)

    def check_pressure_manually(self) -> None:
        """Read PSI files directly to see if pressure has subsided."""
        try:
            with open("/proc/pressure/cpu") as f:
                cpu_text = f.read()
            with open("/proc/pressure/memory") as f:
                mem_text = f.read()
            with open("/proc/pressure/io") as f:
                io_text = f.read()

            snap = parse_psi_output(f"{cpu_text}\n{mem_text}\n{io_text}")

            # Simple threshold check on 10s averages
            if snap.cpu.some_avg10 < self.settings.psi_cpu_threshold:
                if not self.gate.cpu_clear.is_set():
                    log.info("cpu_pressure_subsided", avg10=snap.cpu.some_avg10)
                self.gate.cpu_clear.set()

            if snap.memory.some_avg10 < self.settings.psi_mem_threshold:
                if not self.gate.mem_clear.is_set():
                    log.info("mem_pressure_subsided", avg10=snap.memory.some_avg10)
                self.gate.mem_clear.set()

        except Exception:
            log.exception("psi_manual_check_failed")

    async def _fallback_polling(self) -> None:
        """Periodic polling fallback if event triggers are unavailable."""
        log.info("psi_falling_back_to_polling")
        while self.running:
            self.check_pressure_manually()
            await asyncio.sleep(5.0)


class ProcfsMonitor(ResourceMonitor):
    """Fallback monitor using /proc/meminfo and /proc/stat (no PSI)."""

    async def run(self) -> None:
        log.info("procfs_monitor_started")
        cpu_stat_prev: CgroupCpuStat | None = None

        # Determine CPU count once
        cores = 1.0
        try:
            with open("/sys/fs/cgroup/cpu.max") as f:
                c = parse_cpu_max(f.read())
                if c:
                    cores = c
            if cores == 1.0:
                with open("/proc/stat") as f:
                    cores = float(count_cpus_from_proc_stat(f.read()))
        except Exception:
            pass

        while self.running:
            try:
                # 1. Memory
                with open("/proc/meminfo") as f:
                    mem = parse_meminfo(f.read())

                # Gate if free memory is below threshold (e.g. 512MB)
                if mem.mem_available < self.settings.min_free_mem_kb:
                    self.gate.mem_clear.clear()
                    log.warning("low_memory_detected", available_mb=mem.available_mb)
                else:
                    self.gate.mem_clear.set()

                # 2. CPU
                with open("/sys/fs/cgroup/cpu.stat") as f:
                    stat = parse_cpu_stat(f.read())

                if cpu_stat_prev:
                    util = compute_cpu_util(cpu_stat_prev, stat, cores)
                    if util and util.utilization > self.settings.max_cpu_util:
                        self.gate.cpu_clear.clear()
                        log.warning(
                            "high_cpu_utilization_detected", util=util.utilization
                        )
                    else:
                        self.gate.cpu_clear.set()

                cpu_stat_prev = stat
            except Exception:
                log.exception("procfs_monitor_tick_failed")

            await asyncio.sleep(5.0)


def create_monitor(gate: ResourceGate, settings: PynixdSettings) -> ResourceMonitor:
    """Factory to create the best available local monitor."""
    if os.path.exists("/proc/pressure/cpu"):
        return LocalPSIMonitor(gate, settings)
    return ProcfsMonitor(gate, settings)
