"""Every worker RPC must have a hand-written handler, and nothing else may look like one.

``StoreServiceHandler`` used to inherit ``GeneratedServiceAdapterMixin``, which
reflected the generated service onto the bindings' ``store_*`` proto-dict
entrypoints and raised at *import time* if the two sets disagreed. That check
was load-bearing -- it caught a half-finished ``BuildForHumans`` removal -- and
it disappeared along with the reflection when every RPC got a typed handler.
This is its replacement: an RPC with no override falls through to its service
base's stub and fails at call time, on a request, in a subprocess.

It now covers all three handlers rather than the store's alone. This file used
to say the store needed the check while ``EvalServiceHandler`` got the same
guarantee "for free by having one method per RPC" -- which was wrong, because
one method per RPC is the thing being checked and nothing enforced it. A
misspelled override is not a type error: the base class supplies a method of
that name either way. Eval's handler names are now produced by a mechanical
rename onto ``worker_op``, which makes it more exposed than the store, not less.
"""

from __future__ import annotations

import dataclasses

import pytest
from nanopynix_proto.nix.eval import EvalServiceBase
from nanopynix_proto.nix.store import StoreServiceBase
from nanopynix_proto.nix.worker import WorkerServiceBase

from nanopynix.rpc.worker._worker import WorkerServiceHandler
from nanopynix.rpc.worker._worker_eval import EvalServiceHandler
from nanopynix.rpc.worker._worker_store import StoreServiceHandler


@dataclasses.dataclass(frozen=True)
class Service:
    """One generated service base and the handler that must cover it."""

    name: str
    base: type
    handler: type
    # The store and eval services are wide and mechanical; the worker service is
    # three lifecycle calls. One floor for all three would be meaningless for
    # the third, so each carries its own.
    minimum_rpcs: int


SERVICES: list[Service] = [
    Service("store", StoreServiceBase, StoreServiceHandler, minimum_rpcs=20),
    Service("eval", EvalServiceBase, EvalServiceHandler, minimum_rpcs=20),
    Service("worker", WorkerServiceBase, WorkerServiceHandler, minimum_rpcs=3),
]


def _rpc_names(base: type) -> set[str]:
    return {name for name, value in base.__dict__.items() if not name.startswith("_") and callable(value)}


@pytest.mark.parametrize("service", SERVICES, ids=lambda service: service.name)
def test_every_rpc_has_a_handler(service: Service) -> None:
    missing = sorted(_rpc_names(service.base) - set(service.handler.__dict__))
    assert not missing, f"{service.name} RPCs with no handler on {service.handler.__name__}: {missing}"


@pytest.mark.parametrize("service", SERVICES, ids=lambda service: service.name)
def test_the_handler_declares_no_methods_that_are_not_rpcs(service: Service) -> None:
    """A public non-RPC method would be wrapped as a handler by mistake.

    ``wrap_service_handlers`` decorates every public coroutine function on the
    class, so a stray public async helper would be advertised as an RPC entry
    point. Private helpers (``_run``, ``_resolve``) are exempt by name.
    """
    public = {name for name in service.handler.__dict__ if not name.startswith("_")}
    extra = sorted(public - _rpc_names(service.base))
    assert not extra, f"public non-RPC methods on {service.handler.__name__}: {extra}"


@pytest.mark.parametrize("service", SERVICES, ids=lambda service: service.name)
def test_the_rpc_set_is_not_empty(service: Service) -> None:
    """Guard against the discovery above silently finding nothing."""
    found = len(_rpc_names(service.base))
    assert found >= service.minimum_rpcs, f"{service.name} found {found} RPCs, expected {service.minimum_rpcs}+"


def test_the_discovery_notices_a_handler_that_is_missing() -> None:
    """The gate must fail on a *lost* override, not only on an added method.

    Both assertions above subtract one class ``__dict__`` from another, and a
    mistake in either direction would leave them passing for every input.
    Removing a name from a copy of the handler's method set is the same input a
    lost override produces, without editing the handler to find out.
    """
    store = SERVICES[0]
    handled = set(store.handler.__dict__) - {"is_valid_path"}

    assert sorted(_rpc_names(store.base) - handled) == ["is_valid_path"]


def test_every_service_handler_is_covered() -> None:
    """A fourth handler added without a row here would go unchecked, and look checked.

    Named explicitly rather than discovered from the module, because discovery
    is exactly what would also miss the new one.
    """
    assert {service.handler for service in SERVICES} == {
        StoreServiceHandler,
        EvalServiceHandler,
        WorkerServiceHandler,
    }
