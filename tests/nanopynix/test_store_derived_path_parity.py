"""Both engines must accept Nix's ``^`` DerivedPath output selector.

A plain ``.drv`` path historically means "all of this derivation's outputs".
A ``^`` separator opts into Nix's canonical DerivedPath spelling and selects
specific ones -- ``drv^out``, ``drv^out,dev``, ``drv^*``.

The store's two entry points that take derived paths rather than store paths
(``query_missing``, ``build_paths_with_results``) disagreed about this. The
rpc worker reached C++ through the proto-dict funnel, whose
``request_derived_paths()`` calls ``DerivedPath::parse`` when it sees a ``^``;
inproc reached the direct binding, which takes ``StorePath``s, so every
element went through ``parse_store_path`` and a ``^`` was rejected as an
illegal character in a store path name.

Same store, same input, two answers. :mod:`tests.nanopynix.test_engine_parity`
could not see it -- both engines have a ``query_missing`` taking
``derived_paths`` -- and the sibling method's own docstring documented the ``^``
support that its neighbour did not have.

The paths here are synthetic and deliberately not valid in any store: what is
under test is whether the argument *parses*, and an unknown derivation is a
perfectly good answer to "is this missing?". Building a real derivation would
test Nix's scheduler instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from tests.support.nix_environment import InprocSessionFactory, RpcSessionFactory

# A well-formed store path that no store will ever contain, plus the three
# output-selector spellings Nix accepts on top of one.
_HASH_PART = "a" * 32
_DRV_NAME = f"{_HASH_PART}-nanopynix-parity-probe.drv"

DERIVED_PATH_SUFFIXES: list[str] = [
    "",  # the historical all-outputs spelling
    "^out",
    "^out,dev",
    "^*",
]


async def _query_missing(factory: Any, suffix: str) -> tuple[str, Any]:
    """Run one query_missing on one engine; return its outcome, not its message."""
    async with factory() as session, session.store() as store:
        store_dir = await store.store_dir()
        argument = f"{store_dir.rstrip('/')}/{_DRV_NAME}{suffix}"
        try:
            result = await store.query_missing([argument])
        except Exception as exc:  # the exception type *is* the measurement
            return ("raise", type(exc).__name__)
        return (
            "value",
            {
                "will_build": sorted(str(p) for p in result.will_build),
                "will_substitute": sorted(str(p) for p in result.will_substitute),
                "unknown": sorted(str(p) for p in result.unknown),
            },
        )


@pytest.mark.parametrize("suffix", DERIVED_PATH_SUFFIXES, ids=lambda s: s or "no-selector")
async def test_query_missing_accepts_an_output_selector_on_both_engines(
    suffix: str,
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """``drv^out`` must reach ``DerivedPath::parse`` whichever engine is driving."""
    inproc_outcome = await _query_missing(inproc_session, suffix)
    rpc_outcome = await _query_missing(rpc_session, suffix)

    assert inproc_outcome[0] == "value", (
        f"inproc rejected {suffix!r}: {inproc_outcome[1]!r} -- "
        f"a ^ selector must parse as a DerivedPath, not as a store path name"
    )
    assert inproc_outcome == rpc_outcome


def test_the_selector_table_actually_exercises_selectors() -> None:
    """Without a ``^`` entry this file would test nothing it was written for."""
    assert any("^" in suffix for suffix in DERIVED_PATH_SUFFIXES)
