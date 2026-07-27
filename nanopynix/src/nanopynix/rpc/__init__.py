"""nanopynix's RPC engine — client/worker over gRPC, for crash isolation and
running multiple independent Nix runtimes in one Python process.

This is the client-facing public surface, symmetric with
``nanopynix.inproc``: same names (``Session``, ``Store``, ``EvalSession``,
...), same depth. ``nanopynix.rpc.worker`` is the separate worker-process
entrypoint, not part of this surface.
"""

from __future__ import annotations

# Re-exported from its real home rather than from rpc.client.session: log
# capture is engine-independent and inproc.Session.capture_logs() returns
# this same class. Kept here so `from nanopynix.rpc import LogCapture`
# still resolves.
from nanopynix.logging import LogCapture as LogCapture
from nanopynix.rpc.client._pool import WorkerDiedError as WorkerDiedError
from nanopynix.rpc.client._session import (
    EvalSession as EvalSession,
    LockedFlakeHandle as LockedFlakeHandle,
    ReplSession as ReplSession,
    ValueProxy as ValueProxy,
)
from nanopynix.rpc.client.session import Nix as Nix, Session as Session
from nanopynix.rpc.client.store import Store as Store, StoreHandle as StoreHandle
from nanopynix.rpc.client.types import NixArg as NixArg

__all__ = [
    "EvalSession",
    "LockedFlakeHandle",
    "LogCapture",
    "Nix",
    "NixArg",
    "ReplSession",
    "Session",
    "Store",
    "StoreHandle",
    "ValueProxy",
    "WorkerDiedError",
]
