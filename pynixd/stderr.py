"""
Nix daemon stderr message types.

Every daemon operation wraps its response in a stderr stream:
    [StderrMsg ...] → StderrLast → <response payload>

These classes parse messages from the wire and serialize them back,
making _forward_stderr and _drain_stderr trivial consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from . import constants
from .exceptions import BackendError
from .types.protocol import ActivityType, FieldType, ResultType, Verbosity

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .types.context import ReadContext, WriteContext
    from .wire import NixReader, NixWriter

log = structlog.get_logger(__name__)

# ── Field type (used in StartActivity and Result) ─────────────────

Field = int | str


async def read_fields(r: NixReader) -> list[Field]:
    n = await r.read_uint64()
    fields: list[Field] = []
    for _ in range(n):
        ftype = FieldType(await r.read_uint64())
        if ftype == FieldType.INT:
            fields.append(await r.read_uint64())
        else:
            fields.append(await r.read_string())
    return fields


def write_fields(w: NixWriter, fields: list[Field]) -> None:
    w.write_uint64(len(fields))
    for f in fields:
        if isinstance(f, int):
            w.write_uint64(FieldType.INT)
            w.write_uint64(f)
        else:
            w.write_uint64(FieldType.STRING)
            w.write_string(f)


# ── Stderr message types ──────────────────────────────────────────


@dataclass(slots=True)
class StderrNext:
    """STDERR_NEXT — a log line from the daemon."""

    code: ClassVar[int] = constants.STDERR_NEXT
    text: str = ""

    @classmethod
    async def from_reader(cls, r: NixReader) -> StderrNext:
        obj = cls.__new__(cls)
        obj.text = await r.read_string()
        return obj

    def to_writer(self, w: NixWriter) -> None:
        w.write_uint64(self.code)
        w.write_string(self.text)


@dataclass(slots=True)
class StderrStartActivity:
    """STDERR_START_ACTIVITY — begin a tracked activity."""

    code: ClassVar[int] = constants.STDERR_START_ACTIVITY
    act_id: int = 0
    level: Verbosity = Verbosity.NOTICE
    type: ActivityType = ActivityType.UNKNOWN
    text: str = ""
    fields: list[Field] = field(default_factory=list)
    parent: int = 0

    @classmethod
    async def from_reader(cls, r: NixReader) -> StderrStartActivity:
        obj = cls.__new__(cls)
        obj.act_id = await r.read_uint64()
        obj.level = Verbosity(await r.read_uint64())
        obj.type = ActivityType(await r.read_uint64())
        obj.text = await r.read_string()
        obj.fields = await read_fields(r)
        obj.parent = await r.read_uint64()
        return obj

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

    code: ClassVar[int] = constants.STDERR_STOP_ACTIVITY
    act_id: int = 0

    @classmethod
    async def from_reader(cls, r: NixReader) -> StderrStopActivity:
        obj = cls.__new__(cls)
        obj.act_id = await r.read_uint64()
        return obj

    def to_writer(self, w: NixWriter) -> None:
        w.write_uint64(self.code)
        w.write_uint64(self.act_id)


@dataclass(slots=True)
class StderrResult:
    """STDERR_RESULT — result data for an activity."""

    code: ClassVar[int] = constants.STDERR_RESULT
    act_id: int = 0
    result_type: ResultType = ResultType.PROGRESS
    fields: list[Field] = field(default_factory=list)

    @classmethod
    async def from_reader(cls, r: NixReader) -> StderrResult:
        obj = cls.__new__(cls)
        obj.act_id = await r.read_uint64()
        obj.result_type = ResultType(await r.read_uint64())
        obj.fields = await read_fields(r)
        return obj

    def to_writer(self, w: NixWriter) -> None:
        w.write_uint64(self.code)
        w.write_uint64(self.act_id)
        w.write_uint64(self.result_type)
        write_fields(w, self.fields)


@dataclass(slots=True)
class StderrError:
    """STDERR_ERROR — the daemon is reporting an error."""

    code: ClassVar[int] = constants.STDERR_ERROR
    error_type: str = ""
    level: Verbosity = Verbosity.ERROR
    name: str = ""
    msg: str = ""
    have_pos: int = 0
    traces: list[tuple[int, str]] = field(default_factory=list)

    @classmethod
    async def from_reader(cls, r: NixReader) -> StderrError:
        obj = cls.__new__(cls)
        obj.error_type = await r.read_string()
        obj.level = Verbosity(await r.read_uint64())
        obj.name = await r.read_string()
        obj.msg = await r.read_string()
        obj.have_pos = await r.read_uint64()
        n = await r.read_uint64()
        obj.traces = []
        for _ in range(n):
            t_pos = await r.read_uint64()
            t_hint = await r.read_string()
            obj.traces.append((t_pos, t_hint))
        return obj

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
StderrMsg = StderrNext | StderrStartActivity | StderrStopActivity | StderrResult | StderrError


@dataclass
class OperationLogs:
    """Container for stderr messages from an operation."""

    messages: list[StderrMsg] = field(default_factory=list)

    @property
    def error(self) -> StderrError | None:
        for msg in self.messages:
            if isinstance(msg, StderrError):
                return msg
        return None

    @property
    def has_error(self) -> bool:
        return self.error is not None

    def __bool__(self) -> bool:
        return not self.has_error

    def add(self, msg: StderrMsg) -> None:
        self.messages.append(msg)

    def serialize(self, ctx: WriteContext) -> None:
        for msg in self.messages:
            msg.to_writer(ctx.writer)
        ctx.writer.write_uint64(constants.STDERR_LAST)

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.messages = []
        async for msg in read_stream(ctx.reader):
            if ctx.client:
                await ctx.client.send(msg)
            if ctx.buffer_logs:
                obj.add(msg)
            if isinstance(msg, StderrError):
                raise BackendError(f"Daemon error ({msg.error_type}): {msg.msg}")
        return obj


# ── Parsers mapping msg_type → class ─────────────────────────────

_PARSERS: dict[int, type[StderrMsg]] = {
    constants.STDERR_NEXT: StderrNext,
    constants.STDERR_START_ACTIVITY: StderrStartActivity,
    constants.STDERR_STOP_ACTIVITY: StderrStopActivity,
    constants.STDERR_RESULT: StderrResult,
    constants.STDERR_ERROR: StderrError,
}


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

        if msg_type == constants.STDERR_LAST:
            return

        parser = _PARSERS.get(msg_type)
        if parser is None:
            unknown_streak += 1
            log.warning("stderr_unknown_msg_type", msg_type=msg_type)
            if unknown_streak >= _MAX_UNKNOWN_MSG_TYPES:
                raise ConnectionError(
                    f"Protocol desync: {unknown_streak} consecutive unknown stderr msg_types (last: 0x{msg_type:x})",
                )
            continue

        unknown_streak = 0
        msg = await parser.from_reader(r)
        yield msg
        if isinstance(msg, StderrError):
            return


async def drain(
    r: NixReader,
    raise_on_error: bool = True,
    conn_id: str = "unknown",
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
