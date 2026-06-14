"""WireModel types for the Nix daemon stderr/log stream.

The stderr stream uses a tagged-union wire format::

    [uint64 code][message body][uint64 code][message body]...[uint64 STDERR_LAST]

Each message type has a ``code`` field that is written on the wire
(``serialize=True``) but skipped during body reading (``deserialize=False``)
because ``WireLogs.from_reader`` dispatches on code before delegating to
the individual message's ``from_reader``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field as PydanticField

from ..constants import (
    STDERR_ERROR,
    STDERR_LAST,
    STDERR_NEXT,
    STDERR_RESULT,
    STDERR_START_ACTIVITY,
    STDERR_STOP_ACTIVITY,
)
from ..types.protocol import FieldType
from .wire_message import WireField, WireModel

if TYPE_CHECKING:
    from typing import Any

    from ..types.context import ReadContext, WriteContext


# ── Helper: nested trace element ────────────────────────────────────


class TraceLine(WireModel):
    """A single trace entry inside ``LogError``."""

    pos: int = 0
    hint: str = ""


# ── Helper: tagged-union field inside activities ─────────────────────


class ActivityField(WireModel):
    """A single typed field in ``LogStartActivity`` or ``LogResult``.

    Wire format: ``uint64 type`` then either ``uint64 valint`` (if INT)
    or ``string valstr`` (if STRING).
    """

    type: FieldType = FieldType.INT
    valint: int | None = WireField(default=None, wire_depends_on=lambda self: self.type == FieldType.INT)
    valstr: str | None = WireField(default=None, wire_depends_on=lambda self: self.type == FieldType.STRING)


# ── Log message types ────────────────────────────────────────────────


class LogNext(WireModel):
    """STDERR_NEXT — a log line from the daemon."""

    code: int = WireField(default=STDERR_NEXT, serialize=True, deserialize=False)
    text: str = ""


class LogStartActivity(WireModel):
    """STDERR_START_ACTIVITY — begin a tracked activity."""

    code: int = WireField(default=STDERR_START_ACTIVITY, serialize=True, deserialize=False)
    act_id: int = 0
    level: int = 0
    type: int = 0
    text: str = ""
    fields: list[ActivityField] = PydanticField(default_factory=list)
    parent: int = 0


class LogStopActivity(WireModel):
    """STDERR_STOP_ACTIVITY — end a tracked activity."""

    code: int = WireField(default=STDERR_STOP_ACTIVITY, serialize=True, deserialize=False)
    act_id: int = 0


class LogResult(WireModel):
    """STDERR_RESULT — result data for an activity."""

    code: int = WireField(default=STDERR_RESULT, serialize=True, deserialize=False)
    act_id: int = 0
    result_type: int = 0
    fields: list[ActivityField] = PydanticField(default_factory=list)


class LogError(WireModel):
    """STDERR_ERROR — the daemon is reporting an error."""

    code: int = WireField(default=STDERR_ERROR, serialize=True, deserialize=False)
    type: str = ""
    level: int = 0
    name: str = ""
    msg: str = ""
    have_pos: int = 0
    traces: list[TraceLine] = PydanticField(default_factory=list)


# ── Tagged-union container ───────────────────────────────────────────

# Union of all log message types — used for type annotations
LogMessage: type = LogNext | LogStartActivity | LogStopActivity | LogResult | LogError  # type: ignore[assignment]

# Dispatch table: uint64 code → message class
_LOG_PARSERS: dict[int, type[WireModel]] = {
    STDERR_NEXT: LogNext,
    STDERR_START_ACTIVITY: LogStartActivity,
    STDERR_STOP_ACTIVITY: LogStopActivity,
    STDERR_RESULT: LogResult,
    STDERR_ERROR: LogError,
}


class WireLogs(WireModel):
    """Container for a complete stderr stream.

    ``from_reader`` reads a tagged-union stream (dispatching on ``code``),
    stopping at ``STDERR_LAST`` or after ``LogError``.

    ``to_writer`` writes each message (which includes its ``code``) followed
    by ``STDERR_LAST``.
    """

    messages: list[LogMessage] = PydanticField(default_factory=list)  # type: ignore[valid-type]

    @classmethod
    async def from_reader(cls, ctx: ReadContext) -> WireLogs:
        """Read tagged-union stderr stream."""
        obj = cls.__new__(cls)
        object.__setattr__(obj, "__pydantic_fields_set__", {"messages"})
        object.__setattr__(obj, "__pydantic_extra__", None)
        object.__setattr__(obj, "__pydantic_private__", None)

        msgs: list[Any] = []
        while True:
            code = await ctx.reader.read_uint64()

            if code == STDERR_LAST:
                break

            parser = _LOG_PARSERS.get(code)
            if parser is None:
                # Unknown code — skip (tolerant, like old read_stream)
                continue

            msg = await parser.from_reader(ctx)
            msgs.append(msg)

            # Error terminates the stream (no STDERR_LAST after error)
            if isinstance(msg, LogError):
                break

        object.__setattr__(obj, "messages", msgs)
        return obj

    async def to_writer(self, ctx: WriteContext) -> None:
        """Write each message body (including its code) then STDERR_LAST."""
        for msg in self.messages:
            await msg.to_writer(ctx)
        ctx.writer.write_uint64(STDERR_LAST)
