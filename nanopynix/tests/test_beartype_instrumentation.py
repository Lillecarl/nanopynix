"""The test harness's own teeth: is beartype actually checking what we think?

Every other test in this suite asserts something about nanopynix. These assert
something about the instrumentation those tests run under, because it is the
kind of thing that fails *silently* -- an uninstrumented process runs every
test to a clean pass, it just stops catching anything. Nothing else here would
notice.

The subprocess half is not hypothetical. When beartype was first wired up, the
hook was installed only in the pytest process while ``NANOPYNIX_BEARTYPING``
was exported to the whole environment. Children therefore got the flag --
promoting every ``if TYPE_CHECKING or BEARTYPING:`` import to a real one --
without the import hook that is the flag's entire reason for existing. The
worker side of the rpc engine ran unchecked while inproc's equivalent was
fully checked, and the suite was green throughout.
"""

from __future__ import annotations

import inspect
import multiprocessing
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import NoReturn

import anyio
import beartype.roar
import pytest

from beartype_bootstrap import PACKAGES
from nanopynix._typechecking import BEARTYPING, no_runtime_type_check
from nanopynix.settings import normalize_nix_path, normalize_nix_settings
from nanopynix_testing.nix_markers import LINUX_FORK_THEN_INIT
from test_support.subprocess_output import run_process

# An `int` is wrong for both functions probed below -- `normalize_nix_path`
# takes `str | Sequence[str] | None`, `normalize_nix_settings` takes
# `NixSettings | os.PathLike[str] | str | None`. Both are cheap and pure, so
# probing them costs nothing and touches no Nix state.
_PROBE_ARGUMENT = 123

# How long the forkserver child below gets. Generous against the work it does
# -- the child only imports and probes -- because the point is to convert an
# unbounded hang into a report, not to police startup latency. The whole
# daemon-backend suite runs in about 6 minutes locally, so 120s here cannot
# turn a slow runner into a spurious failure.
_CHILD_TIMEOUT_SECONDS = 120

# How long the whole forkserver block below gets, construction and teardown
# included. Issue #205: `Pool()` starts the forkserver helper, and the helper
# imports each preload module before it answers the first request. An import
# that blocks therefore holds `Pool()`, the bound on `.get()` never fires, and
# GitHub kills the job at 30 minutes with no summary and no `junit.xml`.
# Larger than `_CHILD_TIMEOUT_SECONDS`, so a child that starts and then stalls
# still fails at the inner bound, which names the more specific cause.
_BLOCK_TIMEOUT_SECONDS = 180

# The bound re-arms itself at this interval once it fires. Teardown of a
# half-built pool can block as well, and a second wedge must not be silent.
_REARM_SECONDS = 5.0

# What the probe below gives the bound, and what it gives the whole probe
# process. The preload it plants sleeps for ten minutes, so any bound at all
# proves the point and a short one keeps this module fast.
_PROBE_BOUND_SECONDS = 1.5
_PROBE_PROCESS_BOUND_SECONDS = 60.0


def _is_instrumented() -> bool:
    """Did beartype's hook reach the module ``normalize_nix_path`` lives in?"""
    try:
        normalize_nix_path(_PROBE_ARGUMENT)  # type: ignore[arg-type] -- deliberately wrong, that is the probe
    except beartype.roar.BeartypeCallHintParamViolation:
        return True
    else:
        return False


def _child_report() -> tuple[bool, bool]:
    """Run in a forkserver child: ``(flag_set, actually_instrumented)``.

    Module-level so it is picklable. The mismatch this exists to catch is the
    two disagreeing.

    The import is deliberately inside the function so that the value being
    reported is unmistakably the one read in the child, at the point of use.
    """
    from nanopynix._typechecking import BEARTYPING as CHILD_BEARTYPING  # noqa: PLC0415 -- read in the child, see above

    return CHILD_BEARTYPING, _is_instrumented()


@contextmanager
def _bounded(seconds: float, what: str) -> Generator[None]:
    """Raise :class:`TimeoutError` when the body runs longer than *seconds*.

    **A bound on the body, and not on one call inside it.** The forkserver
    block below blocks in three places, and only the middle one takes a
    ``timeout=`` argument: ``Pool()`` waits for the helper to answer,
    ``.get()`` waits for the child, and the ``with`` exit waits for the pool
    to stop. Issue #205 measured a job that died at the first of the three.

    ``SIGALRM`` is what reaches a blocking read that Python does not own. The
    interpreter runs the handler on the main thread when the read returns
    ``EINTR``, so the exception comes out of whichever call is blocked.

    Linux only, and the one caller is Linux only for its own reasons.
    """
    if threading.current_thread() is not threading.main_thread():
        # `signal.signal` accepts the main thread alone. An unbounded body is
        # worse than a bounded one and better than an error here, so this
        # yields rather than raises.
        yield
        return

    def _fire(signum: int, frame: FrameType | None) -> NoReturn:  # noqa: ARG001 -- the signature is the one `signal.signal` calls
        raise TimeoutError(f"{what} did not finish within {seconds:g}s")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds, _REARM_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_the_pytest_process_is_instrumented() -> None:
    """Baseline: without this, every other assertion here proves nothing."""
    assert BEARTYPING is True, "NANOPYNIX_BEARTYPING was not set before nanopynix was imported"
    assert _is_instrumented(), "beartype's import hook did not reach nanopynix in the pytest process"


def test_subprocesses_inherit_the_startup_shim() -> None:
    """``PYTHONPATH`` carries the shim directory, which is how children get it.

    A freshly exec'd interpreter cannot inherit an import hook; the only thing
    that crosses the boundary is the environment. This asserts the mechanism
    is armed, and the test below asserts it actually fires.
    """
    entries = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    assert any(entry.endswith("_subprocess_startup") for entry in entries), entries


@LINUX_FORK_THEN_INIT
@pytest.mark.skipif(
    "forkserver" not in multiprocessing.get_all_start_methods(),
    reason="the Nix worker is forked from a forkserver helper; nothing to check without one",
)
def test_a_forkserver_child_is_instrumented_not_merely_flagged() -> None:
    """The regression this file was written for.

    ``preload`` mirrors what ``rpc/client/_pool.py`` passes when it spawns a
    worker, so the child imports the same module graph a real worker does --
    under the same conditions, at the same point in startup.

    Both halves are asserted because the failure mode was *disagreement*: the
    flag alone was true, which is what made the gap invisible. A child with
    neither would be a different (and louder) bug.

    **Linux only, and the library draws the same line.** A forkserver child is
    a process that forked and never exec'd, and `resolve_worker_start` answers
    `spawn` on Darwin for exactly that reason: libdispatch and CoreFoundation
    do not survive it. So this builds, on purpose, the one thing nanopynix
    refuses to build on macOS, and it asserts a property of a path that no
    macOS caller ever takes.

    Ungated, it did more than fail. On the runner of the macOS job it killed
    the pytest process at 72 percent of run 31955684530: no summary, no
    `junit.xml`, and the seven real failures of that run unreadable. It passes
    on an M-series Mac, three runs out of three, so the host decides and this
    machine cannot reproduce it. Issue #151.
    """
    ctx = multiprocessing.get_context("forkserver")
    ctx.set_forkserver_preload(["nanopynix.rpc.worker._worker"])
    # Bounded twice, because the block blocks in three places and a plain
    # `pool.apply()` waits forever at any of them. Four CI jobs have hung on
    # this block and been killed: two at 117 and 145 minutes, and two at the
    # 30-minute limit of GitHub, on four Nix versions, with the forkserver
    # child still alive as an orphan process at cleanup. A hang that long
    # reports nothing at all -- no test name, no traceback, no `junit.xml`,
    # and no summary for the tests that already passed.
    #
    # The inner bound covers the child. The outer bound covers `Pool()`, which
    # waits for the forkserver helper to import the preload, and the `with`
    # exit, which waits for the pool to stop. Issue #205 measured a job that
    # died at `Pool()`, where the inner bound cannot reach.
    #
    # Either bound names the test and leaves the preload -- the whole Nix
    # worker module graph, imported in a child that has just been forked from
    # the forkserver helper -- as the thing to look at.
    with _bounded(_BLOCK_TIMEOUT_SECONDS, "the forkserver pool"), ctx.Pool(processes=1) as pool:
        flag_set, instrumented = pool.apply_async(_child_report).get(timeout=_CHILD_TIMEOUT_SECONDS)

    assert flag_set is True, "the child did not inherit NANOPYNIX_BEARTYPING"
    assert instrumented, (
        "the child inherited NANOPYNIX_BEARTYPING but no import hook: its "
        "`if TYPE_CHECKING or BEARTYPING:` imports were promoted while nothing "
        "checked the hints they exist to resolve"
    )


class TestTheBoundHasTeeth:
    """``_bounded`` is the machinery that turns a hang into a report.

    The block it guards hangs on a CI runner and on no machine here, so these
    exercise the bound directly. Without them the guard is code that has never
    run, and issue #205 is about a bound that was there and did not fire.
    """

    def test_a_body_that_blocks_raises_inside_the_bound(self) -> None:
        """A sleep far longer than the bound ends at the bound.

        `time.sleep` is a blocking call the interpreter does not own, which is
        the shape of `Pool()` and of the pool teardown.
        """
        started = time.monotonic()
        with (
            pytest.raises(TimeoutError, match=re.escape("the probe did not finish within 0.2s")),
            _bounded(0.2, "the probe"),
        ):
            time.sleep(30)

        assert time.monotonic() - started < 10, "the bound did not interrupt the blocking call"

    def test_a_body_that_finishes_raises_nothing(self) -> None:
        """The control. Without it the test above passes on a broken bound."""
        with _bounded(30, "the probe"):
            time.sleep(0)

    def test_the_bound_puts_the_handler_and_the_timer_back(self) -> None:
        """A leaked timer would fire in an unrelated test, far from here."""
        before = signal.getsignal(signal.SIGALRM)
        with _bounded(30, "the probe"):
            pass

        assert signal.getsignal(signal.SIGALRM) is before
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)

    def test_the_bound_puts_them_back_after_it_fires(self) -> None:
        """The path that matters, because it unwinds through the handler."""
        before = signal.getsignal(signal.SIGALRM)
        with pytest.raises(TimeoutError), _bounded(0.2, "the probe"):
            time.sleep(30)

        assert signal.getsignal(signal.SIGALRM) is before
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


# The failure of issue #205, built on purpose. A preload module that blocks at
# import holds the forkserver helper, which holds `Pool()`, which is above the
# bound on `.get()`. That is what a CI job died of, four times.
#
# **The bound comes from `_bounded` itself, through `inspect.getsource`.** A
# copy in this string would be a second declaration that drifts, which is the
# mistake `_INSTRUMENTED_IMPORT_PROBE` below records.
#
# The body of the `with` never runs, because `Pool()` never returns. So the
# probe pickles nothing and needs no importable `__main__`.
_HANGING_PRELOAD_PROBE = f"""
from __future__ import annotations

import multiprocessing
import pathlib
import signal
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from types import FrameType
from typing import NoReturn

_REARM_SECONDS = {_REARM_SECONDS!r}

{inspect.getsource(_bounded)}

sys.path.insert(0, sys.argv[1])

ctx = multiprocessing.get_context("forkserver")
ctx.set_forkserver_preload(["slow_preload"])
try:
    with _bounded({_PROBE_BOUND_SECONDS!r}, "the forkserver pool"), ctx.Pool(processes=1):
        pass
except TimeoutError as timed_out:
    answer = f"BOUNDED: {{timed_out}}"
else:
    answer = "NOT BOUNDED"

pathlib.Path(sys.argv[2]).write_text(answer, encoding="utf-8")
"""


@pytest.mark.skipif(
    "forkserver" not in multiprocessing.get_all_start_methods(),
    reason="the bound guards a forkserver pool; nothing to check without one",
)
async def test_the_bound_reaches_a_preload_that_never_finishes_importing(tmp_path: Path) -> None:
    """Issue #205, reproduced and then caught.

    The three tests above bound a `time.sleep`, which proves the timer fires.
    This one builds the real shape: the helper of the forkserver imports a
    module that never returns, so `Pool()` blocks and the bound on `.get()`
    cannot reach it. Before the outer bound, this hung until something killed
    it -- four CI jobs, at 117 minutes, 145 minutes and twice at the 30-minute
    limit of GitHub, each losing its summary and its `junit.xml`.

    A subprocess, and a bound on the subprocess too. If `_bounded` stops
    working, this test fails on the outer bound rather than hanging the run,
    which is the whole property under test.
    """
    # Written here rather than inside the probe, so the source of the probe
    # needs no escaped newline inside an f-string that already holds one. The
    # sleep outlives the bound by far and still clears itself: the helper of
    # the forkserver survives the probe as an orphan, and a short sleep means
    # it is gone in half a minute rather than in ten.
    (tmp_path / "slow_preload.py").write_text("import time\n\ntime.sleep(30)\n", encoding="utf-8")
    answer = tmp_path / "answer.txt"

    # **No pipe, and the answer comes through a file.** The orphan inherits
    # whatever the probe writes to, so a piped stdout never reaches end of
    # file and the parent waits on the orphan rather than on the probe. That
    # is why `run_process` is not the tool here.
    process = await anyio.open_process(
        [sys.executable, "-c", _HANGING_PRELOAD_PROBE, str(tmp_path), str(answer)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    async with process:
        with anyio.fail_after(_PROBE_PROCESS_BOUND_SECONDS):
            await process.wait()

    assert process.returncode == 0, f"the probe exited {process.returncode}"
    assert answer.read_text(encoding="utf-8").startswith("BOUNDED: the forkserver pool did not finish"), (
        "`Pool()` blocked on the preload and the bound did not fire"
    )


class TestTheExemptionDecoratorDoesWhatItClaims:
    """``no_runtime_type_check`` trades runtime checking for nothing else.

    It exists because ``typing.no_type_check`` achieves the same runtime
    exemption at the cost of static checking -- pyright erases the signature
    and stops analysing the body. That half cannot be asserted from inside a
    test process; what can be asserted is that the runtime half still works,
    including for hints that fail when beartype *decorates* rather than when
    the function is called.
    """

    def test_an_exempt_function_raises_its_own_error_not_beartypes(self) -> None:
        """Exercised through a real exempt site, not a locally defined one.

        A function defined *here* would prove nothing: `tests` is deliberately
        absent from the instrumented package list, so beartype never decorates
        it and the assertion holds with or without the decorator.
        `normalize_nix_settings` is one of the 19 real exemptions, and it is
        the clearest of them -- it validates its own argument and documents a
        `TypeError`, which beartype's check would preempt with a
        `BeartypeCallHintParamViolation` (a sibling of `TypeError`, not a
        subclass). So the exception *type* is what discriminates: delete the
        decorator from `nanopynix/settings.py` and this fails.
        """
        with pytest.raises(TypeError, match="settings must be") as raised:
            normalize_nix_settings(_PROBE_ARGUMENT)  # type: ignore[arg-type] -- deliberately wrong, that is the probe

        assert not isinstance(raised.value, beartype.roar.BeartypeCallHintParamViolation)

    def test_an_unexempted_function_still_is(self) -> None:
        """The control for the test above.

        Without it, that test passes just as happily on a process where
        beartype was never installed -- which is exactly how the subprocess
        gap stayed hidden for a whole commit. `tests` is deliberately not in
        the instrumented package list, so the control has to be a function
        from a package that is.
        """
        assert _is_instrumented()

    def test_it_sets_the_attribute_beartype_reads(self) -> None:
        """The mechanism, stated once so a future reader need not infer it.

        ``__no_type_check__`` is what ``typing.no_type_check`` sets and what
        beartype's decorator checks before it analyses any hint -- which is
        why this also covers hints that would fail at decoration time, such as
        ``Never`` or an unresolvable forward reference.
        """

        @no_runtime_type_check
        def anything() -> None: ...

        assert anything.__no_type_check__ is True  # type: ignore[attr-defined] -- set by the decorator under test


# Callables beartype cannot decorate at all, with why. Everything here is
# *silently unchecked* -- beartype gives up on the whole callable, not just the
# offending hint -- so an entry is a real hole and needs a real reason.
#
# Empty, and it should stay that way.
#
# It held two entries until #32: `worker_service_factory` and
# `_shutdown_worker`, both annotating `grpclib._typing.IServable`, a
# third-party protocol that is not `@runtime_checkable`. The reason recorded
# here said "not fixable here: the decorator would have to go on grpclib's
# declaration". That was wrong, and the mistake is worth keeping visible: the
# fix was never to make *their* protocol checkable, but to stop naming it. The
# factory builds three concrete handlers and already cast the list to
# `list[IServable]`, so the annotation described the caller's contract rather
# than the code, and the `cast` was the tell. Naming the union of the three
# handlers is both checkable and more honest, and `Server` still accepts them
# because they satisfy the protocol structurally.
#
# nanopynix's own protocols were in this list too, until `@runtime_checkable`
# was added to them; see nanopynix/protocols.py. That is the other fix, and it
# is the right one when the declaration is ours.
UNDECORATABLE: dict[str, str] = {
    "nanopynix.protocols.AsyncValue.build": (
        "A beartype defect, and the annotation is the one this protocol needs. "
        "`AsyncValue` is generic in the store its `build` accepts, and the "
        "parameter defaults to `Any` so that a bare `AsyncValue` keeps meaning "
        "a value of any engine -- see that class for what the other default "
        "costs. beartype treats a type parameter defaulting to `Any` as "
        "ignorable, and then asserts on the union that holds it: 'Union "
        "StoreT | None containing ignorable child StoreT not itself ignored'. "
        "Reproduced in 20 lines: `T: Base = Any` with `T | None` is skipped "
        "and `T: Base = Base` with `T | None` is not, under the claw hook "
        "only -- a direct `beartype.beartype()` call decorates all of them. "
        "`Optional[T]` is the same hint and fails the same way. "
        "Nothing is lost at run time: the body is `...` and no caller reaches "
        "it, while a consumer annotated with `AsyncValue` is decorated and "
        "checked as before."
    ),
}

# beartype names the callable in the warning's first line, in one of several
# phrasings ("Function ...()", "Coroutine factory function ...()").
_UNDECORATABLE_PATTERN = re.compile(r"BeartypeClawDecorWarning: .*? ([\w.]+)\(\) in file")

# Imports everything instrumented, then probes whether the hook actually fired.
# The probe matters for the day UNDECORATABLE is empty: without it, a child
# where beartype never installed would emit no warnings and pass vacuously.
#
# **The import line is built from `PACKAGES`, and is not written out.** It was
# written out, and issue #222 added `libpynix` to that tuple without adding it
# here. The scan then covered four of the five instrumented packages and
# reported nothing, which is the failure this whole test exists to catch. A
# package added to the hook is now in the probe by construction.
#
# **It walks the submodules, and importing the five packages is not enough.**
# beartype decorates a module when that module is imported, so a module that
# nothing imports is never scanned. `pynix._impl` reaches its heavy modules
# through a PEP 562 table, so `import pynix` loads none of them, and the probe
# covered 5 modules where the checkout holds 131. `pynix._impl._quiet` lost its
# runtime check that way and this test stayed green. Measured: the walk imports
# 131 submodules in 1.4 s, and every one of them imports.
_INSTRUMENTED_IMPORT_PROBE = f"""
import importlib
import pkgutil

import beartype.roar
from nanopynix.settings import normalize_nix_path

for name in {PACKAGES!r}:
    package = importlib.import_module(name)
    for found in pkgutil.walk_packages(package.__path__, prefix=name + "."):
        importlib.import_module(found.name)

try:
    normalize_nix_path(123)
except beartype.roar.BeartypeCallHintParamViolation:
    print("INSTRUMENTED")
"""


async def test_no_callable_is_silently_undecoratable_beyond_the_known_list() -> None:
    """The regression test for making ``nanopynix.protocols`` runtime-checkable.

    That change exists so beartype stops skipping functions annotated with
    those protocols. Asserting the decorator is present (over in
    ``test_protocols.py``) pins the cause; this pins the *effect*, which is
    what actually matters and which nothing else observes -- a skipped
    callable produces a warning on stderr and a green test run.

    An allowlist rather than a count, so it also catches a skip arising from
    some other cause: a stub-only annotation beartype cannot import, say, or
    a hint that raises at decoration time. Any of those is a hole in the
    instrumentation and should have to be named here with a reason.

    A subprocess because the pytest process imported these packages before
    this test could watch it happen. The child inherits ``PYTHONPATH`` and
    ``NANOPYNIX_BEARTYPING`` from the hook, which is exactly how a real Nix
    worker gets instrumented.
    """
    result = await run_process([sys.executable, "-c", _INSTRUMENTED_IMPORT_PROBE])

    assert result.returncode == 0, result.describe()
    assert "INSTRUMENTED" in result.stdout, f"the child ran without beartype's hook; {result.describe()}"

    found = set(_UNDECORATABLE_PATTERN.findall(result.stderr))
    unexplained = sorted(found - set(UNDECORATABLE))
    assert not unexplained, (
        "beartype silently skipped these callables entirely -- fix the annotation, or add each to "
        f"UNDECORATABLE with a reason: {unexplained}\n{result.stderr}"
    )

    stale = sorted(set(UNDECORATABLE) - found)
    assert not stale, f"UNDECORATABLE lists callables beartype now decorates fine (or that no longer exist): {stale}"


def test_the_probe_would_notice_if_it_stopped_working() -> None:
    """Guards the helper every assertion above leans on.

    ``_is_instrumented`` reports False both when beartype is absent and when
    ``normalize_nix_path`` stops rejecting the probe argument (a signature
    change, say). Pinning the second here means a silent widening shows up as
    this test failing rather than as instrumentation appearing to vanish.
    """
    assert sys.modules.get("nanopynix.settings") is not None
    with pytest.raises(beartype.roar.BeartypeCallHintParamViolation):
        normalize_nix_path(_PROBE_ARGUMENT)  # type: ignore[arg-type] -- deliberately wrong, that is the probe
