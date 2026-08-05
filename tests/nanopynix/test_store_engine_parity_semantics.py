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
``CoreStore``, which both engines call -- and this file is what keeps them
fixed.

Scope is deliberate rather than exhaustive. The store operations that *can*
disagree are the ones that normalise an argument or classify an error;
``get_store_dir`` and its kind cannot, and listing them would make this file
look more protective than it is.

Which is why ``PATH_ROWS`` exists. Both divergences above were found on one
operation each, and the fix was shared for all of them -- so pinning two
operations pins the fix but not the surface. Every read-only operation that
takes a store path now gets probed with the same six inputs, from unparseable
through well-formed-but-absent to really-there, and the whole row is compared
at once. Measured when it was added: all twelve rows agree, and the row form is
what makes that cheap to keep true.

An outcome is a returned value or an exception **type name**, never a message:
Nix colourises, ``BuildResult.error_msg`` carries ANSI escapes, and the two
transports wrap differently. The type is what a caller branches on.

What this file *cannot* catch, stated so it is not mistaken for cover: every
assertion here compares the two engines against each other, so a regression
that moves both of them equally still reads as agreement. Normalisation is now
shared, which makes that the likely shape of the next one. Measured: deleting
the absolutization from ``CoreStore._store_path`` turns the two relative-path
cases below red and leaves the malformed ones green, because both engines
degrade together. ``tests/nanopynix/bindings/test_store_empty_path.py`` is what
pins those to an absolute expected type -- the same experiment turns *it* red
on ``x`` and ``garbage``. The two files are complements, and neither is
sufficient alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

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

# A hash part is not a path and does not go through path normalisation, so it
# gets its own inputs: too short, wrong length by one, and a well-formed one
# that matches nothing.
HASH_PARTS: list[str] = ["", "x", "a" * 33, "z" * 32]


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


async def _build_paths_with_results(store: Any, suffix: str) -> list[tuple[str, list[str], str]]:
    """Project to the echoed drv_path, outputs and status, dropping error_msg.

    The echoed derived path is the load-bearing part: Nix hands back the
    *canonical* DerivedPath, so a bare ``.drv`` comes back selecting every
    output and ``^out,dev`` comes back sorted as ``dev, out``. That round trip
    is only possible if the argument actually reached ``DerivedPath::parse``,
    which makes it a far stronger witness than "it did not raise".

    It used to be observed as a ``^`` suffix on ``drv_path``, because that is
    what the field carried. It now carries a store path and ``outputs`` carries
    the selector, so the same evidence is read from the field that means it.
    """
    results = await store.build_paths_with_results([await _drv_argument(store, suffix)])
    return [(str(r.drv_path), [str(o) for o in r.outputs], str(r.status)) for r in results]


def _hash_part(seeded: StorePath) -> str:
    """The 32-character hash component of a store path, without its name."""
    return _relative(seeded).split("-", 1)[0]


def _relative(seeded: StorePath) -> str:
    """The seeded path with its store directory stripped off.

    ``CoreStore._store_path`` resolves this against the store directory. rpc
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


@dataclass(frozen=True)
class PathRow:
    """One operation, probed with every input in ``_row_inputs``.

    A row rather than one case per input, for two reasons. It is the shape the
    question actually has -- "does this operation classify the same way as the
    other engine, across the range from unparseable to present" -- and it is
    one session per operation instead of one per cell, which is what makes
    covering the whole surface affordable rather than a minute of process
    spawning.
    """

    name: str
    operation: Callable[[Any, str], Awaitable[Any]]


# Every read-only operation that takes a store path. Read-only is the whole
# admission criterion: these run against the suite's shared store, twice per
# case, so an operation that changed it would make the second engine's answer
# depend on the first's. That excludes `ensure_path` (substitutes, so also
# slow and network-shaped), the three root-creating calls, and the three
# store-wide ones (`collect_garbage`, `optimise_store`, `verify_store`).
#
# `add_temp_root` is here despite writing something, because what it writes is
# a root that lives and dies with the session holding it -- nothing a later
# case can observe. It earns its place by normalising its argument, which is
# the property under test.
#
# Eleven of the twelve normalise through `CoreStore._store_path`.
# `follow_links_to_store_path` is the one that does not, by design: its argument
# is an arbitrary filesystem path that Nix resolves and then locates in the
# store, so it never sees a `StorePath`. Measured, not assumed -- collapsing
# `_store_path` turns the other eleven rows red and leaves that one green.
PATH_ROWS: list[PathRow] = [
    PathRow("is_valid_path", lambda store, path: store.is_valid_path(path)),
    PathRow("parse_store_path", lambda store, path: store.parse_store_path(path)),
    PathRow("query_path_info", lambda store, path: store.query_path_info(path)),
    PathRow("read_derivation", lambda store, path: store.read_derivation(path)),
    PathRow("compute_fs_closure", lambda store, path: store.compute_fs_closure(path)),
    PathRow("query_referrers", lambda store, path: store.query_referrers(path)),
    PathRow("query_valid_derivers", lambda store, path: store.query_valid_derivers(path)),
    PathRow("query_derivation_outputs", lambda store, path: store.query_derivation_outputs(path)),
    PathRow("query_substitutable_paths", lambda store, path: store.query_substitutable_paths([path])),
    PathRow("get_build_log", lambda store, path: store.get_build_log(path)),
    PathRow("add_temp_root", lambda store, path: store.add_temp_root(path)),
    PathRow("follow_links_to_store_path", lambda store, path: store.follow_links_to_store_path(path)),
]

# The one operation whose argument is not a path. Same row mechanism, different
# inputs, so it is a row of its own rather than an exception inside the table
# above.
HASH_PART_ROWS: list[PathRow] = [
    PathRow("query_path_from_hash_part", lambda store, part: store.query_path_from_hash_part(part)),
]


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
        StoreCase(
            f"parse_store_path_{path or 'empty'!r}", lambda store, _seeded, path=path: store.parse_store_path(path)
        )
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


def _malformed_label(path: str) -> str:
    return f"malformed {path!r}"


def _row_inputs(seeded: StorePath) -> dict[str, str]:
    """The inputs every path row is probed with, worst-formed first.

    Three bands, and the row is only interesting because it spans all of them:
    inputs that cannot be parsed at all, one that parses cleanly but names
    nothing, and one that is really in the store. An engine can agree with the
    other on the first band and still disagree on the third.
    """
    return {
        **{_malformed_label(path): path for path in MALFORMED_PATHS},
        "relative": _relative(seeded),
        "absent": _ABSENT,
        "present": str(seeded),
    }


# Which labels make up each band. The distinctness gate compares bands rather
# than individual cells, so the grouping has to be stated somewhere; keeping it
# next to _row_inputs is what lets a test assert the two agree on the labels
# they name, which is also what catches a new malformed spelling colliding with
# a band's own label and silently deleting a probe.
_BANDS: dict[str, tuple[str, ...]] = {
    "unparseable": tuple(_malformed_label(path) for path in MALFORMED_PATHS),
    "absent": ("absent",),
    # The relative spelling belongs here rather than in a band of its own: it
    # resolves to the seeded path, so a correct engine answers it exactly as it
    # answers the absolute one.
    "present": ("relative", "present"),
}


async def _run_row(factory: Any, row: PathRow, inputs: dict[str, str]) -> dict[str, Outcome]:
    """Every input through one operation, in one session.

    Outcomes are the real objects, not their reprs, so a ``PathInfo`` is
    compared field by field the way the single-case tables compare their
    values.
    """
    outcomes: dict[str, Outcome] = {}
    async with factory() as session, session.store() as store:
        for label, value in inputs.items():
            try:
                outcomes[label] = ("value", await row.operation(store, value))
            except Exception as exc:  # the exception type *is* the measurement
                outcomes[label] = ("raise", type(exc).__name__)
    return outcomes


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


@pytest.mark.parametrize("row", PATH_ROWS, ids=lambda row: row.name)
async def test_engines_agree_across_a_whole_input_row(
    row: PathRow,
    seeded_store_path: StorePath,
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """One operation, six inputs, both engines -- every cell must match.

    The comparison is per label rather than dict-to-dict so a failure names the
    input that disagreed instead of printing two six-entry dicts side by side.
    """
    inputs = _row_inputs(seeded_store_path)
    inproc_row = await _run_row(inproc_session, row, inputs)
    rpc_row = await _run_row(rpc_session, row, inputs)

    for label in inputs:
        assert inproc_row[label] == rpc_row[label], (
            f"{row.name} disagrees on {label}: inproc={inproc_row[label]!r} rpc={rpc_row[label]!r}"
        )


@pytest.mark.parametrize("row", PATH_ROWS, ids=lambda row: row.name)
async def test_every_row_tells_its_inputs_apart(
    row: PathRow,
    seeded_store_path: StorePath,
    inproc_session: InprocSessionFactory,
) -> None:
    """A row that answers everything the same way proves nothing about anything.

    The teeth for the blind spot the module docstring names: the comparison
    above holds two engines against each other, so a change that degraded both
    -- normalisation rejecting every input, or an error handler swallowing
    every failure into one class -- would still read as agreement. Measured:
    collapsing ``CoreStore._store_path`` onto one input turns eleven of the
    twelve rows red here while leaving the parity comparison above green.

    Two named bands, not "any two cells differ". The weaker phrasing is
    satisfied by two malformed spellings failing differently, which says
    nothing about whether the operation can tell a real path from garbage --
    and it is what a comparison over all three bands collapses to anyway, since
    the absent band holds one label and so can only equal a band that is
    uniform. So the assertion is specifically that the present band and the
    unparseable band answer differently.

    Not "present must succeed": ``read_derivation`` raises on the seeded path,
    which is a regular file and not a derivation. That is a different failure
    from rejecting garbage, and telling those two apart is exactly the property
    being asserted.

    One engine is enough. This is about the operation's own behaviour, not
    about the two agreeing, and the test above already covers the agreeing.
    """
    outcomes = await _run_row(inproc_session, row, _row_inputs(seeded_store_path))

    empty = outcomes[_malformed_label("")]
    assert empty[0] == "raise", f"{row.name} accepted an empty path: {empty!r}"
    bands = {band: frozenset(repr(outcomes[label]) for label in labels) for band, labels in _BANDS.items()}
    assert bands["present"] != bands["unparseable"], f"{row.name} cannot tell a real store path from garbage: {bands!r}"


@pytest.mark.parametrize("row", HASH_PART_ROWS, ids=lambda row: row.name)
async def test_engines_agree_across_a_hash_part_row(
    row: PathRow,
    seeded_store_path: StorePath,
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """The hash-part half of the row comparison, including a real hash part.

    The seeded path's own hash part is what makes this more than a table of
    rejections: it is the one input that must resolve back to the path it came
    from, on both engines.
    """
    inputs = {**{f"malformed {part!r}": part for part in HASH_PARTS}, "present": _hash_part(seeded_store_path)}
    inproc_row = await _run_row(inproc_session, row, inputs)
    rpc_row = await _run_row(rpc_session, row, inputs)

    for label in inputs:
        assert inproc_row[label] == rpc_row[label], (
            f"{row.name} disagrees on {label}: inproc={inproc_row[label]!r} rpc={rpc_row[label]!r}"
        )
    assert inproc_row["present"] == ("value", seeded_store_path), (
        f"a real hash part must resolve to its own path, got {inproc_row['present']!r}"
    )


async def test_an_unmapped_gc_action_is_a_ValueError_on_both_engines(  # noqa: N802 -- ValueError is a type name, not a word
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """The one store divergence the wire forces, asserted at the level that agrees.

    Every other case here compares exception *type names*. This one cannot:
    inproc rejects an unmapped ``GcAction`` in the shared ``CoreStore`` and
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

    ``_build_paths_with_results`` keeps only ``drv_path``, ``outputs`` and
    ``status``. If a future change made that projection constant -- or made Nix
    echo the input back unparsed -- every selector case would still compare
    equal across the engines and the parametrization would be decorative. Nix
    canonicalises, so a bare ``.drv`` selects every output and ``^out,dev``
    comes back sorted as ``dev, out``; observing that reordering is what proves
    the string went through ``DerivedPath::parse`` rather than being passed
    along verbatim.
    """
    async with inproc_session() as session, session.store() as store:
        outcomes = {suffix: await _build_paths_with_results(store, suffix) for suffix in DERIVED_PATH_SUFFIXES}

    assert outcomes["^out,dev"][0][1] == ["dev", "out"], (
        f"expected Nix to canonicalise ^out,dev, got {outcomes['^out,dev']!r}"
    )
    assert outcomes[""][0][1] == ["*"], f"expected a bare .drv to select every output, got {outcomes['']!r}"
    assert outcomes["^out"] != outcomes["^out,dev"], "the projection cannot tell two selectors apart"
    # The field is now what its name says. Before this change it was the `^`
    # DerivedPath spelling, which no store-path accessor could read.
    for suffix, results in outcomes.items():
        assert results[0][0].endswith(".drv"), f"{suffix or 'no selector'}: drv_path is not a store path: {results!r}"


async def test_a_bare_drv_round_trips_through_its_own_reply(
    inproc_session: InprocSessionFactory,
) -> None:
    """Feeding a reply's ``drv_path`` back in must ask for the same build.

    Inbound (``models.DerivedPath.for_build``, in Python) and outbound
    (``derived_path_parts``, in a C++ header) are the two halves of the same
    boundary, and now in two different languages with nothing tying them
    together. This is the property that makes them inverses: a bare ``.drv``
    is read as "build every output" and reported as ``outputs == ["*"]``, and
    sending the reported ``drv_path`` -- which is bare again -- must land on
    that same request rather than on an opaque fetch. Two rules that could
    disagree instead compose into a fixed point.
    """
    async with inproc_session() as session, session.store() as store:
        first = await _build_paths_with_results(store, "")
        second = [
            (str(r.drv_path), [str(o) for o in r.outputs], str(r.status))
            for r in await store.build_paths_with_results([first[0][0]])
        ]

    assert first[0][1] == ["*"]
    assert second == first


async def test_a_bare_drv_means_every_output_on_both_engines(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """The convenience half of the split that issue #67 is about.

    ``models.DerivedPath.for_build`` lives in Python and each engine's
    ``Store`` calls it, so this asks whether *both* of them do. The bindings
    deliberately do not --
    ``test_a_bare_derivation_is_opaque_here_and_selects_no_outputs`` in
    ``tests/nanopynix/bindings/test_l1_store_bindings.py`` is that half --
    which means nothing below this layer would catch an engine that dropped
    the call.

    ``query_missing`` is tested beside ``build_paths_with_results`` because
    the two have to agree. A caller asks the first whether the second would do
    any work, and an opaque ``.drv`` answers "nothing to build" for a
    derivation that was never built: a confident wrong answer in the dangerous
    direction, which is what the issue reported.
    """
    outcomes: dict[str, tuple[list[str], dict[str, list[str]], dict[str, list[str]]]] = {}
    for name, factory in (("inproc", inproc_session), ("rpc", rpc_session)):
        async with factory() as session, session.store() as store:
            built = await _build_paths_with_results(store, "")
            outcomes[name] = (built[0][1], await _query_missing(store, ""), await _query_missing(store, "^*"))

    for name, (outputs, bare, explicit) in outcomes.items():
        assert outputs == ["*"], f"{name}: a bare .drv did not select every output, got {outputs!r}"
        assert bare == explicit, f"{name}: a bare .drv disagreed with ^* in query_missing: {bare!r} != {explicit!r}"


def test_the_malformed_table_covers_the_paths_that_used_to_diverge() -> None:
    """``x`` and ``garbage`` are the two absolutization changed; losing them would
    retire the regression test for the convergence without saying so."""
    assert {"x", "garbage", ""} <= set(MALFORMED_PATHS)


def test_both_case_tables_are_populated() -> None:
    """An empty table passes every parametrized test by having no cases at all."""
    assert SUCCESS_CASES
    assert FAILURE_CASES


def test_every_row_input_keeps_its_own_label() -> None:
    """A collision would delete a probe and leave every row still passing.

    ``_row_inputs`` is a dict keyed by label, so two inputs rendering to one
    label means one of them is simply never run -- and the rows would go on
    agreeing about the five that remain. This pins the count, and pins that
    ``_BANDS`` names exactly the labels that exist, which is the same failure
    seen from the other side: a malformed spelling equal to ``"present"`` would
    both swallow a probe and quietly move a cell into the wrong band.
    """
    inputs = _row_inputs(cast("Any", f"/nix/store/{_HASH_PART}-nanopynix-label-probe"))

    assert len(inputs) == len(MALFORMED_PATHS) + 3, f"a row input lost its label: {sorted(inputs)}"
    banded = {label for labels in _BANDS.values() for label in labels}
    assert banded == set(inputs), f"bands and inputs disagree: {banded ^ set(inputs)}"


def test_case_names_are_unique() -> None:
    """Duplicate ids silently mean one case shadows another in the report."""
    names = [case.name for case in SUCCESS_CASES + FAILURE_CASES]
    assert len(names) == len(set(names)), sorted({n for n in names if names.count(n) > 1})
