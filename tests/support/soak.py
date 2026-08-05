"""Run the tests that already exist, concurrently, and record what overlapped.

The ThreadSanitizer jobs only ever see concurrency that somebody wrote on
purpose. `-m concurrency` selects the hand-written overlap tests, and
`-m tsan_stress` selected exactly one of them. A race in a code path that no
such test happens to touch is invisible, and the suite has about 1600 tests
that touch those paths one at a time.

This module drives those tests instead of adding new ones. It finds every test
whose fixtures it can supply, deals them into lanes under a seed, and runs the
lanes together. `nanopynix.inproc` puts each Nix call on an executor thread, so
concurrent coroutines become concurrent threads inside the instrumented
process, which is what TSan needs to see.

**A run records its schedule, so a race can be looked at again.** The exact
interleaving is not reproducible, and it does not need to be: TSan compares
happens-before relations, so it reports two unordered accesses whether or not
the harmful timing occurred. What must come back is the *composition* -- which
tests were in flight together -- and a seed gives that.

`tests/nanopynix/test_concurrent_soak.py` is the test that calls this, and
`tests/meta/test_soak_roster.py` keeps the denylist below honest.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

# The fixtures this driver can supply itself. A test that wants anything else
# stays out of the roster: the driver is not a second pytest, and a fixture it
# fakes badly is worse than a test it leaves alone.
_FACTORY_FIXTURES = ("inproc_session", "rpc_session")
_SUPPLIED_FIXTURES = frozenset({*_FACTORY_FIXTURES, "tmp_path"})

# A mark that means the test cannot join a lane. `live_gc` destroys the shared
# store under its peers, `forked` asks for a process the driver does not give,
# and the three skip/xfail marks mean the outcome is already decided.
_DISQUALIFYING_MARKS = frozenset({"live_gc", "forked", "skip", "skipif", "xfail"})

# Only the library's own tests. `tests/pynix/` drives a CLI and an LSP server
# through subprocesses, which puts no extra thread into this process.
_ROSTER_ROOT = Path("tests/nanopynix")

# A test that cannot share a lane, and the reason it cannot.
#
# **An entry here is a finding that somebody decided not to fix, so it carries
# the reason.** `tests/meta/test_soak_roster.py` fails on an entry with no
# reason, and on an entry whose test no longer exists.
DENYLIST: dict[str, str] = {
    "tests/nanopynix/inproc/test_inproc.py::test_inproc_session_rejects_second_concurrent_session": (
        "asserts that _InprocProcessGuard refuses a second Session. Every lane borrows the one "
        "Session, so there is never a second one to refuse."
    ),
    "tests/nanopynix/inproc/test_inproc.py::test_inproc_copy_closure_rejects_a_store_from_another_session": (
        "needs two distinct Sessions to make a Store foreign to the second one. Its own docstring "
        "says the two are sequential, and a borrowed Session cannot be either of them."
    ),
    "tests/nanopynix/inproc/test_inproc.py::test_inproc_session_log_stream_yields_events": (
        "reads the first event off the session-wide log stream and asserts it is its own. Every "
        "lane emits into that same stream, so the first event belongs to whichever lane won."
    ),
    # The two below forced a segmentation fault in `nix::ExprVar::eval`, on a
    # peer thread, twice at the same seed. `_gc_collect` collects the one Boehm
    # heap that every lane's values live in, so a lane that forces a collection
    # frees what another lane is in the middle of evaluating. `live_gc` names
    # the same hazard for the store collector, and the roster already excludes
    # that mark; these two reach the *Boehm* collector through a helper, which
    # no mark records.
    "tests/nanopynix/inproc/test_inproc.py::test_a_collected_value_gives_its_root_back_to_the_collector": (
        "forces a Boehm collection through _root_bytes. The heap it collects is shared with "
        "every other lane, and a peer mid-evaluation segfaults."
    ),
    "tests/nanopynix/inproc/test_inproc.py::test_a_dropped_parent_gives_its_root_back_while_a_child_lives": (
        "forces a Boehm collection through _root_bytes. See the entry above."
    ),
    # The *session's* verbosity is one setting for the whole session, so a lane
    # that sets it sets it for every peer. The soak found this the moment a
    # second verbosity test joined the roster: the two below ran side by side
    # and the one that read back got the other one's level.
    #
    # Neither test is wrong, and neither can be fixed while the session is
    # shared. A test of a per-session setting needs a session of its own, and
    # a lane never has one.
    #
    # An *evaluator's* level is not shared, because each lane opens its own
    # evaluator. So the per-evaluator tests in `test_verbosity.py` need no
    # entry of their own -- and they would be redundant here anyway, because
    # each drives both engines from one body and `_candidates_in` takes only a
    # test that names exactly one factory fixture.
    "tests/nanopynix/test_verbosity.py::test_a_change_of_level_reaches_a_thread_that_already_ran": (
        "writes the verbosity of the borrowed Session and reads it back. A peer that writes the "
        "same one setting in between makes the read report the peer's level."
    ),
    "tests/nanopynix/inproc/test_inproc.py::test_inproc_session_verbosity_roundtrip": (
        "writes and reads the same session-wide verbosity. See the entry above. It was in the "
        "roster before the entry above joined it, and it passed only because it was the sole writer."
    ),
    # The two below drive Nix to stack exhaustion on purpose. Their own module
    # says what a regression does: it does not fail the test, it aborts the
    # whole pytest process. In a lane that blast radius is every peer.
    #
    # They pass under a plain build and under TSAN, because the evaluator
    # thread gets the 60 MiB that `nix::initNix()` asks for. Under ASAN they
    # do not, and `develop` shows them failing there with no soak involved.
    # The soak then took all 65 peers of the lane down with them.
    #
    # So the rule is not "skip these under ASAN". A test whose failure mode is
    # a dead process cannot share a lane on any build, and the roster reads
    # neither the sanitizer nor a skip mark.
    "tests/nanopynix/test_scalar_accessor_semantics.py::test_inproc_reports_runaway_recursion_at_nixs_default_depth": (
        "drives the evaluator to Nix's recursion limit. A regression aborts the process rather "
        "than failing the test, which would end every peer lane too."
    ),
    "tests/nanopynix/test_scalar_accessor_semantics.py::test_rpc_reports_runaway_recursion_at_nixs_default_depth": (
        "drives the worker to Nix's recursion limit. See the entry above. The worker dies rather "
        "than answering, and a peer that shares the worker gets the same dead connection."
    ),
    # This one counts the values of its own evaluator, and the evaluator is
    # its own, so a peer never adds to the count. What a peer takes away is an
    # idle event loop.
    #
    # The test drops 500 children, collects, and expects the count to come
    # back to one. Its own comment says what stands between the drop and the
    # release: asyncio holds a handle on the most recently awaited result, and
    # `as_dict` awaits a dict of every child. One step of the loop clears that
    # handle when the loop has nothing else to do. Beside seven other lanes
    # the loop always has something else to do, so the handle outlives the
    # step and the roots stay.
    #
    # Seen in CI run 30916818149 at seed 5, and again in run 30920512961 at
    # seed 4 after the entries above changed the roster.
    "tests/nanopynix/inproc/test_inproc.py::test_a_collected_value_releases_its_nix_root": (
        "expects one step of the event loop to drop asyncio's handle on the last awaited result. "
        "A lane never has an idle loop, so the handle outlives the step and the roots stay."
    ),
    # The two below call `time.sleep` on the event loop, on purpose:
    # `test_log_backpressure` is about what a caller who blocks their own loop
    # pays, so the blocking call is the condition under test rather than a
    # defect, and the module says so.
    #
    # The lanes share one event loop. A test that holds that loop closed for
    # three seconds holds it closed for all seven peers, so it does not read
    # its own behaviour under concurrency -- it dictates everybody else's.
    # This is the same rule as the two runaway recursion entries above: a test
    # whose method harms its peers cannot share a lane, whatever it measures.
    "tests/nanopynix/rpc/test_log_backpressure.py::test_the_capture_says_what_the_stall_cost": (
        "holds the shared event loop closed with `time.sleep`, which stalls every peer lane for "
        "three seconds. Seen failing in CI run 30925089313."
    ),
    "tests/nanopynix/rpc/test_log_backpressure.py::test_a_client_that_stops_reading_does_not_stop_the_evaluation": (
        "holds the shared event loop closed with `time.sleep`. See the entry above; it is the "
        "same helper and the same three seconds."
    ),
}


@dataclass(frozen=True)
class SoakCandidate:
    """One test the driver can call, and what it needs to be called with."""

    nodeid: str
    factory: str
    wants_tmp_path: bool
    func: Any

    @property
    def engine(self) -> str:
        return "inproc" if self.factory == "inproc_session" else "rpc"


@dataclass
class SoakEvent:
    """When one test ran, in which lane, and how it ended."""

    nodeid: str
    lane: int
    start_ms: float
    end_ms: float
    outcome: str
    detail: str = ""


@dataclass
class SoakResult:
    seed: int
    lanes: list[list[str]]
    roster_hash: str
    events: list[SoakEvent] = field(default_factory=list[SoakEvent])

    @property
    def failures(self) -> list[SoakEvent]:
        return [event for event in self.events if event.outcome == "fail"]

    def overlapping(self, event: SoakEvent) -> list[str]:
        """Every test that was in flight while ``event`` ran."""
        return sorted(
            other.nodeid
            for other in self.events
            if other is not event and other.start_ms < event.end_ms and other.end_ms > event.start_ms
        )

    def manifest(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "roster_hash": self.roster_hash,
            "lanes": self.lanes,
            "events": [
                {
                    "nodeid": event.nodeid,
                    "lane": event.lane,
                    "start_ms": round(event.start_ms, 3),
                    "end_ms": round(event.end_ms, 3),
                    "outcome": event.outcome,
                    "detail": event.detail,
                }
                for event in self.events
            ],
        }


class BorrowedSession:
    """A Session that a lane may use but must not close.

    ``_InprocProcessGuard`` allows one open ``inproc.Session`` per process, so
    the lanes share one. ``Session.close`` does not count its callers, so the
    first lane to leave its ``async with`` would tear the session out from
    under every other lane. This proxy is what stops that: it forwards every
    attribute and makes the exit a no-op.
    """

    __slots__ = ("_session",)

    def __init__(self, session: Any) -> None:
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def __aenter__(self) -> BorrowedSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def _as_marks(value: Any) -> list[Any]:
    """``pytestmark`` is a list, or one bare mark. pytest accepts both."""
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return list(cast("Sequence[Any]", value))
    return [value]


def _all_marks(module: Any, func: Any) -> list[Any]:
    return [
        *_as_marks(getattr(module, "pytestmark", None)),
        *_as_marks(getattr(func, "pytestmark", None)),
    ]


def _marks_of(module: Any, func: Any) -> set[str]:
    return {mark.name for mark in _all_marks(module, func)}


def _wants_an_unsupplied_fixture(module: Any, func: Any) -> bool:
    """Whether ``usefixtures`` asks for a fixture the driver does not supply.

    A parameter names a fixture, and so does this mark, but only the parameter
    shows up in the signature. Missing that cost real time: three
    `test_inproc_cancel.py` tests use ``short_interrupt_grace``, which patches
    the interrupt grace down from 2.0s *before* the evaluator is built. Run
    without it, they cancel inside a window that has not opened yet and assert
    on an executor that was never poisoned -- which reads exactly like a
    concurrency failure, and is not one.
    """
    for mark in _all_marks(module, func):
        if mark.name == "usefixtures" and not set(mark.args) <= _SUPPLIED_FIXTURES:
            return True
    return False


def _calls_factory_with_kwargs(node: ast.AsyncFunctionDef, factory: str) -> bool:
    """Whether the test's body ever calls ``factory`` with keyword arguments.

    A session built with ``primops=`` or ``experimental_features=`` is not the
    shared one, so such a test cannot borrow. This is the rule that a probe
    found rather than guessed: ``test_inproc_yaml_primops`` was the one test of
    seven that failed against a borrowed session, and it failed because
    ``builtins.fromYAML`` was not registered on it.
    """
    return any(
        isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == factory and call.keywords
        for call in ast.walk(node)
    )


def _session_bindings(node: ast.AsyncFunctionDef, factory: str) -> set[str]:
    """Every local name the test binds the Session to.

    Both spellings occur, and the tests that close a Session use the second:
    ``async with inproc_session() as nix`` and ``session = inproc_session()``.
    """

    def is_factory_call(value: ast.expr | None) -> bool:
        return isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == factory

    bound: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.withitem) and is_factory_call(item.context_expr):
            if isinstance(item.optional_vars, ast.Name):
                bound.add(item.optional_vars.id)
        elif isinstance(item, ast.Assign) and is_factory_call(item.value):
            bound.update(target.id for target in item.targets if isinstance(target, ast.Name))
        elif (
            isinstance(item, ast.AnnAssign | ast.NamedExpr)
            and is_factory_call(item.value)
            and isinstance(item.target, ast.Name)
        ):
            bound.add(item.target.id)
    return bound


def _closes_its_session(node: ast.AsyncFunctionDef, factory: str) -> bool:
    """Whether the test closes the Session it was given.

    A borrowed Session is shared, and ``close`` does not count its callers, so
    one such call takes the executor away from every other lane. The first run
    of this driver proved that in one line: 69 of 72 failures were
    ``Nix executor is closing``, ``Session is not open``, or ``queue.ShutDown``.

    The proxy could swallow ``close`` instead. It must not: a test that asserts
    what closing does would then pass while testing nothing.
    """
    bound = _session_bindings(node, factory)
    return any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "close"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id in bound
        for call in ast.walk(node)
    )


def _module_name(path: Path) -> str:
    return ".".join(path.with_suffix("").parts)


def _candidates_in(tree: ast.AST) -> Iterator[tuple[str, str, bool]]:
    """Yield ``(function name, factory, wants tmp_path)`` for each eligible def."""
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or not node.name.startswith("test_"):
            continue
        params = {arg.arg for arg in node.args.args}
        if not params <= _SUPPLIED_FIXTURES:
            continue
        factories = params & set(_FACTORY_FIXTURES)
        if len(factories) != 1:
            continue
        factory = factories.pop()
        if _calls_factory_with_kwargs(node, factory):
            continue
        # Only the inproc lanes share a Session. An rpc lane owns its own, so
        # it may close it whenever it likes.
        if factory == "inproc_session" and _closes_its_session(node, factory):
            continue
        yield node.name, factory, "tmp_path" in params


def discover_roster(*, root: Path, engine: str | None = None) -> list[SoakCandidate]:
    """Every test the driver can run, sorted by nodeid so the order is fixed.

    Discovery is opt-out rather than a curated list, so the roster grows with
    the suite and nothing is quietly left behind. What keeps it safe is that
    every condition is mechanical: the signature says which fixtures a test
    wants, and the marks say whether it can share a process.
    """
    roster: list[SoakCandidate] = []
    for path in sorted((root / _ROSTER_ROOT).rglob("test_*.py")):
        relative = path.relative_to(root)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        eligible = list(_candidates_in(tree))
        if not eligible:
            continue
        module = importlib.import_module(_module_name(relative))
        for name, factory, wants_tmp_path in eligible:
            nodeid = f"{relative.as_posix()}::{name}"
            if nodeid in DENYLIST:
                continue
            func = getattr(module, name, None)
            if func is None or not inspect.iscoroutinefunction(func):
                continue
            if _marks_of(module, func) & _DISQUALIFYING_MARKS:
                continue
            if _wants_an_unsupplied_fixture(module, func):
                continue
            candidate = SoakCandidate(nodeid=nodeid, factory=factory, wants_tmp_path=wants_tmp_path, func=func)
            if engine is not None and candidate.engine != engine:
                continue
            roster.append(candidate)
    roster.sort(key=lambda candidate: candidate.nodeid)
    return roster


def roster_hash(roster: Sequence[SoakCandidate]) -> str:
    """Name the roster, so a replay can say whether it changed underneath."""
    digest = hashlib.sha256("\n".join(candidate.nodeid for candidate in roster).encode()).hexdigest()
    return f"sha256:{digest[:16]}"


def deal_lanes(roster: Sequence[SoakCandidate], *, seed: int, lanes: int) -> list[list[str]]:
    """Shuffle under ``seed`` and deal round-robin, so each lane gets a mix.

    Round-robin after a shuffle, and not a contiguous split: a contiguous split
    puts one directory in one lane, and the point is to overlap tests that
    normally never meet.
    """
    import random  # noqa: PLC0415 -- local, so importing this module cannot reseed a caller's random state

    order = [candidate.nodeid for candidate in roster]
    random.Random(seed).shuffle(order)  # noqa: S311 -- a test schedule, and reproducibility is the whole point
    dealt: list[list[str]] = [[] for _ in range(lanes)]
    for index, nodeid in enumerate(order):
        dealt[index % lanes].append(nodeid)
    return dealt


def _emit(line: str) -> None:
    """Write one schedule line to stdout, unbuffered.

    Not ``print``, and not ``logging``. The TSan job merges stderr into stdout
    and tees the result, so a race report and these lines land in one file --
    and a report then sits between the START and END lines of whatever was in
    flight. That is what makes the log answer "what ran in parallel" with no
    correlation work. A single write of a whole line keeps a lane from
    interleaving inside another lane's line.
    """
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


async def run_soak(
    roster: Sequence[SoakCandidate],
    *,
    session_factory: Any,
    tmp_path: Path,
    seed: int,
    lanes: list[list[str]],
) -> SoakResult:
    """Run ``lanes`` concurrently and return what happened, in what order."""
    # Open one Store and close it before any lane starts.
    #
    # `nix::openStore` has no cache, so every `nix.store()` builds a fresh
    # `LocalStore` on the same `db.sqlite`. On a database that no store has
    # opened yet, `LocalStore::openDB` switches the journal mode to WAL, and
    # SQLite answers a journal-mode change with SQLITE_BUSY at once when
    # another connection holds the database -- the busy timeout does not
    # apply to it. Eight lanes opening a fresh database together therefore
    # lose one to `SQLite database ... is busy`.
    #
    # This is not the concurrency under test, and it is not a failure a real
    # caller meets: a store whose database exists is already in WAL mode, so
    # the switch never runs again. One open here puts the shared database in
    # that state, in one thread.
    async with session_factory() as warm, warm.store():
        pass

    by_nodeid = {candidate.nodeid: candidate for candidate in roster}
    result = SoakResult(seed=seed, lanes=lanes, roster_hash=roster_hash(roster))
    origin = time.monotonic()

    _emit(
        f"SOAK BEGIN seed={seed} lanes={len(lanes)} "
        f"tests={sum(len(lane) for lane in lanes)} roster={result.roster_hash}"
    )

    def _describe(exc: BaseException, depth: int = 0) -> str:
        """Name the exception, and every exception inside it.

        ``format_exception_only`` of an ``ExceptionGroup`` gives
        ``ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)``
        and stops. That names the container and not the defect, and a report
        that says only the container cannot be read. Any test under
        ``anyio.create_task_group`` raises through one.
        """
        head = "".join(traceback.format_exception_only(exc)).strip()
        nested = getattr(exc, "exceptions", None)
        if not nested or depth >= 3:
            return head
        indent = "  " * (depth + 1)
        inner = "\n".join(f"{indent}{_describe(sub, depth + 1)}" for sub in cast("Sequence[BaseException]", nested))
        return f"{head}\n{inner}"

    async def run_one(lane: int, nodeid: str) -> None:
        candidate = by_nodeid[nodeid]
        kwargs: dict[str, Any] = {candidate.factory: session_factory}
        if candidate.wants_tmp_path:
            own = anyio.Path(tmp_path) / nodeid.replace("/", "_").replace("::", "__")
            await own.mkdir(parents=True, exist_ok=True)
            kwargs["tmp_path"] = Path(own)

        start = time.monotonic() - origin
        _emit(f"SOAK {start:9.3f} lane={lane} START {nodeid}")
        outcome, detail = "ok", ""
        try:
            await candidate.func(**kwargs)
        # Broad on purpose: one lane's failure is data for the report, and it
        # must not cancel the task group and lose every other lane's result.
        except BaseException as exc:
            if type(exc).__name__ == "Skipped":
                outcome, detail = "skip", str(exc)
            else:
                outcome = "fail"
                detail = _describe(exc)
        end = time.monotonic() - origin
        _emit(f"SOAK {end:9.3f} lane={lane} END   {nodeid} {outcome}")
        result.events.append(SoakEvent(nodeid, lane, start, end, outcome, detail))

    async def run_lane(lane: int) -> None:
        for nodeid in lanes[lane]:
            await run_one(lane, nodeid)

    async with anyio.create_task_group() as group:
        for lane in range(len(lanes)):
            group.start_soon(run_lane, lane)

    _emit(f"SOAK END ok={len(result.events) - len(result.failures)} fail={len(result.failures)}")
    return result


async def write_manifest(result: SoakResult, path: Path) -> None:
    target = anyio.Path(path)
    await target.parent.mkdir(parents=True, exist_ok=True)
    await target.write_text(json.dumps(result.manifest(), indent=2) + "\n", encoding="utf-8")


def lanes_from_manifest(manifest: dict[str, Any], roster: Sequence[SoakCandidate]) -> list[list[str]]:
    """Replay a recorded composition, and say plainly when it cannot be replayed.

    The lanes come from the file rather than from the seed, so a run reproduces
    even when the roster changed. A nodeid the roster no longer has is named
    rather than dropped: a replay that quietly runs less is a replay that
    proves less.
    """
    known = {candidate.nodeid for candidate in roster}
    recorded = [list(lane) for lane in manifest["lanes"]]
    missing = sorted({nodeid for lane in recorded for nodeid in lane} - known)
    if missing:
        listed = "\n  ".join(missing)
        raise RuntimeError(f"the manifest names {len(missing)} test(s) this roster no longer has:\n  {listed}")
    return recorded
