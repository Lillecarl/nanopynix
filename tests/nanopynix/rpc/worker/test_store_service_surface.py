"""Every StoreService RPC must have a hand-written handler.

``StoreServiceHandler`` used to inherit ``GeneratedServiceAdapterMixin``, which
reflected the generated service onto the bindings' ``store_*`` proto-dict
entrypoints and raised at *import time* if the two sets disagreed. That check
was load-bearing -- it caught a half-finished ``BuildForHumans`` removal -- and
it disappeared along with the reflection when every RPC got a typed handler.

This is its replacement, and it is the same guarantee ``EvalServiceHandler``
gets for free by having one method per RPC: an RPC with no override would
otherwise fall through to ``StoreServiceBase``'s stub and fail at call time,
on a request, in a subprocess.
"""

from __future__ import annotations

from nanopynix_proto.nix.store import StoreServiceBase

from nanopynix.rpc.worker._worker_store import StoreServiceHandler


def _rpc_names() -> set[str]:
    return {name for name, value in StoreServiceBase.__dict__.items() if not name.startswith("_") and callable(value)}


def test_every_store_rpc_has_a_handler() -> None:
    missing = sorted(_rpc_names() - set(StoreServiceHandler.__dict__))
    assert not missing, f"StoreService RPCs with no handler on StoreServiceHandler: {missing}"


def test_the_handler_declares_no_methods_that_are_not_rpcs() -> None:
    """A public non-RPC method would be wrapped as a handler by mistake.

    ``wrap_service_handlers`` decorates every public coroutine function on the
    class, so a stray public async helper would be advertised as an RPC entry
    point. Private helpers (``_run``, ``_resolve``) are exempt by name.
    """
    extra = sorted({name for name in StoreServiceHandler.__dict__ if not name.startswith("_")} - _rpc_names())
    assert not extra, f"public non-RPC methods on StoreServiceHandler: {extra}"


def test_the_rpc_set_is_not_empty() -> None:
    """Guard against the discovery above silently finding nothing."""
    assert len(_rpc_names()) > 20, "StoreServiceBase reflection found suspiciously few RPCs"
