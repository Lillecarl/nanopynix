from __future__ import annotations

from typing import Any

from pynix._util import _forward_nix_logs

from nanopynix import LogEvent


class _Session:
    async def log_stream(self):
        yield LogEvent(action="stop", args=[42])
        yield LogEvent(action="result", args=[42, 1, [123]])
        yield LogEvent(action="msg", args=["useful message"])


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

    await _forward_nix_logs(_Session(), print_build_logs=False)

    assert logger.calls == [("info", ("nix log",), {"message": "useful message", "action": "msg", "request_id": 0, "result_type": None})]
