"""Does a worker exception arrive as the class the worker raised?

Boundary B used to keep the class of a Nix C++ exception and nothing else,
because the only identity on the wire was the ``"TypeName: "`` prefix that
``convert_handler_errors`` glues onto the status message, and the only table
that reads it holds Nix C++ names. Everything else -- ``SettingNotLiveError``,
and the builtins that ``rpc/worker/`` and ``_core/`` raise throughout --
arrived as a plain ``NixError``, which is the wrong family for all of them.

The worker now also sends a ``nix.common.ErrorIdentity`` in the status
trailer. These tests drive a real worker into each raise site and assert the
class on the client side, which is the only place the round trip is visible.

:mod:`tests.nanopynix.rpc.test_status_details` unit-tests the encoding, the
allowlist and the status-code table. This file is the end-to-end half, and it
is deliberately small: one case per resolution path, not one per raise site.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from grpclib.const import Status
from grpclib.exceptions import GRPCError
from nanopynix_proto.nix.common import NixType
from nanopynix_proto.nix.eval import AsScalarRequest, ConfigureEvalRequest, ListGetRequest, ListLengthRequest

from nanopynix.exceptions import NixError, NixTypeError, SettingNotLiveError, exception_from_wire

if TYPE_CHECKING:
    from tests.support.nix_environment import RpcSessionFactory

# A handle the registry has certainly never allocated. `_handle_registry.get`
# raises KeyError for it.
ABSENT_HANDLE = 999_999


def _proxy(ev: Any) -> Any:
    """The evaluator's ``EvalProxy``, which is how these tests reach the worker.

    Every raise site below is guarded on the client too, so the public route
    never gets there -- ``EvalSession.configure`` checks the settings model
    before it sends, ``ValueProxy.list_get`` resolves the index locally, and
    no caller can name a handle it did not receive. The subject here is the
    worker's own entry point, which is what a second client or a caller
    holding the proxy can reach. ``test_config_flow.py`` reaches it the same
    way, for the same reason.
    """
    return ev._ensure_proxy()


def _wire_status(exc: BaseException) -> Status:
    """The gRPC status code the worker chose for *exc*.

    ``_grpc_call`` raises the resolved exception ``from`` the ``GRPCError``, so
    the code is reachable through ``__cause__`` and needs no API of its own.
    """
    cause = exc.__cause__
    assert isinstance(cause, GRPCError), f"expected a GRPCError cause, got {cause!r}"
    return cause.status


async def test_a_setting_refusal_arrives_as_setting_not_live_error(rpc_session: RpcSessionFactory) -> None:
    """The acceptance criterion of #28, and the case that made it visible.

    ``SettingNotLiveError`` says Nix was never consulted. ``NixError``, which
    is what this used to be, says the opposite in as many words.
    """
    async with rpc_session() as nix, nix.store() as store, nix.eval(store) as ev:
        proxy = _proxy(ev)

        with pytest.raises(SettingNotLiveError) as excinfo:
            await proxy.configure_eval(ConfigureEvalRequest(eval_settings={"pure-eval": "true"}))

    assert "pure-eval" in str(excinfo.value)
    assert not str(excinfo.value).startswith("SettingNotLiveError: "), "the wire prefix must not survive twice"
    assert _wire_status(excinfo.value) == Status.FAILED_PRECONDITION


async def test_an_absent_handle_arrives_as_key_error(rpc_session: RpcSessionFactory) -> None:
    """``_handle_registry.get`` raises ``KeyError``, and so must the client."""
    async with rpc_session() as nix, nix.store() as store, nix.eval(store) as ev:
        proxy = _proxy(ev)

        with pytest.raises(KeyError) as excinfo:
            await proxy.list_length(ListLengthRequest(handle=ABSENT_HANDLE))

    assert not isinstance(excinfo.value, NixError), "Nix was never consulted"
    assert _wire_status(excinfo.value) == Status.INVALID_ARGUMENT


async def test_a_handle_of_the_wrong_kind_arrives_as_type_error(rpc_session: RpcSessionFactory) -> None:
    """``get_typed`` raises ``TypeError`` when the kind does not match.

    The evaluator's own handle is a real handle of the wrong kind, so this
    reaches the branch without inventing a number.
    """
    async with rpc_session() as nix, nix.store() as store, nix.eval(store) as ev:
        proxy = _proxy(ev)
        # A real handle, of a kind the value accessors reject.
        eval_handle: int = proxy._eval_handle

        with pytest.raises(TypeError) as excinfo:
            await proxy.list_length(ListLengthRequest(handle=eval_handle))

    assert not isinstance(excinfo.value, NixError)
    assert _wire_status(excinfo.value) == Status.INVALID_ARGUMENT


async def test_a_negative_index_past_the_start_arrives_as_index_error(rpc_session: RpcSessionFactory) -> None:
    """``list_get`` raises ``IndexError`` before it reaches Nix."""
    async with rpc_session() as nix, nix.store() as store, nix.eval(store) as ev:
        value = await ev.string("[1 2 3]")
        assert await value.list_length() == 3
        proxy = _proxy(ev)

        with pytest.raises(IndexError) as excinfo:
            await proxy.list_get(ListGetRequest(handle=value.handle, index=-1000))

    assert not isinstance(excinfo.value, NixError)
    assert _wire_status(excinfo.value) == Status.INVALID_ARGUMENT


async def test_an_unsupported_scalar_type_arrives_as_value_error(rpc_session: RpcSessionFactory) -> None:
    """``as_scalar`` raises ``ValueError`` for a type it has no accessor for."""
    async with rpc_session() as nix, nix.store() as store, nix.eval(store) as ev:
        value = await ev.string("42")
        assert await value.as_int() == 42
        proxy = _proxy(ev)

        with pytest.raises(ValueError) as excinfo:  # noqa: PT011 -- the class is the assertion; the message is the worker's
            await proxy.as_scalar(AsScalarRequest(handle=value.handle, nix_type=NixType.UNSPECIFIED))

    assert not isinstance(excinfo.value, NixError)
    assert _wire_status(excinfo.value) == Status.INVALID_ARGUMENT


async def test_a_nix_error_still_refines_past_the_class_the_worker_named(
    rpc_session: RpcSessionFactory,
) -> None:
    """The identity must seed the resolution, not end it.

    Nix reports ``1 + "a"`` as a plain ``nix::EvalError``, so the identity says
    ``EvalError`` and only the message says ``NixTypeError``. A client that
    treated the wire name as final would re-coarsen what boundary A got right.
    """
    async with rpc_session() as nix, nix.store() as store, nix.eval(store) as ev:
        with pytest.raises(NixError) as excinfo:
            await (await ev.string('1 + "a"')).to_python()

    raised = excinfo.value
    assert type(raised) is NixTypeError
    assert raised.info is not None, "the NixErrorInfo must still ride the same trailer"
    assert not raised.msg.startswith("EvalError: "), "the wire prefix must not survive twice"
    assert _wire_status(raised) == Status.UNKNOWN, "a Nix failure is not a bad request"


@pytest.mark.parametrize(
    "identity",
    [
        {"class_name": "PleaseImportMe"},
        {"class_name": "os.system"},
        {"nix_type": "NoSuchNixClass"},
        {},
    ],
    ids=["unknown", "dotted", "unknown-nix-type", "empty"],
)
def test_a_name_the_allowlist_does_not_hold_degrades_to_the_old_behaviour(identity: dict[str, Any]) -> None:
    """A payload never selects anything but a key in a table built at import.

    Degradation, not a second failure: the caller still gets the worker's
    message, through the same ``from_response`` path every client used before
    the identity existed.
    """
    resolved = exception_from_wire(
        nix_type=identity.get("nix_type", ""),
        class_name=identity.get("class_name", ""),
        msg="boom",
    )

    assert resolved is None
