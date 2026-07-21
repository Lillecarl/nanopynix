"""Unit tests for the GeneratedServiceAdapterMixin metaprogramming glue.

This module wires generated gRPC service methods onto worker-side handlers
by inspecting a binding's method surface at class-creation time. It has no
Nix/store dependency, but nothing exercises its subclass-creation error
paths, forwarder response handling, or its two small helpers directly --
existing coverage only ever goes through it indirectly via real worker
handlers on the happy path. These are "dumb coverage" tests that pin each
branch down in isolation.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from nanopynix_proto.nix.common import NullValue
from nanopynix_proto.nix.store import GetUriRequest

from nanopynix._core._nix_executor import NixThreadExecutor
from nanopynix.rpc.worker._service_adapter import (
    GeneratedServiceAdapterMixin,
    _proto_shape,  # pyright: ignore[reportPrivateUsage]
    _resolve_attr,  # pyright: ignore[reportPrivateUsage]
)
from nanopynix.rpc.worker._worker import WorkerState


class _FakeServiceBase:
    def get_status(self, request: object) -> NullValue:
        raise NotImplementedError


def test_subclass_without_rpc_service_base_installs_nothing() -> None:
    class _Plain(GeneratedServiceAdapterMixin):
        pass

    assert not hasattr(_Plain, "get_status")


def test_subclass_requires_binding_method_names_with_rpc_service_base() -> None:
    with pytest.raises(TypeError, match="binding_method_names is required"):
        type("_Bad", (GeneratedServiceAdapterMixin,), {}, rpc_service_base=_FakeServiceBase)


def test_nanobind_rpc_call_base_is_not_implemented() -> None:
    class _Plain(GeneratedServiceAdapterMixin):
        pass

    with pytest.raises(NotImplementedError):
        _Plain()._nanobind_rpc_call("x", cast("Any", None))  # pyright: ignore[reportPrivateUsage]


def test_install_rejects_mismatched_binding_method_names() -> None:
    with pytest.raises(TypeError, match="does not match"):
        type(
            "_Bad",
            (GeneratedServiceAdapterMixin,),
            {},
            rpc_service_base=_FakeServiceBase,
            binding_method_names={"binding_wrong_name"},
            method_prefix="binding_",
        )


def test_install_rejects_a_missing_binding_method_alone() -> None:
    with pytest.raises(TypeError, match="missing binding_ methods: get_status"):
        type(
            "_Bad",
            (GeneratedServiceAdapterMixin,),
            {},
            rpc_service_base=_FakeServiceBase,
            binding_method_names=set(),
            method_prefix="binding_",
        )


def test_install_rejects_an_extra_binding_method_alone() -> None:
    with pytest.raises(TypeError, match="extra binding_ methods: extra"):
        type(
            "_Bad",
            (GeneratedServiceAdapterMixin,),
            {},
            rpc_service_base=_FakeServiceBase,
            binding_method_names={"binding_get_status", "binding_extra"},
            method_prefix="binding_",
        )


class _DirectAdapter(
    GeneratedServiceAdapterMixin,
    rpc_service_base=_FakeServiceBase,
    binding_method_names={"binding_get_status"},
    method_prefix="binding_",
):
    """Forwards straight to a canned response, without a Nix executor."""

    def __init__(self, response: object) -> None:
        self._response = response

    def _nanobind_rpc_call(self, binding_method_name: str, message: Any) -> Any:
        del binding_method_name, message
        return self._response


async def test_forwarder_passes_through_a_matching_response_instance() -> None:
    adapter = _DirectAdapter(NullValue())

    result = await adapter.get_status(NullValue())  # type: ignore[reportAttributeAccessIssue] -- installed dynamically by __init_subclass__

    assert isinstance(result, NullValue)


async def test_forwarder_converts_a_mapping_response_via_from_dict() -> None:
    adapter = _DirectAdapter({})

    result = await adapter.get_status(NullValue())  # type: ignore[reportAttributeAccessIssue] -- installed dynamically by __init_subclass__

    assert isinstance(result, NullValue)


async def test_forwarder_rejects_an_unrecognized_response_shape() -> None:
    adapter = _DirectAdapter("not proto-shaped")

    with pytest.raises(TypeError, match="expected proto-shaped mapping"):
        await adapter.get_status(NullValue())  # type: ignore[reportAttributeAccessIssue] -- installed dynamically by __init_subclass__


class _ExecutorAdapter(
    GeneratedServiceAdapterMixin,
    rpc_service_base=_FakeServiceBase,
    binding_method_names={"binding_get_status"},
    method_prefix="binding_",
    nix_executor_attr="executor",
):
    """Forwards through WorkerState.run_request, as real worker handlers do."""

    def __init__(self, state: WorkerState, executor: NixThreadExecutor, response: object) -> None:
        self._state = state
        self.executor = executor
        self._response = response

    def _nanobind_rpc_call(self, binding_method_name: str, message: Any) -> Any:
        del binding_method_name, message
        return self._response


async def test_executor_forwarder_dispatches_through_run_request() -> None:
    state = WorkerState()
    executor = NixThreadExecutor(max_workers=1, thread_name_prefix="test-service-adapter")
    adapter = _ExecutorAdapter(state, executor, NullValue())
    try:
        result = await adapter.get_status(GetUriRequest(request_id=1))  # type: ignore[reportAttributeAccessIssue] -- installed dynamically by __init_subclass__
    finally:
        executor.shutdown(wait=True)

    assert isinstance(result, NullValue)


async def test_executor_forwarder_converts_a_mapping_response_via_from_dict() -> None:
    state = WorkerState()
    executor = NixThreadExecutor(max_workers=1, thread_name_prefix="test-service-adapter")
    adapter = _ExecutorAdapter(state, executor, {})
    try:
        result = await adapter.get_status(GetUriRequest(request_id=1))  # type: ignore[reportAttributeAccessIssue] -- installed dynamically by __init_subclass__
    finally:
        executor.shutdown(wait=True)

    assert isinstance(result, NullValue)


async def test_executor_forwarder_rejects_an_unrecognized_response_shape() -> None:
    state = WorkerState()
    executor = NixThreadExecutor(max_workers=1, thread_name_prefix="test-service-adapter")
    adapter = _ExecutorAdapter(state, executor, "not proto-shaped")
    try:
        with pytest.raises(TypeError, match="expected proto-shaped mapping"):
            await adapter.get_status(GetUriRequest(request_id=1))  # type: ignore[reportAttributeAccessIssue] -- installed dynamically by __init_subclass__
    finally:
        executor.shutdown(wait=True)


def test_resolve_attr_walks_a_dotted_path() -> None:
    class _Inner:
        value = 42

    class _Outer:
        inner = _Inner()

    assert _resolve_attr(_Outer(), "inner.value") == 42


def test_proto_shape_normalizes_nested_containers() -> None:
    assert _proto_shape({"a": [1, "two", {"b": 3}]}) == {"a": [1, "two", {"b": 3}]}
    assert _proto_shape((1, 2, 3)) == [1, 2, 3]
    assert _proto_shape("just a string") == "just a string"
    assert _proto_shape(b"raw bytes") == b"raw bytes"
    assert _proto_shape(7) == 7
