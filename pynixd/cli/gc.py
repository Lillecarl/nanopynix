"""pynixd gc — trigger garbage collection on stores."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from .base import load_settings, setup_logging

if TYPE_CHECKING:
    import argparse


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
    settings = load_settings()
    setup_logging(settings)
    log = structlog.get_logger(__name__)

    if not settings.gc_enabled:
        log.warning("gc_disabled", message="Garbage collection is disabled globally")
        return

    if args.execute:
        log.warning("gc_execute", message="Would run GC", execute=True)
    else:
        log.warning("gc_dry_run", message="Would run GC (dry-run)", dry_run=True)
