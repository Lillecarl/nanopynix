"""
Custom profiling operations for pynixd.

These are pynixd-specific extensions to the Nix daemon protocol,
not part of the standard Nix protocol. They allow clients to
trigger pyinstrument profiling of pynixd's execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

import pyinstrument

if TYPE_CHECKING:
    from ..proxy import DaemonProxy

from ..protocol import Op
from .base import (
    EmptyRequest,
    EmptyResponse,
    OpResponse,
    SingleStringResponse,
)

# ── Global profiler ───────────────────────────────────────────────


_profiler: pyinstrument.Profiler | None = None


# ── StartProfiling ──────────────────────────────────────────────────


@dataclass
class StartProfilingResponse(EmptyResponse):
    pass


@dataclass
class StartProfilingRequest(EmptyRequest[StartProfilingResponse]):
    op: ClassVar[int] = Op.PynixdStartProfiling
    response_type: ClassVar[type[OpResponse]] = StartProfilingResponse

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> StartProfilingResponse:
        global _profiler
        _profiler = pyinstrument.Profiler(async_mode="enabled")
        _profiler.start()
        return StartProfilingResponse()


# ── StopProfiling ──────────────────────────────────────────────────


@dataclass
class StopProfilingResponse(SingleStringResponse):
    """Response containing the pyinstrument profile output as text."""

    async def to_writer(self, writer, version):
        global _profiler
        if _profiler is not None:
            _profiler.stop()
            self.value = _profiler.output_text(unicode=True, color=False, show_all=True)
            _profiler = None
        else:
            self.value = ""
        await super().to_writer(writer, version)


@dataclass
class StopProfilingRequest(EmptyRequest[StopProfilingResponse]):
    op: ClassVar[int] = Op.PynixdStopProfiling
    response_type: ClassVar[type[OpResponse]] = StopProfilingResponse

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> StopProfilingResponse:  # noqa: ARG003
        return StopProfilingResponse()
