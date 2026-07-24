"""Wire-protocol constants shared across the worker/client process boundary.

These aren't data types (see ``nanopynix.models``) -- they're literal values
and a handle-kind tag that the worker and client sides must agree on
independently, since nothing about Python's type system enforces agreement
between two separate processes talking gRPC.
"""

from __future__ import annotations

import enum


class HandleKind(enum.StrEnum):
    """Kind tag for an opaque int handle allocated by a worker's HandleRegistry."""

    STORE = "store"
    EVAL = "eval"
    VALUE = "value"
    LOCKED_FLAKE = "locked_flake"


CALL_ROUTE = "/nix.manager.ManagerPrimopService/Call"
"""gRPC route for ManagerPrimopService.Call -- betterproto2 bakes this string
into the generated stub method body with no importable constant of its own,
so worker and client sides share this single source of truth instead of each
hardcoding the identical literal independently."""

NIX_USER_CONF_FILES_ENV = "NIX_USER_CONF_FILES"
NIX_CONFIG_ENV = "NIX_CONFIG"

WORKER_INIT_STATUS_OK = "ok"
"""The only status value InitResponse's producer (the worker) sends and its
consumer (the client pool) checks for."""

DEFAULT_STORE_URI = "auto"
"""Sentinel store URI meaning "let Nix pick the default store"."""

NO_GC_LIMIT = 2**64 - 1
"""``max_freed`` sentinel meaning "no limit" for collect_garbage."""
