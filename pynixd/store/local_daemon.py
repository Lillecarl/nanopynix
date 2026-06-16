"""LocalStore — thin subclass of DaemonStore for Unix socket transport."""

from __future__ import annotations

from .local import LocalSocketStore


class LocalStore(LocalSocketStore):
    """Unix socket-connected store without SQLite fast-paths.

    For a store with SQLite fast-paths, use LocalDBStore instead.

    All operations fall through to the daemon via the wire protocol.
    This exists as a base for LocalDBStore and for configs where
    use_db=False.
    """
