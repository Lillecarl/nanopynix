from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog


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
async def forward_nix_logs(session: Any) -> AsyncIterator[None]:
    configure_logging()
    task = asyncio.create_task(_forward_nix_logs(session))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _forward_nix_logs(session: Any) -> None:
    logger = structlog.get_logger("pynix.nix")
    async for event in session.log_stream():
        message = event.message_without_ansi
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
