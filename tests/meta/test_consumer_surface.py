"""What a first-party consumer may import from ``nanopynix``.

CLAUDE.md states two rules about this boundary, and nothing checked either one.
``pynix`` must depend on the public APIs of ``nanopynix``, and a narrow
dependency on a private module is acceptable only when a redesign is not
justified. Measured before this file existed: 30 private import sites across
``pynix``, ``ekn`` and ``nanopynix-helpers``, and none of them recorded the
decision. The rule was clear, it was written down, and nothing looked.

Two gates, and they are not the same kind of thing.

The public gate is a **regression guard**. Every name that a consumer imports
from the ``nanopynix`` top level is in ``nanopynix.__all__`` today, so this
test is green on the day it lands. It turns the dogfooding consumer into a
source of truth for the public surface: a name that a consumer needs, but that
``__all__`` does not carry, is a defect in the export list. (The completeness
of ``__all__`` against the module itself belongs to #15, not here.)

The private gate is a **ledger**. Whether to promote a private name is a
judgement, not a derivation, so the literal below carries the decision and a
diff against the tree makes a new one visible. This is the same shape as
``WIRE_CLASSES`` in ``tests/nanopynix/test_exceptions_classify.py``, and it is
deliberate friction: the set cannot update itself.

The unit tests below are not decoration. A scanner that quietly matched nothing
would leave this file passing forever while enforcing nothing, so they pin both
directions -- the same reasoning as ``tests/meta/test_suppression_grammar.py``.
"""

from __future__ import annotations

from pathlib import Path

import nanopynix
from tests.support.consumer_imports import (
    CONSUMER_ROOTS as CONSUMER_ROOTS,
    WHOLE_MODULE as WHOLE_MODULE,
    ConsumerImport as ConsumerImport,
    format_report as format_report,
    iter_python_files as iter_python_files,
    scan_consumers as scan_consumers,
    scan_source as scan_source,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Each private nanopynix name that a first-party consumer imports, keyed by
# (module, name), with the reason the dependency is acceptable.
#
# Keyed by name and not by site, on purpose. A twenty-first use of an approved
# name is not a new decision, and thirty identical failures teach nothing. A
# new *name*, or a new *module*, is the decision point -- that is what a diff
# against this literal catches.
#
# Add an entry only after you decide. CLAUDE.md permits a narrow private
# dependency when a redesign is not justified; this is where that judgement is
# recorded, rather than at each import site.
CONSUMER_PRIVATE_IMPORTS: dict[tuple[str, str], str] = {
    ("nanopynix._typechecking", "BEARTYPING"): (
        "Shared test instrumentation, not library API. The module docstring "
        "waives the public-API rule explicitly and gives the reason: a public "
        "re-export would be surface with no caller outside this repository, "
        "and the flag must be readable before nanopynix/__init__.py finishes "
        "running. Promotion is tracked by #15."
    ),
    ("nanopynix._typechecking", "no_runtime_type_check"): (
        "The decorator half of the same instrumentation switch, and it travels "
        "with BEARTYPING. Same waiver, same module docstring."
    ),
}


def test_the_scanner_can_see_the_consumers() -> None:
    """Fail loudly if the gate is pointed somewhere with no source in it.

    The packaged CI runner ``cd``s into a store copy of the repository root
    rather than running from the checkout. If that copy ever stopped carrying
    the consumer packages, the conformance tests below would pass by scanning
    nothing -- the exact silent no-op this whole design is trying to avoid.
    """
    for root in CONSUMER_ROOTS:
        base = REPO_ROOT / root
        assert base.is_dir(), f"consumer root {base} is missing; is the source tree present?"
        assert list(iter_python_files(base)), f"no python files under {base}; is the source tree present?"


def test_the_scanner_sees_a_private_import() -> None:
    found = scan_source("from nanopynix._typechecking import BEARTYPING", Path("x.py"))
    assert [i.key for i in found] == [("nanopynix._typechecking", "BEARTYPING")]
    assert found[0].is_private


def test_the_scanner_reads_the_deepest_module_not_the_package() -> None:
    """``nanopynix._core._objects``, not ``nanopynix``."""
    found = scan_source("from nanopynix._core._objects import CoreStore", Path("x.py"))
    assert found[0].module == "nanopynix._core._objects"


def test_the_scanner_sees_a_private_name_out_of_a_public_module() -> None:
    """A public module can still hand out a private name."""
    found = scan_source("from nanopynix import _wire", Path("x.py"))
    assert found[0].is_private


def test_the_scanner_leaves_a_public_import_alone() -> None:
    found = scan_source("from nanopynix import Session\nfrom nanopynix.stores import LocalStore", Path("x.py"))
    assert found
    assert not any(i.is_private for i in found)


def test_the_scanner_sees_a_whole_module_import() -> None:
    found = scan_source("import nanopynix._core", Path("x.py"))
    assert found[0].key == ("nanopynix._core", WHOLE_MODULE)
    assert found[0].is_private


def test_the_scanner_ignores_a_mention_that_is_not_an_import() -> None:
    """A docstring naming a private module is prose, not a dependency."""
    assert not scan_source('"""Reaching nanopynix._core is what this forbids."""', Path("x.py"))


def test_the_scanner_ignores_a_package_with_a_similar_name() -> None:
    """``nanopynix_helpers`` is a separate distribution, not a submodule."""
    assert not scan_source("from nanopynix_helpers import build", Path("x.py"))


def test_the_private_ledger_is_what_this_file_says_it_is() -> None:
    private = [i for i in scan_consumers(REPO_ROOT) if i.is_private]
    derived = {i.key for i in private}
    declared = set(CONSUMER_PRIVATE_IMPORTS)

    added = sorted(derived - declared)
    if added:
        sites = format_report(i for i in private if i.key in set(added))
        raise AssertionError(
            f"{len(added)} private nanopynix name(s) are imported by a consumer but are not in "
            "CONSUMER_PRIVATE_IMPORTS.\nCLAUDE.md permits a narrow private dependency only when a "
            "redesign is not justified. Decide, then record the reason in the ledger in this file "
            "-- or add the capability to the public API of nanopynix instead.\n\n"
            f"names: {added}\n\nsites:\n{sites}"
        )

    stale = sorted(declared - derived)
    assert not stale, (
        f"{len(stale)} entr(y/ies) in CONSUMER_PRIVATE_IMPORTS have no importer left. "
        f"Delete them; a ledger that outlives its subject stops meaning anything: {stale}"
    )


def test_every_public_name_a_consumer_imports_is_exported() -> None:
    """A name a consumer needs, that ``__all__`` omits, is a defect in the list.

    A plain ``import nanopynix`` binds no name out of the package, so the
    whole-module sentinel is not a claim about ``__all__``.
    """
    exported = set(nanopynix.__all__)
    consumed = {
        i.name
        for i in scan_consumers(REPO_ROOT)
        if i.module == "nanopynix" and not i.is_private and i.name != WHOLE_MODULE
    }
    missing = sorted(consumed - exported)
    assert not missing, (
        f"{len(missing)} name(s) are imported from the nanopynix top level by a consumer but are "
        f"absent from nanopynix.__all__. Add them to __all__, or stop consuming them: {missing}"
    )
