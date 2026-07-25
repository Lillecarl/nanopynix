"""nanopynix's RPC engine — client/worker over gRPC, for crash isolation and
running multiple independent Nix runtimes in one Python process.

This is the client-facing public surface, symmetric with
``nanopynix.inproc``: same names (``Session``, ``Store``, ``EvalSession``,
...), same depth. ``nanopynix.rpc.worker`` is the separate worker-process
entrypoint, not part of this surface.
"""

from __future__ import annotations

from nanopynix.rpc.client._pool import WorkerDiedError as WorkerDiedError
from nanopynix.rpc.client._session import (
    EvalSession as EvalSession,
)
from nanopynix.rpc.client._session import (
    LockedFlakeHandle as LockedFlakeHandle,
)
from nanopynix.rpc.client._session import (
    ReplSession as ReplSession,
)
from nanopynix.rpc.client._session import (
    ValueAttrs as ValueAttrs,
)
from nanopynix.rpc.client._session import (
    ValueList as ValueList,
)
from nanopynix.rpc.client._session import (
    ValueProxy as ValueProxy,
)
from nanopynix.rpc.client.session import LogCapture as LogCapture
from nanopynix.rpc.client.session import Nix as Nix
from nanopynix.rpc.client.session import Session as Session
from nanopynix.rpc.client.store import Store as Store
from nanopynix.rpc.client.store import StoreHandle as StoreHandle
from nanopynix.rpc.client.types import NixArg as NixArg
from nanopynix.rpc.client.types import NixValue as NixValue

__all__ = [
    "EvalSession",
    "LockedFlakeHandle",
    "LogCapture",
    "Nix",
    "NixArg",
    "NixValue",
    "ReplSession",
    "Session",
    "Store",
    "StoreHandle",
    "ValueAttrs",
    "ValueList",
    "ValueProxy",
    "WorkerDiedError",
]
