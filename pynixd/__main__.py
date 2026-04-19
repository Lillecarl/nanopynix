"""Entry point: python -m pynixd

All configuration is via environment variables and/or a JSON config file:

  PYNIXD_CONFIG        JSON config file path (also read for settings fields)
  PYNIXD_LOG_LEVEL     Log level: DEBUG, INFO, WARNING, ERROR (default: WARNING)

All other settings use PYNIXD_<FIELD_NAME> env vars (e.g. PYNIXD_SSH_PORT).
See PynixdSettings for the full list.
"""

from __future__ import annotations

import asyncio
import logging

import structlog
from environs import env

from .config import PynixdSettings
from .instance import Server


async def async_main() -> None:
    settings = PynixdSettings()
    local_store, stores = settings.to_stores()

    server = Server(local_store=local_store, stores=stores, settings=settings)
    try:
        await server.start()
        await server.wait_finished()
    finally:
        await server.close()


def main() -> None:
    log_level_str = env.str("PYNIXD_LOG_LEVEL", "WARNING").upper()

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
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
