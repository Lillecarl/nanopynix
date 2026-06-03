"""Entry point: python -m pynixd

Subcommands:
  daemon    Start the pynixd daemon server
  gc        Trigger garbage collection on stores
"""

from __future__ import annotations

import argparse
import asyncio
import signal

import structlog
import uvloop

from .cli.base import load_settings, setup_logging
from .cli.gc import register as register_gc
from .instance import Server

log = structlog.get_logger(__name__)


async def async_daemon_main() -> None:
    settings = load_settings()
    stores = settings.to_stores()

    server = Server(stores=stores, settings=settings)
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


def daemon_main(_args: argparse.Namespace) -> None:
    settings = load_settings()
    setup_logging(settings)

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

    asyncio.run(async_daemon_main())


def main() -> None:
    parser = argparse.ArgumentParser(description="pynixd — Nix daemon protocol proxy")
    root_sub = parser.add_subparsers(dest="subcommand", required=True)

    daemon_parser = root_sub.add_parser("daemon", help="Start the pynixd daemon server")
    daemon_parser.set_defaults(func=daemon_main)

    register_gc(root_sub)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
