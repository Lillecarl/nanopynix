"""Entry point: python -m pynixd

Configuration is via environment variables and/or a JSON config file.
See PynixdSettings for env var mapping (PYNIXD_<FIELD_NAME>).
All settings fall through from env vars → config file → defaults.
"""

from __future__ import annotations

import asyncio
import logging
import signal

import structlog

from .config import PynixdSettings
from .instance import Server

log = structlog.get_logger(__name__)


async def async_main() -> None:
    settings = PynixdSettings()
    local_store, stores = settings.to_stores()

    server = Server(local_store=local_store, stores=stores, settings=settings)
    shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        if shutdown_event.is_set():
            log.info("forced_shutdown")
            raise SystemExit(1)
        log.info("shutdown_signal_received")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await server.start()

    await shutdown_event.wait()
    await server.close()


def main() -> None:
    settings = PynixdSettings()
    log_level_str = settings.log_level.upper()

    logging.basicConfig(
        level=log_level_str,
        format="%(message)s",
    )

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    asyncio.run(async_main())


if __name__ == "__main__":
    main()
