"""What every suite here needs, and that names no Nix concept.

Register it from a ``pytest.ini``::

    addopts = -p test_support.plugin

Never with ``pytest_plugins`` in a conftest: that is legal only in a top-level
conftest, so a project's own conftest is legal under its own rootdir and
illegal in a run from the repository root.

**This module is the only one in ``test_support`` that imports pytest**, and
that is why ``pytest`` is not a dependency of the project. ``notes``,
``hang_report`` and ``subprocess_output`` must stay callable by a program that
is not running pytest, and a test runner in that program's closure would be a
cost with no return. A project that registers this plugin runs pytest by
definition, so the import always resolves where it matters.

It also pins the soft descriptor limit of the process, which belongs to no
suite either. :func:`keep_the_process_inside_an_fd_set` gives the reason, and
``-p`` is what makes the moment early enough.

It carries what the repository's root ``tests/conftest.py`` gave every suite
before issue #130 split the suites into their own projects:

- **A deadline on every async test.** A wedged subprocess or a pipe nobody
  drains never raises, so the test, the run and the CI job all stop returning.
  The wrapper turns that into a ``TimeoutError`` that names what was still
  alive. See :mod:`test_support.deadline`.
- **The order that collection must take.** Every ``forked`` test runs first,
  and the static gates second. Both reasons are under
  :func:`pytest_collection_modifyitems`.
- **A stand-in for pytest-agent's ``agent_notes``.** A packaged runner leaves
  pytest-agent out on purpose, because it auto-activates on import. Without
  the stand-in, every test that records a note errors at *collection* rather
  than on its own subject, in CI only, while every dev shell passes.

No item above belongs to one suite, and a copy in each project's conftest is
how the copies start to differ.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import resource

import pytest

from test_support.deadline import with_test_timeout

#: The soft ``RLIMIT_NOFILE`` this plugin pins the process to. See
#: :func:`keep_the_process_inside_an_fd_set` for the whole reason.
DESCRIPTOR_LIMIT = 2048

#: Set this to a number to pin a different soft limit, or to ``0`` to leave the
#: limit alone. A suite that really needs more descriptors says so here rather
#: than by editing the constant above.
DESCRIPTOR_LIMIT_ENV_VAR = "NANOPYNIX_TEST_DESCRIPTOR_LIMIT"


def keep_the_process_inside_an_fd_set() -> None:
    """Pin the soft descriptor limit, so ``select.select`` keeps working.

    ``select.select`` raises ``ValueError: filedescriptor out of range in
    select()`` for a descriptor at or above ``FD_SETSIZE``, which is 1024 on
    Linux. ``prompt_toolkit`` reads its input that way
    (``input/posix_utils.py``), so the search interface of ``pynix`` stops
    reading keys the moment this process holds more than 1024 descriptors.

    Nix 2.35 makes that reachable. ``PosixDirectorySourceAccessor`` caches a
    descriptor for each directory it walks, and
    ``getGlobalDirFdCacheLimit`` in ``src/libutil/posix-source-accessor.cc``
    sizes that cache as ``min(4096, RLIMIT_NOFILE / 8)``. The soft limit in a
    dev shell here is 524288 and on a CI runner it is 65536, so Nix holds up
    to 4096 open directories before it drops one. That is right for ``nix``
    the program, and fatal for a ``select`` in the same process. Nix 2.34 has
    no such cache, which is the whole of the version split in issue #271:
    every ``nix_2_34`` job of run 32804941201 passed and every ``nix_2_35``
    and ``git`` job failed.

    A soft limit of 2048 gives Nix a 256-entry cache and leaves the rest of an
    ``fd_set`` for the stores, the sockets and the pipes. Measured on
    ``pynix/tests`` with ``NANOPYNIX_TEST_FAITHFUL_SESSIONS=1`` and Nix 2.35:
    628 descriptors at the peak before, and the whole-repository run on CI
    went past 1024.

    **This must run before the first accessor exists.** ``getGlobalFdLimit``
    memoises the answer in a function-local static, so a limit lowered later
    changes nothing. ``pytest.ini`` registers this plugin with ``-p``, which
    pytest processes while it parses the command line -- before the first
    conftest, and before anything imports Nix.

    The limit only ever goes down. A process that already sits below the
    number keeps what it has.
    """
    requested = os.environ.get(DESCRIPTOR_LIMIT_ENV_VAR)
    limit = DESCRIPTOR_LIMIT if requested is None else int(requested)
    if limit <= 0:
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft <= limit:
        return
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(limit, hard), hard))


keep_the_process_inside_an_fd_set()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Put every async test under the deadline, then order the run.

    A hook and not a fixture: the wrapper replaces the test function itself,
    and a fixture cannot reach that.

    **The order is here and not in a conftest, and issue #130 is why.** It sat
    in the repository's root ``tests/conftest.py``, which reaches ``tests/``
    and nothing else. The move put the ``forked`` tests in
    ``nanopynix/tests/``, where that hook does not reach, and a run of that
    project alone then wedged: `test_a_killed_worker_fails_the_call_that_was
    _in_flight` was still the current test after 70 seconds, with 1131 of 1755
    already done. Below is the reason that is a deadlock and not a slow test.

    pytest-forked's fork() only keeps the calling thread; any lock another
    thread held at fork time stays locked forever in the child. By the time
    any other test has touched Nix (L1 init, an inproc.Session's thread
    executor, an L3 worker/daemon subprocess, pynix's live-log manager
    thread), the pytest process is "multithreading-dirty" and forking it is
    a deadlock risk, not just slow. Run every ``forked`` test first, before
    anything else has a chance to spawn those threads.

    The static gates run second. A lint error or a type error is the cheapest
    finding in the run and needs no Nix, so it belongs near the front rather
    than behind several minutes of store work. After the forked tests, and not
    before them: each gate runs a tool through ``anyio.open_process``, and the
    asyncio child watcher gives that child a thread of its own. The fork rule
    above keeps the first word.

    A suite with neither marker is left exactly as collection gave it, so this
    costs a project that has no forked test nothing at all.
    """
    for item in items:
        if isinstance(item, pytest.Function) and inspect.iscoroutinefunction(item.obj):
            item.obj = with_test_timeout(item.obj)

    def rank(item: pytest.Item) -> int:
        if item.get_closest_marker("forked") is not None:
            return 0
        if item.get_closest_marker("static_gate") is not None:
            return 1
        return 2

    # `sort` is stable, so every group keeps the order collection gave it,
    # including the shuffle that pytest-randomly applies.
    items.sort(key=rank)


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
        pytest-agent's own fixture rather than this one -- a plugin fixture
        would otherwise shadow the plugin's.
        """
        return _NoopNotes()
