"""What is left at the root once each suite can carry its own fixtures.

**Everything reusable moved out in issue #130.** A conftest reaches its own
directory and below, so a suite that moves into its own project loses every
fixture declared here. The Nix session fixtures are now
``nanopynix_testing.fixtures``, and the deadline and the ``agent_notes``
stand-in are ``test_support.plugin``.

**This file registers no plugin.** ``pytest.ini`` names every one of them
with ``-p``, and that file gives the reason: ``pytest_plugins`` is legal
only in a top-level conftest, so a rule that holds here breaks in each
project. One route for the whole repository is the only route that works
from the repository root and from a project alike.

What stays is what belongs to *this* run and to no project: the hard exit
that ends the session.

The order that collection must take moved to ``test_support.plugin`` with
the suites. It was here, and here it reached ``tests/`` alone, so the
``forked`` tests of ``nanopynix/tests/`` ran unordered and the run wedged.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import coverage
import pytest

# Session scope, and anyio's own `anyio_backend` is module scope. Both are
# global plugins now that `pytest.ini` registers ours with `-p`, and anyio
# wins that tie -- every session-scoped fixture that requests it then fails
# with `ScopeMismatch`. A conftest beats any plugin, so importing the fixture
# here settles it. `nanopynix/tests/conftest.py` carries the same import and
# the full account.
from nanopynix_testing.nix_environment import anyio_backend as anyio_backend

if TYPE_CHECKING:
    from collections.abc import Generator


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
