"""Encode and decode of the wire types that carry a Nix concept (#19).

Every test here goes through **real bytes** -- ``bytes(msg)`` and
``Msg().parse(...)`` -- rather than comparing two Python objects. An
object-to-object check would pass while the field never reached the wire, and
that is the failure this file exists to catch.

The subject is nanopynix's own converters, not betterproto2's. ``_codec.py``
turns Python values into ``ScalarValue``/``DeepValue`` and back, and
``_worker_eval.py`` turns a ``CallArg`` tree back into arguments for Nix. Those
are hand-written, so they are what can drift.

**The proto3 oneof trap is the reason for the falsy-value tests.** Each of
these converters asks ``if field is not None``. A ``string_value`` of ``""``,
an ``int_value`` of ``0`` and a ``bool_value`` of ``False`` are all falsy, so a
converter written with ``if field:`` would silently turn every one of them into
``None`` -- and only after a serialize-and-parse cycle, because that is when
proto3 presence is decided. Measured today: all of them survive. These tests
keep it that way.
"""

from __future__ import annotations

import pytest
from nanopynix_proto.nix.common import (
    BuildResult,
    BuildResultList,
    CallArg,
    CallArgAttrs,
    CallArgList,
    DeepValue,
    DerivationOutputs,
    MissingInfo,
    NullValue,
    PathInfo,
    RemoteCallArg,
    ScalarValue,
    ValueHandle,
)

from nanopynix._core._codec import (  # type: ignore[reportPrivateUsage] -- the converters under test have no public name
    deep_value_to_python,
    python_to_deep_value,
    python_to_scalar,
    scalar_to_python,
)
from nanopynix.rpc.worker._worker_eval import (  # type: ignore[reportPrivateUsage] -- test imports private module
    EvalServiceHandler,
)

# Every scalar a primop argument or result may be, falsy ones first. The falsy
# half is the point: see the module docstring.
_SCALARS = ["", 0, False, 0.0, None, "hello", -7, True, 1.5]


def _round_trip_deep(value: object) -> object:
    """Encode, serialize, parse, decode. All four, in that order."""
    return deep_value_to_python(DeepValue().parse(bytes(python_to_deep_value(value))))


class _DecoderOnly(EvalServiceHandler):
    """The ``CallArg`` decoder, without a worker behind it.

    Only the ``remote_value`` branch needs worker state, and this replaces
    exactly that. The scalar, list and attrs branches are the production code
    unchanged, which is what makes this a test of the decoder rather than of a
    copy of it.
    """

    def __init__(self) -> None:
        """Deliberately does not call the base ``__init__``; there is no state."""

    def _resolve(self, handle: int) -> object:
        return f"<worker value {handle}>"


@pytest.mark.parametrize("value", _SCALARS, ids=repr)
def test_every_scalar_survives_the_wire_with_its_type(value: object) -> None:
    """Type as well as value: ``True`` must not come back as ``1``.

    ``python_to_scalar`` checks ``bool`` before ``int`` because ``bool`` is a
    subclass of ``int``. Reordering those two branches would send every
    boolean as ``int_value`` and is invisible without a type assertion.
    """
    received = _round_trip_deep(value)
    assert received == value
    assert type(received) is type(value), f"{value!r} came back as {type(received).__name__}"


@pytest.mark.parametrize("value", _SCALARS, ids=repr)
def test_the_scalar_converters_agree_with_each_other(value: object) -> None:
    """``scalar_to_python`` is the inverse of ``python_to_scalar``."""
    received = scalar_to_python(ScalarValue().parse(bytes(python_to_scalar(value))))
    assert received == value
    assert type(received) is type(value)


def test_an_unsupported_type_raises_or_stringifies_as_asked() -> None:
    """The two boundaries want opposite answers, so the default matters.

    The primop boundary takes JSON-compatible input, so an odd type is a
    caller bug and must raise. The eval transport receives Nix values such as
    a path, which have to reach the wire as a string.
    """
    with pytest.raises(TypeError, match="unsupported RPC primop value type"):
        python_to_scalar({1, 2})

    stringified = python_to_scalar({1, 2}, on_unsupported="stringify")
    assert stringified.string_value is not None

    with pytest.raises(TypeError, match="unsupported RPC primop value type"):
        python_to_scalar(object())


def test_a_nested_tree_survives_the_wire() -> None:
    """Lists inside attrs inside lists, with a falsy leaf at each level."""
    value: dict[str, object] = {
        "empty_attrs": {},
        "empty_list": [],
        "mixed": [0, "", False, None, {"deep": [1.5, {"deeper": "x"}]}],
        "falsy_leaf": "",
    }
    assert _round_trip_deep(value) == value


def test_an_empty_container_is_not_confused_with_a_missing_one() -> None:
    """``{}`` and ``[]`` are values, and ``None`` is a different value.

    All three encode to a short message, so a converter that tested presence
    with ``if`` rather than ``is not None`` would collapse them together.
    """
    empty_attrs: dict[str, object] = {}
    empty_list: list[object] = []
    nested_attrs: dict[str, object] = {"a": empty_attrs}
    nested_list: list[object] = [empty_list]

    assert _round_trip_deep(empty_attrs) == empty_attrs
    assert _round_trip_deep(empty_list) == empty_list
    assert _round_trip_deep(None) is None
    assert _round_trip_deep(nested_attrs) == nested_attrs
    assert _round_trip_deep(nested_list) == nested_list


def test_a_remote_handle_is_refused_rather_than_silently_dropped() -> None:
    """A primop result cannot carry a worker-side handle.

    Returning ``None`` here would hand the caller a null in place of a value,
    which is the failure that is hardest to trace back to this line.
    """
    with pytest.raises(TypeError, match="remote value handles are not supported"):
        deep_value_to_python(DeepValue(remote_value=ValueHandle(handle=3)))


def test_nesting_is_bounded_by_betterproto_and_not_by_the_codec() -> None:
    """A deeply nested value raises ``RecursionError``, and this says whose.

    Max depth per stage, with one ``{"k": [...]}`` per level:

    ==========================  =============  ==========
    stage                       under pytest   plain
    ==========================  =============  ==========
    ``python_to_deep_value``    239            over 400
    ``bytes(...)``              79             82
    ``DeepValue().parse(...)``  63             65
    ==========================  =============  ==========

    **Two columns, because the first measurement taken was wrong.** beartype
    wraps every callable under the test suite and not under a plain
    interpreter, so it doubles the frames per level in nanopynix's own code
    while leaving betterproto2's untouched. That is visible above: the encoder
    loses about 40% of its depth and the other two lose three levels.

    The conclusion survives both columns, and the gap makes it sharper.
    nanopynix's converter is not the limit -- betterproto2's serializer and
    parser are, by a factor of three. This is pinned rather than corrected,
    because choosing what a caller should see instead of a third-party
    ``RecursionError`` is hostile-input work (#20), not round-trip work.

    The assertion stays loose on purpose. It pins that a shallow tree works
    and that a very deep one fails, not a boundary that moves with the
    interpreter's recursion limit and with whether beartype is on.
    """
    shallow: object = "leaf"
    for _ in range(20):
        shallow = {"k": [shallow]}
    assert _round_trip_deep(shallow) == shallow

    deep: object = "leaf"
    for _ in range(500):
        deep = {"k": [deep]}
    with pytest.raises(RecursionError):
        _round_trip_deep(deep)


def test_a_call_arg_tree_survives_the_wire() -> None:
    """The client's argument encoding, decoded by the worker's own function.

    ``CallArg`` is a separate tree from ``DeepValue`` -- it carries a
    ``RemoteCallArg`` where ``DeepValue`` carries a ``ValueHandle`` -- and it
    had no test of any kind.
    """
    arg = CallArg(
        attrs=CallArgAttrs(
            entries={
                "zero": CallArg(scalar=ScalarValue(int_value=0)),
                "empty": CallArg(scalar=ScalarValue(string_value="")),
                "false": CallArg(scalar=ScalarValue(bool_value=False)),
                "null": CallArg(scalar=ScalarValue(null_value=NullValue())),
                "items": CallArg(
                    list=CallArgList(
                        items=[
                            CallArg(scalar=ScalarValue(float_value=0.0)),
                            CallArg(remote_value=RemoteCallArg(handle=7)),
                        ],
                    ),
                ),
            },
        ),
    )

    decoded = _DecoderOnly()._call_arg_to_python(CallArg().parse(bytes(arg)), None)

    assert decoded == {
        "zero": 0,
        "empty": "",
        "false": False,
        "null": None,
        "items": [0.0, "<worker value 7>"],
    }
    assert type(decoded["false"]) is bool, "False must not arrive as an int"


def test_an_empty_call_arg_is_refused() -> None:
    """A ``CallArg`` with no branch set is a bug on the sending side."""
    with pytest.raises(TypeError, match="unsupported call argument"):
        _DecoderOnly()._call_arg_to_python(CallArg(), None)


def test_the_recursive_derivation_output_tree_survives_the_wire() -> None:
    """``DerivationOutputs`` nests once per level of dynamic derivation.

    The subject here is the wire, and not the builder that fills the tree in.
    A message that carries itself as a field is the shape that a flat encoder
    truncates, so this builds three levels and reads the innermost one back
    through real bytes.

    ``test_the_input_drvs_builder_recurses_and_keeps_every_output`` in
    ``test_store_metadata_fidelity.py`` is the test of the builder.
    """
    tree = DerivationOutputs(outputs=["out"], dynamic_outputs={})
    for level in range(3):
        tree = DerivationOutputs(outputs=[f"out{level}"], dynamic_outputs={f"dyn{level}": tree})

    received = type(tree)().parse(bytes(tree))

    assert received == tree
    innermost = received.dynamic_outputs["dyn2"].dynamic_outputs["dyn1"].dynamic_outputs["dyn0"]
    assert innermost.outputs == ["out"]
    assert innermost.dynamic_outputs == {}


def test_the_flat_result_messages_survive_the_wire() -> None:
    """The messages a store operation answers with, falsy fields included.

    ``BuildResultList`` had no mention anywhere in the suite, and a
    ``success`` of ``False`` on a ``BuildResult`` is the field a caller acts
    on most.
    """
    results = BuildResultList(
        results=[
            BuildResult(drv_path="/nix/store/a.drv", success=False, status="PermanentFailure", error_msg="boom"),
            BuildResult(drv_path="/nix/store/b.drv", success=True, status="Built", outputs=["/nix/store/b"]),
        ],
    )
    received = BuildResultList().parse(bytes(results))
    assert received == results
    assert received.results[0].success is False, "a failed build must not arrive as a successful one"
    assert received.results[1].outputs == ["/nix/store/b"]

    missing = MissingInfo(will_build=["/nix/store/a.drv"], will_substitute=[], unknown=[], download_size=0, nar_size=0)
    assert MissingInfo().parse(bytes(missing)) == missing

    info = PathInfo(path="/nix/store/x", nar_hash="sha256:0", nar_size=0, references=[], ultimate=False, sigs=[])
    received_info = PathInfo().parse(bytes(info))
    assert received_info == info
    assert received_info.deriver is None, "an absent optional must not become an empty string"
