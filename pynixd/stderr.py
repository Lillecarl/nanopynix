"""
Nix daemon stderr message types.

Every daemon operation wraps its response in a stderr stream:
    [StderrMsg ...] → StderrLast → <response payload>

These classes parse messages from the wire and serialize them back,
making _forward_stderr and _drain_stderr trivial consumers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from . import wire
from .exceptions import BackendError
from .wire import NixReader, NixWriter

log: logging.Logger = logging.getLogger(__name__)

# ── Field type (used in StartActivity and Result) ─────────────────

Field = int | str


async def _read_fields(r: NixReader) -> list[Field]:
    n = await r.read_uint64()
    fields: list[Field] = []
    for _ in range(n):
        ftype = await r.read_uint64()
        if ftype == 0:
            fields.append(await r.read_uint64())
        else:
            fields.append(await r.read_string())
    return fields


def _write_fields(w: NixWriter, fields: list[Field]) -> None:
    w.write_uint64(len(fields))
    for f in fields:
        if isinstance(f, int):
            w.write_uint64(0)
            w.write_uint64(f)
        else:
            w.write_uint64(1)
            w.write_string(f)


# ── Stderr message types ──────────────────────────────────────────


@dataclass(slots=True)
class StderrNext:
    """STDERR_NEXT — a log line from the daemon."""

    text: str

    @classmethod
    async def from_reader(cls, r: NixReader) -> StderrNext:
        return cls(text=await r.read_string())

    def to_writer(self, w: NixWriter) -> None:
        w.write_uint64(wire.STDERR_NEXT)
        w.write_string(self.text)


@dataclass(slots=True)
class StderrStartActivity:
    """STDERR_START_ACTIVITY — begin a tracked activity."""

    act_id: int
    level: int
    type: int
    text: str
    fields: list[Field]
    parent: int

    @classmethod
    async def from_reader(cls, r: NixReader) -> StderrStartActivity:
        return cls(
            act_id=await r.read_uint64(),
            level=await r.read_uint64(),
            type=await r.read_uint64(),
            text=await r.read_string(),
            fields=await _read_fields(r),
            parent=await r.read_uint64(),
        )

    def to_writer(self, w: NixWriter) -> None:
        w.write_uint64(wire.STDERR_START_ACTIVITY)
        w.write_uint64(self.act_id)
        w.write_uint64(self.level)
        w.write_uint64(self.type)
        w.write_string(self.text)
        _write_fields(w, self.fields)
        w.write_uint64(self.parent)


@dataclass(slots=True)
class StderrStopActivity:
    """STDERR_STOP_ACTIVITY — end a tracked activity."""

    act_id: int

    @classmethod
    async def from_reader(cls, r: NixReader) -> StderrStopActivity:
        return cls(act_id=await r.read_uint64())

    def to_writer(self, w: NixWriter) -> None:
        w.write_uint64(wire.STDERR_STOP_ACTIVITY)
        w.write_uint64(self.act_id)


@dataclass(slots=True)
class StderrResult:
    """STDERR_RESULT — result data for an activity."""

    act_id: int
    result_type: int
    fields: list[Field]

    @classmethod
    async def from_reader(cls, r: NixReader) -> StderrResult:
        return cls(
            act_id=await r.read_uint64(),
            result_type=await r.read_uint64(),
            fields=await _read_fields(r),
        )

    def to_writer(self, w: NixWriter) -> None:
        w.write_uint64(wire.STDERR_RESULT)
        w.write_uint64(self.act_id)
        w.write_uint64(self.result_type)
        _write_fields(w, self.fields)


@dataclass(slots=True)
class StderrError:
    """STDERR_ERROR — the daemon is reporting an error."""

    error_type: str
    level: int
    name: str
    msg: str
    have_pos: int
    traces: list[tuple[int, str]]

    @classmethod
    async def from_reader(cls, r: NixReader) -> StderrError:
        error_type = await r.read_string()
        level = await r.read_uint64()
        name = await r.read_string()
        msg = await r.read_string()
        have_pos = await r.read_uint64()
        n = await r.read_uint64()
        traces = []
        for _ in range(n):
            t_pos = await r.read_uint64()
            t_hint = await r.read_string()
            traces.append((t_pos, t_hint))
        return cls(
            error_type=error_type,
            level=level,
            name=name,
            msg=msg,
            have_pos=have_pos,
            traces=traces,
        )

    def to_writer(self, w: NixWriter) -> None:
        w.write_uint64(wire.STDERR_ERROR)
        w.write_string(self.error_type)
        w.write_uint64(self.level)
        w.write_string(self.name)
        w.write_string(self.msg)
        w.write_uint64(self.have_pos)
        w.write_uint64(len(self.traces))
        for t_pos, t_hint in self.traces:
            w.write_uint64(t_pos)
            w.write_string(t_hint)


# Union of all stderr message types
StderrMsg = (
    StderrNext | StderrStartActivity | StderrStopActivity | StderrResult | StderrError
)

# Sentinel returned when the stream ends with STDERR_LAST
LAST = object()

# ── Parsers mapping msg_type → class ─────────────────────────────

_PARSERS: dict[int, type[StderrMsg]] = {
    wire.STDERR_NEXT: StderrNext,
    wire.STDERR_START_ACTIVITY: StderrStartActivity,
    wire.STDERR_STOP_ACTIVITY: StderrStopActivity,
    wire.STDERR_RESULT: StderrResult,
    wire.STDERR_ERROR: StderrError,
}

_TYPE_NAMES: dict[int, str] = {
    wire.STDERR_NEXT: "STDERR_NEXT",
    wire.STDERR_LAST: "STDERR_LAST",
    wire.STDERR_ERROR: "STDERR_ERROR",
    wire.STDERR_START_ACTIVITY: "STDERR_START_ACTIVITY",
    wire.STDERR_STOP_ACTIVITY: "STDERR_STOP_ACTIVITY",
    wire.STDERR_RESULT: "STDERR_RESULT",
}


def msg_type_name(code: int) -> str:
    return _TYPE_NAMES.get(code, f"UNKNOWN(0x{code:x})")


# ── Stream reader ────────────────────────────────────────────────


_MAX_UNKNOWN_MSG_TYPES = 3


async def read_stream(r: NixReader) -> AsyncIterator[StderrMsg]:
    """Yield stderr messages until STDERR_LAST.

    The caller reads the response payload after this iterator is exhausted.
    Raises if too many consecutive unknown msg_types are seen (protocol desync).
    """
    unknown_streak = 0
    while True:
        msg_type = await r.read_uint64()

        if msg_type == wire.STDERR_LAST:
            return

        parser = _PARSERS.get(msg_type)
        if parser is None:
            unknown_streak += 1
            log.warning("stderr: unknown msg_type 0x%x", msg_type)
            if unknown_streak >= _MAX_UNKNOWN_MSG_TYPES:
                raise ConnectionError(
                    f"Protocol desync: {unknown_streak} consecutive "
                    f"unknown stderr msg_types (last: 0x{msg_type:x})"
                )
            continue

        unknown_streak = 0
        msg = await parser.from_reader(r)
        yield msg


async def drain(r: NixReader, raise_on_error: bool = True) -> None:
    """Read and discard all stderr messages until STDERR_LAST."""
    async for msg in read_stream(r):
        if isinstance(msg, StderrError):
            log.warning("Daemon error during drain: [%s] %s", msg.error_type, msg.msg)
            if raise_on_error:
                raise BackendError(f"Daemon error: {msg.msg}")


async def collect(
    r: NixReader,
    queue: asyncio.Queue[StderrMsg | None],
) -> StderrError | None:
    """Read stderr from backend and put messages on a queue.

    Reads until STDERR_LAST. Non-error messages are put on the queue
    for the drain task to write to the client. StderrError is returned
    (not queued) so the caller can handle it — e.g. retry on another
    backend, record health info, etc.

    Args:
        r: Backend reader (source of stderr messages)
        queue: Queue for the client drain task to consume

    Returns:
        StderrError if the backend reported an error, else None.
    """
    async for msg in read_stream(r):
        if isinstance(msg, StderrError):
            # Forward to client so they see the error, but return it
            # to the caller for health/retry decisions.
            queue.put_nowait(msg)
            return msg
        queue.put_nowait(msg)
    return None
