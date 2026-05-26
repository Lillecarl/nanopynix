"""pynixd gc — trigger garbage collection on stores."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..operations.pynixd_collect_garbage import PynixdCollectGarbageRequest
from ..store import LocalSocketStore
from ..types import PynixdGCAction
from .base import load_settings, setup_logging

if TYPE_CHECKING:
    import argparse

DEFAULT_SOCKET = Path("/run/pynixd/pynixd.sock")


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("gc", help="Trigger garbage collection on stores")
    parser.add_argument(
        "--store",
        help="Only run GC on this store (default: all stores)",
        default=None,
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually run GC (default is dry-run)",
    )
    parser.set_defaults(func=gc_main)


def gc_main(args: argparse.Namespace) -> None:
    asyncio.run(_gc_main(args))


async def _gc_main(args: argparse.Namespace) -> None:
    settings = load_settings()
    setup_logging(settings)
    log = structlog.get_logger(__name__)

    socket_path = settings.unix_path or DEFAULT_SOCKET
    action = PynixdGCAction.EXECUTE if args.execute else PynixdGCAction.DRY_RUN

    log.warning("gc_debug", step="creating_store", socket_path=str(socket_path), action=action.name)

    store = LocalSocketStore(
        store_id="cli",
        store_path=Path("/"),
        socket_path=socket_path,
        probe=False,
        monitor=False,
    )

    log.warning("gc_debug", step="starting_store")
    await store.start()
    log.warning("gc_debug", step="store_started", features=store.features)

    try:
        log.warning("gc_debug", step="executing_op")
        resp = await store.execute(PynixdCollectGarbageRequest(action=action))
        log.warning("gc_debug", step="op_completed", resp_type=type(resp).__name__)
    except Exception as e:
        log.warning("gc_debug", step="op_failed", error=str(e), exc_info=True)
        raise
    finally:
        await store.close()

    if args.execute:
        log.warning("gc_complete", message="GC pass triggered on daemon")
    else:
        log.warning("gc_dry_run", message="Dry-run: daemon would trigger GC")
