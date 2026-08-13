"""The two things every suite here needs and that name no Nix concept.

Register it from a ``conftest.py``::

    pytest_plugins = ("test_support.plugin",)

**This module is the only one in ``test_support`` that imports pytest**, and
that is why ``pytest`` is not a dependency of the project. ``notes``,
``hang_report`` and ``subprocess_output`` must stay callable by a program that
is not running pytest, and a test runner in that program's closure would be a
cost with no return. A project that registers this plugin runs pytest by
definition, so the import always resolves where it matters.

It carries what the repository's root ``tests/conftest.py`` gave every suite
before issue #130 split the suites into their own projects:

- **A deadline on every async test.** A wedged subprocess or a pipe nobody
  drains never raises, so the test, the run and the CI job all stop returning.
  The wrapper turns that into a ``TimeoutError`` that names what was still
  alive. See :mod:`test_support.deadline`.
- **A stand-in for pytest-agent's ``agent_notes``.** A packaged runner leaves
  pytest-agent out on purpose, because it auto-activates on import. Without
  the stand-in, every test that records a note errors at *collection* rather
  than on its own subject, in CI only, while every dev shell passes.

Neither belongs to one suite, and copying either into each project's conftest
is how the two copies start to differ.
"""

from __future__ import annotations

import importlib.util
import inspect

import pytest

from test_support.deadline import with_test_timeout


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Put every async test under the deadline.

    A hook and not a fixture: the wrapper replaces the test function itself,
    and a fixture cannot reach that.
    """
    for item in items:
        if isinstance(item, pytest.Function) and inspect.iscoroutinefunction(item.obj):
            item.obj = with_test_timeout(item.obj)


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
