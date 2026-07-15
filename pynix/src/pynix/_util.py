from __future__ import annotations

import asyncio
import contextlib
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_RESULT_BUILD_LOG_LINE = 101
_RESULT_POST_BUILD_LOG_LINE = 107
_LOG_DRAIN_SECONDS = 0.5


def prepare_sys_path() -> None:
    cwd = str(Path.cwd())
    sys.path[:] = [p for p in sys.path if p not in ("", ".", cwd)]
    configure_logging()


def configure_logging() -> None:
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.KeyValueRenderer(sort_keys=True),
        ],
    )


@asynccontextmanager
async def forward_nix_logs(session: Any, *, print_build_logs: bool = False) -> AsyncGenerator[None]:
    configure_logging()
    task = asyncio.create_task(_forward_nix_logs(session, print_build_logs=print_build_logs))
    try:
        yield
    finally:
        await asyncio.sleep(_LOG_DRAIN_SECONDS)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _forward_nix_logs(session: Any, *, print_build_logs: bool) -> None:
    logger = structlog.get_logger("pynix.nix")
    async for event in session.log_stream():
        message = event.message_without_ansi
        result_type, result_message = _result_event(event)
        if result_type in {_RESULT_BUILD_LOG_LINE, _RESULT_POST_BUILD_LOG_LINE}:
            if print_build_logs:
                logger.info(
                    "nix build log", message=result_message, request_id=event.request_id, result_type=result_type
                )
            continue
        if event.action == "error":
            logger.error("nix log", message=message, action=event.action, request_id=event.request_id)
        elif event.action == "warn":
            logger.warning("nix log", message=message, action=event.action, request_id=event.request_id)
        else:
            logger.info(
                "nix log",
                message=message,
                action=event.action,
                request_id=event.request_id,
                result_type=event.result_type.name if event.result_type else None,
            )


def _result_event(event: Any) -> tuple[int | None, str | None]:
    if event.action != "result":
        return None, None
    args = event.args
    if len(args) < 2 or not isinstance(args[1], int):
        return None, None
    result_type = args[1]
    fields: list[Any] = cast("list[Any]", args[2]) if len(args) > 2 else []
    message = None
    for field in reversed(fields):
        if isinstance(field, str):
            message = field
            break
    return result_type, message
