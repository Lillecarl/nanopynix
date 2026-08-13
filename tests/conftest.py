"""What is left at the root once each suite can carry its own fixtures.

**The Nix session fixtures moved to ``nanopynix_testing.fixtures`` in issue
#130.** A conftest reaches its own directory and below, so a suite that moves
into its own project loses every fixture declared here. What stays are the
three things that belong to *this run* rather than to any one suite: the
beartype hook, the stand-in for pytest-agent's ``agent_notes``, and the two
whole-session hooks below.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import sys
from typing import TYPE_CHECKING

# Installs beartype's import hook and arranges the same for subprocesses; must
# run before `import nanopynix` below. A plain `import tests.support.
# beartype_hook` statement would be fair game for ruff's isort, which sorts
# `nanopynix` ahead of it -- exactly backwards. Routing the side effect
# through a function call keeps it outside anything isort will reorder.
# `-p` in pytest.ini would be earlier still but cannot resolve the name that
# early; see the hook's module docstring.
importlib.import_module("tests.support.beartype_hook")

import coverage  # noqa: E402 -- see hook install above
import pytest  # noqa: E402 -- see hook install above

# `with_test_timeout` turns a hung async test into a TimeoutError that carries
# a report of what was still alive. Issue #130 moved it and `run_process` to
# `test_support`: neither names a Nix concept, and a suite that is not this one
# needs both just as much. `tests/harness/` held the test of the deadline;
# `test-support/tests/` holds it now.
from test_support.deadline import with_test_timeout  # noqa: E402 -- see hook install above

pytest_plugins = (
    "tests.support.lsp_environment",
    "nanopynix_testing.nix_environment",
    "nanopynix_testing.nix_runtime",
    "nanopynix_testing.fixtures",
)

if TYPE_CHECKING:
    from collections.abc import Generator


def _pytest_agent_installed() -> bool:
    return importlib.util.find_spec("pytest_agent") is not None


if not _pytest_agent_installed():

    class _NoopNotes:
        """Stand-in for pytest-agent's ``agent_notes``, for runs without the plugin."""

        def note(self, **values: object) -> None:
            """Discard the recording. Notes are observability, never an assertion."""

    @pytest.fixture
    def agent_notes() -> _NoopNotes:
        """``agent_notes`` when pytest-agent is not installed.

        The packaged test runner (``nanopynix/tests.nix``) deliberately leaves
        pytest-agent out -- it auto-activates on import, and that runner is what
        CI executes. Without this fallback, every test that records a note
        errors at *collection* with "fixture 'agent_notes' not found", so it
        fails in CI on every Nix version and both backends while passing in any
        dev shell. That is how ``test_error_detail_survives_every_boundary``
        failed: not on its own subject matter at all.

        Defined only when the real plugin is absent, so a normal run still gets
        pytest-agent's own fixture rather than this one -- a conftest fixture
        would otherwise shadow the plugin's.
        """
        return _NoopNotes()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:  # noqa: ARG001 -- hookspec signature requires config arg
    for item in items:
        if isinstance(item, pytest.Function) and inspect.iscoroutinefunction(item.obj):
            item.obj = with_test_timeout(item.obj)

    # pytest-forked's fork() only keeps the calling thread; any lock another
    # thread held at fork time stays locked forever in the child. By the time
    # any other test has touched Nix (L1 init, an inproc.Session's thread
    # executor, an L3 worker/daemon subprocess, pynix's live-log manager
    # thread), the pytest process is "multithreading-dirty" and forking it is
    # a deadlock risk, not just slow. Run every @pytest.mark.forked test
    # first, before anything else has a chance to spawn those threads.
    # The static gates run second. A lint error or a type error is the cheapest
    # finding in the run and needs no Nix, so it belongs near the front rather
    # than behind several minutes of store work.
    #
    # After the forked tests, and not before them: each gate runs a tool
    # through `anyio.open_process`, and the asyncio child watcher gives that
    # child a thread of its own. The fork rule above keeps the first word.
    def rank(item: pytest.Item) -> int:
        if item.get_closest_marker("forked") is not None:
            return 0
        if item.get_closest_marker("static_gate") is not None:
            return 1
        return 2

    # `sorted` is stable, so every group keeps the order collection gave it,
    # including the shuffle that pytest-randomly applies.
    items.sort(key=rank)


def _save_coverage_before_hard_exit() -> None:
    """Persist ``coverage run`` data that the ``os._exit`` below would discard.

    ``coverage run`` writes its data file from an atexit handler, and
    ``os._exit`` runs none -- so under the test runner's coverage mode the
    entire session's measurement vanished and ``coverage combine`` reported
    "No data to combine".

    Only in that mode. Under a plain ``pytest --cov`` the data is already
    written and combined by pytest-cov, from inside ``pytest_runtestloop``
    and therefore before this hook; saving again there would just leave an
    uncombined part file behind.

    Coverage running at all is optional, so a missing instance is normal, not
    an error.
    """
    if not os.environ.get("NANOPYNIX_COVERAGE"):
        return
    current = coverage.Coverage.current()
    if current is None:
        return
    current.stop()
    current.save()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(exitstatus: int | pytest.ExitCode) -> Generator[None]:
    """Force-exit once every other sessionfinish hook has run, terminal
    summary included.

    CPython's normal shutdown joins every non-daemon thread before the
    process can exit. A worker-pool executor thread or subprocess-handling
    thread that isn't perfectly torn down at session end otherwise leaves the
    whole pytest process (and therefore the CI job) hanging forever even
    though every test already finished and every report (coverage, junitxml,
    the terminal "N passed/failed" line) is already written.

    Coverage's own reporting runs earlier still, inside pytest_runtestloop.
    The terminal reporter's final summary line is printed from *its own*
    ``pytest_sessionfinish`` wrapper hook, after its ``yield``. Wrapper hooks
    nest like context managers: ``tryfirst=True`` makes ours the outermost
    one, so our ``yield`` runs first (deferring to every other hookimpl,
    wrapper or not) and our code after ``yield`` runs dead last, once
    everything else -- including the terminal summary -- has finished.
    """
    yield
    sys.stdout.flush()
    sys.stderr.flush()
    _save_coverage_before_hard_exit()
    os._exit(int(exitstatus))
