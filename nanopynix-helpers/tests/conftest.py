"""What this project's suite needs, and nothing that it does not.

**This file is the template for a suite that lives inside its own project.**
Issue #130 moved the tests here so that a Nix invocation reads one project
rather than the whole repository, and this conftest is what makes the suite
stand up on its own.

Two registrations, and each one earns its place:

- ``nanopynix_testing.beartype_hook`` installs beartype's import hook over
  ``nanopynix_helpers``. It has to run before ``nanopynix_helpers`` is
  imported, and a suite that skips it loses runtime type checking and still
  reports every test as passed. That silence is why it comes first.
- ``test_support.plugin`` puts a deadline on every async test, and supplies
  the stand-in for pytest-agent's ``agent_notes``.

**``nanopynix_testing.fixtures`` is deliberately absent.** These tests drive
doubles and open no store, so the autouse fixture that initialises libstore
would be work with no purpose. Register it in a suite that needs a real store.
"""

from __future__ import annotations

import importlib

# The side effect runs through a function call rather than an import
# statement, because `ruff check --fix` alphabetizes an import block and would
# sort `nanopynix_helpers` ahead of this -- exactly backwards. See the hook's
# module docstring.
importlib.import_module("nanopynix_testing.beartype_hook")

pytest_plugins = ("test_support.plugin",)
