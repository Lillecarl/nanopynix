"""What this project's suite needs, and nothing that it does not.

**This file is the template for a suite that lives inside its own project.**
Issue #130 moved the tests here so that a Nix invocation reads one project
rather than the whole repository, and this conftest is what makes the suite
stand up on its own.

One registration, and it earns its place:

- ``test_support.plugin`` puts a deadline on every async test, and supplies
  the stand-in for pytest-agent's ``agent_notes``.

beartype's import hook is not here. ``../pytest.ini`` loads it with ``-p``,
which runs before any conftest; that file gives the reason.

**``nanopynix_testing.fixtures`` is deliberately absent.** These tests drive
doubles and open no store, so the autouse fixture that initialises libstore
would be work with no purpose. Register it in a suite that needs a real store.
"""

from __future__ import annotations

pytest_plugins = ("test_support.plugin",)
