"""Entry point: python -m pynixd

Subcommands:
  daemon    Start the pynixd daemon server
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal

import structlog

from .config import PynixdSettings
from .instance import Server

log = structlog.get_logger(__name__)


async def async_daemon_main(config_path: str | None = None) -> None:
    if config_path is not None:
        os.environ["PYNIXD_CONFIG"] = config_path

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


def daemon_main(args: argparse.Namespace) -> None:
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

    asyncio.run(async_daemon_main(config_path=args.config))


def main() -> None:
    parser = argparse.ArgumentParser(description="pynixd — Nix daemon protocol proxy")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    daemon_parser = sub.add_parser("daemon", help="Start the pynixd daemon server")
    daemon_parser.add_argument(
        "--config",
        help="Path to JSON config file (overrides PYNIXD_CONFIG env var)",
        default=None,
    )
    daemon_parser.set_defaults(func=daemon_main)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
