"""Do the bindings and the wire agree on the fields of a shared type?

A store query has two implementations of one answer. ``nanopynix_bindings``
reads the C++ struct, and ``nanopynix_proto`` carries the same data between the
manager and an rpc worker. Nothing checked that the two carry the same fields.

The failure is silent in each direction, and the two directions are different
defects:

* **A field on the wire, and not in the bindings.** The rpc worker has nothing
  to put in it, so it sends a default. The caller reads a plausible value that
  Nix never produced.
* **A field in the bindings, and not on the wire.** The inproc engine reports
  it and the rpc engine does not. The two engines then disagree, which this
  repository treats as a defect unless process isolation forces it.

``nanopynix/tests/test_store_metadata_fidelity.py`` is the record of what this
class of defect costs. Three fields reached Python as a plausible wrong answer
rather than an error: ``PathInfo.sigs`` read back as ``[]``, which is also what
an unsigned path gives; ``Derivation.structured_attrs`` reported nothing for a
derivation that used structured attrs; and ``Derivation.input_drvs`` lost the
nesting of a ``DerivedPathMap``. That file tests each against real Nix
behaviour, and it is the right place for that question.

**This file asks the cheaper question that no test asked**: that the two
Python-visible schemas of one type name the same fields. It cannot catch a
field that every layer forgot, and it does not try to. It catches a layer that
moved without the other.

Issue #141 records the measurement and the reason. ``AGENTS.md`` states the
rule this file follows: a convention that a machine can check belongs in
``tests/meta/``.

The ledger below is deliberate friction, in the same shape as
``PRIVATE_IMPORTS`` in ``test_consumer_surface.py``. A difference between the
two schemas is a judgement, so the literal carries the decision and its reason.
The set cannot update itself.

``test_the_ledger_is_not_stale`` and ``test_the_scanner_reads_real_fields``
exist because a scanner that quietly matched nothing would leave this file
green forever while it enforced nothing. That is the same reasoning as
``tests/meta/test_suppression_grammar.py``.
"""

from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from nanopynix_bindings import store as bindings_store
from nanopynix_proto.nix.common import (
    Derivation,
    DerivationOutput,
    DerivationOutputs,
    MissingInfo,
    PathInfo,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class WireAgreement:
    """One bound type, the message that carries it, and every allowed gap.

    ``bindings_only`` and ``wire_only`` map a field name to the reason that
    field is on one side alone. An empty reason is not accepted, because the
    reason is the whole value of the ledger.
    """

    bound: type[Any]
    wire: type[Any]
    bindings_only: Mapping[str, str]
    wire_only: Mapping[str, str]


# Each bound type of `nanopynix_bindings.store`, against the message that
# carries the same answer over rpc. Add an entry when issue #141 converts the
# next helper.
AGREEMENTS: Mapping[str, WireAgreement] = {
    "ValidPathInfo": WireAgreement(
        bound=bindings_store.ValidPathInfo,
        wire=PathInfo,
        bindings_only={
            "store_dir": (
                "`UnkeyedValidPathInfo::storeDir`, the directory of this store "
                "object. Nix carries it to support a relocatable store object, "
                "whose directory need not be the directory of the store that "
                "answered the query. The bound type renders `path` from it. The "
                "wire carries `path` already rendered, so it needs no such field."
            ),
            "store_path": (
                "The `nix::StorePath` itself, for a caller that compares or "
                "hashes a path and never needs the text. `path` carries the "
                "text, and that is what crosses the wire."
            ),
        },
        wire_only={},
    ),
    "MissingPaths": WireAgreement(
        bound=bindings_store.MissingPaths,
        wire=MissingInfo,
        bindings_only={
            "store_dir": (
                "The directory of the store that answered the query. "
                "`nix::MissingPaths` carries no such field, so `PyMissingPaths` "
                "holds one and renders each path from it. Every path the wire "
                "carries is rendered already."
            ),
        },
        wire_only={},
    ),
    "Derivation": WireAgreement(
        bound=bindings_store.Derivation,
        wire=Derivation,
        bindings_only={
            "store_dir": (
                "The directory of the store that read the derivation. "
                "`nix::Derivation` carries no such field, so `PyDerivation` "
                "holds one and renders `input_srcs` and each key of "
                "`input_drvs` from it. Every path the wire carries is rendered "
                "already."
            ),
        },
        wire_only={},
    ),
    "DerivationOutput": WireAgreement(
        bound=bindings_store.DerivationOutput,
        wire=DerivationOutput,
        bindings_only={
            "store_dir": (
                "The same field, for the same reason. An `InputAddressed` "
                "output and a `CAFixed` output each name a store path, and "
                "`PyDerivationOutput` renders that path from this directory."
            ),
        },
        wire_only={},
    ),
    "DerivationOutputs": WireAgreement(
        bound=bindings_store.DerivationOutputs,
        wire=DerivationOutputs,
        # This node holds output names and child nodes, and no store path. So
        # it needs no store directory, and the two schemas agree exactly.
        bindings_only={},
        wire_only={},
    ),
}


def bound_fields(bound: type[Any]) -> frozenset[str]:
    """Each field a caller can read off a bound nanobind type."""
    return frozenset(name for name in dir(bound) if not name.startswith("_"))


def wire_fields(wire: type[Any]) -> frozenset[str]:
    """Each field of a betterproto2 message, which is a dataclass."""
    return frozenset(field.name for field in dataclasses.fields(wire))


@pytest.mark.parametrize("name", sorted(AGREEMENTS))
class TestBindingsAndWireAgree:
    def test_no_field_is_in_the_bindings_alone(self, name: str) -> None:
        """A field the rpc engine cannot report, and the inproc engine can."""
        entry = AGREEMENTS[name]
        unexplained = bound_fields(entry.bound) - wire_fields(entry.wire) - set(entry.bindings_only)
        assert not unexplained, (
            f"{name} exposes {sorted(unexplained)}, which no field of "
            f"{entry.wire.__name__} carries. Add the field to the message, or "
            f"record the reason in AGREEMENTS['{name}'].bindings_only."
        )

    def test_no_field_is_on_the_wire_alone(self, name: str) -> None:
        """A field the rpc worker has to invent a value for."""
        entry = AGREEMENTS[name]
        unexplained = wire_fields(entry.wire) - bound_fields(entry.bound) - set(entry.wire_only)
        assert not unexplained, (
            f"{entry.wire.__name__} carries {sorted(unexplained)}, which "
            f"{name} does not expose. The rpc worker can only send a default "
            f"for each one. Bind the field, or record the reason in "
            f"AGREEMENTS['{name}'].wire_only."
        )

    def test_the_ledger_is_not_stale(self, name: str) -> None:
        """A name the ledger excuses, and that both sides now carry.

        A stale entry is worse than a missing one. It reads as a live
        constraint, and it hides the next real difference behind it.
        """
        entry = AGREEMENTS[name]
        bound, wire = bound_fields(entry.bound), wire_fields(entry.wire)
        stale_bindings = set(entry.bindings_only) - (bound - wire)
        stale_wire = set(entry.wire_only) - (wire - bound)
        assert not stale_bindings, (
            f"AGREEMENTS['{name}'].bindings_only excuses {sorted(stale_bindings)}, which is no longer only in the bindings"
        )
        assert not stale_wire, (
            f"AGREEMENTS['{name}'].wire_only excuses {sorted(stale_wire)}, which is no longer only on the wire"
        )

    def test_every_reason_says_something(self, name: str) -> None:
        """The reason is the value of the ledger, so an empty one is a defect."""
        entry = AGREEMENTS[name]
        for side, excused in (("bindings_only", entry.bindings_only), ("wire_only", entry.wire_only)):
            for field, reason in excused.items():
                assert len(reason.split()) >= 5, f"AGREEMENTS['{name}'].{side}['{field}'] gives no reason worth reading"


class TestTheScannerIsNotVacuous:
    """A scanner that matched nothing would leave this file green forever."""

    def test_the_scanner_reads_real_fields(self) -> None:
        """Both readers must return a set that a person recognises."""
        assert "nar_hash" in bound_fields(bindings_store.ValidPathInfo)
        assert "nar_hash" in wire_fields(PathInfo)
        assert "will_build" in bound_fields(bindings_store.MissingPaths)
        assert "will_build" in wire_fields(MissingInfo)

    def test_the_readers_do_not_return_everything(self) -> None:
        """A reader that returned every attribute would excuse every gap."""
        bound = bound_fields(bindings_store.ValidPathInfo)
        assert "query_path_info" not in bound
        assert not any(name.startswith("__") for name in bound)

    def test_the_ledger_covers_every_bound_type(self) -> None:
        """A type bound and never added here would go unchecked.

        The roster is derived, and not written down twice: a new
        ``nb::class_`` that carries store data joins this test the moment it
        appears in the module.
        """
        bound_types = {
            name
            for name in dir(bindings_store)
            if not name.startswith("_")
            and isinstance(attr := getattr(bindings_store, name), type)
            # An `nb::enum_` is an `enum.Enum` here, and it names no fields.
            # Exclude it by kind, so the next enum needs no edit to this file.
            and not issubclass(attr, enum.Enum)
        }
        # `StorePath` and `Store` are handles, and not a struct of fields that
        # the wire carries. Every other bound type answers a store query, and
        # the wire carries that answer.
        handles = {"StorePath", "Store"}
        unchecked = bound_types - handles - set(AGREEMENTS)
        assert not unchecked, (
            f"{sorted(unchecked)} is bound and this file does not check it. "
            f"Add an entry to AGREEMENTS, or add it to `handles` if it carries "
            f"no fields that cross the wire."
        )
