"""
Nix daemon stderr message types.

Every daemon operation wraps its response in a stderr stream:
    [StderrMsg ...] → StderrLast → <response payload>

These classes parse messages from the wire and serialize them back,
making _forward_stderr and _drain_stderr trivial consumers.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import ClassVar

import structlog

from . import wire
from .exceptions import BackendError
from .wire import NixReader, NixWriter

log = structlog.get_logger(__name__)

# ── Field type (used in StartActivity and Result) ─────────────────

Field = int | str


async def read_fields(r: NixReader) -> list[Field]:
    n = await r.read_uint64()
    fields: list[Field] = []
    for _ in range(n):
        ftype = await r.read_uint64()
        if ftype == 0:
            fields.append(await r.read_uint64())
        else:
            fields.append(await r.read_string())
    return fields


def write_fields(w: NixWriter, fields: list[Field]) -> None:
    w.write_uint64(len(fields))
    for f in fields:
        if isinstance(f, int):
            w.write_uint64s([0, f])
        else:
            w.write_uint64(1)
            w.write_string(f)


# ── Stderr message types ──────────────────────────────────────────


@dataclass(slots=True)
class StderrNext:
    """STDERR_NEXT — a log line from the daemon."""

    code: ClassVar[int] = wire.STDERR_NEXT
    text: str = ""

    async def from_reader(self, r: NixReader) -> StderrNext:
        self.text = await r.read_string()
        return self

    def to_writer(self, w: NixWriter) -> None:
        w.write_uint64(self.code)
        w.write_string(self.text)


@dataclass(slots=True)
class StderrStartActivity:
    """STDERR_START_ACTIVITY — begin a tracked activity."""

    code: ClassVar[int] = wire.STDERR_START_ACTIVITY
    act_id: int = 0
    level: int = 0
    type: int = 0
    text: str = ""
    fields: list[Field] = field(default_factory=list)
    parent: int = 0

    async def from_reader(self, r: NixReader) -> StderrStartActivity:
        self.act_id = await r.read_uint64()
        self.level = await r.read_uint64()
        self.type = await r.read_uint64()
        self.text = await r.read_string()
        self.fields = await read_fields(r)
        self.parent = await r.read_uint64()
        return self

    def to_writer(self, w: NixWriter) -> None:
        w.write_uint64(self.code)
        w.write_uint64(self.act_id)
        w.write_uint64(self.level)
        w.write_uint64(self.type)
        w.write_string(self.text)
        write_fields(w, self.fields)
        w.write_uint64(self.parent)


@dataclass(slots=True)
class StderrStopActivity:
    """STDERR_STOP_ACTIVITY — end a tracked activity."""

    code: ClassVar[int] = wire.STDERR_STOP_ACTIVITY
    act_id: int = 0

    async def from_reader(self, r: NixReader) -> StderrStopActivity:
        self.act_id = await r.read_uint64()
        return self

    def to_writer(self, w: NixWriter) -> None:
        w.write_uint64(self.code)
        w.write_uint64(self.act_id)


@dataclass(slots=True)
class StderrResult:
    """STDERR_RESULT — result data for an activity."""

    code: ClassVar[int] = wire.STDERR_RESULT
    act_id: int = 0
    result_type: int = 0
    fields: list[Field] = field(default_factory=list)

    async def from_reader(self, r: NixReader) -> StderrResult:
        self.act_id = await r.read_uint64()
        self.result_type = await r.read_uint64()
        self.fields = await read_fields(r)
        return self

    def to_writer(self, w: NixWriter) -> None:
        w.write_uint64(self.code)
        w.write_uint64(self.act_id)
        w.write_uint64(self.result_type)
        write_fields(w, self.fields)


@dataclass(slots=True)
class StderrError:
    """STDERR_ERROR — the daemon is reporting an error."""

    code: ClassVar[int] = wire.STDERR_ERROR
    error_type: str = ""
    level: int = 0
    name: str = ""
    msg: str = ""
    have_pos: int = 0
    traces: list[tuple[int, str]] = field(default_factory=list)

    async def from_reader(self, r: NixReader) -> StderrError:
        self.error_type = await r.read_string()
        self.level = await r.read_uint64()
        self.name = await r.read_string()
        self.msg = await r.read_string()
        self.have_pos = await r.read_uint64()
        n = await r.read_uint64()
        self.traces = []
        for _ in range(n):
            t_pos = await r.read_uint64()
            t_hint = await r.read_string()
            self.traces.append((t_pos, t_hint))
        return self

    def to_writer(self, w: NixWriter) -> None:
        w.write_uint64(self.code)
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
    Stops after StderrError — the daemon sends no STDERR_LAST after an error.
    """
    unknown_streak = 0
    while True:
        msg_type = await r.read_uint64()

        if msg_type == wire.STDERR_LAST:
            return

        parser = _PARSERS.get(msg_type)
        if parser is None:
            unknown_streak += 1
            log.warning("stderr_unknown_msg_type", msg_type=msg_type)
            if unknown_streak >= _MAX_UNKNOWN_MSG_TYPES:
                raise ConnectionError(
                    f"Protocol desync: {unknown_streak} consecutive "
                    f"unknown stderr msg_types (last: 0x{msg_type:x})"
                )
            continue

        unknown_streak = 0
        msg = await parser().from_reader(r)
        yield msg
        if isinstance(msg, StderrError):
            return


async def drain(
    r: NixReader, raise_on_error: bool = True, conn_id: str = "unknown"
) -> StderrError | None:
    """Read and discard all stderr messages until STDERR_LAST.

    Returns the last StderrError if found, None otherwise.
    """
    last_error: StderrError | None = None
    async for msg in read_stream(r):
        if isinstance(msg, StderrError):
            log.warning(
                "daemon_error_stderr_stream",
                conn_id=conn_id,
                error_type=msg.error_type,
                error_msg=msg.msg,
            )
            last_error = msg
            if raise_on_error:
                raise BackendError(f"Backend error: {msg.msg}")
    return last_error


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
