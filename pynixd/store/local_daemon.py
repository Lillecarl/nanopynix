"""LocalStore — thin subclass of DaemonStore for Unix socket transport."""

from __future__ import annotations

from .daemon import DaemonStore


class LocalStore(DaemonStore):
    """DaemonStore connected via Unix socket.

    No database access — for reading the root Nix store as a user process
    where the Nix DB is owned by root.  All operations fall through to
    the daemon via the wire protocol.

    For a store with SQLite fast-paths, use LocalDBStore instead.
    """
