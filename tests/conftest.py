"""What is left at the root once each suite can carry its own fixtures.

**Everything reusable moved out in issue #130.** A conftest reaches its own
directory and below, so a suite that moves into its own project loses every
fixture declared here. The Nix session fixtures are now
``nanopynix_testing.fixtures``, and the deadline and the ``agent_notes``
stand-in are ``test_support.plugin``.

What stays is what belongs to *this* run and to no project: the order that
collection must take, and the hard exit that ends the session.
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import TYPE_CHECKING

# Installs beartype's import hook and arranges the same for subprocesses; must
# run before `import nanopynix` below. A plain
# `import nanopynix_testing.beartype_hook` statement would be fair game for
# ruff's isort, which sorts `nanopynix` ahead of it -- exactly backwards.
# Routing the side effect through a function call keeps it outside anything
# isort will reorder. See the hook's module docstring for the `-p` route,
# which issue #130 made possible and which this file does not take.
importlib.import_module("nanopynix_testing.beartype_hook")

import coverage  # noqa: E402 -- see hook install above
import pytest  # noqa: E402 -- see hook install above

pytest_plugins = (
    "test_support.plugin",
    "tests.support.lsp_environment",
    "nanopynix_testing.nix_environment",
    "nanopynix_testing.nix_runtime",
    "nanopynix_testing.fixtures",
)

if TYPE_CHECKING:
    from collections.abc import Generator


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Order the run. The deadline is `test_support.plugin`'s hook, not this.

    pytest-forked's fork() only keeps the calling thread; any lock another
    thread held at fork time stays locked forever in the child. By the time
    any other test has touched Nix (L1 init, an inproc.Session's thread
    executor, an L3 worker/daemon subprocess, pynix's live-log manager
    thread), the pytest process is "multithreading-dirty" and forking it is
    a deadlock risk, not just slow. Run every @pytest.mark.forked test
    first, before anything else has a chance to spawn those threads.
    The static gates run second. A lint error or a type error is the cheapest
    finding in the run and needs no Nix, so it belongs near the front rather
    than behind several minutes of store work.

    After the forked tests, and not before them: each gate runs a tool
    through `anyio.open_process`, and the asyncio child watcher gives that
    child a thread of its own. The fork rule above keeps the first word.

    **This stays at the root, and did not go to a plugin.** It orders the
    markers of this repository's own suite, and `tests/gates/` exists only
    here. A project's own suite has one directory and nothing to order.
    """

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
