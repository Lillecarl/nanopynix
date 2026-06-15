"""DaemonStore — talks to a Nix daemon over the wire protocol."""

from __future__ import annotations

from .base import Store


class DaemonStore(Store):
    """Store that communicates with a Nix daemon via the wire protocol.

    Does not override any virtual fast-path methods — all operations
    fall through to ``call()`` which sends requests to the daemon.
    """

    # Inherits call() from Store — the wire protocol implementation.
    # Subclasses override connect() for different transports (Unix socket, SSH).
    # LocalDBStore overrides fast-path hooks with SQLite.
