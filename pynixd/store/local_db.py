"""LocalDBStore — LocalStore with SQLite database for fast-path queries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .local_daemon import LocalStore

if TYPE_CHECKING:
    from ..local_store_db import LocalStoreDB
    from ..path_tracker import PathTrackerInstance


class LocalDBStore(LocalStore):
    """LocalStore with SQLite database for fast-path query optimizations.

    Owns the database and path tracker. Overrides fast-path hooks
    with SQLite implementations. Falls through to DaemonStore.call()
    when the database can't answer a query.
    """

    db: LocalStoreDB | None  # set by factory — always non-None in practice
    tracker: PathTrackerInstance

    # Fast-path overrides will be added in subsequent phases.
    # For now, all methods inherit the None-returning defaults from Store,
    # which fall through to call() → daemon.
