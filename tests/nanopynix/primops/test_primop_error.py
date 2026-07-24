"""Tests for nanopynix.PrimopError.

PrimopError is the dedicated type a primop raises to control exactly what
message the Nix evaluator sees -- shown bare, with no type-name prefix and no
truncation of multi-line messages. Anything else (a plain ValueError, or a
genuinely unexpected exception) still goes through the generic fallback in
py_primop_bridge. Uses worker-imported (import_path) primops, like real
primop implementations (yaml, ipaddress, jsonschema) do -- not rpc=True
manager-side primops, which propagate errors through a separate gRPC-status
path rather than py_primop_bridge.
"""

from __future__ import annotations

import pytest

from nanopynix import PrimopError
from nanopynix.models import PrimOpSpec
from nanopynix.rpc import Session

pytestmark = pytest.mark.nix_version(minimum="2.32")


def raise_primop_error(_value: object) -> object:
    raise PrimopError("line one\nline two\nline three")


def raise_unexpected(_value: object) -> object:
    raise KeyError("boom")


def _test_primops() -> list[PrimOpSpec]:
    return [
        PrimOpSpec(
            name="testRaiseCustomError",
            arity=1,
            args=["value"],
            doc="raises PrimopError with a multi-line message, for testing",
            import_path="tests.nanopynix.primops.test_primop_error:raise_primop_error",
        ),
        PrimOpSpec(
            name="testRaiseUnexpected",
            arity=1,
            args=["value"],
            doc="raises a plain KeyError, for testing the type-name-prefix fallback",
            import_path="tests.nanopynix.primops.test_primop_error:raise_unexpected",
        ),
    ]


@pytest.mark.anyio
async def test_primop_error_message_is_shown_bare_and_multiline():
    async with (
        Session(primops=_test_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        with pytest.raises(Exception) as exc_info:  # noqa: PT011
            await (await eval.string("builtins.testRaiseCustomError null")).force_deep()
        message = str(exc_info.value)
        # Nix's own error pretty-printer re-indents continuation lines under
        # "error:", so check each line survived rather than the exact
        # original multi-line substring.
        assert "line one" in message
        assert "line two" in message
        assert "line three" in message
        assert "PrimopError" not in message


@pytest.mark.anyio
async def test_unexpected_exception_keeps_its_type_name_prefix():
    async with (
        Session(primops=_test_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        with pytest.raises(Exception) as exc_info:  # noqa: PT011
            await (await eval.string("builtins.testRaiseUnexpected null")).force_deep()
        message = str(exc_info.value)
        assert "KeyError" in message
