"""Same store, same operation, both engines -- do they *behave* the same?

The store half of :mod:`tests.nanopynix.test_engine_parity_semantics`, which
covers eval only. It exists because the store's two cross-engine divergences
were both found by hand-probing rather than by any gate:

* ``query_missing`` rejected Nix's ``^`` output selector on inproc and
  accepted it on rpc, because inproc reached a ``StorePath``-taking binding
  while rpc went through the proto-dict funnel's ``DerivedPath::parse``.
* A *relative* store path was absolutized by rpc and not by inproc, so
  ``parse_store_path("x")`` answered ``NixError`` on one engine and
  ``BadStorePathError`` on the other.

:mod:`tests.nanopynix.test_engine_parity` could see neither: both engines had
the same method names and the same parameter lists throughout. Both are fixed
-- path normalisation and derived-path parsing now live in the shared
``LocalStore``, which both engines call -- and this file is what keeps them
fixed.

Scope is deliberate rather than exhaustive. The store operations that *can*
disagree are the ones that normalise an argument or classify an error;
``get_store_dir`` and its kind cannot, and listing them would make this file
look more protective than it is.

An outcome is a returned value or an exception **type name**, never a message:
Nix colourises, ``BuildResult.error_msg`` carries ANSI escapes, and the two
transports wrap differently. The type is what a caller branches on.

What this file *cannot* catch, stated so it is not mistaken for cover: every
assertion here compares the two engines against each other, so a regression
that moves both of them equally still reads as agreement. Normalisation is now
shared, which makes that the likely shape of the next one. Measured: deleting
the absolutization from ``LocalStore._store_path`` turns the two relative-path
cases below red and leaves the malformed ones green, because both engines
degrade together. ``tests/nanopynix/bindings/test_store_empty_path.py`` is what
pins those to an absolute expected type -- the same experiment turns *it* red
on ``x`` and ``garbage``. The two files are complements, and neither is
sufficient alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nanopynix import StorePath
    from tests.support.nix_environment import InprocSessionFactory, RpcSessionFactory

# A well-formed store path that no store will ever contain. What is under test
# is whether the argument *parses*; an unknown derivation is a perfectly good
# answer to "is this missing?" and to "build this". Using a real derivation
# would test Nix's scheduler instead.
_HASH_PART = "a" * 32
_DRV_NAME = f"{_HASH_PART}-nanopynix-parity-probe.drv"
_ABSENT = f"/nix/store/{_HASH_PART}-nanopynix-absent"

# The historical all-outputs spelling, plus the three selector spellings Nix
# accepts on top of one.
DERIVED_PATH_SUFFIXES: list[str] = ["", "^out", "^out,dev", "^*"]

# Malformed in four different ways: empty (the input that used to abort the
# process rather than raise), a bare name, a bare word, and an absolute path
# outside the store. All four must land on the same type on both engines.
MALFORMED_PATHS: list[str] = ["", "x", "garbage", "/etc/passwd"]


# ── Operations ───────────────────────────────────────────────────────


async def _drv_argument(store: Any, suffix: str) -> str:
    store_dir = (await store.store_dir()).rstrip("/")
    return f"{store_dir}/{_DRV_NAME}{suffix}"


async def _query_missing(store: Any, suffix: str) -> dict[str, list[str]]:
    result = await store.query_missing([await _drv_argument(store, suffix)])
    return {
        "will_build": sorted(str(p) for p in result.will_build),
        "will_substitute": sorted(str(p) for p in result.will_substitute),
        "unknown": sorted(str(p) for p in result.unknown),
    }


async def _build_paths_with_results(store: Any, suffix: str) -> list[tuple[str, str]]:
    """Project to the echoed drv_path and status, dropping error_msg.

    The echoed ``drv_path`` is the load-bearing part: Nix hands back the
    *canonical* DerivedPath, so a bare ``.drv`` comes back as ``drv^*`` and
    ``^out,dev`` comes back sorted as ``^dev,out``. That round trip is only
    possible if the argument actually reached ``DerivedPath::parse``, which
    makes it a far stronger witness than "it did not raise".
    """
    results = await store.build_paths_with_results([await _drv_argument(store, suffix)])
    return [(str(r.drv_path), str(r.status)) for r in results]


def _relative(seeded: StorePath) -> str:
    """The seeded path with its store directory stripped off.

    ``LocalStore._store_path`` resolves this against the store directory. rpc
    always did (its C++ funnel called ``store_path_from_string``); inproc never
    did (its direct binding deliberately does not absolutize). Both go through
    the one shared implementation now.
    """
    return str(seeded).rsplit("/", 1)[-1]


# ── The cases ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StoreCase:
    """One store operation, run identically on both engines."""

    name: str
    operation: Callable[[Any, StorePath], Awaitable[Any]]


SUCCESS_CASES: list[StoreCase] = [
    # The two entry points that take DerivedPaths rather than StorePaths.
    # query_missing is where the ^ divergence was found; build_paths_with_results
    # is its twin -- same C++ rework, and its own docstring already advertised
    # the selector support its neighbour lacked.
    *[
        StoreCase(
            f"query_missing{suffix or '_no_selector'}",
            lambda store, _seeded, suffix=suffix: _query_missing(store, suffix),
        )
        for suffix in DERIVED_PATH_SUFFIXES
    ],
    *[
        StoreCase(
            f"build_paths_with_results{suffix or '_no_selector'}",
            lambda store, _seeded, suffix=suffix: _build_paths_with_results(store, suffix),
        )
        for suffix in DERIVED_PATH_SUFFIXES
    ],
    # The positive half of the absolutization change. The malformed cases below
    # prove the two engines now reject the same things; these prove they accept
    # the same things, which is the behaviour that actually changed.
    StoreCase("is_valid_path_absolute", lambda store, seeded: store.is_valid_path(str(seeded))),
    StoreCase("is_valid_path_relative", lambda store, seeded: store.is_valid_path(_relative(seeded))),
    StoreCase("parse_store_path_absolute", lambda store, seeded: store.parse_store_path(str(seeded))),
    StoreCase("parse_store_path_relative", lambda store, seeded: store.parse_store_path(_relative(seeded))),
]


FAILURE_CASES: list[StoreCase] = [
    # Every malformed spelling, through both the parser itself and a method
    # that normalises on the way in. "x" and "garbage" are the two that used to
    # answer differently per engine.
    *[
        StoreCase(f"parse_store_path_{path or 'empty'!r}", lambda store, _seeded, path=path: store.parse_store_path(path))
        for path in MALFORMED_PATHS
    ],
    *[
        StoreCase(f"is_valid_path_{path or 'empty'!r}", lambda store, _seeded, path=path: store.is_valid_path(path))
        for path in MALFORMED_PATHS
    ],
    # Well-formed but absent: the error must classify as "not in this store"
    # rather than "not a store path", on both engines.
    StoreCase("query_path_info_absent", lambda store, _seeded: store.query_path_info(_ABSENT)),
    StoreCase("read_derivation_absent", lambda store, _seeded: store.read_derivation(_ABSENT)),
    StoreCase("compute_fs_closure_absent", lambda store, _seeded: store.compute_fs_closure(_ABSENT)),
]


# ── Running one case on one engine ───────────────────────────────────

Outcome = tuple[str, Any]


async def _run(factory: Any, case: StoreCase, seeded: StorePath) -> Outcome:
    async with factory() as session, session.store() as store:
        try:
            return ("value", await case.operation(store, seeded))
        except Exception as exc:  # the exception type *is* the measurement
            return ("raise", type(exc).__name__)


@pytest.mark.parametrize("case", SUCCESS_CASES, ids=lambda case: case.name)
async def test_engines_agree_on_success(
    case: StoreCase,
    seeded_store_path: StorePath,
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """The same operation on the same store must return the same value."""
    inproc_outcome = await _run(inproc_session, case, seeded_store_path)
    rpc_outcome = await _run(rpc_session, case, seeded_store_path)

    assert inproc_outcome[0] == "value", f"inproc raised {inproc_outcome[1]!r}"
    assert inproc_outcome == rpc_outcome


@pytest.mark.parametrize("case", FAILURE_CASES, ids=lambda case: case.name)
async def test_engines_agree_on_failure(
    case: StoreCase,
    seeded_store_path: StorePath,
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """The same bad input must arrive as the same exception type on both engines."""
    inproc_outcome = await _run(inproc_session, case, seeded_store_path)
    rpc_outcome = await _run(rpc_session, case, seeded_store_path)

    assert inproc_outcome[0] == "raise", f"inproc did not raise: {inproc_outcome!r}"
    assert inproc_outcome == rpc_outcome


async def test_an_unmapped_gc_action_is_a_ValueError_on_both_engines(  # noqa: N802 -- ValueError is a type name, not a word
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """The one store divergence the wire forces, asserted at the level that agrees.

    Every other case here compares exception *type names*. This one cannot:
    inproc rejects an unmapped ``GcAction`` in the shared ``LocalStore`` and
    raises ``ValueError``, while rpc never reaches the shared layer -- a proto
    enum field cannot carry an arbitrary object, so pydantic rejects it during
    request construction and raises its own ``ValidationError``.

    That asymmetry is forced by serialisation rather than chosen, so it is not
    a defect to be retired. What a caller actually branches on still agrees,
    because pydantic's ``ValidationError`` subclasses ``ValueError``: one
    ``except ValueError`` is correct on both engines. Asserting exactly that,
    in its own test, keeps the main comparison table strict instead of
    quietly loosening one entry in it.

    The action is rejected before any collection happens, so this is safe to
    run against the suite's shared store.
    """
    for factory in (inproc_session, rpc_session):
        async with factory() as session, session.store() as store:
            with pytest.raises(ValueError, match=r"unsupported garbage-collection action|validation error"):
                await store.collect_garbage(object())  # type: ignore[arg-type] -- exercising the unmapped-action guard


# ── The harness's own teeth ──────────────────────────────────────────


def test_the_selector_table_actually_exercises_selectors() -> None:
    """Without a ``^`` entry the derived-path cases would test nothing."""
    assert any("^" in suffix for suffix in DERIVED_PATH_SUFFIXES)


async def test_the_selector_actually_reaches_nix(
    inproc_session: InprocSessionFactory,
) -> None:
    """Different selectors must produce different results, or the projection is blind.

    ``_build_paths_with_results`` keeps only ``drv_path`` and ``status``. If a
    future change made that projection constant -- or made Nix echo the input
    back unparsed -- every selector case would still compare equal across the
    engines and the parametrization would be decorative. Nix canonicalises, so
    a bare ``.drv`` returns ``^*`` and ``^out,dev`` returns ``^dev,out``;
    observing that reordering is what proves the string went through
    ``DerivedPath::parse`` rather than being passed along verbatim.
    """
    async with inproc_session() as session, session.store() as store:
        outcomes = {suffix: await _build_paths_with_results(store, suffix) for suffix in DERIVED_PATH_SUFFIXES}

    assert outcomes["^out,dev"][0][0].endswith("^dev,out"), (
        f"expected Nix to canonicalise ^out,dev, got {outcomes['^out,dev']!r}"
    )
    assert outcomes[""][0][0].endswith("^*"), f"expected a bare .drv to canonicalise to ^*, got {outcomes['']!r}"
    assert outcomes["^out"] != outcomes["^out,dev"], "the projection cannot tell two selectors apart"


def test_the_malformed_table_covers_the_paths_that_used_to_diverge() -> None:
    """``x`` and ``garbage`` are the two absolutization changed; losing them would
    retire the regression test for the convergence without saying so."""
    assert {"x", "garbage", ""} <= set(MALFORMED_PATHS)


def test_both_case_tables_are_populated() -> None:
    """An empty table passes every parametrized test by having no cases at all."""
    assert SUCCESS_CASES
    assert FAILURE_CASES


def test_case_names_are_unique() -> None:
    """Duplicate ids silently mean one case shadows another in the report."""
    names = [case.name for case in SUCCESS_CASES + FAILURE_CASES]
    assert len(names) == len(set(names)), sorted({n for n in names if names.count(n) > 1})
