from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pynix._util import _forward_nix_logs, _LogActivity

from nanopynix import LogEvent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _Session:
    async def log_stream(self) -> AsyncIterator[LogEvent]:
        yield LogEvent(action="stop", args=[42])
        yield LogEvent(action="result", args=[42, 1, [123]])
        # `[level, text]`, which is what `PyLogger::log` sends: `_cb(id,
        # "msg", int(lvl), s)`. The level was missing here, and the event
        # still reported a message because `LogEvent.message` read `args[-1]`.
        # It reads the position the action puts the text in now, so a `msg`
        # without a level is the malformed event it always was.
        yield LogEvent(action="msg", args=[3, "useful message"])


class _Logger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def info(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("info", args, kwargs))

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("warning", args, kwargs))

    def error(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("error", args, kwargs))


async def test_nix_log_forwarder_skips_empty_activity_events(monkeypatch: Any) -> None:
    logger = _Logger()

    def get_logger(_name: str) -> _Logger:
        return logger

    monkeypatch.setattr("pynix._util.structlog.get_logger", get_logger)

    activity = _LogActivity()
    await _forward_nix_logs(_Session(), print_build_logs=False, activity=activity)

    assert logger.calls == [
        ("info", ("nix log",), {"message": "useful message", "action": "msg", "request_id": 0, "result_type": None})
    ]
    # The exit drain stops once this counter stops moving, so it has to count
    # every event the stream produced -- including the two this forwarder
    # filters out. Counting only logged events would let a burst of skipped
    # ones look like a quiet stream and cut the drain short.
    assert activity.count == 3
