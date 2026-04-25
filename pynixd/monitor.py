"""
System resource monitoring and concurrency gating for pynixd stores.
Consolidated monitoring logic for local and remote (SSH) stores.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import structlog

from .exceptions import ResourceExhaustedError
from .psi import (
    CgroupCpuStat,
    CpuUtil,
    MemInfo,
    SystemHealth,
    compute_cpu_util,
    count_cpus_from_proc_stat,
    parse_cpu_max,
    parse_cpu_stat,
    parse_meminfo,
    parse_psi_output,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from .config import PynixdSettings

log = structlog.get_logger(__name__)


class ResourceGate:
    """Async synchronization primitive that gates access based on system pressure."""

    def __init__(self) -> None:
        self.cpu_clear = asyncio.Event()
        self.mem_clear = asyncio.Event()
        self.io_clear = asyncio.Event()
        self.cpu_clear.set()
        self.mem_clear.set()
        self.io_clear.set()

    async def wait_cpu_clear(self, timeout: float = 5.0) -> None:
        """Wait for CPU pressure to drop below threshold."""
        try:
            await asyncio.wait_for(self.cpu_clear.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise ResourceExhaustedError(
                "CPU pressure remains too high after timeout",
            ) from None

    async def wait_mem_clear(self, timeout: float = 5.0) -> None:
        """Wait for Memory pressure to drop below threshold."""
        try:
            await asyncio.wait_for(self.mem_clear.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise ResourceExhaustedError(
                "Memory pressure remains too high after timeout",
            ) from None

    async def wait_io_clear(self, timeout: float = 5.0) -> None:
        """Wait for IO pressure to drop below threshold."""
        try:
            await asyncio.wait_for(self.io_clear.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise ResourceExhaustedError(
                "IO pressure remains too high after timeout",
            ) from None


class ResourceMonitor(ABC):
    """Abstract base for system monitoring."""

    def __init__(self, gate: ResourceGate, settings: PynixdSettings) -> None:
        self.gate = gate
        self.settings = settings
        self.running = False
        self.task: asyncio.Task[None] | None = None
        self.health = SystemHealth()

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
            with contextlib.suppress(asyncio.CancelledError):
                await self.task


class DummyResourceMonitor(ResourceMonitor):
    """Monitor for stores that don't support telemetry. Reports static healthy stats."""

    def __init__(
        self,
        gate: ResourceGate,
        settings: PynixdSettings,
        cpu_util: float = 0.0,
    ) -> None:
        super().__init__(gate, settings)
        self.cpu_util = cpu_util

    async def run(self) -> None:
        log.info("dummy_resource_monitor_started", cpu_util=self.cpu_util)
        self.health = SystemHealth(
            cpu_util=CpuUtil(utilization=self.cpu_util, cores=1.0, throttled_pct=0.0),
            timestamp=time.monotonic(),
        )
        self.gate.cpu_clear.set()
        self.gate.mem_clear.set()
        self.gate.io_clear.set()
        while self.running:
            await asyncio.sleep(60)


class GenericResourcePoller(ResourceMonitor):
    """Polling monitor that works over any read/exists functions (Local/SSH)."""

    def __init__(
        self,
        gate: ResourceGate,
        settings: PynixdSettings,
        read_fn: Callable[[str], Coroutine[None, None, str]],
        exists_fn: Callable[[str], Coroutine[None, None, bool]],
    ) -> None:
        super().__init__(gate, settings)
        self.read_fn = read_fn
        self.exists_fn = exists_fn
        self.interval = 5.0
        self.cpu_stat_prev: CgroupCpuStat | None = None
        self.cpu_cores: float | None = None

    async def run(self) -> None:
        log.info("resource_poller_started")

        # 1. Detect CPU cores once
        try:
            if await self.exists_fn("/sys/fs/cgroup/cpu.max"):
                text = await self.read_fn("/sys/fs/cgroup/cpu.max")
                self.cpu_cores = parse_cpu_max(text)

            if self.cpu_cores is None and await self.exists_fn("/proc/stat"):
                text = await self.read_fn("/proc/stat")
                self.cpu_cores = float(count_cpus_from_proc_stat(text))
        except (PermissionError, FileNotFoundError, OSError):
            log.info("resource_poller_metadata_unavailable", info="cpu_cores")
            self.cpu_cores = 1.0
        except Exception:
            log.debug("cpu_core_detection_failed")
            self.cpu_cores = 1.0

        while self.running:
            try:
                # 2. Read PSI if available
                psi_text = ""
                has_psi = False
                try:
                    parts = []
                    for p in ["cpu", "memory", "io"]:
                        path = f"/proc/pressure/{p}"
                        if await self.exists_fn(path):
                            parts.append(await self.read_fn(path))
                            has_psi = True
                    if has_psi:
                        psi_text = "\n".join(parts)
                except (PermissionError, FileNotFoundError, OSError):
                    # Stop trying PSI if we hit a permission issue
                    log.info("resource_poller_psi_unavailable")
                    has_psi = False
                except Exception:
                    pass

                # 3. Read Memory
                meminfo = None
                try:
                    # Prefer cgroupv2 memory.current/max if available (more accurate for containers)
                    if await self.exists_fn("/sys/fs/cgroup/memory.current"):
                        curr_text = await self.read_fn("/sys/fs/cgroup/memory.current")
                        curr = int(curr_text.strip())
                        max_text = await self.read_fn("/sys/fs/cgroup/memory.max")
                        max_raw = max_text.strip()
                        total = None if max_raw == "max" else int(max_raw)

                        # Fallback to /proc/meminfo for totals if cgroup is unlimited
                        if total is None and await self.exists_fn("/proc/meminfo"):
                            p_mem_text = await self.read_fn("/proc/meminfo")
                            p_mem = parse_meminfo(p_mem_text)
                            total = p_mem.mem_total * 1024  # parse_meminfo returns kB

                        meminfo = MemInfo(
                            mem_total=(total // 1024) if total else 0,
                            mem_available=((total - curr) // 1024) if total else 0,
                        )
                    elif await self.exists_fn("/proc/meminfo"):
                        text = await self.read_fn("/proc/meminfo")
                        meminfo = parse_meminfo(text)
                except (PermissionError, FileNotFoundError, OSError):
                    log.info("resource_poller_memory_unavailable")
                except Exception:
                    pass

                # 4. Read CPU util
                cpu_util = None
                try:
                    if await self.exists_fn("/sys/fs/cgroup/cpu.stat"):
                        stat_text = await self.read_fn("/sys/fs/cgroup/cpu.stat")
                        stat = parse_cpu_stat(stat_text)
                        if self.cpu_stat_prev:
                            cpu_util = compute_cpu_util(
                                self.cpu_stat_prev,
                                stat,
                                self.cpu_cores,
                            )
                        self.cpu_stat_prev = stat
                except (PermissionError, FileNotFoundError, OSError):
                    log.info("resource_poller_cpu_unavailable")
                except Exception:
                    pass

                # 5. Update Health and Gate
                self.health = SystemHealth(
                    psi=parse_psi_output(psi_text) if has_psi else None,
                    meminfo=meminfo,
                    cpu_util=cpu_util,
                    timestamp=time.monotonic(),
                )

                # Reset gate if data is missing (assume healthy)
                if self.health.is_cpu_stressed(
                    self.settings.psi_cpu_threshold,
                    self.settings.max_cpu_util,
                ):
                    self.gate.cpu_clear.clear()
                else:
                    self.gate.cpu_clear.set()

                if self.health.is_mem_stressed(
                    self.settings.psi_mem_threshold,
                    self.settings.min_available_memory_mb * 1024,
                ):
                    self.gate.mem_clear.clear()
                else:
                    self.gate.mem_clear.set()

                if self.health.is_io_stressed(self.settings.psi_io_threshold):
                    self.gate.io_clear.clear()
                else:
                    self.gate.io_clear.set()

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("resource_poller_tick_failed")

            await asyncio.sleep(self.interval)


class LocalPSIMonitor(ResourceMonitor):
    """Specialized Linux cgroupv2 PSI monitor using instant triggers."""

    def __init__(self, gate: ResourceGate, settings: PynixdSettings) -> None:
        super().__init__(gate, settings)
        self.cpu_fd: int | None = None
        self.mem_fd: int | None = None
        self.io_fd: int | None = None

    async def run(self) -> None:
        """Uses loop.add_reader to listen for kernel PSI events."""
        loop = asyncio.get_running_loop()

        cpu_threshold = f"some {int(self.settings.psi_cpu_threshold * 1000)} 1000000"
        mem_threshold = f"some {int(self.settings.psi_mem_threshold * 1000)} 1000000"
        io_threshold = f"some {int(self.settings.psi_io_threshold * 1000)} 1000000"

        try:
            if not os.access("/sys/fs/cgroup/cpu.pressure", os.R_OK | os.W_OK):
                raise PermissionError("Insufficient permissions for PSI triggers")

            self.cpu_fd = os.open(
                "/sys/fs/cgroup/cpu.pressure",
                os.O_RDWR | os.O_NONBLOCK,
            )
            os.write(self.cpu_fd, cpu_threshold.encode())

            self.mem_fd = os.open(
                "/sys/fs/cgroup/memory.pressure",
                os.O_RDWR | os.O_NONBLOCK,
            )
            os.write(self.mem_fd, mem_threshold.encode())

            self.io_fd = os.open(
                "/sys/fs/cgroup/io.pressure",
                os.O_RDWR | os.O_NONBLOCK,
            )
            os.write(self.io_fd, io_threshold.encode())

            def on_cpu_event():
                log.warning("cpu_pressure_event_fired")
                self.gate.cpu_clear.clear()
                loop.call_later(2.0, self.check_pressure_manually)

            def on_mem_event():
                log.warning("mem_pressure_event_fired")
                self.gate.mem_clear.clear()
                loop.call_later(2.0, self.check_pressure_manually)

            def on_io_event():
                log.warning("io_pressure_event_fired")
                self.gate.io_clear.clear()
                loop.call_later(2.0, self.check_pressure_manually)

            loop.add_reader(self.cpu_fd, on_cpu_event)
            loop.add_reader(self.mem_fd, on_mem_event)
            loop.add_reader(self.io_fd, on_io_event)

            log.info(
                "psi_notifier_started",
                cpu=cpu_threshold,
                mem=mem_threshold,
                io=io_threshold,
            )

            while self.running:
                await asyncio.sleep(10)
                self.check_pressure_manually()

        except Exception as e:
            log.info("psi_notifier_unavailable", reason=str(e))

            # Local fallback functions
            async def local_read(path: str) -> str:
                with open(path) as f:
                    return f.read()

            async def local_exists(path: str) -> bool:
                return os.path.exists(path)

            # Fallback to generic poller
            poller = GenericResourcePoller(
                self.gate,
                self.settings,
                local_read,
                local_exists,
            )
            await poller.run()
        finally:
            if self.cpu_fd:
                loop.remove_reader(self.cpu_fd)
                os.close(self.cpu_fd)
            if self.mem_fd:
                loop.remove_reader(self.mem_fd)
                os.close(self.mem_fd)
            if self.io_fd:
                loop.remove_reader(self.io_fd)
                os.close(self.io_fd)

    def check_pressure_manually(self) -> None:
        """Update health state via manual read."""
        try:
            parts = []
            for p in ["cpu", "memory", "io"]:
                with open(f"/proc/pressure/{p}") as f:
                    parts.append(f.read())

            snap = parse_psi_output("\n".join(parts))
            self.health.psi = snap

            # We also need meminfo for absolute threshold check
            with open("/proc/meminfo") as f:
                self.health.meminfo = parse_meminfo(f.read())

            self.health.timestamp = time.monotonic()

            if snap.cpu.some_avg10 < self.settings.psi_cpu_threshold:
                self.gate.cpu_clear.set()

            if (
                snap.memory.some_avg10 < self.settings.psi_mem_threshold
                and self.health.meminfo.mem_available >= self.settings.min_available_memory_mb * 1024
            ):
                self.gate.mem_clear.set()

            if snap.io.some_avg10 < self.settings.psi_io_threshold:
                self.gate.io_clear.set()

        except Exception:
            log.exception("psi_manual_check_failed")


def create_monitor(gate: ResourceGate, settings: PynixdSettings) -> ResourceMonitor:
    """Factory to create the best available local monitor."""

    async def local_read(path: str) -> str:
        with open(path) as f:
            return f.read()

    async def local_exists(path: str) -> bool:
        return os.path.exists(path)

    if os.path.exists("/sys/fs/cgroup/cpu.pressure"):
        return LocalPSIMonitor(gate, settings)
    return GenericResourcePoller(gate, settings, local_read, local_exists)
